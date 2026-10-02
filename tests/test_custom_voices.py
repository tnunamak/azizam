"""Per-key storage of custom voices and consent recordings (gateway/custom_voices.py)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gateway import custom_voices as cv
from gateway.transforms import CANONICAL_TTS_VOICES, LEGACY_TTS_VOICE_ALIASES

WAV = b"RIFF----WAVEfmt " + b"\x00" * 64


def make_voice(store: cv.VoiceStore, owner, name="Mine", **overrides):
    args = dict(
        name=name, voice_type="audio_sample", reference_wav=WAV, duration_seconds=9.5, original=b"orig",
        original_extension="mp3", consent_id=None, description=None, labels={}, measurements={}, warnings=[],
    )
    args.update(overrides)
    return store.create_voice(owner, **args)


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = cv.VoiceStore(self.root)


class VoiceLifecycleTests(StoreTestCase):
    def test_create_get_list_update_delete(self) -> None:
        meta = make_voice(self.store, "key-a", description="d", labels={"accent": "uk"})
        self.assertRegex(meta["id"], r"^voice_[0-9a-f]{24}$")
        self.assertEqual((meta["object"], meta["type"], meta["name"]), ("audio.voice", "audio_sample", "Mine"))
        self.assertEqual(self.store.get_voice("key-a", meta["id"])["labels"], {"accent": "uk"})
        self.assertTrue(self.store.reference_path("key-a", meta["id"]).is_file())
        renamed = self.store.update_voice("key-a", meta["id"], {"name": "Renamed"})
        self.assertEqual(renamed["name"], "Renamed")
        self.assertEqual([m["id"] for m in self.store.list_voices("key-a")[0]], [meta["id"]])
        self.store.delete_voice("key-a", meta["id"])
        with self.assertRaises(cv.VoiceError) as ctx:
            self.store.get_voice("key-a", meta["id"])
        self.assertEqual((ctx.exception.status, ctx.exception.code), (404, "voice_not_found"))

    def test_delete_removes_the_clip_the_original_and_engine_caches(self) -> None:
        meta = make_voice(self.store, "key-a")
        directory = self.store.reference_path("key-a", meta["id"]).parent
        (directory / "reference.wav.turbo-conds.pt").write_bytes(b"cache")
        self.store.delete_voice("key-a", meta["id"])
        self.assertFalse(directory.exists())

    def test_voices_are_owned_by_one_key(self) -> None:
        mine = make_voice(self.store, "key-a")
        for call in (self.store.get_voice, self.store.delete_voice):
            with self.subTest(call=call.__name__):
                with self.assertRaises(cv.VoiceError) as ctx:
                    call("key-b", mine["id"])
                self.assertEqual(ctx.exception.status, 404)  # never 403: no existence leak
        self.assertEqual(self.store.list_voices("key-b"), ([], False))
        self.assertTrue(self.store.get_voice("key-a", mine["id"]))

    def test_no_raw_key_material_on_disk(self) -> None:
        make_voice(self.store, "secret-key-hash")
        self.assertNotIn("secret-key-hash", "".join(str(p) for p in self.root.rglob("*")))

    def test_auth_off_uses_the_local_owner(self) -> None:
        meta = make_voice(self.store, None)
        self.assertTrue((self.root / "local" / "voices" / meta["id"] / "meta.json").is_file())

    def test_list_is_newest_first_and_paginates_by_cursor(self) -> None:
        ids = []
        for n in range(5):
            meta = make_voice(self.store, "k", name=f"v{n}")
            self.store.update_voice("k", meta["id"], {"created_at": 1000 + n})
            ids.append(meta["id"])
        page, more = self.store.list_voices("k", limit=2)
        self.assertEqual(([m["id"] for m in page], more), (ids[:2:-1] + [], True))
        page2, more2 = self.store.list_voices("k", after=page[-1]["id"], limit=2)
        self.assertEqual(([m["id"] for m in page2], more2), ([ids[2], ids[1]], True))
        page3, more3 = self.store.list_voices("k", after=page2[-1]["id"], limit=2)
        self.assertEqual(([m["id"] for m in page3], more3), ([ids[0]], False))
        with self.assertRaises(cv.VoiceError) as ctx:
            self.store.list_voices("k", after="voice_" + "0" * 24)
        self.assertEqual(ctx.exception.status, 404)

    def test_a_half_created_voice_is_invisible_and_cleaned(self) -> None:
        owner_voices = self.root / cv.owner_dir_name("k") / "voices"
        (owner_voices / ".voice_deadbeef.new").mkdir(parents=True)
        meta = make_voice(self.store, "k")
        self.assertEqual([m["id"] for m in self.store.list_voices("k")[0]], [meta["id"]])

    def test_malformed_ids_are_not_found_not_path_traversal(self) -> None:
        for bad in ("../x", "voice_../..", "alloy", "", "voice_" + "0" * 23):
            with self.subTest(id=bad):
                with self.assertRaises(cv.VoiceError) as ctx:
                    self.store.get_voice("k", bad)
                self.assertEqual(ctx.exception.status, 404)


class ValidationTests(StoreTestCase):
    def test_built_in_names_and_legacy_aliases_cannot_be_used_as_a_name(self) -> None:
        for name in (*CANONICAL_TTS_VOICES, "Alloy", " SHIMMER ", *list(LEGACY_TTS_VOICE_ALIASES)[:3]):
            with self.subTest(name=name):
                with self.assertRaises(cv.VoiceError) as ctx:
                    cv.validate_name(name)
                self.assertEqual(ctx.exception.code, "voice_name_reserved")

    def test_ordinary_names_pass_and_bad_names_fail(self) -> None:
        self.assertEqual(cv.validate_name("  My narrator "), "My narrator")
        for bad in ("", "   ", None, 3, "x" * 257, "bad\nname"):
            with self.subTest(name=bad):
                with self.assertRaises(cv.VoiceError):
                    cv.validate_name(bad)

    def test_ids_cannot_collide_with_built_in_names(self) -> None:
        self.assertFalse(cv.is_voice_id("alloy"))
        self.assertTrue(cv.is_voice_id("voice_" + "a" * 24))
        for builtin in CANONICAL_TTS_VOICES:
            self.assertFalse(cv.is_voice_id(builtin))

    def test_labels_are_bounded(self) -> None:
        limits = cv.VoiceLimits()
        self.assertEqual(cv.validate_labels({"a": "b"}, limits), {"a": "b"})
        self.assertEqual(cv.validate_labels(None, limits), {})
        for bad in ({str(i): "x" for i in range(11)}, {"k": 1}, {"": "x"}, ["a"], {"k" * 41: "v"}):
            with self.subTest(labels=bad):
                with self.assertRaises(cv.VoiceError):
                    cv.validate_labels(bad, limits)


class QuotaTests(StoreTestCase):
    def test_voice_count_quota_is_per_key(self) -> None:
        store = cv.VoiceStore(self.root, cv.VoiceLimits(max_voices=2))
        make_voice(store, "a")
        make_voice(store, "a")
        with self.assertRaises(cv.VoiceError) as ctx:
            make_voice(store, "a")
        self.assertEqual(ctx.exception.code, "voice_quota_exceeded")
        make_voice(store, "b")  # another key is unaffected

    def test_byte_quota_counts_clips_and_originals(self) -> None:
        store = cv.VoiceStore(self.root, cv.VoiceLimits(max_bytes=500))
        make_voice(store, "a", original=b"x" * 100)
        with self.assertRaises(cv.VoiceError) as ctx:
            make_voice(store, "a", original=b"x" * 400)
        self.assertEqual(ctx.exception.code, "voice_quota_exceeded")

    def test_delete_frees_quota(self) -> None:
        store = cv.VoiceStore(self.root, cv.VoiceLimits(max_voices=1))
        first = make_voice(store, "a")
        store.delete_voice("a", first["id"])
        make_voice(store, "a")


class ConsentTests(StoreTestCase):
    def consent(self, owner="k", name="Jo", language="en-US"):
        return self.store.create_consent(owner, name=name, language=language, recording=b"rec", extension="wav")

    def test_consent_crud_and_scoping(self) -> None:
        meta = self.consent()
        self.assertRegex(meta["id"], r"^cons_[0-9a-f]{24}$")
        self.assertEqual((meta["object"], meta["language"]), ("audio.voice_consent", "en-US"))
        self.assertEqual(self.store.rename_consent("k", meta["id"], "New")["name"], "New")
        with self.assertRaises(cv.VoiceError) as ctx:
            self.store.get_consent("other", meta["id"])
        self.assertEqual(ctx.exception.status, 404)
        self.store.delete_consent("k", meta["id"])
        self.assertEqual(self.store.list_consents("k", after=None, limit=20), ([], False))

    def test_a_voice_records_its_consent_and_keeps_it_after_the_consent_is_deleted(self) -> None:
        consent = self.consent()
        voice = make_voice(self.store, "k", consent_id=consent["id"])
        self.assertEqual(voice["consent"], consent["id"])
        self.store.delete_consent("k", consent["id"])
        self.assertEqual(self.store.get_voice("k", voice["id"])["consent"], consent["id"])

    def test_consent_of_another_key_or_unknown_is_not_found(self) -> None:
        consent = self.consent("owner")
        with self.assertRaises(cv.VoiceError) as ctx:
            make_voice(self.store, "thief", consent_id=consent["id"])
        self.assertEqual((ctx.exception.status, ctx.exception.code), (404, "consent_not_found"))
        with self.assertRaises(cv.VoiceError) as ctx:
            make_voice(self.store, "k", consent_id="nonsense")
        self.assertEqual(ctx.exception.code, "invalid_consent")

    def test_required_mode_refuses_a_sample_voice_without_consent(self) -> None:
        store = cv.VoiceStore(self.root, cv.VoiceLimits(consent_mode="required"))
        with self.assertRaises(cv.VoiceError) as ctx:
            make_voice(store, "k")
        self.assertEqual(ctx.exception.code, "consent_required")
        consent = store.create_consent("k", name="Jo", language="en", recording=b"r", extension="wav")
        make_voice(store, "k", consent_id=consent["id"])
        make_voice(store, "k", voice_type="prompt", original=None)  # designed voices have no speaker to consent

    def test_consent_quota(self) -> None:
        store = cv.VoiceStore(self.root, cv.VoiceLimits(max_consents=1))
        store.create_consent("k", name="a", language="en", recording=b"r", extension="wav")
        with self.assertRaises(cv.VoiceError):
            store.create_consent("k", name="b", language="en", recording=b"r", extension="wav")


class LifecycleCleanupTests(StoreTestCase):
    def test_purge_owner_removes_every_voice_and_consent_of_that_key_only(self) -> None:
        make_voice(self.store, "gone")
        self.store.create_consent("gone", name="x", language="en", recording=b"r", extension="wav")
        keep = make_voice(self.store, "stay")
        self.assertTrue(self.store.purge_owner("gone"))
        self.assertEqual(self.store.list_voices("gone"), ([], False))
        self.assertFalse(self.store.purge_owner("gone"))
        self.assertTrue(self.store.get_voice("stay", keep["id"]))

    def test_purge_orphans_keeps_only_active_keys(self) -> None:
        make_voice(self.store, "active")
        make_voice(self.store, "stale")
        removed = self.store.purge_orphans(["active"])
        self.assertEqual(removed, [cv.owner_dir_name("stale")])
        self.assertEqual(len(self.store.list_voices("active")[0]), 1)


if __name__ == "__main__":
    unittest.main()
