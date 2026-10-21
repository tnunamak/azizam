"""Identity routes: multipart parsing, error envelope, JSON payload, OpenAPI typing."""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gateway import identity_api  # noqa: E402

TEMPLATE = json.loads((ROOT / "workflows/identity/qwen-image-2.1-identity.json").read_text())
BINDING = {"image_nodes": [f"L{i}" for i in range(1, 11)], "prompt_node": "7", "seed_node": "8"}


def png(color: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (1280, 960), color).save(out, format="PNG")
    return out.getvalue()


def vec(i: int) -> list[float]:
    v = [0.0] * 8
    v[i] = 1.0
    return v


def face(x: float, age: int, emb: list[float]) -> dict:
    return {"bbox": [x, 100, x + 120, 260], "det_score": 0.9, "age": age, "gender": "female", "embedding": emb}


PHOTO = png((10, 20, 30))
GOOD = png((1, 1, 1))
BAD = png((2, 2, 2))
FACES = {
    PHOTO: [face(100, 70, vec(0)), face(400, 40, vec(1))],
    GOOD: [face(90, 70, vec(0)), face(380, 40, vec(1))],
    BAD: [face(90, 70, vec(5)), face(380, 40, vec(6))],
}


class FakeClient:
    async def analyze(self, image: bytes, model: str) -> list[dict]:
        return FACES[image]


def build(outputs: list[bytes], *, ready: bool = True):
    calls: list[dict] = []
    seen: dict = {"policy": []}

    async def ensure():
        return None if ready else __import__("fastapi").Response(status_code=503, content=b"{}")

    async def run_workflow(auth, graph, uploads):
        calls.append({"graph": graph, "uploads": uploads, "auth": auth})
        if len(calls) > len(outputs):
            raise RuntimeError("ComfyUI returned no image")
        return outputs[len(calls) - 1]

    def store(request, auth, content, fmt):
        return {"asset_id": "img_1", "asset_url": "http://x/img_1", "media_type": "image/png", "b64_json": "AAAA"}

    def enforce(auth, path):
        seen["policy"].append(path)
        return None

    app = FastAPI()
    identity_api.register_identity_routes(app, identity_api.IdentityApiDeps(
        authenticate=lambda request, path: "auth",
        enforce_policy=enforce,
        ensure_face_service=ensure,
        face_client=lambda: FakeClient(),
        run_workflow=run_workflow,
        template=lambda: (TEMPLATE, BINDING),
        store_image=store,
        models=("buffalo_l",),
    ))
    return TestClient(app), calls, seen


class LikenessRouteTests(unittest.TestCase):
    def post(self, client, candidate: bytes, **data):
        return client.post(identity_api.LIKENESS_PATH,
                           files=[("reference", ("p.png", PHOTO, "image/png")), ("candidate", ("c.png", candidate, "image/png"))],
                           data=data)

    def test_match_and_mismatch(self) -> None:
        client, _, seen = build([])
        ok = self.post(client, GOOD).json()
        self.assertEqual({p["verdict"] for p in ok["people"]}, {"match"})
        self.assertEqual(len(ok["references"]), 2)
        bad = self.post(client, BAD).json()
        self.assertEqual(bad["overall"]["status"], "fail")
        self.assertEqual(seen["policy"], [identity_api.LIKENESS_PATH] * 2)

    def test_people_and_thresholds_are_parsed(self) -> None:
        client, _, _ = build([])
        people = json.dumps([{"label": "left", "box": [0, 0, 300, 400]}])
        report = self.post(client, GOOD, people=people, thresholds=json.dumps({"match": 0.9})).json()
        self.assertEqual([p["label"] for p in report["people"]], ["left"])
        self.assertEqual(report["thresholds"]["match"], 0.9)

    def test_pooling_is_accepted_and_echoed(self) -> None:
        client, _, _ = build([])
        self.assertEqual(self.post(client, GOOD, pooling="mean").json()["pooling"], "mean")
        self.assertEqual(self.post(client, GOOD, pooling="median").json()["error"]["param"], "pooling")

    def test_errors_use_the_envelope(self) -> None:
        client, _, _ = build([])
        r = client.post(identity_api.LIKENESS_PATH, files=[("candidate", ("c.png", GOOD, "image/png"))])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["param"], "reference")
        r = self.post(client, GOOD, people="not json")
        self.assertEqual(r.json()["error"]["param"], "people")
        r = self.post(client, GOOD, thresholds=json.dumps({"bogus": 1}))
        self.assertEqual(r.json()["error"]["param"], "thresholds")
        r = client.post(identity_api.LIKENESS_PATH, json={})
        self.assertEqual(r.status_code, 400)

    def test_face_service_down_is_returned(self) -> None:
        client, _, _ = build([], ready=False)
        self.assertEqual(self.post(client, GOOD).status_code, 503)


