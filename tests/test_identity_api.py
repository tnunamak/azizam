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
BINDING = {"image_nodes": [f"L{i}" for i in range(1, 7)], "prompt_node": "7", "seed_node": "8"}


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
