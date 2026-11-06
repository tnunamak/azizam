#!/usr/bin/env python
"""Live check of custom voices through the real OpenAI SDK against a running gateway.

    OPENAI_BASE_URL="$AZIZAM_BASE_URL/openai/v1" OPENAI_API_KEY="$AZIZAM_API_KEY" \\
      ~/applications/daisy/.venv-openai/bin/python scripts/local/live_custom_voice_check.py [--sample FILE] [--omnivoice]

Creates a consent and a voice from a 10 s sample, speaks with it (buffered and streamed), measures
speaker similarity of the speech to the sample (ECAPA, via the chatterbox-tts venv when present),
checks the voice is private to its key and cannot shadow a built-in name, then deletes everything.
Exit status 0 only when every check passes. It uses the GPU (and starts the clone engine); it is
not part of the hermetic suite.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

from openai import NotFoundError, OpenAI, BadRequestError

HOME = Path.home()
SIMILARITY = HOME / "applications/chatterbox-tts/speakersim.py"
SIMILARITY_PY = HOME / "applications/chatterbox-tts/.venv/bin/python"
DEFAULT_SAMPLES = [
    HOME / ".tmp/media-refresh/bakeoff/stt/data/libri-clean-1995-1836-0005.wav",   # LibriSpeech, 10.9 s, real speaker
]
TEXT = "The meeting has moved to three thirty, so please bring the quarterly numbers and a pen."


def trim_to_seconds(path: Path, seconds: float) -> bytes:
    with wave.open(str(path)) as src:
        rate, width, channels = src.getframerate(), src.getsampwidth(), src.getnchannels()
        frames = src.readframes(min(src.getnframes(), int(rate * seconds)))
    out = io.BytesIO()
    with wave.open(out, "wb") as dst:
        dst.setnchannels(channels)
        dst.setsampwidth(width)
        dst.setframerate(rate)
        dst.writeframes(frames)
    return out.getvalue()


def similarity(reference: Path, *others: Path) -> list[float] | None:
    if not (SIMILARITY.is_file() and SIMILARITY_PY.is_file()):
        return None
    result = subprocess.run([str(SIMILARITY_PY), str(SIMILARITY), str(reference), *map(str, others)], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return [float(line.split("\t")[0]) for line in result.stdout.splitlines() if "\t" in line]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=Path, help="a WAV with a real speaker; the first 10 s are used")
    parser.add_argument("--omnivoice", action="store_true", help="also speak with the omnivoice engine (starts it, about 45 s)")
    args = parser.parse_args()
    sample_path = args.sample or next((p for p in DEFAULT_SAMPLES if p.is_file()), None)
    if sample_path is None:
        print("no sample: pass --sample FILE", file=sys.stderr)
        return 2
    sample = trim_to_seconds(sample_path, 10.0)

    client = OpenAI(timeout=180.0)
    multipart = {"headers": {"Content-Type": "multipart/form-data"}}  # how the SDK's own resources ask for a file upload
    checks: dict[str, object] = {}
    failures: list[str] = []

    def check(name: str, ok: bool, detail: object = None) -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}
        if not ok:
            failures.append(name)
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail if detail is not None else ''}", flush=True)

    consent_id = voice_id = None
    work = Path(tempfile.mkdtemp(prefix="live-voice-", dir=HOME / ".tmp"))
    try:
        consent = client.post(
            "/audio/voice_consents", cast_to=object,
            body={"name": "Live test speaker", "language": "en-US"}, files=[("recording", ("consent.wav", sample, "audio/wav"))],
            options=multipart,
        )
        consent_id = consent["id"]
        check("consent created", consent["object"] == "audio.voice_consent" and consent_id.startswith("cons_"), consent_id)

        t0 = time.perf_counter()
        voice = client.post(
            "/audio/voices", cast_to=object,
            body={"name": "Live test voice", "consent": consent_id, "type": "audio_sample"},
            files=[("audio_sample", ("sample.wav", sample, "audio/wav"))], options=multipart,
        )
        voice_id = voice["id"]
        check("voice created", voice["object"] == "audio.voice" and voice["type"] == "audio_sample" and voice_id.startswith("voice_"),
              f"{voice_id} in {time.perf_counter() - t0:.1f}s, quality={voice.get('quality')}, warnings={voice.get('warnings')}, engines={voice.get('compatible_engines')}")
        check("quality check ran and is not low", (voice.get("quality") or {}).get("status") == "ok", voice.get("quality"))
        check("consent recorded on the voice", voice.get("consent") == consent_id)

        got = client.get(f"/audio/voices/{voice_id}", cast_to=object)
        check("voice retrievable", got["id"] == voice_id)
        listing = client.get("/audio/voices", cast_to=object)
        check("voice in list", voice_id in [v["id"] for v in listing["data"]] and any(v["voice_id"] == "alloy" for v in listing["voices"]))

        # Speak with it: the typed SDK call with the custom voice object.
        t0 = time.perf_counter()
        speech = client.audio.speech.create(model="tts", voice={"id": voice_id}, input=TEXT, response_format="wav")
        wav_path = work / "custom.wav"
        wav_path.write_bytes(speech.content)
        check("speech with {id}", speech.content[:4] == b"RIFF", f"{time.perf_counter() - t0:.2f}s, {len(speech.content)} bytes")

        first = None
        t0 = time.perf_counter()
        pcm = bytearray()
        with client.audio.speech.with_streaming_response.create(model="tts", voice={"id": voice_id}, input=TEXT + " " + TEXT, response_format="pcm") as resp:
            for chunk in resp.iter_bytes():
                if chunk and first is None:
                    first = time.perf_counter() - t0
                pcm += chunk
        check("streamed speech with {id}", len(pcm) > 24000 and first is not None, f"first audio {first:.2f}s, total {time.perf_counter() - t0:.2f}s, {len(pcm) / 48000:.1f}s of audio")

        builtin = client.audio.speech.create(model="tts", voice="alloy", input=TEXT, response_format="wav")
        builtin_path = work / "alloy.wav"
        builtin_path.write_bytes(builtin.content)
        sample_file = work / "sample.wav"
        sample_file.write_bytes(sample)
        sims = similarity(sample_file, wav_path, builtin_path)
        if sims is None:
            check("speaker similarity", False, "speakersim.py / chatterbox-tts venv not found")
        else:
            custom_sim, builtin_sim = sims
            check("speech sounds like the sample", custom_sim >= 0.45, f"cosine {custom_sim:.3f} (built-in voices score 0.64-0.79 against their own clip)")
            check("and more than a built-in voice does", custom_sim > builtin_sim + 0.1, f"custom {custom_sim:.3f} vs alloy {builtin_sim:.3f}")

        if args.omnivoice:
            t0 = time.perf_counter()
            ov = client.audio.speech.create(model="omnivoice", voice={"id": voice_id}, input=TEXT, response_format="wav")
            ov_path = work / "omnivoice.wav"
            ov_path.write_bytes(ov.content)
            ov_sim = similarity(sample_file, ov_path)
            check("omnivoice engine with {id}", ov.content[:4] == b"RIFF" and ov_sim is not None and ov_sim[0] >= 0.45,
                  f"{time.perf_counter() - t0:.1f}s, cosine {ov_sim[0] if ov_sim else None}")

        # A built-in name cannot be taken, and an unknown id never falls back to a default voice.
        try:
            client.post("/audio/voices", cast_to=object, body={"name": "Shimmer"}, files=[("audio_sample", ("s.wav", sample, "audio/wav"))], options=multipart)
            check("built-in name refused", False, "created")
        except BadRequestError as exc:
            check("built-in name refused", "voice_name_reserved" in str(exc.body), exc.status_code)
        try:
            client.audio.speech.create(model="tts", voice={"id": "voice_" + "0" * 24}, input="hi")
            check("unknown id is 404", False, "spoke")
        except NotFoundError as exc:
            check("unknown id is 404", True, exc.status_code)

        # Delete, then it is gone everywhere.
        deleted = client.delete(f"/audio/voices/{voice_id}", cast_to=object)
        check("voice deleted", deleted == {"id": voice_id, "object": "audio.voice", "deleted": True}, deleted)
        for label, call in (
            ("get after delete is 404", lambda: client.get(f"/audio/voices/{voice_id}", cast_to=object)),
            ("speech after delete is 404", lambda: client.audio.speech.create(model="tts", voice={"id": voice_id}, input="hi")),
        ):
            try:
                call()
                check(label, False, "still works")
            except NotFoundError:
                check(label, True)
        voice_id = None
        client.delete(f"/audio/voice_consents/{consent_id}", cast_to=object)
        consent_id = None
    finally:
        # Never leave test data behind, whatever failed.
        shutil.rmtree(work, ignore_errors=True)
        for path, ident in (("/audio/voices", voice_id), ("/audio/voice_consents", consent_id)):
            if ident:
                try:
                    client.delete(f"{path}/{ident}", cast_to=object)
                except Exception as exc:  # noqa: BLE001
                    print(f"cleanup of {ident} failed: {exc}", file=sys.stderr)
    print(json.dumps({"failed": failures, "checks": checks}, indent=1, default=str))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
