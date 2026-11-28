"""OpenAI Python SDK Responses and media surface probes.

Local SDK surface checks run without network access. Live calls are opt-in and
require ``AZIZAM_BASE_URL`` plus the per-surface flag documented below.

Install with::

    pip install -r tests/compat/python/requirements-openai.txt

Run local checks with::

    python -m unittest tests.compat.python.probe_openai_responses_media -v

Optional live checks::

    AZIZAM_SMOKE_RESPONSES=1            # client.responses.create
    AZIZAM_SMOKE_IMAGES=1               # client.images.generate
    AZIZAM_SMOKE_AUDIO_SPEECH=1         # client.audio.speech.create
    AZIZAM_SMOKE_AUDIO_TRANSCRIPTION=1  # client.audio.transcriptions.create
    AZIZAM_SMOKE_AUDIO_TRANSLATION=1    # client.audio.translations.create
    AZIZAM_SMOKE_VIDEO=1                # client.videos.create

The transcription/translation cases use a tiny in-memory PCM16 WAV
(``tests.compat.harness.silent_wav_bytes``) so no binary fixture is
checked in. Translation is conditionally supported: if the configured
STT backend does not advertise the ``translation`` capability the
gateway returns a 400 ``unsupported_audio_translation`` error and the
SDK-level probe self-skips, deferring to the shape probe in
``probe_gateway_contract.py``.
"""

from __future__ import annotations

import io
import os
import unittest

from tests.compat.harness import ConfigError, load_config, silent_wav_bytes

try:  # pragma: no cover - optional dependency
    import openai as _openai  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised only without the SDK
    _openai = None


def _sdk_version() -> str:
    if _openai is None:
        return "not installed"
    return str(getattr(_openai, "__version__", "unknown"))


@unittest.skipUnless(_openai is not None, "openai SDK not installed")
class TestOpenAIResponsesMediaSurfaces(unittest.TestCase):
    """No-network checks for SDK resources the gateway may emulate."""

    def setUp(self) -> None:
        assert _openai is not None
        self.client = _openai.OpenAI(
            base_url="http://127.0.0.1:9/v1",
            api_key="compat-probe",
            timeout=0.01,
        )

    def test_responses_resource_exists(self) -> None:
        self.assertTrue(
            hasattr(self.client, "responses"),
            f"OpenAI SDK {_sdk_version()} does not expose client.responses",
        )
        self.assertTrue(hasattr(self.client.responses, "create"))
        self.assertTrue(hasattr(self.client.responses, "retrieve"))
        self.assertTrue(hasattr(self.client.responses, "cancel"))

    def test_image_resource_exists(self) -> None:
        self.assertTrue(hasattr(self.client, "images"))
        self.assertTrue(hasattr(self.client.images, "generate"))
        self.assertTrue(hasattr(self.client.images, "edit"))

    def test_audio_resources_exist(self) -> None:
        self.assertTrue(hasattr(self.client, "audio"))
        self.assertTrue(hasattr(self.client.audio, "speech"))
        self.assertTrue(hasattr(self.client.audio.speech, "create"))
        self.assertTrue(hasattr(self.client.audio, "transcriptions"))
        self.assertTrue(hasattr(self.client.audio.transcriptions, "create"))
        self.assertTrue(hasattr(self.client.audio, "translations"))
        self.assertTrue(hasattr(self.client.audio.translations, "create"))

    def test_video_resource_exists_when_sdk_supports_it(self) -> None:
        if not hasattr(self.client, "videos"):
            self.skipTest(f"OpenAI SDK {_sdk_version()} does not expose client.videos")
        self.assertTrue(hasattr(self.client.videos, "create"))
        self.assertTrue(hasattr(self.client.videos, "retrieve"))
        self.assertTrue(hasattr(self.client.videos, "list"))


