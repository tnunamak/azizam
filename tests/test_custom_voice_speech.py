"""Custom voices through the gateway: speech routing, listing, shadowing, and key revocation."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import proxy
from gateway import admin_store, custom_voices as cv
from gateway.admin_api import AdminContext
from gateway.admin_runtime import AdminSQLiteStore
from tests.voice_test_audio import HAVE_FFMPEG, speechlike

WAV = b"RIFF----WAVE"


class _FakeClient:
    """Records POSTs; answers /transcriptions with JSON and everything else with audio."""

    requests: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url, headers=None, content=None, json=None, files=None, data=None):  # noqa: A002
        body = globals()["json"].loads(content) if content is not None else json
        self.__class__.requests.append({"url": url, "body": body, "files": files, "data": data})
        if "/transcriptions" in url:
            return proxy.httpx.Response(200, json={"text": "this is the reference transcript"})
        return proxy.httpx.Response(200, content=WAV, headers={"content-type": "audio/wav"})


class CustomVoiceRouteCase(unittest.TestCase):
    owner: str | None = "key-a"

    def setUp(self) -> None:
        _FakeClient.requests = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = cv.VoiceStore(Path(self.tmp.name))
        cfg = copy.deepcopy(proxy.config)
        cfg["backends"]["tts-supertonic"] = {"name": "supertonic", "host": "127.0.0.1", "port": 8010, "capabilities": ["audio", "speech"]}
        self.ready = AsyncMock(return_value=None)
        self.client = TestClient(proxy.app)
        self.patches = [
            patch.object(proxy, "AUTH_ENABLED", False),
            patch.object(proxy, "_enforce_runtime_route_policy", return_value=None),
            patch.object(proxy, "config", cfg),
            patch.object(proxy.httpx, "AsyncClient", _FakeClient),
            patch.object(proxy, "_service_availability_response", return_value=None),
            patch.object(proxy, "_ensure_speech_service_ready", self.ready),
            patch.object(proxy, "_voice_store", lambda: self.store),
            patch.object(proxy, "_asset_owner_key", lambda _auth: self.owner),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def make_voice(self, owner="key-a", **overrides):
        args = dict(
            name="Mine", voice_type="audio_sample", reference_wav=WAV, duration_seconds=9.0, original=b"o",
            original_extension="wav", consent_id=None, description=None, labels={}, measurements={}, warnings=[],
        )
        args.update(overrides)
        return self.store.create_voice(owner, **args)

    def speak(self, voice, model="tts", **extra):
        return self.client.post("/v1/audio/speech", json={"model": model, "voice": voice, "input": "hello", "response_format": "wav", **extra})


class SpeakWithCustomVoiceTests(CustomVoiceRouteCase):
    def test_object_form_reaches_chatterbox_with_the_stored_clip(self) -> None:
        meta = self.make_voice()
        resp = self.speak({"id": meta["id"]})
        self.assertEqual(resp.status_code, 200, resp.text)
        sent = _FakeClient.requests[-1]
        self.assertEqual(sent["url"], "http://127.0.0.1:8014/v1/audio/speech")
        self.assertEqual(sent["body"]["model"], "chatterbox-turbo")
        self.assertEqual(sent["body"]["voice_ref"], str(self.store.reference_path("key-a", meta["id"])))
        self.assertEqual(sent["body"]["voice"], "alloy")  # placeholder; the engine clones voice_ref
        self.assertNotIn("ref_text", sent["body"])

    def test_a_bare_id_string_works_too(self) -> None:
        meta = self.make_voice()
        self.assertEqual(self.speak(meta["id"]).status_code, 200)
        self.assertEqual(_FakeClient.requests[-1]["body"]["voice_ref"], str(self.store.reference_path("key-a", meta["id"])))

    def test_every_openai_model_name_reaches_the_clone_engine(self) -> None:
        meta = self.make_voice()
        for model in ("tts-1", "tts-1-hd", "gpt-4o-mini-tts", "tts-quality", "chatterbox"):
            with self.subTest(model=model):
                self.assertEqual(self.speak({"id": meta["id"]}, model=model).status_code, 200)
                self.assertIn(":8014", _FakeClient.requests[-1]["url"])

    def test_unknown_malformed_and_foreign_ids_are_404_and_never_fall_back_to_a_default_voice(self) -> None:
        theirs = self.make_voice(owner="key-b")
        for label, voice in (
            ("unknown", {"id": "voice_" + "0" * 24}),
            ("unknown string", "voice_" + "1" * 24),
            ("malformed", {"id": "alloy"}),
            ("missing id", {}),
            ("foreign", {"id": theirs["id"]}),
        ):
            with self.subTest(label):
                before = len(_FakeClient.requests)
                resp = self.speak(voice)
                self.assertEqual(resp.status_code, 404, resp.text)
                self.assertEqual((resp.json()["error"]["code"], resp.json()["error"]["param"]), ("voice_not_found", "voice"))
                self.assertEqual(len(_FakeClient.requests), before)

    def test_an_engine_without_custom_voice_support_is_a_clear_400(self) -> None:
        meta = self.make_voice()
        for model in ("supertonic", "tts-fast", "kokoro"):
            with self.subTest(model=model):
                resp = self.speak({"id": meta["id"]}, model=model)
                self.assertEqual(resp.status_code, 400, resp.text)
                self.assertEqual(resp.json()["error"]["code"], "custom_voice_engine_unsupported")
                self.assertIn("chatterbox", resp.json()["error"]["message"])

    def test_omnivoice_gets_the_reference_transcript_once(self) -> None:
        designed = self.make_voice(voice_type="prompt", original=None, prompt="calm", transcript="the exact words")
        self.assertEqual(self.speak({"id": designed["id"]}, model="omnivoice").status_code, 200)
        sent = _FakeClient.requests[-1]
        self.assertEqual((sent["body"]["ref_text"], sent["body"]["model"]), ("the exact words", "k2-fsa/OmniVoice"))
        self.assertNotIn(":8014", sent["url"])
        self.assertEqual([r for r in _FakeClient.requests if "/transcriptions" in r["url"]], [])

        cloned = self.make_voice()
        self.assertEqual(self.speak({"id": cloned["id"]}, model="omnivoice").status_code, 200)
        self.assertEqual(_FakeClient.requests[-1]["body"]["ref_text"], "this is the reference transcript")
        self.assertEqual(self.store.get_voice("key-a", cloned["id"])["transcript"], "this is the reference transcript")
        transcriptions = len([r for r in _FakeClient.requests if "/transcriptions" in r["url"]])
        self.speak({"id": cloned["id"]}, model="omnivoice")
        self.assertEqual(len([r for r in _FakeClient.requests if "/transcriptions" in r["url"]]), transcriptions)  # cached

    def test_clients_cannot_inject_voice_ref_with_a_built_in_voice(self) -> None:
        resp = self.speak("shimmer", voice_ref="/etc/passwd", ref_text="x")
        self.assertEqual(resp.status_code, 200)
        body = _FakeClient.requests[-1]["body"]
        self.assertNotIn("voice_ref", body)
        self.assertNotIn("ref_text", body)
        self.assertEqual(body["voice"], "shimmer")

    def test_clients_cannot_inject_voice_ref_beside_a_custom_voice(self) -> None:
        meta = self.make_voice()
        self.speak({"id": meta["id"]}, voice_ref="/etc/passwd")
        self.assertEqual(_FakeClient.requests[-1]["body"]["voice_ref"], str(self.store.reference_path("key-a", meta["id"])))

    def test_built_in_voices_are_unchanged(self) -> None:
        self.make_voice(name="Narrator")
        for given, expected in (("shimmer", "shimmer"), ("Marin", "marin"), ("cheerful_female", "shimmer"), ("nope", "alloy")):
            with self.subTest(voice=given):
                self.assertEqual(self.speak(given).status_code, 200)
                self.assertEqual(_FakeClient.requests[-1]["body"]["voice"], expected)


class ShadowingTests(CustomVoiceRouteCase):
    def test_a_custom_voice_cannot_take_a_built_in_name(self) -> None:
        for name in ("shimmer", "Alloy", "cheerful_female"):
            with self.subTest(name=name):
                with self.assertRaises(cv.VoiceError) as ctx:
                    cv.validate_name(name)
                self.assertEqual(ctx.exception.code, "voice_name_reserved")

    def test_a_custom_voice_with_a_look_alike_name_does_not_change_what_the_built_in_name_means(self) -> None:
        self.make_voice(name="shimmer 2")
        self.speak("shimmer")
        self.assertEqual(_FakeClient.requests[-1]["body"]["voice"], "shimmer")
        self.assertNotIn("voice_ref", _FakeClient.requests[-1]["body"])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
class VoiceRoutesThroughTheGatewayTests(CustomVoiceRouteCase):
    def setUp(self) -> None:
        super().setUp()
        quality = AsyncMock(return_value={"speaker_similarity": 0.66, "encoder": "ecapa", "engine": "chatterbox"})
        patcher = patch.object(proxy, "_probe_voice_quality", quality)
        patcher.start()
        self.addCleanup(patcher.stop)

    def create(self, name="Narrator"):
        return self.client.post("/v1/audio/voices", data={"name": name}, files={"audio_sample": ("s.wav", speechlike(9), "audio/wav")})

    def test_create_use_list_delete_with_the_real_routes(self) -> None:
        created = self.create()
        self.assertEqual(created.status_code, 200, created.text)
        voice = created.json()
        self.assertEqual(voice["compatible_engines"], ["chatterbox", "omnivoice"])
        self.assertEqual(self.speak({"id": voice["id"]}).status_code, 200)
        listing = self.client.get("/v1/audio/voices").json()
        self.assertEqual([v["id"] for v in listing["data"]], [voice["id"]])
        self.assertEqual((listing["object"], listing["has_more"]), ("list", False))
        self.assertIn("alloy", {v["voice_id"] for v in listing["voices"]})  # the built-in listing is intact
        self.assertEqual(self.client.delete(f"/v1/audio/voices/{voice['id']}").json()["deleted"], True)
        self.assertEqual(self.speak({"id": voice["id"]}).status_code, 404)

    def test_the_list_only_shows_the_callers_voices(self) -> None:
        self.create()
        self.owner = "key-b"
        self.assertEqual(self.client.get("/v1/audio/voices").json()["data"], [])

    def test_a_built_in_name_is_refused_over_http(self) -> None:
        resp = self.create(name="Shimmer")
        self.assertEqual((resp.status_code, resp.json()["error"]["code"]), (400, "voice_name_reserved"))

    def test_list_pagination_arguments_are_validated(self) -> None:
        self.assertEqual(self.client.get("/v1/audio/voices", params={"limit": 0}).status_code, 400)
        self.assertEqual(self.client.get("/v1/audio/voices", params={"after": "voice_" + "0" * 24}).status_code, 404)


class RevocationCleanupTests(CustomVoiceRouteCase):
    def test_revoking_a_credential_removes_its_voices_and_only_its_voices(self) -> None:
        gone = self.make_voice(owner="hash-gone")
        keep = self.make_voice(owner="hash-keep")
        proxy._purge_voices_for_credential({"hash": "hash-gone", "prefix": "opk_x"})
        with self.assertRaises(cv.VoiceError):
            self.store.get_voice("hash-gone", gone["id"])
        self.assertTrue(self.store.get_voice("hash-keep", keep["id"]))

    def test_the_admin_store_fires_the_hook_on_revoke(self) -> None:
        db = Path(self.tmp.name) / "admin.db"
        admin_store.init_db(db)
        store = AdminSQLiteStore(db)
        account = admin_store.ensure_service_account("svc", path=db)
        app = admin_store.create_app("svc-app", kind="service", owner_service_account_id=account["id"], path=db)
        credential = admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], hash="hash-1", prefix="opk", path=db)
        seen = []
        store.on_credential_revoked = lambda record: seen.append(record.get("hash"))
        store.revoke_credential(credential_id=credential["id"], actor=AdminContext(subject="t", roles=frozenset({"owner"})))
        self.assertEqual(seen, ["hash-1"])


if __name__ == "__main__":
    unittest.main()