class IdentityRouteTests(unittest.TestCase):
    def post(self, client, **data):
        return client.post(identity_api.IDENTITY_PATH,
                           files=[("image", ("p.png", PHOTO, "image/png"))],
                           data={"prompt": "They look into each other's eyes.", **data})

    def test_best_seed_and_payload(self) -> None:
        client, calls, _ = build([BAD, GOOD])
        r = self.post(client, attempts="3", seed="7", stop_when_pass="true")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(len(calls), 2)  # second attempt met the threshold
        self.assertEqual(body["data"][0]["asset_id"], "img_1")
        self.assertEqual([a["seed"] for a in body["attempts"]], [7, 8])
        self.assertEqual([a["selected"] for a in body["attempts"]], [False, True])
        self.assertEqual(body["likeness"]["object"], "likeness.check")
        self.assertIn("<image2>", calls[0]["graph"]["7"]["inputs"]["prompt"])
        self.assertEqual(set(calls[0]["uploads"]), {"L1", "L2", "L3"})

    def test_validation_errors(self) -> None:
        client, calls, _ = build([GOOD])
        for data, param in (
            ({"prompt": ""}, "prompt"),
            ({"attempts": "99"}, "attempts"),
            ({"attempts": "x"}, "attempts"),
            ({"people": json.dumps({"a": 1})}, "people"),
            ({"people": json.dumps([{"image": 3}])}, "people"),
            ({"response_format": "png"}, "response_format"),
            ({"cfg": "20"}, "cfg"),
        ):
            r = self.post(client, **data) if data.get("prompt") != "" else client.post(
                identity_api.IDENTITY_PATH, files=[("image", ("p.png", PHOTO, "image/png"))], data={"prompt": ""})
            self.assertEqual(r.status_code, 400, data)
            self.assertEqual(r.json()["error"]["param"], param, data)
        self.assertEqual(calls, [])

    def test_backend_failure_is_a_502(self) -> None:
        client, _, _ = build([])
        r = self.post(client)
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["error"]["code"], "identity_backend_error")


class PhotoLimitTests(unittest.TestCase):
    def test_up_to_twelve_reference_photos_are_accepted(self) -> None:
        client, _, _ = build([])
        many = [("reference", (f"p{i}.png", PHOTO, "image/png")) for i in range(identity_api.MAX_PHOTOS)]
        people = json.dumps([{"label": "a", "images": [{"image": 0, "box": [0, 0, 300, 400]}, *range(1, identity_api.MAX_PHOTOS)]}])
        r = client.post(identity_api.LIKENESS_PATH, files=many + [("candidate", ("c.png", GOOD, "image/png"))], data={"people": people})
        self.assertEqual(r.status_code, 200, r.text)
        too_many = many + [("reference", ("x.png", PHOTO, "image/png"))]
        r = client.post(identity_api.LIKENESS_PATH, files=too_many + [("candidate", ("c.png", GOOD, "image/png"))])
        self.assertEqual(r.status_code, 400)


