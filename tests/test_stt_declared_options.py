"""A transcription request never has an option silently dropped or a silently different response shape.

A record's ``chunking`` block declares the ``response_formats`` its engine produces and whether it honours ``prompt``
(``prompt_supported``). The route refuses what the alias cannot do before any byte reaches the engine; a record that
declares nothing behaves as before, plus one backstop: a ``verbose_json`` reply without ``segments`` is an error that
says so. The capabilities listing publishes the declaration, the one-piece audio maximum and the formats cut for long
audio.
"""

from __future__ import annotations

import json
import unittest
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import httpx

import proxy
from gateway import stt_chunking, stt_declared_options
from gateway.engine_protocol import chunked_stt
from tests import test_stt_tiers as tiers
from tests.speech_tier_helpers import SttCase

PARAKEET = "stt-parakeet-gpu"  # behind stt-quality, port 8017
NEMOTRON = "stt-nemotron"  # behind stt-fast, port 8012

SEGMENTS_REPLY = {"text": "hello", "language": "en", "duration": 1.0, "segments": [{"id": 0, "text": "hello"}]}
TIMING_REPLY = {"text": "hello", "timing": {"total": 0.2}}


def _declare(case: SttCase, service: str, **declaration) -> None:
    case.cfg["backends"][service]["chunking"].update(declaration)


def _file_first_past_peek(case: SttCase, fields, **kwargs):
    """The upload first and the model field hidden behind it, so the service is chosen after the parse."""
    return tiers.SttServiceSelectionTests._post_file_first_past_peek(cast(Any, case), fields, **kwargs)


class RefusalTests(SttCase):
    def test_prompt_to_an_alias_whose_record_has_no_prompt_input_is_a_400_before_the_engine(self) -> None:
        _declare(self, PARAKEET, prompt_supported=False)
        seen = self._json_upstream()
        response = self._post([("model", "stt-quality"), ("prompt", "Azizam, Parakeet")])
        self.assertEqual(response.status_code, 400, response.text)
        error = response.json()["error"]
        self.assertEqual((error["type"], error["param"], error["code"]), ("invalid_request_error", "prompt", "unsupported_parameter"))
        self.assertIn("'stt-quality'", error["message"])
        self.assertIn("prompt", error["message"])
        self.assertEqual(seen, [])

    def test_an_empty_prompt_is_not_an_error(self) -> None:
        _declare(self, PARAKEET, prompt_supported=False)
        seen = self._json_upstream()
        for empty in ("", "   "):
            with self.subTest(prompt=repr(empty)):
                response = self._post([("model", "stt-quality"), ("prompt", empty)])
                self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(seen), 2)

    def test_a_record_that_honours_prompt_or_declares_nothing_passes_it_on_as_before(self) -> None:
        _declare(self, PARAKEET, prompt_supported=True)
        seen = self._json_upstream()
        for model in ("stt-quality", "stt-fast"):  # declared true, and declared nothing
            with self.subTest(model=model):
                self.assertEqual(self._post([("model", model), ("prompt", "hint")]).status_code, 200)
                self.assertIn(b'name="prompt"\r\n\r\nhint\r\n', seen[-1].content)

    def test_a_format_the_record_does_not_list_is_a_400_naming_what_it_returns(self) -> None:
        _declare(self, NEMOTRON, response_formats=["json", "text"])
        seen = self._json_upstream()
        response = self._post([("model", "stt-fast"), ("response_format", "verbose_json")])
        self.assertEqual(response.status_code, 400, response.text)
        error = response.json()["error"]
        self.assertEqual((error["type"], error["param"], error["code"]),
                         ("invalid_request_error", "response_format", "unsupported_response_format"))
        self.assertIn("'stt-fast'", error["message"])
        self.assertIn("verbose_json", error["message"])
        self.assertIn("json, text", error["message"])
        self.assertEqual(seen, [])

    def test_a_listed_format_passes_whatever_its_case(self) -> None:
        _declare(self, NEMOTRON, response_formats=["json", "text"])
        seen = self._json_upstream()
        for value in ("json", "text", "JSON", " text "):
            with self.subTest(response_format=value):
                self.assertEqual(self._post([("model", "stt-fast"), ("response_format", value)]).status_code, 200)
        # Naming no format asks for the engine's default, which is not a format to refuse.
        self.assertEqual(self._post([("model", "stt-fast")]).status_code, 200)
        self.assertEqual(len(seen), 5)

    def test_a_record_that_declares_nothing_is_unchanged(self) -> None:
        seen = self._upstream_replying(SEGMENTS_REPLY)
        for value in ("srt", "vtt", "text", "verbose_json", "diarized_json"):
            with self.subTest(response_format=value):
                response = self._post([("model", "stt-fast"), ("response_format", value), ("prompt", "hint")])
                self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(seen), 5)

    def test_the_refusal_applies_when_the_model_field_comes_after_the_file(self) -> None:
        _declare(self, NEMOTRON, prompt_supported=False, response_formats=["json"])
        seen = self._json_upstream()
        for fields, param in (([("model", "stt-fast"), ("prompt", "hint")], "prompt"),
                              ([("model", "stt-fast"), ("response_format", "srt")], "response_format")):
            with self.subTest(param=param):
                response, body = _file_first_past_peek(self, fields)
                self.assertEqual(response.status_code, 400, body)
                self.assertEqual(json.loads(body)["error"]["param"], param)
        self.assertEqual(seen, [])

    def test_the_refusal_applies_to_translations_too(self) -> None:
        self.cfg["backends"][NEMOTRON]["capabilities"].append("translations")
        _declare(self, NEMOTRON, prompt_supported=False)
        seen = self._json_upstream()
        response, body = _file_first_past_peek(
            self, [("model", "stt-fast"), ("prompt", "hint")], operation="translations")
        self.assertEqual(response.status_code, 400, body)
        self.assertEqual(json.loads(body)["error"]["param"], "prompt")
        self.assertEqual(seen, [])

    def test_a_malformed_declaration_is_a_configuration_error_not_a_pass(self) -> None:
        for declaration in ({"response_formats": "json"}, {"response_formats": []}, {"prompt_supported": "no"}):
            with self.subTest(declaration=declaration):
                self.cfg["backends"][NEMOTRON]["chunking"] = {"enabled": False, "spool_dir": self.tmp.name, **declaration}
                seen = self._json_upstream()
                response = self._post([("model", "stt-fast")])
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json()["error"]["code"], "stt_configuration_invalid")
                self.assertEqual(seen, [])

    def _upstream_replying(self, payload: dict):
        return self.use_upstream(lambda _request: httpx.Response(200, json=payload))


