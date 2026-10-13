"""Differential observations for the engine-knowledge Phase 2 runtime migrations.

The harness drives the real gateway routes with the owner's sanitized service
configuration and records what leaves the gateway (exact outbound request
bytes) and what the caller receives. ``engine_p2_golden.json`` holds the
observations of the code before the migrations, with the configuration of that
time (``owner_config.json``). The migrated code, run with the migrated
configuration (``owner_config.migrated.json``), must reproduce them except for
the intended changes that ``tests/test_engine_p2_differential.py`` lists.

Regenerate a golden only from the pre-migration commit::

    python tests/engine_p2_differential.py tests/fixtures/engine_p2/owner_config.json > golden.json
"""
from __future__ import annotations

import asyncio
import copy
import functools
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import AsyncMock, patch

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "engine_p2"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class _Upstream:
    """Fake engines behind a real ``httpx.AsyncClient``; records every request."""

    def __init__(self, httpx_module: Any, speech_wav: bytes) -> None:
        self.httpx = httpx_module
        self.real_client = httpx_module.AsyncClient
        self.speech_wav = speech_wav
        self.requests: list[dict[str, Any]] = []
        self.speech_status = 200
        self.stt_payloads: list[bytes] = []

    def client(self, *_args: Any, **_kwargs: Any) -> Any:
        return self.real_client(transport=self.httpx.MockTransport(self._handle))

    async def _handle(self, request: Any) -> Any:
        body = await request.aread()
        content_type = request.headers.get("content-type", "")
        boundary = re.search(r"boundary=([0-9a-f]+)", content_type)
        if boundary:
            body = body.replace(boundary.group(1).encode(), b"BOUNDARY")
            content_type = content_type.replace(boundary.group(1), "BOUNDARY")
        self.requests.append({
            "method": request.method,
            "url": str(request.url),
            "content_type": content_type,
            "body": body.decode("utf-8", "replace") if len(body) <= 1024 else f"sha256:{hashlib.sha256(body).hexdigest()} ({len(body)} bytes)",
        })
        path = request.url.path
        if path.endswith("/audio/speech"):
            if self.speech_status != 200:
                return self.httpx.Response(self.speech_status, json={"error": "engine says no"})
            return self.httpx.Response(200, content=self.speech_wav, headers={"content-type": "audio/wav"})
        if path.endswith("/audio/transcriptions"):
            payload = self.stt_payloads.pop(0) if self.stt_payloads else b'{"text":"direct"}'
            return self.httpx.Response(200, content=payload, headers={"content-type": "application/json"})
        return self.httpx.Response(404, json={"error": "unexpected path"})

    def take(self) -> list[dict[str, Any]]:
        taken, self.requests = self.requests, []
        return taken


def _overlaid_config(base: dict[str, Any], fixture: dict[str, Any], voices_dir: str) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    aliases = dict(merged.get("model_aliases") or {})
    aliases.update(copy.deepcopy(fixture["model_aliases"]))
    merged["model_aliases"] = aliases
    merged["voices"] = {**copy.deepcopy(fixture["voices"]), "dir": voices_dir}
    backends = dict(merged.get("backends") or {})
    backends.update(copy.deepcopy(fixture["backends"]))
    merged["backends"] = backends
    return merged


def _response(resp: Any) -> dict[str, Any]:
    content_type = resp.headers.get("content-type", "")
    if "json" in content_type:
        return {"status": resp.status_code, "json": resp.json()}
    return {"status": resp.status_code, "content_type": content_type, "bytes": len(resp.content)}


# ---- P2-1: voice design ------------------------------------------------------