class PeopleParsingTests(unittest.TestCase):
    def test_images_list_and_repeated_labels_merge_into_one_person(self) -> None:
        people = identity_api.parse_people([
            {"label": "grandma", "images": [0, {"image": 2, "box": [1, 2, 3, 4]}], "description": "glasses"},
            {"label": "baby", "image": 1, "infant": True},
            {"label": "grandma", "image": 3},
        ], 4)
        self.assertEqual([p.label for p in people], ["grandma", "baby"])
        grandma = people[0]
        self.assertEqual([(s.image, s.box) for s in grandma.sources()], [(0, None), (2, (1.0, 2.0, 3.0, 4.0)), (3, None)])
        self.assertEqual(grandma.description, "glasses")
        self.assertTrue(people[1].infant)

    def test_generate_false_marks_a_scoring_only_photo(self) -> None:
        person = identity_api.parse_people([{"label": "a", "images": [0, {"image": 1, "generate": False}]}], 2)[0]
        self.assertEqual([s.generate for s in person.sources()], [True, False])
        with self.assertRaises(identity_api.IdentityError):
            identity_api.parse_people([{"label": "a", "images": [{"image": 0, "generate": "no"}]}], 1)

    def test_bad_sources_are_rejected(self) -> None:
        for raw in ([{"label": "a", "images": []}], [{"label": "a", "images": ["x"]}], [{"label": "a", "images": [9]}],
                    [{"label": "a", "images": [{"image": 0, "box": [1, 2]}]}]):
            with self.assertRaises(identity_api.IdentityError):
                identity_api.parse_people(raw, 2)

    def test_scene_options_defaults_and_ranges(self) -> None:
        req = identity_api.build_scene_request({}, [b"x"], "p")
        self.assertEqual((req.refine, req.pooling, req.pose_policy, req.max_refs_per_person, req.resolution),
                         ("auto", "best", "likeness", 3, None))
        for bad in ({"refine": "sometimes"}, {"pooling": "median"}, {"max_refs_per_person": "9"}, {"refine_attempts": "0"}):
            with self.assertRaises(identity_api.IdentityError):
                identity_api.build_scene_request(bad, [b"x"], "p")

    def test_mode_resolution(self) -> None:
        quick = identity_api.build_scene_request({"attempts": "1", "refine": "off"}, [b"x"], "p")
        slow = identity_api.build_scene_request({}, [b"x"], "p")
        self.assertFalse(identity_api.wants_async({}, quick))
        self.assertTrue(identity_api.wants_async({}, slow))
        self.assertFalse(identity_api.wants_async({"mode": "sync"}, slow))
        self.assertTrue(identity_api.wants_async({"mode": "async"}, quick))
        self.assertFalse(identity_api.wants_async({}, slow, default="sync"))


class AsyncRouteTests(unittest.TestCase):
    def test_identity_route_hands_long_runs_to_the_job_queue(self) -> None:
        from fastapi.responses import JSONResponse

        auth = object()
        submitted = []
        workflow_calls = []
        stored = []

        async def submit_job(request, received_auth, req, fmt):
            submitted.append((received_auth, req, fmt))
            return JSONResponse(
                {
                    "object": "media.job",
                    "id": "job_1",
                    "status": "queued",
                    "progress": 0,
                    "poll_url": "/gateway/v1/media/jobs/job_1",
                    "content_url": "/gateway/v1/media/jobs/job_1/content",
                },
                status_code=202,
            )

        async def run_workflow(received_auth, graph, uploads):
            workflow_calls.append((received_auth, graph, uploads))
            return GOOD

        def store_image(request, received_auth, content, fmt):
            stored.append((received_auth, content, fmt))
            return {
                "asset_id": "selected-image",
                "asset_url": "http://testserver/selected-image",
                "media_type": "image/png",
            }

        app = FastAPI()
        identity_api.register_identity_routes(
            app,
            identity_api.IdentityApiDeps(
                authenticate=lambda request, path: auth,
                enforce_policy=lambda received_auth, path: None,
                ensure_face_service=_none,
                face_client=lambda: FakeClient(),
                run_workflow=run_workflow,
                template=lambda: (TEMPLATE, BINDING),
                store_image=store_image,
                models=("buffalo_l",),
                submit_job=submit_job,
            ),
        )
        with TestClient(app) as client:
            queued = client.post(
                identity_api.IDENTITY_PATH,
                files=[("image", ("p.png", PHOTO, "image/png"))],
                data={
                    "prompt": "A garden portrait",
                    "attempts": "3",
                    "refine": "off",
                    "seed": "17",
                    "response_format": "url",
                },
            )
            self.assertEqual(queued.status_code, 202, queued.text)
            self.assertEqual(
                queued.json()["poll_url"], "/gateway/v1/media/jobs/job_1",
            )
            self.assertEqual(len(submitted), 1)
            received_auth, req, fmt = submitted[0]
            self.assertIs(received_auth, auth)
            self.assertEqual(req.photos, [PHOTO])
            self.assertEqual(req.prompt, "A garden portrait")
            self.assertEqual((req.attempts, req.seed, fmt), (3, 17, "url"))
            self.assertEqual(workflow_calls, [])
            self.assertEqual(stored, [])

            completed = client.post(
                identity_api.IDENTITY_PATH,
                files=[("image", ("p.png", PHOTO, "image/png"))],
                data={
                    "prompt": "A garden portrait",
                    "attempts": "1",
                    "refine": "off",
                    "seed": "18",
                    "response_format": "url",
                },
            )
            self.assertEqual(completed.status_code, 200, completed.text)
            self.assertEqual(len(submitted), 1)
            self.assertEqual(len(workflow_calls), 1)
            self.assertIs(workflow_calls[0][0], auth)
            self.assertEqual(stored, [(auth, GOOD, "url")])
            self.assertEqual(
                completed.json()["data"][0]["asset_id"], "selected-image",
            )



