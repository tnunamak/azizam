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
from gateway.identity_scene import IdentityDeps, IdentityError, PersonSpec, RefSource, SceneRequest  # noqa: E402

TEMPLATE = json.loads((ROOT / "workflows/identity/qwen-image-2.1-identity.json").read_text())
NODES = [f"L{i}" for i in range(1, 11)]


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


FACES: dict = {}
CENTRAL = {"bbox": [400, 400, 624, 624], "det_score": 0.9, "age": 40, "gender": "female", "embedding": vec(4),
           "kps": [[450, 480], [574, 480], [512, 540], [460, 590], [564, 590]]}
GOOD_PASTE = png((5, 5, 5), (64, 64))
HURTS_OTHER_PASTE = png((6, 6, 6), (64, 64))
OUTPUTS[GOOD_PASTE] = [face(100, 70, vec(0)), face(400, 1, vec(1))]          # baby fixed, grandma intact
OUTPUTS[HURTS_OTHER_PASTE] = [face(100, 70, vec(7)), face(400, 1, vec(1))]    # baby fixed, grandma ruined


class Fakes:
    def __init__(self, outputs: list[bytes]) -> None:
        self.outputs = outputs
        self.graphs: list[dict] = []
        self.uploads: list[dict] = []

    async def analyze(self, image: bytes, model: str) -> list[dict]:
        if image == PHOTO:
            return [face(100, 70, vec(0)), face(400, 1, vec(1))]
        return FACES.get(image) or OUTPUTS.get(image) or [CENTRAL]

    async def run_workflow(self, graph: dict, uploads: dict) -> bytes:
        self.graphs.append(graph)
        self.uploads.append(uploads)
        return self.outputs[len(self.graphs) - 1]


def run(coro):
    return asyncio.run(coro)


