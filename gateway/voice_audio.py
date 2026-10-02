"""Validate and prepare a user's voice sample for cloning.

Standard library plus ffmpeg only (the gateway environment has no numpy). The pipeline turns an
upload of any common audio format into one clean reference clip:

    decode (ffmpeg) -> measure -> reject unusable -> trim silence -> pick the best window
    -> normalise loudness -> 24 kHz mono 16-bit WAV

Clone engines condition on a short clip. Chatterbox Turbo uses only the first 10-15 s and
OmniVoice prefers 3-10 s, so a long upload is cut to its best window instead of its first seconds.
"""
from __future__ import annotations

import array
import io
import math
import operator
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path

SAMPLE_RATE = 24000
FRAME_SECONDS = 0.02
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_SECONDS)
PAD_SECONDS = 0.15
TARGET_LEVEL_DBFS = -20.0
PEAK_LIMIT = 0.95
CLIP_LEVEL = 32500           # samples at or above this magnitude count as clipped
SPEECH_ABOVE_NOISE_DB = 12.0
SPEECH_FLOOR_DBFS = -50.0
SPEECH_CEILING_DBFS = -35.0
DECODE_TIMEOUT_S = 60


class VoiceSampleError(Exception):
    """The upload cannot become a voice. ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str, *, param: str | None = "audio_sample"):
        super().__init__(message)
        self.code = code
        self.message = message
        self.param = param


@dataclass(frozen=True)
class SampleLimits:
    min_seconds: float = 5.0
    max_seconds: float = 60.0
    min_speech_seconds: float = 3.5
    target_seconds: float = 12.0
    max_bytes: int = 10 * 1024 * 1024


@dataclass
class ProcessedSample:
    wav: bytes                       # 24 kHz mono 16-bit WAV, the stored reference clip
    duration_seconds: float          # of the stored clip
    source_seconds: float            # of the decoded upload
    speech_seconds: float            # of the stored clip
    snr_db: float | None
    clipping_fraction: float
    window_start_seconds: float      # where the stored clip starts inside the upload
    denoised: bool
    warnings: list[str] = field(default_factory=list)

    def measurements(self) -> dict[str, object]:
        return {
            "source_seconds": round(self.source_seconds, 2),
            "speech_seconds": round(self.speech_seconds, 2),
            "snr_db": None if self.snr_db is None else round(self.snr_db, 1),
            "clipping_fraction": round(self.clipping_fraction, 5),
            "window_start_seconds": round(self.window_start_seconds, 2),
            "denoised": self.denoised,
        }


def decode_pcm(data: bytes, *, max_seconds: float, denoise: bool = False) -> array.array:
    """Decode any audio upload to mono 24 kHz signed 16-bit samples, at most ``max_seconds + 1`` long."""
    if not data:
        raise VoiceSampleError("invalid_audio_sample", "The audio sample is empty.")
    filters = ["-af", "afftdn=nf=-25"] if denoise else []
    # A file, not a pipe: mp4/m4a often keep their index at the end and need a seekable input.
    with tempfile.TemporaryDirectory(prefix="voice-sample-") as tmp:
        source = Path(tmp) / "upload"
        source.write_bytes(data)
        command = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source), "-vn",
            "-t", f"{max_seconds + 1.0:.2f}", *filters, "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1",
        ]
        try:
            result = subprocess.run(command, capture_output=True, timeout=DECODE_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired as exc:
            raise VoiceSampleError("invalid_audio_sample", "Decoding the audio sample took too long.") from exc
        except FileNotFoundError as exc:
            raise VoiceSampleError("audio_processing_unavailable", "ffmpeg is not installed on the gateway.") from exc
    samples = array.array("h")
    samples.frombytes(result.stdout[: len(result.stdout) // 2 * 2])
    if result.returncode != 0 or len(samples) == 0:
        raise VoiceSampleError("invalid_audio_sample", "The file could not be decoded as audio.")
    return samples


def _frame_db(samples: array.array) -> list[float]:
    levels: list[float] = []
    for start in range(0, len(samples) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
        frame = samples[start : start + FRAME_SAMPLES]
        mean_square = sum(map(operator.mul, frame, frame)) / FRAME_SAMPLES
        levels.append(10.0 * math.log10(max(mean_square, 1.0) / (32768.0 * 32768.0)))
    return levels


def _percentile(sorted_values: list[float], fraction: float) -> float:
    index = min(len(sorted_values) - 1, max(0, int(round(fraction * (len(sorted_values) - 1)))))
    return sorted_values[index]


def _best_window(speech: list[bool], levels: list[float], clip_frames: list[bool], first: int, last: int, length: int) -> int:
    """First frame of the ``length``-frame window inside [first, last] with the most speech and least clipping."""
    step = max(1, int(0.5 / FRAME_SECONDS))
    best_start, best_score = first, -1.0
    start = first
    while start + length <= last + 1:
        window = range(start, start + length)
        score = sum(speech[i] for i in window) / length - 5.0 * sum(clip_frames[i] for i in window) / length
        if score > best_score + 1e-9:
            best_start, best_score = start, score
        start += step
    # Snap both edges to the quietest frame within 0.4 s so the cut lands in a pause.
    reach = int(0.4 / FRAME_SECONDS)
    lo = max(first, best_start - reach)
    snapped = min(range(lo, min(best_start + reach, last - length + 1) + 1), key=lambda i: levels[i], default=best_start)
    return max(first, min(snapped, last - length + 1))


def process_sample(data: bytes, limits: SampleLimits, *, denoise: bool = False) -> ProcessedSample:
    if len(data) > limits.max_bytes:
        raise VoiceSampleError("sample_too_large", f"The audio sample is larger than {limits.max_bytes // (1024 * 1024)} MiB.")
    samples = decode_pcm(data, max_seconds=limits.max_seconds, denoise=denoise)
    source_seconds = len(samples) / SAMPLE_RATE
    if source_seconds > limits.max_seconds:
        raise VoiceSampleError("sample_too_long", f"The audio sample is longer than {limits.max_seconds:g} seconds.")
    if source_seconds < limits.min_seconds:
        raise VoiceSampleError("sample_too_short", f"The audio sample is {source_seconds:.1f} seconds; at least {limits.min_seconds:g} are needed.")

    levels = _frame_db(samples)
    ordered = sorted(levels)
    noise_floor = _percentile(ordered, 0.10)
    # Capped so audio with no pauses (a designed voice, a continuous read) is not judged against its own level.
    threshold = min(max(noise_floor + SPEECH_ABOVE_NOISE_DB, SPEECH_FLOOR_DBFS), SPEECH_CEILING_DBFS)
    speech = [level > threshold for level in levels]
    speech_seconds_total = sum(speech) * FRAME_SECONDS
    if speech_seconds_total < limits.min_speech_seconds:
        raise VoiceSampleError(
            "insufficient_speech",
            f"Only {speech_seconds_total:.1f} seconds of speech were found; at least {limits.min_speech_seconds:g} are needed. "
            "Record a clear voice in a quiet room.",
        )
    clip_frames = [
        any(abs(s) >= CLIP_LEVEL for s in samples[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]) for i in range(len(levels))
    ]

    pad = int(PAD_SECONDS / FRAME_SECONDS)
    voiced = [i for i, flag in enumerate(speech) if flag]
    first, last = max(0, voiced[0] - pad), min(len(levels) - 1, voiced[-1] + pad)
    length = int(limits.target_seconds / FRAME_SECONDS)
    warnings: list[str] = []
    if last - first + 1 > length + int(1.0 / FRAME_SECONDS):
        first = _best_window(speech, levels, clip_frames, first, last, length)
        last = first + length - 1
        warnings.append("trimmed_to_best_window")
    window = samples[first * FRAME_SAMPLES : (last + 1) * FRAME_SAMPLES]
    window_speech = speech[first : last + 1]
    clipped_samples = sum(1 for s in window if abs(s) >= CLIP_LEVEL)
    clipping_fraction = clipped_samples / max(1, len(window))
    speech_levels = sorted(level for level, flag in zip(levels[first : last + 1], window_speech) if flag)
    snr_db = _percentile(speech_levels, 0.90) - noise_floor if speech_levels else None
    if not any(not flag for flag in window_speech):
        snr_db = None  # no pauses to measure the noise in

    if clipping_fraction > 0.01:
        warnings.append("clipping")
    if snr_db is not None and snr_db < 15.0:
        warnings.append("low_snr")
    if sum(window_speech) / max(1, len(window_speech)) < 0.5:
        warnings.append("long_silences")

    # Loudness: speech frames to -20 dBFS RMS, peak-limited. Chatterbox and OmniVoice both
    # assume a sane level; a quiet phone clip is the common cause of a weak clone.
    mean_square = sum(10 ** (level / 10) for level, flag in zip(levels[first : last + 1], window_speech) if flag) / max(1, sum(window_speech))
    speech_db = 10.0 * math.log10(max(mean_square, 1e-12))
    gain = 10 ** ((TARGET_LEVEL_DBFS - speech_db) / 20.0)
    peak = max(max(window), -min(window), 1)
    gain = min(gain, PEAK_LIMIT * 32767.0 / peak)
    scaled = array.array("h", (max(-32768, min(32767, int(round(s * gain)))) for s in window))

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(scaled.tobytes())
    return ProcessedSample(
        wav=buffer.getvalue(),
        duration_seconds=len(scaled) / SAMPLE_RATE,
        source_seconds=source_seconds,
        speech_seconds=sum(window_speech) * FRAME_SECONDS,
        snr_db=snr_db,
        clipping_fraction=clipping_fraction,
        window_start_seconds=first * FRAME_SECONDS,
        denoised=denoise,
        warnings=warnings,
    )