_DESIGN_WORDS = (
    "female", "woman", "women", "girl", "lady", "feminine", "male", "man", "men", "boy", "guy", "masculine",
    "child", "kid", "childlike", "teenager", "teen", "teenage", "adolescent", "young adult", "young", "twenties",
    "thirties", "youthful", "middle-aged", "middle aged", "middle age", "forties", "fifties", "mature", "elderly",
    "old", "older", "senior", "aged", "very low", "very deep", "bass", "booming", "very high", "squeaky", "shrill",
    "low", "deep", "baritone", "husky", "high", "bright", "light", "soprano", "moderate", "medium", "mid-range",
    "midrange", "mid pitch", "whisper", "whispering", "whispered", "hushed", "american", "british", "uk", "u.k.",
    "london", "australian", "aussie", "canadian", "indian", "chinese", "japanese", "korean", "portuguese", "russian",
)
_DESIGN_PROMPTS = (
    *_DESIGN_WORDS,
    *(word.upper() for word in _DESIGN_WORDS),
    *(f"A calm {word} voice, please." for word in _DESIGN_WORDS),
    # overlaps: the first attribute of a category wins, the more specific pitch first
    "a man and a woman", "young but old", "very low and deep", "deep, very low", "very high bright squeaky",
    "middle aged elderly", "middle-aged old man", "a british american", "uk accent", "U.K.", "the u.k. london",
    "low high", "moderate medium mid-range", "whispering whisper hushed", "child teenager adult",
    # words inside other words never match
    "male, a manly tone", "male, businesslike", "female, light-hearted", "an oldish man", "highlands lady",
    "boyish", "ladylike", "deep-voiced woman", "russian-born man",
    # ignored terms, noise words, case and punctuation
    "A warm, calm young woman with a bright voice and an Australian accent",
    "An old man, deep voice, whispering",
    "a little girl", "a child", "female, middle-aged, low pitch, british accent",
    "Female!!! (British)", "female;old;deep", "FEMALE—british", "woman's voice, it's warm", "  male  ",
    "the a an and with voice pitch accent sounding sounds like who is of in very tone speaker narrator person male",
    "Fémale, café-owner, British", "male 123 4th", "male\nbritish\twhisper", "o'brien man", "x-ray man",
    "Sounds like a cheerful, energetic, upbeat narrator; female; very high",
    # nothing designable
    "warm and calm", "warm, calm, friendly narrator", "!!!", "123", "café",
)


async def _observe_voice_design(proxy: Any, client: Any, upstream: _Upstream) -> dict[str, Any]:
    from gateway import voices_api

    out: dict[str, Any] = {}

    def create(case: str, body: dict[str, Any]) -> None:
        resp = client.post("/v1/audio/voices", json={"type": "prompt", "name": "Designed", **body})
        result = _response(resp)
        if resp.status_code == 200:
            created = result["json"]
            client.delete(f"/v1/audio/voices/{created['id']}")
            result["json"] = {k: created.get(k) for k in ("type", "name", "seed", "prompt", "design_instruct", "warnings")}
        out[case] = {"response": result, "upstream": upstream.take()}

    for index, prompt in enumerate(_DESIGN_PROMPTS):
        create(f"prompt[{index}] {prompt!r}", {"prompt": prompt, "seed": 7})
    create("explicit script", {"prompt": "female, british", "seed": 3, "script_hint": "  Read this exact sentence aloud, slowly and clearly, for the designed voice.  "})
    create("string seed", {"prompt": "male", "seed": "12"})
    create("zero seed", {"prompt": "male", "seed": 0})
    with patch.object(voices_api.os, "urandom", return_value=b"\x01\x02\x03"):
        create("default seed", {"prompt": "male, deep"})
    create("explicit auto model", {"prompt": "male", "seed": 1, "model": "auto"})
    for case, body in (
        ("bad seed", {"prompt": "male", "seed": "x"}),
        ("bad model", {"prompt": "male", "seed": 1, "model": "omnivoice"}),
        ("empty prompt", {"prompt": "  ", "seed": 1}),
        ("long prompt", {"prompt": "male " * 401, "seed": 1}),
        ("empty script", {"prompt": "male", "seed": 1, "script_hint": " "}),
    ):
        create(case, body)
    for status in (400, 500):
        upstream.speech_status = status
        create(f"engine answers {status}", {"prompt": "female", "seed": 5})
    upstream.speech_status = 200
    engine = proxy.config["model_aliases"]["speech"]["engines"].get(proxy.config["voices"].get("design_engine"))
    if engine is not None:
        with patch.dict(proxy.config["backends"][engine["service"]], {"enabled": False}):
            create("design engine disabled", {"prompt": "female", "seed": 5})
            create("design engine disabled, nothing designable", {"prompt": "warm", "seed": 5})
    return out


# ---- P2-4: speech voices -----------------------------------------------------