@unittest.skipUnless(_openai is not None, "openai SDK not installed")
@unittest.skipUnless(
    os.environ.get("AZIZAM_BASE_URL"),
    "AZIZAM_BASE_URL not set; live SDK probe disabled",
)
class TestOpenAIResponsesMediaLive(unittest.TestCase):
    """Opt-in live probes for potentially billable or slow SDK calls."""

    def setUp(self) -> None:
        try:
            self.cfg = load_config()
        except ConfigError as exc:  # pragma: no cover - env-dependent
            self.skipTest(str(exc))
        assert _openai is not None
        self.client = _openai.OpenAI(
            base_url=self.cfg.openai_base_url(),
            api_key=self.cfg.api_key or "anonymous",
            timeout=self.cfg.request_timeout_s,
        )

    def _require_flag(self, name: str) -> None:
        if os.environ.get(name) != "1":
            self.skipTest(f"{name}=1 not set; live probe disabled")

    def _text_model(self) -> str:
        return os.environ.get("AZIZAM_SMOKE_RESPONSES_MODEL", "default")

    def test_responses_create(self) -> None:
        self._require_flag("AZIZAM_SMOKE_RESPONSES")
        result = self.client.responses.create(
            model=self._text_model(),
            input="Reply with exactly: compatibility probe",
            temperature=0,
        )
        self.assertTrue(getattr(result, "id", None))
        output_text = getattr(result, "output_text", None)
        if output_text is not None:
            self.assertIsInstance(output_text, str)

    def test_images_generate(self) -> None:
        self._require_flag("AZIZAM_SMOKE_IMAGES")
        result = self.client.images.generate(
            model=os.environ.get("AZIZAM_SMOKE_IMAGE_MODEL", "default"),
            prompt="A small blue square on a white background.",
            size=os.environ.get("AZIZAM_SMOKE_IMAGE_SIZE", "256x256"),
            n=1,
        )
        self.assertGreater(len(result.data), 0)
        first = result.data[0]
        self.assertTrue(getattr(first, "b64_json", None) or getattr(first, "url", None))

    def test_audio_speech_create(self) -> None:
        self._require_flag("AZIZAM_SMOKE_AUDIO_SPEECH")
        result = self.client.audio.speech.create(
            model=os.environ.get("AZIZAM_SMOKE_AUDIO_SPEECH_MODEL", "default"),
            voice=os.environ.get("AZIZAM_SMOKE_AUDIO_VOICE", "alloy"),
            input="compatibility probe",
            response_format="mp3",
        )
        content = result.read()
        self.assertIsInstance(content, bytes)
        self.assertGreater(len(content), 0)

    def _wav_upload(self) -> tuple[str, io.BytesIO, str]:
        # Re-build the WAV per call so the file pointer is at byte 0; the
        # SDK consumes the stream during multipart encoding.
        return ("probe.wav", io.BytesIO(silent_wav_bytes()), "audio/wav")

    def test_audio_transcriptions_create(self) -> None:
        self._require_flag("AZIZAM_SMOKE_AUDIO_TRANSCRIPTION")
        assert _openai is not None
        try:
            result = self.client.audio.transcriptions.create(
                model=os.environ.get(
                    "AZIZAM_SMOKE_AUDIO_TRANSCRIPTION_MODEL", "default"
                ),
                file=self._wav_upload(),
            )
        except _openai.APIStatusError as exc:
            self.fail(
                f"transcription returned {exc.status_code}: {exc.response.text[:300]}"
            )
        text = getattr(result, "text", None)
        self.assertIsInstance(
            text,
            str,
            f"OpenAI transcription response must expose .text as a string, got {result!r}",
        )

    def test_audio_translations_create(self) -> None:
        """Translation is optional — auto-skip when the backend lacks it.

        On gateways where translation is unsupported, the gateway returns
        a 400 ``unsupported_audio_translation`` error and this case
        skips. The corresponding shape probe in
        ``probe_gateway_contract.py`` covers the error envelope.
        """

        self._require_flag("AZIZAM_SMOKE_AUDIO_TRANSLATION")
        assert _openai is not None
        try:
            result = self.client.audio.translations.create(
                model=os.environ.get(
                    "AZIZAM_SMOKE_AUDIO_TRANSLATION_MODEL", "default"
                ),
                file=self._wav_upload(),
            )
        except _openai.BadRequestError as exc:
            body = getattr(exc, "body", None) or {}
            if not isinstance(body, dict):
                try:
                    body = exc.response.json()
                except Exception:
                    body = {}
            error = body.get("error", body) if isinstance(body, dict) else None
            code = (error or {}).get("code") if isinstance(error, dict) else None
            if code == "unsupported_audio_translation":
                self.skipTest(
                    "gateway backend does not advertise translation capability"
                )
            raise
        text = getattr(result, "text", None)
        self.assertIsInstance(
            text,
            str,
            f"OpenAI translation response must expose .text as a string, got {result!r}",
        )

    def test_video_create(self) -> None:
        self._require_flag("AZIZAM_SMOKE_VIDEO")
        if not hasattr(self.client, "videos"):
            self.skipTest(f"OpenAI SDK {_sdk_version()} does not expose client.videos")
        result = self.client.videos.create(
            model=os.environ.get("AZIZAM_SMOKE_VIDEO_MODEL", "default"),
            prompt="A one second shot of a small blue square.",
        )
        self.assertTrue(getattr(result, "id", None))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
