"""Identity scene pipeline: people resolution, prompt tagging, workflow prep, seed selection."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import unittest
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import identity_scene as ids  # noqa: E402
from gateway.identity_scene import IdentityDeps, IdentityError, PersonSpec, SceneRequest  # noqa: E402

TEMPLATE = json.loads((ROOT / "workflows/identity/qwen-image-2.1-identity.json").read_text())
NODES = [f"L{i}" for i in range(1, 7)]


def png(color: tuple[int, int, int], size: tuple[int, int] = (1280, 960)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="PNG")
    return out.getvalue()


def vec(i: int) -> list[float]:
    v = [0.0] * 8
    v[i] = 1.0
    return v


def face(x: float, age: int, emb: list[float], gender: str = "female") -> dict:
    return {"bbox": [x, 100, x + 120, 260], "det_score": 0.9, "age": age, "gender": gender, "embedding": emb}


PHOTO = png((10, 20, 30))
OUTPUTS = {  # attempt output bytes -> faces found in it
    png((1, 1, 1), (64, 64)): [face(100, 70, vec(7)), face(400, 1, vec(6))],       # strangers
    png((2, 2, 2), (64, 64)): [face(100, 70, vec(0)), face(400, 1, vec(1))],       # both match
}
OUT_LIST = list(OUTPUTS)


class Fakes:
    def __init__(self, outputs: list[bytes]) -> None:
        self.outputs = outputs
        self.graphs: list[dict] = []
        self.uploads: list[dict] = []

    async def analyze(self, image: bytes, model: str) -> list[dict]:
        if image == PHOTO:
            return [face(100, 70, vec(0)), face(400, 1, vec(1))]
        return OUTPUTS[image]

    async def run_workflow(self, graph: dict, uploads: dict) -> bytes:
        self.graphs.append(graph)
        self.uploads.append(uploads)
        return self.outputs[len(self.graphs) - 1]


def run(coro):
    return asyncio.run(coro)


def scene(fakes: Fakes, req: SceneRequest):
    deps = IdentityDeps(analyze=fakes.analyze, run_workflow=fakes.run_workflow, models=("buffalo_l",))
    return run(ids.run_identity_scene(
        req, deps, template=TEMPLATE, image_nodes=NODES, prompt_node="7", seed_node="8"
    ))


class HelperTests(unittest.TestCase):
    def test_head_crop_is_roomy_square_and_inside_image(self) -> None:
        box = ids.head_crop_box([880, 40, 990, 200], (1280, 964))
        self.assertEqual(box[2] - box[0], box[3] - box[1])
        self.assertGreater(box[2] - box[0], 110 * 2)
        self.assertTrue(0 <= box[0] and box[2] <= 1280 and 0 <= box[1] and box[3] <= 964)

    def test_head_reference_is_upscaled_not_a_tiny_tile(self) -> None:
        crop = Image.open(io.BytesIO(ids.head_reference_png(PHOTO, [100, 100, 220, 260])))
        self.assertGreaterEqual(max(crop.size), ids.HEAD_LONG_SIDE)

    def test_describe(self) -> None:
        self.assertEqual(ids.describe(1, "female"), "a baby")
        self.assertEqual(ids.describe(70, "female"), "an older woman")
        self.assertEqual(ids.describe(35, "male"), "a man")

    def test_prepare_workflow_prunes_unused_slots(self) -> None:
        graph = ids.prepare_workflow(
            TEMPLATE, image_nodes=NODES, used_images=3, prompt_node="7", seed_node="8",
            prompt="p", seed=9, resolution=1280, steps=20, cfg=1.5,
        )
        self.assertEqual({"L1", "L2", "L3"}, {k for k in graph if k.startswith("L")})
        keys = [k for k in graph["7"]["inputs"] if k.startswith("images.")]
        self.assertEqual(keys, ["images.image_1", "images.image_2", "images.image_3"])
        self.assertEqual(graph["8"]["inputs"]["seed"], 9)
        self.assertEqual(graph["7"]["inputs"]["resolution"], 1280)
        self.assertNotIn("_meta", graph)
        self.assertIn("images.image_6", TEMPLATE["7"]["inputs"])  # the template itself is not mutated

    def test_pick_face(self) -> None:
        faces = [{"bbox": [0, 0, 50, 50]}, {"bbox": [200, 0, 320, 120]}]
        self.assertEqual(ids.pick_face(faces, None)["bbox"][0], 200)
        self.assertEqual(ids.pick_face(faces, [0, 0, 100, 100])["bbox"][0], 0)
        self.assertIsNone(ids.pick_face(faces, [500, 500, 600, 600]))


class PipelineTests(unittest.TestCase):
    def test_auto_people_prompt_tags_and_early_stop(self) -> None:
        fakes = Fakes([OUT_LIST[1]])
        result = scene(fakes, SceneRequest(photos=[PHOTO], prompt="They look into each other's eyes.", attempts=4))
        self.assertEqual(len(fakes.graphs), 1)  # first attempt passed
        prompt = fakes.graphs[0]["7"]["inputs"]["prompt"]
        for tag in ("<image1>", "<image2>", "<image3>"):
            self.assertIn(tag, prompt)
        self.assertIn("a baby", prompt)
        self.assertIn("an older woman", prompt)
        self.assertEqual(result.report["overall"]["status"], "review")  # infant match is never "pass"
        self.assertTrue(result.report["overall"]["all_meet_threshold"])
        self.assertEqual(len(fakes.uploads[0]), 3)
        self.assertEqual(Image.open(io.BytesIO(fakes.uploads[0]["L2"])).size[0] >= ids.HEAD_LONG_SIDE, True)

    def test_best_of_n_picks_the_matching_seed(self) -> None:
        fakes = Fakes([OUT_LIST[0], OUT_LIST[1], OUT_LIST[0]])
        result = scene(fakes, SceneRequest(photos=[PHOTO], prompt="x", attempts=3, seed=40, stop_when_pass=False))
        self.assertEqual(len(fakes.graphs), 3)
        self.assertEqual(result.image, OUT_LIST[1])
        self.assertEqual([a["seed"] for a in result.attempts], [40, 41, 42])
        self.assertEqual([a["selected"] for a in result.attempts], [False, True, False])

    def test_all_attempts_fail_still_returns_best_with_fail_status(self) -> None:
        fakes = Fakes([OUT_LIST[0], OUT_LIST[0]])
        result = scene(fakes, SceneRequest(photos=[PHOTO], prompt="x", attempts=2))
        self.assertEqual(result.report["overall"]["status"], "fail")
        self.assertEqual(len(result.attempts), 2)

    def test_explicit_people_with_labels_and_box(self) -> None:
        fakes = Fakes([OUT_LIST[1]])
        people = [
            PersonSpec(label="grandma", box=(0, 0, 300, 400)),
            PersonSpec(label="baby", box=(350, 0, 600, 400)),
        ]
        result = scene(fakes, SceneRequest(photos=[PHOTO], prompt="x", people=people, photo_as_canvas=False))
        prompt = fakes.graphs[0]["7"]["inputs"]["prompt"]
        self.assertIn("<image1> is the identity reference for grandma", prompt)
        self.assertEqual([p["label"] for p in result.people], ["grandma", "baby"])
        self.assertEqual(len(fakes.uploads[0]), 2)

    def test_facing_requirement_keeps_trying_when_pose_is_unknown(self) -> None:
        fakes = Fakes([OUT_LIST[1], OUT_LIST[1], OUT_LIST[1]])
        result = scene(fakes, SceneRequest(photos=[PHOTO], prompt="x", attempts=3, facing_each_other=True))
        self.assertEqual(len(fakes.graphs), 3)  # faces carry no landmarks, so facing is never confirmed
        self.assertIsNone(result.attempts[0]["facing_each_other"])

    def test_box_without_a_face_is_an_error(self) -> None:
        fakes = Fakes([])
        with self.assertRaises(IdentityError) as ctx:
            scene(fakes, SceneRequest(photos=[PHOTO], prompt="x", people=[PersonSpec(box=(900, 900, 1000, 1000))]))
        self.assertEqual(ctx.exception.code, "no_face")

    def test_validation(self) -> None:
        fakes = Fakes([])
        for req in (
            SceneRequest(photos=[PHOTO], prompt=" "),
            SceneRequest(photos=[PHOTO], prompt="x", attempts=99),
            SceneRequest(photos=[], prompt="x"),
            SceneRequest(photos=[PHOTO], prompt="x", people=[PersonSpec(label="a"), PersonSpec(label="a")]),
        ):
            with self.assertRaises(IdentityError):
                scene(fakes, req)


if __name__ == "__main__":
    unittest.main()