_SPEECH_MODELS = (
    "tts-1", "tts-1-hd", "gpt-4o-mini-tts", "tts", "tts-fast", "tts-quality",
    "omnivoice", "supertonic", "kokoro", "chatterbox", "k2-fsa/OmniVoice", "no-such-model",
)
_OPENAI_VOICES = ("alloy", "ash", "ballad", "cedar", "coral", "echo", "fable", "marin", "nova", "onyx", "sage", "shimmer", "verse")
_LEGACY_VOICES = ("neutral_female", "casual_female", "cheerful_female", "neutral_male", "casual_male",
                  "sophia", "isabella", "lily", "juniper", "evan", "adam")
_PRESET_VOICES = tuple(f"{lang}_{gender}" for lang in ("ar", "de", "es", "fr", "hi", "it", "nl", "pt") for gender in ("female", "male"))
_NATIVE_VOICES = ("F1", "f3", "M5", "m2", "af_heart", "AF_Bella", "bm_george", "am_onyx")
_UNKNOWN_VOICES = ("nope", "Jenny", "voice", "alloy ", " alloy", "alloy-2", "F9", "af_nobody")
_EMPTY_VOICES = ("<omitted>", "", None)


_SPEECH_VOICES = (
    *_EMPTY_VOICES, *_OPENAI_VOICES, *(v.upper() for v in _OPENAI_VOICES), "Marin", "sHiMmEr",
    *_LEGACY_VOICES, "Cheerful_Female", *_PRESET_VOICES, "DE_Female", *_NATIVE_VOICES, *_UNKNOWN_VOICES,
    "voice_0123456789abcdef01234567", {"id": "voice_0123456789abcdef01234567"},
)


def _speak(client: Any, upstream: _Upstream, model: str, voice: Any) -> dict[str, Any]:
    """One speech request and what it sent upstream; the voice "<omitted>" sends none."""
    body: dict[str, Any] = {"model": model, "input": "hello there", "response_format": "wav"}
    if voice != "<omitted>":
        body["voice"] = voice
    resp = client.post("/v1/audio/speech", json=body)
    return {"response": _response(resp), "upstream": upstream.take()}


async def _observe_speech(proxy: Any, client: Any, upstream: _Upstream, models: tuple[str, ...] = _SPEECH_MODELS) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for model in models:
        for voice in _SPEECH_VOICES:
            out[f"{model} voice={json.dumps(voice)}"] = _speak(client, upstream, model, voice)
    if models == _SPEECH_MODELS:
        listing = client.get("/v1/audio/voices")
        out["GET /v1/audio/voices"] = {"response": _response(listing), "upstream": upstream.take()}
    return out


# ---- P2-6: STT chunk merge ---------------------------------------------------

def _stt_chunk_payloads(kind: str, count: int) -> list[bytes]:
    payloads = []
    for index in range(count):
        item: dict[str, Any] = {"text": f" chunk {index} words. "}
        if kind in ("timing", "timing+usage"):
            item["timing"] = {"wall_ms": 10.5 + index, "rtf": 0.01}
        if kind in ("usage", "timing+usage"):
            item["usage"] = {"type": "duration", "seconds": 60 + index}
        if kind == "segments":
            item["segments"] = []
        if kind == "timing-only-some" and index == 1:
            item["timing"] = {"wall_ms": 1}
        payloads.append(json.dumps(item).encode())
    return payloads