async def _none():
    return None


async def _good():
    return GOOD


THREE = png((77, 78, 79))
FACES[THREE] = [
    {"bbox": [373, 91, 486, 252], "det_score": 0.9, "age": 43, "gender": "female", "embedding": vec(2)},
    {"bbox": [576, 379, 665, 483], "det_score": 0.9, "age": 2, "gender": "male", "embedding": vec(1)},
    {"bbox": [872, 38, 994, 209], "det_score": 0.9, "age": 68, "gender": "female", "embedding": vec(0)},
]


class WhoIsWhoRouteTests(unittest.TestCase):
    def post(self, client, path, people, **data):
        files = [("image" if path == identity_api.IDENTITY_PATH else "reference", ("p.png", THREE, "image/png"))]
        if path == identity_api.LIKENESS_PATH:
            files.append(("candidate", ("c.png", GOOD, "image/png")))
        return client.post(path, files=files, data={"prompt": "x", "people": json.dumps(people), **data})

    def test_ambiguous_people_get_a_400_with_the_detected_faces_on_every_route(self) -> None:
        client, calls, _ = build([GOOD])
        people = [{"label": "person 1"}, {"label": "person 2"}]
        for path, extra in ((identity_api.IDENTITY_PATH, {"mode": "sync"}), (identity_api.LIKENESS_PATH, {})):
            r = self.post(client, path, people, **extra)
            self.assertEqual(r.status_code, 400, path)
            body = r.json()["error"]
            self.assertEqual(body["code"], "ambiguous_faces")
            self.assertEqual([f["bbox"][0] for f in body["faces"]], [373, 576, 872])
            self.assertEqual([f["age_estimate"] for f in body["faces"]], [43, 2, 68])
        self.assertEqual(calls, [])

    def test_the_async_path_refuses_before_a_job_is_created(self) -> None:
        submitted = []

        async def submit_job(request, auth, req, fmt):
            submitted.append(req)
            from fastapi.responses import JSONResponse
            return JSONResponse({"id": "job"}, status_code=202)

        from fastapi import FastAPI
        app = FastAPI()
        identity_api.register_identity_routes(app, identity_api.IdentityApiDeps(
            authenticate=lambda r, p: "auth", enforce_policy=lambda a, p: None, ensure_face_service=lambda: _none(),
            face_client=lambda: FakeClient(), run_workflow=lambda a, g, u: _good(), template=lambda: (TEMPLATE, BINDING),
            store_image=lambda *a: {}, models=("buffalo_l",), submit_job=submit_job))
        c = TestClient(app)
        r = self.post(c, identity_api.IDENTITY_PATH, [{"label": "person 1"}, {"label": "person 2"}], mode="async")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "ambiguous_faces")
        self.assertEqual(submitted, [])
        ok = self.post(c, identity_api.IDENTITY_PATH, [{"label": "grandma"}, {"label": "baby", "infant": True}], mode="async")
        self.assertEqual(ok.status_code, 202)
        self.assertEqual(len(submitted), 1)

    def test_the_likeness_route_reports_the_chosen_face_and_reason(self) -> None:
        client, _, _ = build([])
        r = self.post(client, identity_api.LIKENESS_PATH, [{"label": "grandma"}, {"label": "baby", "infant": True}])
        self.assertEqual(r.status_code, 200, r.text)
        refs = {x["label"]: x for x in r.json()["references"]}
        self.assertEqual(refs["grandma"]["face_bbox"][0], 872.0)
        self.assertEqual(refs["baby"]["face_bbox"][0], 576.0)
        self.assertEqual((refs["grandma"]["reason"], refs["baby"]["reason"]), ("age-match", "infant-youngest"))


class OpenApiTests(unittest.TestCase):
    def test_routes_are_typed(self) -> None:
        client, _, _ = build([])
        spec = client.get("/openapi.json").json()
        for path in (identity_api.LIKENESS_PATH, identity_api.IDENTITY_PATH):
            body = spec["paths"][path]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
            self.assertIn("properties", body)
        ident = spec["paths"][identity_api.IDENTITY_PATH]["post"]["requestBody"]["content"]["multipart/form-data"]["schema"]
        self.assertEqual(ident["properties"]["attempts"]["maximum"], 8)
        self.assertEqual(set(ident["required"]), {"image", "prompt"})


if __name__ == "__main__":
    unittest.main()