class VerboseJsonShapePredicateTests(unittest.TestCase):
    def test_verbose_json_requires_an_object_with_segments(self) -> None:
        fields = [("response_format", "verbose_json")]
        for payload in (None, "hello", []):
            with self.subTest(payload=payload):
                self.assertTrue(stt_declared_options.verbose_json_missing_segments(fields, payload))
        self.assertFalse(stt_declared_options.verbose_json_missing_segments(fields, {"segments": []}))

    def test_other_formats_do_not_require_segments(self) -> None:
        for payload in (None, "hello", []):
            with self.subTest(payload=payload):
                self.assertFalse(stt_declared_options.verbose_json_missing_segments([("response_format", "json")], payload))


class VerboseJsonBackstopTests(SttCase):
    def _reply(self, payload: dict):
        return self.use_upstream(lambda _request: httpx.Response(200, json=payload))

    def test_verbose_json_without_segments_is_an_error_that_says_so(self) -> None:
        self._reply(TIMING_REPLY)
        response = self._post([("model", "stt-fast"), ("response_format", "verbose_json")])
        self.assertEqual(response.status_code, 502, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], "stt_response_format_not_returned")
        self.assertIn("did not return verbose_json", error["message"])
        self.assertIn("'stt-fast'", error["message"])
        self.assertNotIn("timing", response.text)

    def test_an_empty_segment_list_is_an_answer(self) -> None:
        self._reply({"text": " ", "segments": []})
        self.assertEqual(self._post([("model", "stt-fast"), ("response_format", "verbose_json")]).status_code, 200)

    def test_a_reply_with_segments_passes_unchanged(self) -> None:
        self._reply(SEGMENTS_REPLY)
        response = self._post([("model", "stt-fast"), ("response_format", "verbose_json")])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), SEGMENTS_REPLY)

    def test_other_formats_are_not_held_to_it(self) -> None:
        self._reply(TIMING_REPLY)
        for fields in ([("model", "stt-fast")], [("model", "stt-fast"), ("response_format", "json")]):
            with self.subTest(fields=fields):
                self.assertEqual(self._post(fields).status_code, 200)

    def test_a_declared_record_whose_engine_still_omits_segments_gets_the_same_error(self) -> None:
        _declare(self, NEMOTRON, response_formats=["json", "verbose_json"])
        self._reply(TIMING_REPLY)
        response = self._post([("model", "stt-fast"), ("response_format", "verbose_json")])
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["error"]["code"], "stt_response_format_not_returned")

    def test_a_merged_chunked_reply_is_held_to_it_as_well(self) -> None:
        for body, status in (({"text": "a b"}, 502), (dict(SEGMENTS_REPLY), 200)):
            with self.subTest(status=status):
                adapter = AsyncMock()
                adapter.invoke.return_value = chunked_stt.TranscriptReply(200, {}, body=body, mode="chunked")
                with patch.object(proxy, "_stt_adapter", return_value=adapter):
                    response = self._post([("model", "stt-fast"), ("response_format", "verbose_json")])
                self.assertEqual(response.status_code, status, response.text)
                if status == 502:
                    self.assertEqual(response.json()["error"]["code"], "stt_response_format_not_returned")