async def _observe_stt(proxy: Any, upstream: _Upstream, scratch: Path, only: frozenset[str] | None = None) -> dict[str, Any]:
    from starlette.requests import Request

    from gateway import stt_chunking
    from tests.support.access import access_context
    from tests.voice_test_audio import speechlike

    audio = speechlike(1)

    out: dict[str, Any] = {}
    snapshot = proxy._stt_execution_snapshot
    services = [sid for sid, svc in proxy.config["backends"].items()
                if sid.startswith("stt") and isinstance(svc, dict)]
    cases = [(sid, kind, chunks) for sid in sorted(services)
             for kind in ("plain", "timing", "usage", "timing+usage", "timing-only-some", "segments")
             for chunks in (2, 3)]
    cases += [(sid, "timing", 1) for sid in sorted(services)]  # short audio: direct passthrough
    for service_id, kind, chunks in cases:
        if only is not None and f"{service_id} {kind} x{chunks}" not in only:
            continue
        execution = snapshot("transcriptions", service_id)
        if execution is None:
            out[f"{service_id} {kind} x{chunks}"] = "no execution config"
            continue
        spool = scratch / f"spool-{service_id}"
        spool.mkdir(exist_ok=True)
        from dataclasses import replace
        execution = replace(execution, spool_dir=spool)
        segment = execution.target_seconds
        duration = execution.threshold_seconds + 1 if chunks > 1 else execution.threshold_seconds - 1
        silences = [(segment * (i + 1) - 0.5, segment * (i + 1) + 0.5) for i in range(chunks)]
        upstream.stt_payloads = _stt_chunk_payloads(kind, chunks)
        boundary = "harnessboundary"
        fields = [("model", "whisper-1")]
        parts = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n'.encode() for n, v in fields
        ) + (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="talk.wav"\r\n'
             f"Content-Type: audio/wav\r\n\r\n").encode() + audio + f"\r\n--{boundary}--\r\n".encode()
        sent = False

        async def receive() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": parts, "more_body": False}
            await asyncio.Future()
            return {}

        async def extract(_src: Path, dest: Path, **_kw: Any) -> None:
            dest.write_bytes(b"RIFF-" + dest.name.encode())

        def plan(**_kw: Any) -> list[tuple[float, float]]:
            edges = [0.0, *[segment * (i + 1) for i in range(chunks - 1)], duration]
            return list(zip(edges, edges[1:]))

        request = Request({"type": "http", "method": "POST", "path": "/v1/audio/transcriptions",
                           "headers": [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())]}, receive)
        with ExitStack() as stack:
            stack.enter_context(patch.object(proxy, "_stt_execution_snapshot", return_value=execution))
            stack.enter_context(patch.object(stt_chunking, "probe_audio", AsyncMock(return_value=stt_chunking.ProbedAudio(duration, 16000, 1))))
            stack.enter_context(patch.object(stt_chunking, "detect_silences", AsyncMock(return_value=silences)))
            stack.enter_context(patch.object(stt_chunking, "plan_silence_only_segments", plan))
            stack.enter_context(patch.object(stt_chunking, "extract_segment", extract))
            stack.enter_context(patch.object(proxy.receipt_store, "begin"))
            stack.enter_context(patch.object(proxy.receipt_store, "finalize", return_value=True))
            response = await proxy.proxy_stt_request(request, access_context())
        out[f"{service_id} {kind} x{chunks}"] = {
            "response": {"status": response.status_code, "body": bytes(response.body).decode("utf-8", "replace")},
            "upstream": upstream.take(),
        }
    return out


# ---- runner ------------------------------------------------------------------

def _memoized_process_sample(real):
    """``process_sample`` decodes through ffmpeg and walks every sample in Python, about 0.1 s of
    CPU per call; a design sweep gives it the same upstream clip hundreds of times.  It is a pure
    function of its arguments, so the real function runs once per distinct input and each caller
    gets its own copy of the result."""
    @functools.lru_cache(maxsize=None)
    def cached(data: bytes, limits: Any, denoise: bool) -> Any:
        return real(data, limits, denoise=denoise)

    def process(data: bytes, limits: Any, *, denoise: bool = False) -> Any:
        return copy.deepcopy(cached(data, limits, denoise))

    return process


@functools.lru_cache(maxsize=None)
def _fake_engine_wav() -> bytes:
    """The clip every fake engine returns.  Building it walks 260k samples in Python, so a worker builds it once."""
    from tests.voice_test_audio import speechlike

    return speechlike(11)