def scene(fakes: Fakes, req: SceneRequest, nodes=None):
    deps = IdentityDeps(analyze=fakes.analyze, run_workflow=fakes.run_workflow, models=("buffalo_l",))
    return run(ids.run_identity_scene(
        req, deps, template=TEMPLATE, image_nodes=nodes or NODES, prompt_node="7", seed_node="8"
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
        self.assertIn("images.image_10", TEMPLATE["7"]["inputs"])  # the template itself is not mutated

    def test_pick_face(self) -> None:
        faces = [{"bbox": [0, 0, 50, 50]}, {"bbox": [200, 0, 320, 120]}]
        self.assertEqual(ids.pick_face(faces, None)["bbox"][0], 200)
        self.assertEqual(ids.pick_face(faces, [0, 0, 100, 100])["bbox"][0], 0)
        self.assertIsNone(ids.pick_face(faces, [500, 500, 600, 600]))


class PipelineTests(unittest.TestCase):
    def test_auto_people_prompt_tags_and_early_stop(self) -> None:
        fakes = Fakes([OUT_LIST[1]])
        result = scene(fakes, SceneRequest(refine="off", photos=[PHOTO], prompt="They look into each other's eyes.", attempts=4))
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
        result = scene(fakes, SceneRequest(refine="off", photos=[PHOTO], prompt="x", attempts=3, seed=40, stop_when_pass=False))
        self.assertEqual(len(fakes.graphs), 3)
        self.assertEqual(result.image, OUT_LIST[1])
        self.assertEqual([a["seed"] for a in result.attempts], [40, 41, 42])
        self.assertEqual([a["selected"] for a in result.attempts], [False, True, False])

    def test_all_attempts_fail_still_returns_best_with_fail_status(self) -> None:
        fakes = Fakes([OUT_LIST[0], OUT_LIST[0]])
        result = scene(fakes, SceneRequest(refine="off", photos=[PHOTO], prompt="x", attempts=2))
        self.assertEqual(result.report["overall"]["status"], "fail")
        self.assertEqual(len(result.attempts), 2)

    def test_explicit_people_with_labels_and_box(self) -> None:
        fakes = Fakes([OUT_LIST[1]])
        people = [
            PersonSpec(label="grandma", box=(0, 0, 300, 400)),
            PersonSpec(label="baby", box=(350, 0, 600, 400)),
        ]
        result = scene(fakes, SceneRequest(refine="off", photos=[PHOTO], prompt="x", people=people, photo_as_canvas=False))
        prompt = fakes.graphs[0]["7"]["inputs"]["prompt"]
        self.assertIn("<image1> is the identity reference for grandma", prompt)
        self.assertEqual([p["label"] for p in result.people], ["grandma", "baby"])
        self.assertEqual(len(fakes.uploads[0]), 2)

    def test_facing_requirement_keeps_trying_when_pose_is_unknown(self) -> None:
        fakes = Fakes([OUT_LIST[1], OUT_LIST[1], OUT_LIST[1]])
        result = scene(fakes, SceneRequest(refine="off", photos=[PHOTO], prompt="x", attempts=3, facing_each_other=True))
        self.assertEqual(len(fakes.graphs), 3)  # faces carry no landmarks, so facing is never confirmed
        self.assertIsNone(result.attempts[0]["facing_each_other"])

    def test_box_without_a_face_is_an_error(self) -> None:
        fakes = Fakes([])
        with self.assertRaises(IdentityError) as ctx:
            scene(fakes, SceneRequest(refine="off", photos=[PHOTO], prompt="x", people=[PersonSpec(box=(900, 900, 1000, 1000))]))
        self.assertEqual(ctx.exception.code, "no_face")

    def test_validation(self) -> None:
        fakes = Fakes([])
        for req in (
            SceneRequest(refine="off", photos=[PHOTO], prompt=" "),
            SceneRequest(refine="off", photos=[PHOTO], prompt="x", attempts=99),
            SceneRequest(refine="off", photos=[], prompt="x"),
            SceneRequest(refine="off", photos=[PHOTO], prompt="x", people=[PersonSpec(label="a"), PersonSpec(label="a")]),
        ):
            with self.assertRaises(IdentityError):
                scene(fakes, req)


class WhoIsWhoTests(unittest.TestCase):
    """Without boxes every person must get their own face, chosen from cues or refused."""

    ADULT = face(373, 43, vec(2), "female")
    BABY = face(576, 2, vec(1), "male")
    GRANDMA = face(872, 68, vec(0), "female")

    def resolve(self, people, faces=None, photos=None):
        shot = png((40, 41, 42))
        FACES[shot] = list(faces or [self.ADULT, self.BABY, self.GRANDMA])
        fakes = Fakes([])
        deps = IdentityDeps(analyze=fakes.analyze, run_workflow=fakes.run_workflow, models=("buffalo_l",))
        req = SceneRequest(refine="off", photos=photos or [shot], prompt="x", people=people)
        return run(ids.resolve_people(req, deps, with_heads=False))

    def test_infant_gets_the_youngest_face_and_grandma_the_old_one(self) -> None:
        got = self.resolve([PersonSpec(label="grandma", description="elderly woman with glasses"),
                            PersonSpec(label="baby", infant=True)])
        by = {r.label: r for r in got}
        self.assertEqual(by["grandma"].face_bbox[0], 872)
        self.assertEqual(by["baby"].face_bbox[0], 576)
        self.assertEqual(by["baby"].reason, "infant-youngest")
        self.assertEqual(by["grandma"].reason, "age-match")
        self.assertNotEqual(by["grandma"].face_bbox, by["baby"].face_bbox)

    def test_the_order_of_the_people_does_not_matter(self) -> None:
        got = self.resolve([PersonSpec(label="baby", infant=True), PersonSpec(label="grandma")])
        self.assertEqual([r.face_bbox[0] for r in got], [576, 872])

    def test_age_words_alone_are_enough(self) -> None:
        got = self.resolve([PersonSpec(label="the toddler"), PersonSpec(label="mom", description="woman in her forties")])
        self.assertEqual([r.face_bbox[0] for r in got], [576, 373])

    def test_gender_words_separate_two_adults(self) -> None:
        a, b = face(100, 40, vec(0), "male"), face(400, 42, vec(1), "female")
        got = self.resolve([PersonSpec(label="the man"), PersonSpec(label="the woman")], faces=[a, b])
        self.assertEqual([r.face_bbox[0] for r in got], [100, 400])

    def test_unboxed_people_with_no_cues_are_refused_with_the_face_list(self) -> None:
        with self.assertRaises(IdentityError) as ctx:
            self.resolve([PersonSpec(label="person 1"), PersonSpec(label="person 2")])
        self.assertEqual(ctx.exception.code, "ambiguous_faces")
        self.assertEqual(len(ctx.exception.faces), 3)
        self.assertEqual({"photo", "bbox", "age_estimate", "gender_estimate"} <= set(ctx.exception.faces[0]), True)
        self.assertIn("box", str(ctx.exception))

    def test_one_unboxed_person_among_several_faces_is_refused_not_the_largest(self) -> None:
        with self.assertRaises(IdentityError) as ctx:
            self.resolve([PersonSpec(label="Sam")])
        self.assertEqual(ctx.exception.code, "ambiguous_faces")

    def test_a_lone_person_with_a_lone_face_needs_no_cue(self) -> None:
        got = self.resolve([PersonSpec(label="Sam")], faces=[self.GRANDMA])
        self.assertEqual(got[0].reason, "only-face")

    def test_more_people_than_faces_is_refused(self) -> None:
        with self.assertRaises(IdentityError) as ctx:
            self.resolve([PersonSpec(label="grandma"), PersonSpec(label="baby", infant=True)], faces=[self.GRANDMA])
        self.assertEqual(ctx.exception.code, "ambiguous_faces")

    def test_two_boxes_on_the_same_face_are_refused(self) -> None:
        with self.assertRaises(IdentityError) as ctx:
            self.resolve([PersonSpec(label="a", box=(800, 0, 1000, 300)), PersonSpec(label="b", box=(850, 0, 1000, 300))])
        self.assertEqual(ctx.exception.code, "same_face")

    def test_boxed_people_keep_their_box_and_unboxed_ones_get_what_is_left(self) -> None:
        got = self.resolve([PersonSpec(label="grandma", box=(800, 0, 1000, 300)),
                            PersonSpec(label="baby", infant=True)])
        self.assertEqual([r.reason for r in got], ["box", "infant-youngest"])
        self.assertEqual(got[1].face_bbox[0], 576)

    def test_a_person_cannot_take_a_face_a_boxed_person_already_has(self) -> None:
        # the only way left for "baby" is the youngest of the faces nobody claimed
        got = self.resolve([PersonSpec(label="baby", box=(500, 50, 800, 300), infant=True),
                            PersonSpec(label="grandma")])
        self.assertEqual(got[1].face_bbox[0], 872)

    def test_auto_detection_reports_its_reason(self) -> None:
        got = self.resolve([])
        self.assertEqual({r.reason for r in got}, {"auto"})
        self.assertEqual(len({r.face_bbox for r in got}), 3)

    def test_the_scene_response_reports_the_chosen_face_and_reason(self) -> None:
        shot = png((43, 44, 45))
        FACES[shot] = [self.ADULT, self.BABY, self.GRANDMA]
        fakes = Fakes([OUT_LIST[1]])
        people = [PersonSpec(label="grandma"), PersonSpec(label="baby", infant=True)]
        result = scene(fakes, SceneRequest(refine="off", photos=[shot], prompt="x", people=people, photo_as_canvas=False, attempts=1))
        self.assertEqual([(p["label"], p["face_bbox"][0], p["reason"]) for p in result.people],
                         [("grandma", 872.0, "age-match"), ("baby", 576.0, "infant-youngest")])

    def test_two_people_in_an_extra_photo_cannot_share_a_face(self) -> None:
        group = png((46, 47, 48))
        FACES[group] = [face(50, 70, vec(0)), face(250, 3, vec(1))]
        shot = png((49, 50, 51))
        FACES[shot] = [face(100, 70, vec(0)), face(400, 3, vec(1))]
        people = [PersonSpec(label="grandma", box=(0, 0, 300, 400), more=[RefSource(image=1)]),
                  PersonSpec(label="baby", box=(350, 0, 600, 400), more=[RefSource(image=1)])]
        got = self.resolve(people, photos=[shot, group])
        used = [tuple(f.bbox) for r in got for f, i in r.sources_used if i == 1]
        self.assertEqual(len(used), len(set(used)))


class SteeringTests(unittest.TestCase):
    def test_profile_and_forehead_phrases_are_rewritten_with_a_warning(self) -> None:
        text = "Two subjects in profile facing each other, foreheads nearly touching, golden light."
        out, warnings = ids.steer_prompt(text, "likeness")
        self.assertNotIn("profile", out.split(ids.STEERING_SENTENCE)[0].lower())
        self.assertNotIn("foreheads", out.lower())
        self.assertIn("three-quarter", out)
        self.assertEqual(warnings[0]["code"], "pose_rewritten")

    def test_a_negated_profile_is_left_alone(self) -> None:
        text = "Both faces are clearly visible and well lit, not in profile. Avoid side view."
        out, warnings = ids.steer_prompt(text, "likeness")
        self.assertIn("not in profile", out)
        self.assertIn("Avoid side view", out)
        self.assertEqual(warnings, [])

    def test_as_written_keeps_the_prompt_and_warns_about_risk(self) -> None:
        out, warnings = ids.steer_prompt("a side view portrait", "as_written")
        self.assertEqual(out, "a side view portrait")
        self.assertEqual(warnings[0]["code"], "pose_risks_likeness")

    def test_plain_prompt_gets_the_steering_sentence_and_no_warning(self) -> None:
        out, warnings = ids.steer_prompt("They hug in a garden.", "likeness")
        self.assertTrue(out.endswith(ids.STEERING_SENTENCE))
        self.assertEqual(warnings, [])

    def test_auto_resolution_shrinks_with_more_references(self) -> None:
        self.assertGreater(ids.auto_resolution(3), ids.auto_resolution(5))
        self.assertGreater(ids.auto_resolution(5), ids.auto_resolution(8))


class MultiReferenceTests(unittest.TestCase):
    def test_several_photos_per_person_are_used_for_generation_and_scoring(self) -> None:
        second = png((90, 90, 90))
        FACES[second] = [face(60, 70, vec(0))]
        fakes = Fakes([OUT_LIST[1]])
        people = [
            PersonSpec(label="grandma", box=(0, 0, 300, 400), more=[RefSource(image=1)]),
            PersonSpec(label="baby", box=(350, 0, 600, 400)),
        ]
        result = scene(fakes, SceneRequest(refine="off", photos=[PHOTO, second], prompt="x", people=people, photo_as_canvas=False))
        prompt = fakes.graphs[0]["7"]["inputs"]["prompt"]
        self.assertIn("are 2 photos of the same person, grandma", prompt)
        self.assertEqual(len(fakes.uploads[0]), 3)  # two grandma heads + one baby head
        self.assertEqual([p["reference_photos"] for p in result.people], [2, 1])
        grandma = next(p for p in result.report["people"] if p["label"] == "grandma")
        self.assertEqual(len(grandma["match"]["per_reference"]), 2)

    def test_extra_photo_without_a_box_picks_the_face_that_looks_like_the_person(self) -> None:
        group = png((7, 7, 7))
        FACES[group] = [face(50, 40, vec(5)), face(250, 70, vec(0)), face(450, 3, vec(1))]  # stranger, grandma, baby
        fakes = Fakes([OUT_LIST[1]])
        people = [PersonSpec(label="grandma", box=(0, 0, 300, 400), more=[RefSource(image=1)]),
                  PersonSpec(label="baby", box=(350, 0, 600, 400), more=[RefSource(image=1)])]
        result = scene(fakes, SceneRequest(refine="off", photos=[PHOTO, group], prompt="x", people=people, photo_as_canvas=False))
        self.assertEqual([sorted(p["reference_photo_indices"]) for p in result.people], [[0, 1], [0, 1]])
        self.assertEqual([w for w in result.warnings if w["code"] == "reference_skipped"], [])

    def test_photo_with_nobody_who_matches_is_skipped_with_a_warning(self) -> None:
        stranger_only = png((8, 8, 8))
        FACES[stranger_only] = [face(100, 40, vec(5))]
        fakes = Fakes([OUT_LIST[1]])
        people = [PersonSpec(label="grandma", box=(0, 0, 300, 400), more=[RefSource(image=1)]),
                  PersonSpec(label="baby", box=(350, 0, 600, 400))]
        result = scene(fakes, SceneRequest(refine="off", photos=[PHOTO, stranger_only], prompt="x", people=people, photo_as_canvas=False))
        self.assertEqual(result.people[0]["reference_photos"], 1)
        self.assertEqual([w["code"] for w in result.warnings if w["code"] == "reference_skipped"], ["reference_skipped"])
        self.assertIn("grandma", result.warnings[0]["param"])

    def test_the_best_references_are_sent_first_and_capped(self) -> None:
        shots = []
        for i, (width, yaw_dx) in enumerate([(60, 0), (200, 0), (200, 30), (150, 0)], start=1):
            shot = png((20 + i, 0, 0))
            kps = ((30, 50.0), (90, 50.0), (60 + yaw_dx, 80.0), (35, 110.0), (85, 110.0))
            FACES[shot] = [{"bbox": [100, 100, 100 + width, 100 + width], "det_score": 0.9, "age": 70, "gender": "female",
                            "embedding": vec(0), "kps": [list(k) for k in kps]}]
            shots.append(shot)
        people = [PersonSpec(label="a", image=0, box=(0, 0, 300, 400), more=[RefSource(image=i) for i in (1, 2, 3, 4)])]
        fakes = Fakes([OUT_LIST[1]])
        result = scene(fakes, SceneRequest(refine="off", photos=[PHOTO, *shots], prompt="x", people=people,
                                           photo_as_canvas=False, max_refs_per_person=3, attempts=1))
        self.assertEqual(result.people[0]["reference_heads_sent"], 3)
        self.assertEqual(result.people[0]["reference_photos"], 5)
        self.assertEqual(result.people[0]["reference_photo_indices"][0], 2)  # large and frontal first
        self.assertNotIn(1, result.people[0]["reference_photo_indices"])      # the 60 px face is dropped

    def test_scoring_only_photos_are_not_sent_to_the_generator(self) -> None:
        extra = png((9, 9, 9))
        FACES[extra] = [face(40, 70, vec(0))]
        fakes = Fakes([OUT_LIST[1]])
        people = [PersonSpec(label="grandma", box=(0, 0, 300, 400), more=[RefSource(image=1, generate=False)]),
                  PersonSpec(label="baby", box=(350, 0, 600, 400))]
        result = scene(fakes, SceneRequest(refine="off", photos=[PHOTO, extra], prompt="x", people=people, photo_as_canvas=False))
        self.assertEqual(result.people[0]["reference_photos"], 2)      # both score
        self.assertEqual(result.people[0]["reference_heads_sent"], 1)  # one generates
        self.assertEqual(len(fakes.uploads[0]), 2)

    def test_too_many_reference_images_is_an_error(self) -> None:
        many = [PersonSpec(label=f"p{i}", box=(0, 0, 300, 400)) for i in range(2)]
        deps_nodes = ids.MAX_IMAGE_SLOTS
        self.assertEqual(deps_nodes, 10)
        with self.assertRaises(IdentityError):
            scene(Fakes([OUT_LIST[1]]), SceneRequest(refine="off", photos=[PHOTO], prompt="x", people=many, max_refs_per_person=1,
                  photo_as_canvas=False), nodes=["L1"])


class RefinementTests(unittest.TestCase):
    def refine_scene(self, refined: bytes):
        half = png((3, 3, 3), (64, 64))                       # grandma right, baby wrong
        FACES[half] = [face(100, 70, vec(0)), face(400, 1, vec(6))]
        fakes = Fakes([half, refined])
        result = scene(fakes, SceneRequest(refine="auto", refine_attempts=1, photos=[PHOTO], prompt="x", attempts=1))
        return fakes, result

    def test_a_helpful_pass_is_kept_and_edits_the_whole_picture_with_one_persons_references(self) -> None:
        fakes, result = self.refine_scene(GOOD_PASTE)
        self.assertEqual(len(fakes.graphs), 2)                 # one compose, one refine
        refine_graph = fakes.graphs[1]["7"]["inputs"]["prompt"]
        self.assertIn("the right person in <image1> (a baby)", refine_graph)
        self.assertIn("<image2>", refine_graph)
        self.assertNotIn("<image3>", refine_graph)             # only the baby's reference, not the grandmother's
        self.assertEqual(len(fakes.uploads[1]), 2)             # the composed picture + the baby's head
        self.assertTrue(result.refinements[0]["accepted"])
        self.assertEqual(result.refinements[0]["person"], "person 2")
        self.assertEqual(result.image, GOOD_PASTE)
        baby = next(p for p in result.report["people"] if p["label"] == "person 2")
        self.assertGreater(baby["match"]["score"], 0.9)

    def test_a_pass_that_hurts_the_other_person_is_rejected(self) -> None:
        fakes, result = self.refine_scene(HURTS_OTHER_PASTE)
        self.assertFalse(result.refinements[0]["accepted"])
        self.assertNotEqual(result.image, HURTS_OTHER_PASTE)
        grandma = next(p for p in result.report["people"] if p["label"] == "person 1")
        self.assertGreater(grandma["match"]["score"], 0.9)

    def test_refine_off_runs_no_extra_pass(self) -> None:
        fakes = Fakes([OUT_LIST[0]])
        scene(fakes, SceneRequest(refine="off", photos=[PHOTO], prompt="x", attempts=1))
        self.assertEqual(len(fakes.graphs), 1)

    def test_people_who_already_match_are_not_refined_in_auto_mode(self) -> None:
        fakes = Fakes([OUT_LIST[1]])
        scene(fakes, SceneRequest(refine="auto", photos=[PHOTO], prompt="x", attempts=1, stop_when_pass=False))
        self.assertEqual(len(fakes.graphs), 1)

    def test_improvement_rule(self) -> None:
        before = {"people": [{"label": "a", "match": {"score": 0.2}}, {"label": "b", "match": {"score": 0.7}}], "overall": {"extra_faces": 0}}
        better = {"people": [{"label": "a", "match": {"score": 0.5}}, {"label": "b", "match": {"score": 0.69}}], "overall": {"extra_faces": 0}}
        worse_b = {"people": [{"label": "a", "match": {"score": 0.5}}, {"label": "b", "match": {"score": 0.6}}], "overall": {"extra_faces": 0}}
        extra = {"people": better["people"], "overall": {"extra_faces": 1}}
        self.assertTrue(ids.refinement_improves(before, better, "a"))
        self.assertFalse(ids.refinement_improves(before, worse_b, "a"))
        self.assertFalse(ids.refinement_improves(before, extra, "a"))
        self.assertFalse(ids.refinement_improves(before, before, "a"))

    def test_where_in_frame(self) -> None:
        self.assertEqual(ids.where_in_frame([0, 0, 200, 200], 1000), "left")
        self.assertEqual(ids.where_in_frame([800, 0, 1000, 200], 1000), "right")
        self.assertEqual(ids.where_in_frame([400, 0, 600, 200], 1000), "middle")


if __name__ == "__main__":
    unittest.main()
