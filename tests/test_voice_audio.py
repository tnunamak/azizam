"""Sample validation and preparation for custom voices (gateway/voice_audio.py)."""

from __future__ import annotations

import array
import math
import unittest

from gateway import voice_audio as va
from tests.voice_test_audio import HAVE_FFMPEG, RATE, samples_of, speechlike, transcode, wav_of

LIMITS = va.SampleLimits()


def rms_dbfs(samples) -> float:
    return 10 * math.log10(max(sum(s * s for s in samples) / len(samples), 1) / 32768**2)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
class ProcessSampleTests(unittest.TestCase):
    def test_a_clean_ten_second_sample_is_kept_whole_and_leveled(self) -> None:
        result = va.process_sample(speechlike(10), LIMITS)
        self.assertEqual(result.warnings, [])
        self.assertAlmostEqual(result.duration_seconds, 10.0, delta=0.6)
        self.assertEqual(result.window_start_seconds, 0.0)
        out = samples_of(result.wav)
        self.assertLessEqual(max(abs(s) for s in out), int(0.95 * 32767) + 1)
        # Speech frames sit at about -20 dBFS (the whole clip includes pauses, so it is quieter).
        self.assertGreater(rms_dbfs(out), -30)
        self.assertLess(rms_dbfs(out), -15)
        self.assertGreater(result.snr_db or 0, 25)

    def test_a_quiet_clip_is_brought_up_and_a_loud_one_down(self) -> None:
        quiet = va.process_sample(speechlike(8, amplitude=900), LIMITS)
        loud = va.process_sample(speechlike(8, amplitude=20000), LIMITS)
        self.assertAlmostEqual(rms_dbfs(samples_of(quiet.wav)), rms_dbfs(samples_of(loud.wav)), delta=2.5)

    def test_a_long_upload_is_cut_to_the_best_window(self) -> None:
        # 6 s of noise-only, then 20 s of voice: the stored clip must come from the voiced part.
        silent = array.array("h", samples_of(speechlike(6, amplitude=0, noise=30)))
        voiced = array.array("h", samples_of(speechlike(20)))
        result = va.process_sample(wav_of(silent + voiced), LIMITS)
        self.assertIn("trimmed_to_best_window", result.warnings)
        self.assertAlmostEqual(result.duration_seconds, LIMITS.target_seconds, delta=0.1)
        self.assertGreaterEqual(result.window_start_seconds, 5.5)
        self.assertGreater(result.speech_seconds / result.duration_seconds, 0.6)

    def test_leading_and_trailing_silence_is_trimmed(self) -> None:
        pad = array.array("h", samples_of(speechlike(2, amplitude=0, noise=20)))
        body = array.array("h", samples_of(speechlike(8)))
        result = va.process_sample(wav_of(pad + body + pad), LIMITS)
        self.assertLess(result.duration_seconds, 8.9)
        self.assertGreater(result.window_start_seconds, 1.5)

    def test_too_short_too_long_and_too_quiet_are_refused_with_codes(self) -> None:
        for wav, code in (
            (speechlike(3), "sample_too_short"),
            (speechlike(61), "sample_too_long"),
            (speechlike(8, amplitude=0, noise=25), "insufficient_speech"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(va.VoiceSampleError) as ctx:
                    va.process_sample(wav, LIMITS)
                self.assertEqual(ctx.exception.code, code)

    def test_not_audio_and_empty_files_are_refused(self) -> None:
        for data in (b"", b"this is not audio" * 200):
            with self.subTest(size=len(data)):
                with self.assertRaises(va.VoiceSampleError) as ctx:
                    va.process_sample(data, LIMITS)
                self.assertEqual(ctx.exception.code, "invalid_audio_sample")

    def test_oversize_upload_is_refused_before_decoding(self) -> None:
        with self.assertRaises(va.VoiceSampleError) as ctx:
            va.process_sample(b"x" * 2048, va.SampleLimits(max_bytes=1024))
        self.assertEqual(ctx.exception.code, "sample_too_large")

    def test_clipping_and_low_snr_warn_but_do_not_refuse(self) -> None:
        clipped = va.process_sample(speechlike(8, amplitude=60000), LIMITS)
        self.assertIn("clipping", clipped.warnings)
        noisy = va.process_sample(speechlike(8, amplitude=6000, noise=1100), LIMITS)
        self.assertIn("low_snr", noisy.warnings)

    def test_common_formats_decode_to_the_same_clip(self) -> None:
        wav = speechlike(8)
        for extension in ("mp3", "ogg", "flac", "m4a"):
            with self.subTest(format=extension):
                result = va.process_sample(transcode(wav, extension), LIMITS)
                self.assertAlmostEqual(result.duration_seconds, 8.0, delta=0.9)

    def test_denoise_is_opt_in_and_recorded(self) -> None:
        wav = speechlike(8, noise=800)
        self.assertFalse(va.process_sample(wav, LIMITS).denoised)
        self.assertTrue(va.process_sample(wav, LIMITS, denoise=True).denoised)

    def test_output_is_a_24khz_mono_wav(self) -> None:
        import io
        import wave

        with wave.open(io.BytesIO(va.process_sample(speechlike(8, rate=16000), LIMITS).wav)) as handle:
            self.assertEqual((handle.getnchannels(), handle.getsampwidth(), handle.getframerate()), (1, 2, RATE))


if __name__ == "__main__":
    unittest.main()