# The only parts of a provenance tag that change between two requests for the same clip: the request id
# (which appears twice, as the id and inside the receipt path) and the creation second.  Both have a fixed length.
_REQUEST_VARIABLES = re.compile(r"req_[0-9a-f]{32}|\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
_REWRITES: dict[Any, tuple[tuple[str, ...], bytes]] = {}


def _memoized_ffmpeg_rewrite(real):
    """``_ffmpeg_rewrite`` starts an ffmpeg per response (about 40 ms) to write the provenance tag; a sweep
    gets the same clip back hundreds of times and the tags differ only by request id and second.  The real
    function runs once per distinct input with those two values set aside; a later call receives that output
    with its own two values written in, which is the bytes the real function would return (equal lengths)."""
    def rewrite(content: bytes, suffix_or_mime: str, workdir: Any, extra_out: list[str]) -> bytes:
        joined = "\x00".join(extra_out)
        variables = tuple(dict.fromkeys(_REQUEST_VARIABLES.findall(joined)))
        key = (real, content, suffix_or_mime, _REQUEST_VARIABLES.sub("#", joined))
        if key not in _REWRITES:
            _REWRITES[key] = (variables, real(content, suffix_or_mime, workdir, extra_out))
        recorded, output = _REWRITES[key]
        for was, now in zip(recorded, variables, strict=True):
            output = output.replace(was.encode(), now.encode())
        return output

    return rewrite


@contextmanager
def running_gateway(fixture: dict[str, Any]) -> Iterator[tuple[Any, Any, _Upstream, Path]]:
    """The imported gateway with the fixture's configuration and fake engines: (proxy, client, upstream, scratch)."""
    import proxy
    from fastapi.testclient import TestClient

    from gateway import media_jobs, media_metadata, receipt_store, voices_api
    from tests.support.access import authenticated
    from tests.route_matrix import one_portal

    with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
        root = Path(temp)
        receipt_store.init_db(root / "receipts.db")
        media_jobs.init_db(root / "media_jobs.db")
        upstream = _Upstream(proxy.httpx, _fake_engine_wav())
        # Each request opens and closes the stores several times. Closing the last connection of a
        # WAL database checkpoints and deletes the WAL, which costs an fsync per close and made one
        # sweep take minutes on a busy host. An idle connection per store keeps the close cheap.
        for store in (root / "receipts.db", root / "media_jobs.db", proxy.ADMIN_STORE.path):
            keeper = sqlite3.connect(store)
            keeper.execute("PRAGMA journal_mode=WAL").fetchone()
            stack.callback(keeper.close)
        merged = _overlaid_config(proxy.config, fixture, str(root / "voices"))
        for patcher in (
            patch.dict(proxy.config, merged, clear=True),
            authenticated(),
            patch.object(proxy, "_enforce_runtime_route_policy", return_value=None),
            patch.object(proxy, "_current_loaded_model_policy_decision", return_value=None),
            patch.object(proxy.httpx, "AsyncClient", side_effect=upstream.client),
            patch.object(proxy, "_ensure_speech_service_ready", AsyncMock(return_value=None)),
            patch.object(receipt_store, "DB_PATH", root / "receipts.db"),
            patch.object(media_jobs, "DB_PATH", root / "media_jobs.db"),
            patch.object(voices_api, "process_sample", _memoized_process_sample(voices_api.process_sample)),
            patch.object(media_metadata, "_ffmpeg_rewrite", _memoized_ffmpeg_rewrite(media_metadata._ffmpeg_rewrite)),
        ):
            stack.enter_context(patcher)
        # One event loop for every request of the context instead of a thread and a loop per request.
        yield proxy, stack.enter_context(one_portal(TestClient(proxy.app))), upstream, root


def observe(fixture: dict[str, Any], sections: tuple[str, ...] = ("voice_design", "speech", "stt"),
            stt_cases: frozenset[str] | None = None) -> dict[str, Any]:
    """Run the requested observation sections against the imported gateway; `stt_cases` limits the stt sweep to those cases."""
    results: dict[str, Any] = {}
    with running_gateway(fixture) as (proxy, client, upstream, root):
        if "voice_design" in sections:
            results["voice_design"] = asyncio.run(_observe_voice_design(proxy, client, upstream))
        if "speech" in sections:
            results["speech"] = asyncio.run(_observe_speech(proxy, client, upstream))
        if "stt" in sections:
            results["stt"] = asyncio.run(_observe_stt(proxy, upstream, root, stt_cases))
    return results


def main(argv: list[str]) -> int:
    fixture = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    sections = tuple(argv[2].split(",")) if len(argv) > 2 else ("voice_design", "speech", "stt")
    if "HERMETIC_TEST_STATE_DIR" not in os.environ:
        state = Path(tempfile.mkdtemp(prefix="engine-p2-", dir=os.path.expanduser("~/.tmp")))
        for name, filename in (("AZIZAM_ADMIN_DB", "admin.db"), ("AZIZAM_METRICS_DB", "metrics.db"),
                               ("AZIZAM_RESPONSES_DB", "responses.db"), ("AZIZAM_RECEIPTS_DB", "receipts.db"),
                               ("AZIZAM_MEDIA_JOBS_DB", "media_jobs.db")):
            os.environ[name] = str(state / filename)
    sys.path.insert(0, os.getcwd())
    json.dump(observe(fixture, sections), sys.stdout, indent=1, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
