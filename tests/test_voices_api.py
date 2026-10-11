"""HTTP routes for custom voices and consents (gateway/voices_api.py), with fake engines."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from gateway import custom_voices as cv
from gateway import voices_api as api
from gateway.voice_audio import SampleLimits
from gateway.voices_api import VoiceApiDeps
from tests.voice_test_audio import HAVE_FFMPEG, speechlike
from tests.test_voice_design import OMNIVOICE as SEEDED_DESIGN_SETTINGS

SAMPLE = speechlike(10) if HAVE_FFMPEG else b""


class ApiCase(unittest.TestCase):
    design_wav: bytes = b""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.limits = cv.VoiceLimits(max_voices=3)
        self.store = cv.VoiceStore(Path(self.tmp.name), self.limits)
        self.probed: list[Path] = []
        self.designs: list[tuple] = []
        self.design_settings = SEEDED_DESIGN_SETTINGS
        self.allow = True

        self.probe_result = {"speaker_similarity": 0.71, "encoder": "ecapa-test", "engine": "chatterbox"}
        self.probe_error = False

        async def probe(path: Path):
            self.probed.append(path)
            if self.probe_error:
                raise RuntimeError("engine down")
            return self.probe_result

        async def design(prompt: str, script: str, seed: int) -> bytes:
            self.designs.append((prompt, script, seed))
            return self.design_wav or speechlike(11)

        def authenticate(request, _path):
            if not self.allow:
                return JSONResponse({"error": {"message": "no key"}}, status_code=401)
            return SimpleNamespace(owner=request.headers.get("x-owner", "key-a"))

        deps = VoiceApiDeps(
            store=lambda: self.store,
            sample_limits=lambda: SampleLimits(),
            authenticate=authenticate,
            enforce_policy=lambda _auth, _path: None,
            owner_of=lambda auth: auth.owner,
            probe_quality=probe,
            design_voice=design,
            design_settings=lambda: self.design_settings,
            compatible_engines=lambda: ["chatterbox", "omnivoice"],
            quality_warn_below=lambda: 0.45,
        )
        app = FastAPI()
        api.register_voice_routes(app, deps)
        self.client = TestClient(app)

    def upload(self, *, name="Narrator", wav=SAMPLE, owner="key-a", extra=None, filename="s.wav", mime="audio/wav", prefix="/v1"):
        return self.client.post(
            f"{prefix}/audio/voices",
            data={"name": name, **(extra or {})},
            files={"audio_sample": (filename, wav, mime)},
            headers={"x-owner": owner},
        )


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
class CreateVoiceTests(ApiCase):
    def test_creates_an_openai_shaped_voice_with_quality_feedback(self) -> None:
        resp = self.upload(extra={"description": "my voice", "labels": '{"accent": "uk"}'})
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual((body["object"], body["type"], body["name"]), ("audio.voice", "audio_sample", "Narrator"))
        self.assertRegex(body["id"], r"^voice_[0-9a-f]{24}$")
        self.assertIsInstance(body["created_at"], int)
        self.assertEqual(body["compatible_engines"], ["chatterbox", "omnivoice"])
        self.assertEqual(body["labels"], {"accent": "uk"})
        self.assertEqual(body["quality"], {"speaker_similarity": 0.71, "encoder": "ecapa-test", "engine": "chatterbox", "status": "ok"})
        self.assertAlmostEqual(body["duration_seconds"], 10, delta=0.7)
        self.assertEqual(self.probed, [self.store.reference_path("key-a", body["id"])])
        self.assertTrue(self.probed[0].is_file())

    def test_the_openai_alias_prefix_works(self) -> None:
        self.assertEqual(self.upload(prefix="/openai/v1").status_code, 200)

    def test_low_similarity_is_a_warning_not_a_failure(self) -> None:
        self.probe_result = {"speaker_similarity": 0.31, "encoder": "e", "engine": "chatterbox"}
        body = self.upload().json()
        self.assertEqual(body["quality"]["status"], "low")
        self.assertIn("low_speaker_similarity", body["warnings"])

    def test_an_unavailable_probe_does_not_block_creation(self) -> None:
        self.probe_result = None
        body = self.upload().json()
        self.assertEqual(body["quality"]["status"], "unchecked")
        self.assertIn("quality_check_unavailable", body["warnings"])

    def test_an_unchecked_voice_says_why(self) -> None:
        self.probe_result = {"error": "gpu_busy"}
        quality = self.upload().json()["quality"]
        self.assertEqual((quality["status"], quality["reason"]), ("unchecked", "gpu_busy"))

    def test_a_probe_that_raises_does_not_block_creation(self) -> None:
        self.probe_error = True
        body = self.upload().json()
        self.assertEqual(body["quality"]["status"], "unchecked")

    def test_bad_samples_are_400_with_codes(self) -> None:
        cases = {
            "too short": (speechlike(2), "sample_too_short"),
            "not audio": (b"hello" * 500, "invalid_audio_sample"),
            "silence": (speechlike(8, amplitude=0, noise=20), "insufficient_speech"),
        }
        for label, (wav, code) in cases.items():
            with self.subTest(label):
                resp = self.upload(wav=wav)
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.json()["error"]["code"], code)
                self.assertEqual(resp.json()["error"]["param"], "audio_sample")
        self.assertEqual(self.store.list_voices("key-a")[0], [])  # nothing half-created

    def test_oversize_upload_is_refused(self) -> None:
        resp = self.client.post(
            "/v1/audio/voices", data={"name": "x"},
            files={"audio_sample": ("s.wav", b"\0" * (10 * 1024 * 1024 + 10), "audio/wav")},
        )
        self.assertIn(resp.status_code, (400, 413))
        self.assertIn(resp.json()["error"]["code"], ("sample_too_large",))

    def test_field_validation(self) -> None:
        for label, kwargs, code in (
            ("missing name", {"name": ""}, "invalid_name"),
            ("reserved name", {"name": "Alloy"}, "voice_name_reserved"),
            ("bad type", {"extra": {"type": "clone"}}, "invalid_type"),
            ("bad labels", {"extra": {"labels": "[1]"}}, "invalid_labels"),
            ("unknown consent", {"extra": {"consent": "cons_" + "0" * 24}}, "consent_not_found"),
            ("malformed consent", {"extra": {"consent": "nope"}}, "invalid_consent"),
        ):
            with self.subTest(label):
                resp = self.upload(**kwargs)
                self.assertGreaterEqual(resp.status_code, 400, resp.text)
                self.assertEqual(resp.json()["error"]["code"], code)

    def test_missing_file_and_wrong_content_type(self) -> None:
        resp = self.client.post("/v1/audio/voices", files={"name": (None, "x")})
        self.assertEqual(resp.json()["error"]["code"], "missing_file")
        resp = self.client.post("/v1/audio/voices", json={"name": "x"})
        self.assertEqual(resp.json()["error"]["code"], "multipart_required")

    def test_quota_is_enforced_per_key(self) -> None:
        for n in range(3):
            self.assertEqual(self.upload(name=f"v{n}").status_code, 200)
        resp = self.upload(name="v4")
        self.assertEqual((resp.status_code, resp.json()["error"]["code"]), (400, "voice_quota_exceeded"))
        self.assertEqual(self.upload(name="other", owner="key-b").status_code, 200)

    def test_unauthenticated_requests_are_refused(self) -> None:
        self.allow = False
        self.assertEqual(self.upload().status_code, 401)
        self.assertEqual(self.client.get("/v1/audio/voices/voice_" + "0" * 24).status_code, 401)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
class ConsentFlowTests(ApiCase):
    def consent(self, owner="key-a", **overrides):
        data = {"name": "Jo Doe", "language": "en-US", **overrides}
        return self.client.post("/v1/audio/voice_consents", data=data, files={"recording": ("c.wav", SAMPLE, "audio/wav")}, headers={"x-owner": owner})

    def test_consent_crud_matches_the_openai_shapes(self) -> None:
        created = self.consent()
        self.assertEqual(created.status_code, 200, created.text)
        body = created.json()
        self.assertEqual(set(body), {"object", "id", "name", "language", "created_at"})
        self.assertEqual(body["object"], "audio.voice_consent")
        got = self.client.get(f"/v1/audio/voice_consents/{body['id']}").json()
        self.assertEqual(got, body)
        listed = self.client.get("/v1/audio/voice_consents", params={"limit": 1}).json()
        self.assertEqual((listed["object"], listed["first_id"], listed["last_id"], listed["has_more"]), ("list", body["id"], body["id"], False))
        renamed = self.client.post(f"/v1/audio/voice_consents/{body['id']}", json={"name": "Renamed"}).json()
        self.assertEqual(renamed["name"], "Renamed")
        deleted = self.client.delete(f"/v1/audio/voice_consents/{body['id']}").json()
        self.assertEqual(deleted, {"id": body["id"], "object": "audio.voice_consent", "deleted": True})
        self.assertEqual(self.client.get(f"/v1/audio/voice_consents/{body['id']}").status_code, 404)

    def test_consent_validation(self) -> None:
        self.assertEqual(self.consent(language="not a tag!").json()["error"]["code"], "invalid_language")
        self.assertEqual(self.consent(name=" ").json()["error"]["code"], "invalid_name")
        self.assertEqual(self.client.get("/v1/audio/voice_consents", params={"limit": 101}).status_code, 400)

    def test_consent_is_scoped_to_its_key(self) -> None:
        consent = self.consent().json()
        resp = self.client.get(f"/v1/audio/voice_consents/{consent['id']}", headers={"x-owner": "key-b"})
        self.assertEqual(resp.status_code, 404)
        resp = self.upload(owner="key-b", extra={"consent": consent["id"]})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.upload(extra={"consent": consent["id"]}).json()["consent"], consent["id"])

    def test_required_mode_over_http(self) -> None:
        self.store = cv.VoiceStore(self.store.root, cv.VoiceLimits(consent_mode="required"))
        resp = self.upload()
        self.assertEqual((resp.status_code, resp.json()["error"]["code"], resp.json()["error"]["param"]), (400, "consent_required", "consent"))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
class VoiceResourceTests(ApiCase):
    def test_get_list_rename_delete_and_sample(self) -> None:
        voice = self.upload().json()
        vid = voice["id"]
        self.assertEqual(self.client.get(f"/v1/audio/voices/{vid}").json()["id"], vid)
        sample = self.client.get(f"/v1/audio/voices/{vid}/sample")
        self.assertEqual((sample.status_code, sample.headers["content-type"]), (200, "audio/wav"))
        self.assertEqual(sample.content[:4], b"RIFF")
        renamed = self.client.post(f"/v1/audio/voices/{vid}", json={"name": "Better", "labels": {"k": "v"}}).json()
        self.assertEqual((renamed["name"], renamed["labels"]), ("Better", {"k": "v"}))
        self.assertEqual(self.client.post(f"/v1/audio/voices/{vid}", json={"name": "Alloy"}).json()["error"]["code"], "voice_name_reserved")
        self.assertEqual(self.client.post(f"/v1/audio/voices/{vid}", json={}).json()["error"]["code"], "nothing_to_update")
        deleted = self.client.delete(f"/v1/audio/voices/{vid}")
        self.assertEqual(deleted.json(), {"id": vid, "object": "audio.voice", "deleted": True})
        for method in ("get", "delete"):
            self.assertEqual(getattr(self.client, method)(f"/v1/audio/voices/{vid}").status_code, 404)
        self.assertEqual(self.client.get(f"/v1/audio/voices/{vid}/sample").status_code, 404)
        self.assertFalse(self.store.reference_path("key-a", vid).exists())

    def test_another_key_sees_404_on_every_verb(self) -> None:
        vid = self.upload().json()["id"]
        headers = {"x-owner": "key-b"}
        self.assertEqual(self.client.get(f"/v1/audio/voices/{vid}", headers=headers).status_code, 404)
        self.assertEqual(self.client.get(f"/v1/audio/voices/{vid}/sample", headers=headers).status_code, 404)
        self.assertEqual(self.client.post(f"/v1/audio/voices/{vid}", json={"name": "x"}, headers=headers).status_code, 404)
        self.assertEqual(self.client.delete(f"/v1/audio/voices/{vid}", headers=headers).status_code, 404)
        self.assertEqual(self.client.get(f"/v1/audio/voices/{vid}").status_code, 200)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
class PromptVoiceTests(ApiCase):
    def create(self, **body):
        return self.client.post("/v1/audio/voices", json={"type": "prompt", "name": "Warm", "prompt": "a warm, calm young woman, british accent", **body})

    def test_prompt_voice_is_designed_once_then_stored_as_a_clip(self) -> None:
        resp = self.create(seed=7)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual((body["type"], body["seed"], body["prompt"]), ("prompt", 7, "a warm, calm young woman, british accent"))
        # OmniVoice designs from a fixed vocabulary: the description is read for it and the rest is reported, not dropped.
        self.assertEqual(self.designs[0][0], "female, young adult, british accent")
        self.assertEqual(body["design_instruct"], "female, young adult, british accent")
        self.assertIn("design_terms_ignored: warm, calm", body["warnings"])
        self.assertEqual(self.designs[0][2], 7)
        self.assertIn("welcome", self.designs[0][1])
        meta = self.store.get_voice("key-a", body["id"])
        self.assertEqual(meta["transcript"], self.designs[0][1])  # kept so OmniVoice can clone it later
        self.assertTrue(self.store.reference_path("key-a", body["id"]).is_file())

    def test_an_engine_without_a_vocabulary_receives_the_exact_description(self) -> None:
        self.design_settings = None
        resp = self.create(seed=7, prompt="  A warm, calm narrator who sounds like a lighthouse keeper.  ")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self.designs[0][0], "  A warm, calm narrator who sounds like a lighthouse keeper.  ")
        self.assertEqual(resp.json()["design_instruct"], self.designs[0][0])
        self.assertFalse([w for w in resp.json()["warnings"] if w.startswith("design_terms_ignored")])
        # Nothing is refused for lacking a vocabulary word.
        self.assertEqual(self.create(prompt="warm and calm").status_code, 200)

    def test_script_hint_replaces_the_default_script(self) -> None:
        hint = "This is the exact sentence that this designed voice should say during creation. " * 2
        self.create(script_hint=hint)
        self.assertEqual(self.designs[0][1], hint.strip())

    def test_seed_is_random_when_absent_and_reproducible_when_given(self) -> None:
        self.create()
        self.create(seed=5)
        self.assertNotEqual(self.designs[0][2], 5)
        self.assertEqual(self.designs[1][2], 5)

    def test_validation(self) -> None:
        for label, body, code in (
            ("empty prompt", {"prompt": " "}, "invalid_prompt"),
            ("no designable attribute", {"prompt": "warm and calm"}, "invalid_prompt"),
            ("long prompt", {"prompt": "x" * 2001}, "invalid_prompt"),
            ("bad model", {"model": "2026-10-01"}, "unsupported_model"),
            ("bad seed", {"seed": "x"}, "invalid_seed"),
            ("empty hint", {"script_hint": " "}, "invalid_script_hint"),
        ):
            with self.subTest(label):
                resp = self.create(**body)
                self.assertEqual((resp.status_code, resp.json()["error"]["code"]), (400, code))

    def test_a_too_short_script_is_reported_against_script_hint(self) -> None:
        ApiCase.design_wav = speechlike(2)
        self.addCleanup(setattr, ApiCase, "design_wav", b"")
        resp = self.create(script_hint="Hi.")
        self.assertEqual((resp.status_code, resp.json()["error"]["code"], resp.json()["error"]["param"]), (400, "script_too_short", "script_hint"))

    def test_design_engine_failure_is_a_503(self) -> None:
        async def down(*_a):
            raise cv.VoiceError(503, "backend_unavailable", "Voice design needs the omnivoice engine.")

        # Rebuild the app with a failing design function.
        deps = VoiceApiDeps(
            store=lambda: self.store, sample_limits=lambda: SampleLimits(),
            authenticate=lambda r, p: SimpleNamespace(owner="key-a"), enforce_policy=lambda a, p: None,
            owner_of=lambda a: a.owner, probe_quality=lambda p: None, design_voice=down, design_settings=lambda: None,
            compatible_engines=lambda: [], quality_warn_below=lambda: 0.45,
        )
        app = FastAPI()
        api.register_voice_routes(app, deps)
        resp = TestClient(app).post("/v1/audio/voices", json={"type": "prompt", "name": "x", "prompt": "calm old man"})
        self.assertEqual((resp.status_code, resp.json()["error"]["code"]), (503, "backend_unavailable"))


class VoiceIdParsingTests(unittest.TestCase):
    def test_object_form_and_exact_id_strings_are_custom(self) -> None:
        vid = "voice_" + "ab" * 12
        self.assertEqual(api.custom_voice_id({"id": vid}), vid)
        self.assertEqual(api.custom_voice_id(vid), vid)
        self.assertEqual(api.custom_voice_id(f" {vid} "), vid)

    def test_everything_else_keeps_its_built_in_meaning(self) -> None:
        for value in ("alloy", "Shimmer", "voice_short", "voice_" + "zz" * 12, "", None, 5, ["id"], "de_female"):
            with self.subTest(value=value):
                self.assertIsNone(api.custom_voice_id(value))

    def test_an_object_with_a_missing_or_bad_id_is_still_custom(self) -> None:
        # Returned as "" so the caller reports voice_not_found instead of silently using a default voice.
        self.assertEqual(api.custom_voice_id({}), "")
        self.assertEqual(api.custom_voice_id({"id": 5}), "")


if __name__ == "__main__":
    unittest.main()