class CapabilitiesTests(SttCase):
    def setUp(self) -> None:
        super().setUp()
        # The listing's live-availability probe reaches for the network; these rows are about declared facts.
        patcher = patch.object(proxy, "_capabilities_availability_evidence", new=AsyncMock(return_value={}))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _row(self, model: str) -> dict:
        response = self.client.get("/gateway/v1/capabilities")
        self.assertEqual(response.status_code, 200, response.text)
        rows = [row for row in response.json()["capabilities"]
                if row["intent"] == "speech.transcription" and row.get("model") == model]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_a_declared_alias_row_lists_formats_prompt_and_the_one_piece_maximum(self) -> None:
        _declare(self, PARAKEET, prompt_supported=False, response_formats=["json", "text"], enabled=True,
                 target_seconds=30, threshold_seconds=40, search_seconds=2, max_segment_seconds=40)
        limits = self._row("stt-quality")["limits"]
        self.assertEqual(limits["response_formats"], ["json", "text"])
        self.assertIs(limits["prompt_supported"], False)
        self.assertEqual(limits["max_audio_seconds"], 40)
        self.assertEqual(limits["split_response_formats"], ["json"])

    def test_an_undeclared_alias_row_says_null_not_a_guess(self) -> None:
        limits = self._row("stt-fast")["limits"]
        self.assertIsNone(limits["response_formats"])
        self.assertIsNone(limits["prompt_supported"])
        self.assertIsNone(limits["max_audio_seconds"])
        self.assertEqual(limits["split_response_formats"], [])  # chunking is off on this record

    def test_an_engine_with_native_long_audio_has_no_one_piece_maximum(self) -> None:
        _declare(self, NEMOTRON, enabled=True, target_seconds=30, threshold_seconds=40, search_seconds=2,
                 max_segment_seconds=40, native_long_audio_offline=True)
        self.assertIsNone(self._row("stt-fast")["limits"]["max_audio_seconds"])

    def test_the_split_list_follows_request_supports_chunking(self) -> None:
        _declare(self, PARAKEET, enabled=True, target_seconds=30, threshold_seconds=40, search_seconds=2, max_segment_seconds=40)
        self.assertEqual(self._row("stt-quality")["limits"]["split_response_formats"], ["json", "verbose_json"])
        real = stt_chunking.request_supports_chunking

        def json_only(fields, *, file_count):
            return real(fields, file_count=file_count) and dict(fields).get("response_format") == "json"

        with patch.object(stt_chunking, "request_supports_chunking", json_only):
            self.assertEqual(self._row("stt-quality")["limits"]["split_response_formats"], ["json"])
        with patch.object(stt_chunking, "request_supports_chunking", return_value=False):
            self.assertEqual(self._row("stt-quality")["limits"]["split_response_formats"], [])

    def test_a_declared_list_bounds_the_split_list(self) -> None:
        _declare(self, PARAKEET, response_formats=["text", "diarized_json"], enabled=True,
                 target_seconds=30, threshold_seconds=40, search_seconds=2, max_segment_seconds=40)
        with patch.object(stt_chunking, "request_supports_chunking", return_value=True):
            self.assertEqual(self._row("stt-quality")["limits"]["split_response_formats"], ["text", "diarized_json"])

    def test_a_malformed_declaration_leaves_the_listing_working(self) -> None:
        _declare(self, NEMOTRON, response_formats="json")
        limits = self._row("stt-fast")["limits"]
        self.assertIsNone(limits["response_formats"])


class DeclarationReaderTests(unittest.TestCase):
    def test_reads_lowercased_unique_formats_and_a_strict_bool(self) -> None:
        options = stt_declared_options.declared_options({"response_formats": ["JSON", " text ", "json"], "prompt_supported": False})
        self.assertEqual(options.response_formats, ("json", "text"))
        self.assertIs(options.prompt_supported, False)

    def test_nothing_declared_is_none_not_false(self) -> None:
        for block in (None, {}, {"enabled": True}):
            options = stt_declared_options.declared_options(block)
            self.assertIsNone(options.response_formats)
            self.assertIsNone(options.prompt_supported)

    def test_refusal_names_only_the_first_unmet_option(self) -> None:
        options = stt_declared_options.DeclaredOptions(response_formats=("json",), prompt_supported=False)
        both = stt_declared_options.refuse(options, [("prompt", "x"), ("response_format", "srt")], model="m")
        assert both is not None
        self.assertEqual(both.param, "prompt")
        self.assertIsNone(stt_declared_options.refuse(options, [("prompt", ""), ("response_format", "json")], model="m"))


if __name__ == "__main__":
    unittest.main()
