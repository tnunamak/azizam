"""YuE2 song and Stable Audio 3 small-sfx on POST /v1/audio/generate.

Drives the route against a fake ComfyUI with the real ``proxy_config.yml`` and
workflow JSON, and tests the peak-normalize post-process on real ffmpeg audio.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def _peak_db(data: bytes, suffix: str) -> float:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"a{suffix}"
        path.write_bytes(data)
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True,
        )
    return float(re.search(r"max_volume:\s*(-?[0-9.]+) dB", out.stderr).group(1))


def _sine(volume_db: float, suffix: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"s{suffix}"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=1", "-af", f"volume={volume_db}dB", str(path)],
            check=True,
        )
        return path.read_bytes()


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg required")
class PeakNormalizeTests(unittest.TestCase):
    def test_quiet_flac_is_raised_to_target_peak(self) -> None:
        quiet = _sine(-25.0, ".flac")
        self.assertLess(_peak_db(quiet, ".flac"), -20.0)
        out = proxy._peak_normalize_audio(quiet, "audio/flac", -1.0)
        self.assertAlmostEqual(_peak_db(out, ".flac"), -1.0, delta=0.6)
        self.assertEqual(out[:4], b"fLaC")

    def test_wav_content_type_with_parameters(self) -> None:
        quiet = _sine(-30.0, ".wav")
        out = proxy._peak_normalize_audio(quiet, "audio/wav; charset=binary", -1.0)
        self.assertAlmostEqual(_peak_db(out, ".wav"), -1.0, delta=0.6)

    def test_already_at_target_returns_input_unchanged(self) -> None:
        loud = proxy._peak_normalize_audio(_sine(-20.0, ".flac"), "audio/flac", -1.0)
        self.assertEqual(proxy._peak_normalize_audio(loud, "audio/flac", -1.0), loud)

    def test_unknown_type_and_garbage_pass_through(self) -> None:
        self.assertEqual(proxy._peak_normalize_audio(b"abc", "application/octet-stream", -1.0), b"abc")
        self.assertEqual(proxy._peak_normalize_audio(b"not audio", "audio/flac", -1.0), b"not audio")


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, content: bytes = b"", headers=None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.headers = headers or {}
        self.text = ""

    def json(self):
        return self._payload


class _JsonRequest:
    headers = {"content-type": "application/json"}

    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


class AudioGenerateGraphTests(unittest.IsolatedAsyncioTestCase):
    async def _generate(self, body: dict, *, content_type: str, raw: bytes = b"RAW"):
        submitted: list[dict] = []

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, url, **kwargs):
                submitted.append(kwargs["json"]["prompt"])
                return _FakeResponse(200, {"prompt_id": "p1"})

            async def get(self, url, **kwargs):
                if "/history/" in url:
                    return _FakeResponse(200, {"p1": {
                        "status": {"status_str": "success"},
                        "outputs": {"8": {"audio": [{"filename": "o.bin", "type": "output"}]}},
                    }})
                return _FakeResponse(200, content=raw, headers={"content-type": content_type})

        with patch.object(proxy.httpx, "AsyncClient", FakeClient), patch.object(
            proxy, "_service_availability_response", return_value=None
        ), patch.object(proxy.asyncio, "sleep", return_value=None):
            response = await proxy.proxy_audio_generate(
                _JsonRequest(body), access_context()  # type: ignore[arg-type]
            )
        return response, (submitted[0] if submitted else None)

    def test_aliases_resolve(self) -> None:
        self.assertEqual(proxy._resolve_audio_workflow("sfx")[0], "sa3-small-sfx")
        self.assertEqual(proxy._resolve_audio_workflow("ambient")[0], "sa3-small-sfx")
        self.assertEqual(proxy._resolve_audio_workflow("tangoflux")[0], "tangoflux")
        self.assertEqual(proxy._resolve_audio_workflow("yue2")[0], "yue2-song")
        self.assertEqual(proxy._resolve_audio_workflow("song")[0], "yue2-song")
        self.assertEqual(proxy._resolve_audio_workflow("music")[0], "ace-step-1.5-xl-turbo")

    async def test_yue2_maps_tags_lyrics_duration_seed_to_real_inputs(self) -> None:
        response, graph = await self._generate(
            {"model": "yue2-song", "tags": "synth pop, female vocals", "lyrics": "[Verse]\nla la",
             "duration": 60, "seed": 9},
            content_type="audio/mpeg",
        )
        self.assertEqual(response.status_code, 200, response.body)
        self.assertEqual(graph["2"]["inputs"]["value"], "synth pop, female vocals")
        self.assertEqual(graph["3"]["inputs"]["value"], "[Verse]\nla la")
        self.assertEqual(graph["5"]["inputs"]["max_duration"], 60.0)
        self.assertEqual(graph["30"]["inputs"]["value"], 9)
        for node in ("2", "3", "5", "30"):
            self.assertNotIn("text", graph[node]["inputs"])
            self.assertNotIn("duration", graph[node]["inputs"])
        self.assertEqual(response.body, b"RAW")  # mp3: no normalization configured

    async def test_yue2_without_lyrics_is_instrumental_not_sample_lyrics(self) -> None:
        _response, graph = await self._generate(
            {"model": "yue2-song", "tags": "ambient piano"}, content_type="audio/mpeg"
        )
        self.assertEqual(graph["3"]["inputs"]["value"], "[instrumental]")

    def test_sa3_workflow_uses_untiled_decode(self) -> None:
        # VAEDecodeAudioTiled raised a tensor size mismatch for a 4 s request.
        graph = json.loads((ROOT / "workflows/audio/sa3-small-sfx.json").read_text())
        classes = {n["class_type"] for k, n in graph.items() if k != "_meta"}
        self.assertIn("VAEDecodeAudio", classes)
        self.assertNotIn("VAEDecodeAudioTiled", classes)

    async def test_yue2_default_duration_is_120_not_30(self) -> None:
        _response, graph = await self._generate(
            {"model": "yue2-song", "tags": "folk", "lyrics": "x"}, content_type="audio/mpeg"
        )
        self.assertEqual(graph["5"]["inputs"]["max_duration"], 120.0)

    async def test_yue2_rejects_duration_above_cap(self) -> None:
        response, graph = await self._generate(
            {"model": "yue2-song", "tags": "folk", "duration": 600}, content_type="audio/mpeg"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(graph)

    async def test_default_sfx_alias_runs_sa3_and_sets_seconds(self) -> None:
        response, graph = await self._generate(
            {"model": "sfx", "prompt": "glass breaking", "duration": 3, "seed": 5},
            content_type="application/octet-stream",
        )
        self.assertEqual(response.status_code, 200, response.body)
        self.assertEqual(response.headers["X-Audio-Model"], "sa3-small-sfx")
        self.assertEqual(graph["3"]["inputs"]["text"], "glass breaking")
        self.assertEqual(graph["5"]["inputs"]["seconds"], 3.0)
        self.assertEqual(graph["6"]["inputs"]["seed"], 5)

    async def test_sa3_default_duration_is_10(self) -> None:
        _response, graph = await self._generate(
            {"model": "sa3-small-sfx", "prompt": "rain"}, content_type="application/octet-stream"
        )
        self.assertEqual(graph["5"]["inputs"]["seconds"], 10.0)

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg required")
    async def test_sa3_output_is_peak_normalized(self) -> None:
        quiet = _sine(-28.0, ".flac")
        response, _graph = await self._generate(
            {"model": "sa3-small-sfx", "prompt": "rain"}, content_type="audio/flac", raw=quiet
        )
        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(_peak_db(response.body, ".flac"), -1.0, delta=0.6)

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg required")
    async def test_tangoflux_output_is_not_modified(self) -> None:
        quiet = _sine(-28.0, ".wav")
        response, graph = await self._generate(
            {"model": "tangoflux", "prompt": "rain", "duration": 4}, content_type="audio/wav", raw=quiet
        )
        self.assertEqual(response.body, quiet)
        self.assertEqual(graph["2"]["inputs"]["duration"], 4.0)


if __name__ == "__main__":
    unittest.main()
