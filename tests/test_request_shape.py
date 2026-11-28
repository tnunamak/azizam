from __future__ import annotations

import unittest

from gateway.backends.request_shape import (
    MAX_ADMIN_STAGE_COUNT,
    MAX_RECORDED_STAGES,
    MAX_STAGE_FIELD_COUNT,
    REQUEST_SHAPE_STAGES,
    TERMINAL_REASONS,
    is_unbounded_nonstream,
    record_request_shape_stage,
    request_body_byte_count,
    request_shape_fields,
    sanitize_request_shape_fields_for_admin,
    sanitize_request_shape_stages_for_admin,
)
from gateway.redaction import find_forbidden_keys


class RequestShapeFieldsTests(unittest.TestCase):
    def test_captures_only_structural_fields(self) -> None:
        body = {
            "model": "default",
            "stream": True,
            "max_tokens": 512,
            "messages": [{"role": "user", "content": "secret prompt text"}],
            "tools": [{"type": "function", "function": {"name": "do_thing"}}],
        }
        fields = request_shape_fields(body)
        self.assertEqual(fields["stream"], True)
        self.assertEqual(fields["max_tokens"], 512)
        self.assertEqual(fields["message_count"], 1)
        self.assertEqual(fields["tool_count"], 1)
        self.assertNotIn("messages", fields)
        self.assertNotIn("tools", fields)
        self.assertNotIn("model", fields)

    def test_never_contains_forbidden_keys_regardless_of_input_shape(self) -> None:
        body = {
            "stream": False,
            "messages": [{"role": "user", "content": "x" * 5000}],
            "tools": [{"type": "function", "function": {"name": "n", "arguments": "{}"}}],
            "prompt": "raw prompt text",
            "api_key": "sk-should-never-appear",
        }
        fields = request_shape_fields(body)
        self.assertEqual(find_forbidden_keys(fields), [])

    def test_non_mapping_body_returns_empty(self) -> None:
        self.assertEqual(request_shape_fields(None), {})
        self.assertEqual(request_shape_fields("not-a-mapping"), {})  # type: ignore[arg-type]

    def test_missing_optional_fields_are_omitted_not_null(self) -> None:
        fields = request_shape_fields({"stream": True})
        self.assertNotIn("max_tokens", fields)
        self.assertNotIn("message_count", fields)
        self.assertNotIn("tool_count", fields)


class UnboundedNonstreamTests(unittest.TestCase):
    def test_stream_false_absent_token_limit_is_unbounded(self) -> None:
        fields = request_shape_fields({"stream": False})
        self.assertTrue(fields["is_unbounded_nonstream"])

    def test_stream_absent_defaults_to_false_and_is_unbounded_without_limit(self) -> None:
        fields = request_shape_fields({})
        self.assertTrue(fields["is_unbounded_nonstream"])

    def test_stream_false_negative_one_token_limit_is_unbounded(self) -> None:
        fields = request_shape_fields({"stream": False, "max_tokens": -1})
        self.assertTrue(fields["is_unbounded_nonstream"])

    def test_stream_false_with_finite_limit_is_bounded(self) -> None:
        fields = request_shape_fields({"stream": False, "max_tokens": 8192})
        self.assertFalse(fields["is_unbounded_nonstream"])

    def test_stream_true_is_never_unbounded_nonstream(self) -> None:
        fields = request_shape_fields({"stream": True})
        self.assertFalse(fields["is_unbounded_nonstream"])

    def test_incident_shape_task_59337_is_flagged(self) -> None:
        # temp 0 / stream false / reasoning off / n_predict -1 (or absent).
        fields = request_shape_fields({
            "stream": False,
            "temperature": 0,
            "n_predict": -1,
        })
        self.assertTrue(fields["is_unbounded_nonstream"])
        fields_absent = request_shape_fields({"stream": False, "temperature": 0})
        self.assertTrue(fields_absent["is_unbounded_nonstream"])

    def test_direct_helper_matches_field_based_result(self) -> None:
        fields = {"stream": False, "max_tokens": None}
        self.assertTrue(is_unbounded_nonstream(fields))
        fields = {"stream": False, "max_tokens": 100}
        self.assertFalse(is_unbounded_nonstream(fields))


class RequestBodyByteCountTests(unittest.TestCase):
    def test_counts_bytes(self) -> None:
        self.assertEqual(request_body_byte_count(b"hello"), 5)

    def test_none_is_zero(self) -> None:
        self.assertEqual(request_body_byte_count(None), 0)

    def test_empty_is_zero(self) -> None:
        self.assertEqual(request_body_byte_count(b""), 0)


class RecordRequestShapeStageTests(unittest.TestCase):
    def test_appends_stage_with_fields(self) -> None:
        metadata: dict = {}
        record_request_shape_stage(metadata, "accepted", {"stream": True, "max_tokens": 100})
        self.assertEqual(len(metadata["request_shape_stages"]), 1)
        entry = metadata["request_shape_stages"][0]
        self.assertEqual(entry["stage"], "accepted")
        self.assertEqual(entry["stream"], True)
        self.assertEqual(entry["max_tokens"], 100)

    def test_multiple_stages_accumulate_in_order(self) -> None:
        metadata: dict = {}
        record_request_shape_stage(metadata, "accepted", {"stream": False})
        record_request_shape_stage(metadata, "outer_egress", {"stream": False})
        record_request_shape_stage(metadata, "terminal", {"stream": False})
        stages = [entry["stage"] for entry in metadata["request_shape_stages"]]
        self.assertEqual(stages, ["accepted", "outer_egress", "terminal"])

    def test_extra_fields_are_merged_in(self) -> None:
        metadata: dict = {}
        record_request_shape_stage(
            metadata, "terminal", {"stream": True}, extra={"terminal_reason": "completed"}
        )
        entry = metadata["request_shape_stages"][0]
        self.assertEqual(entry["terminal_reason"], "completed")

    def test_stage_count_is_capped(self) -> None:
        metadata: dict = {}
        for i in range(MAX_RECORDED_STAGES + 5):
            record_request_shape_stage(metadata, f"stage-{i}", {"stream": False})
        self.assertEqual(len(metadata["request_shape_stages"]), MAX_RECORDED_STAGES)

    def test_field_count_per_stage_is_capped(self) -> None:
        metadata: dict = {}
        huge_extra = {f"field_{i}": i for i in range(MAX_STAGE_FIELD_COUNT + 10)}
        record_request_shape_stage(metadata, "accepted", {"stream": True}, extra=huge_extra)
        entry = metadata["request_shape_stages"][0]
        self.assertLessEqual(len(entry), MAX_STAGE_FIELD_COUNT)

    def test_string_value_is_capped_in_length(self) -> None:
        metadata: dict = {}
        record_request_shape_stage(
            metadata, "terminal", {"stream": True},
            extra={"terminal_reason": "x" * 10_000},
        )
        entry = metadata["request_shape_stages"][0]
        from gateway.backends.request_shape import MAX_STAGE_STRING_LENGTH
        self.assertLessEqual(len(entry["terminal_reason"]), MAX_STAGE_STRING_LENGTH)

    def test_numeric_value_magnitude_is_capped(self) -> None:
        metadata: dict = {}
        record_request_shape_stage(
            metadata, "accepted", {"stream": False}, extra={"status_code": 10**12}
        )
        entry = metadata["request_shape_stages"][0]
        from gateway.backends.request_shape import MAX_STAGE_NUMERIC_MAGNITUDE
        self.assertLessEqual(entry["status_code"], MAX_STAGE_NUMERIC_MAGNITUDE)

    def test_list_value_length_is_capped(self) -> None:
        metadata: dict = {}
        record_request_shape_stage(
            metadata, "accepted", {"stream": False}, extra={"warnings": list(range(1000))}
        )
        entry = metadata["request_shape_stages"][0]
        from gateway.backends.request_shape import MAX_STAGE_LIST_LENGTH
        self.assertLessEqual(len(entry["warnings"]), MAX_STAGE_LIST_LENGTH)

    def test_never_contains_forbidden_keys(self) -> None:
        metadata: dict = {}
        body = {
            "stream": False,
            "messages": [{"role": "user", "content": "secret"}],
            "api_key": "sk-should-never-appear",
        }
        record_request_shape_stage(metadata, "accepted", body)
        self.assertEqual(find_forbidden_keys(metadata["request_shape_stages"][0]), [])


class SanitizeRequestShapeFieldsForAdminTests(unittest.TestCase):
    """The admin trust boundary — see independent-gate-3.md finding 4."""

    def test_accepts_every_legitimate_field(self) -> None:
        shape = {
            "stream": True,
            "max_tokens": 64,
            "max_completion_tokens": 128,
            "n_predict": -1,
            "message_count": 3,
            "tool_count": 2,
            "is_unbounded_nonstream": False,
        }
        self.assertEqual(sanitize_request_shape_fields_for_admin(shape), shape)

    def test_non_mapping_returns_empty(self) -> None:
        self.assertEqual(sanitize_request_shape_fields_for_admin(None), {})
        self.assertEqual(sanitize_request_shape_fields_for_admin("not a dict"), {})
        self.assertEqual(sanitize_request_shape_fields_for_admin([1, 2, 3]), {})

    def test_unknown_key_is_discarded(self) -> None:
        out = sanitize_request_shape_fields_for_admin(
            {"stream": True, "arguments": "private args", "tool_name": "private_tool"}
        )
        self.assertEqual(out, {"stream": True})

    def test_nested_mapping_value_is_discarded_not_recursed_into(self) -> None:
        out = sanitize_request_shape_fields_for_admin(
            {"stream": True, "headers": {"authorization": "Bearer stage-secret"}}
        )
        self.assertEqual(out, {"stream": True})
        self.assertNotIn("headers", out)

    def test_wrong_typed_value_is_discarded(self) -> None:
        out = sanitize_request_shape_fields_for_admin(
            {"stream": "not-a-bool", "message_count": "not-an-int", "tool_count": 3.5}
        )
        self.assertEqual(out, {})

    def test_bool_never_satisfies_a_numeric_field(self) -> None:
        # isinstance(True, int) is True in Python — a stray boolean must not
        # silently pass through max_tokens/message_count as if it were 0/1.
        out = sanitize_request_shape_fields_for_admin(
            {"max_tokens": True, "message_count": False}
        )
        self.assertEqual(out, {})

    def test_numeric_magnitude_is_capped(self) -> None:
        out = sanitize_request_shape_fields_for_admin({"max_tokens": 10**9})
        self.assertLessEqual(out["max_tokens"], 10_000_000)

    def test_unbounded_string_length_field_is_impossible(self) -> None:
        # There is no string-typed shape field at all — every field is a
        # bool or number — so a 20000-char string smuggled under a
        # legitimate-looking key is discarded by type mismatch alone.
        out = sanitize_request_shape_fields_for_admin({"max_tokens": "x" * 20000})
        self.assertEqual(out, {})


class SanitizeRequestShapeStagesForAdminTests(unittest.TestCase):
    def test_accepts_legitimate_stage_entries(self) -> None:
        stages = [
            {"stage": "accepted", "stream": False, "message_count": 1},
            {"stage": "terminal", "terminal_reason": "completed", "status_code": 200},
        ]
        out = sanitize_request_shape_stages_for_admin(stages)
        self.assertEqual(out, stages)

    def test_non_list_returns_empty(self) -> None:
        self.assertEqual(sanitize_request_shape_stages_for_admin(None), [])
        self.assertEqual(sanitize_request_shape_stages_for_admin({"stage": "x"}), [])

    def test_unknown_stage_value_is_discarded(self) -> None:
        out = sanitize_request_shape_stages_for_admin(
            [{"stage": "not_a_real_stage", "stream": True}]
        )
        self.assertEqual(out, [{"stream": True}])

    def test_unknown_terminal_reason_is_discarded(self) -> None:
        out = sanitize_request_shape_stages_for_admin(
            [{"stage": "terminal", "terminal_reason": "made_up_reason"}]
        )
        self.assertEqual(out, [{"stage": "terminal"}])

    def test_call_id_and_url_shaped_keys_are_discarded(self) -> None:
        """Reproduces independent-gate-3.md's exact exploit values."""
        out = sanitize_request_shape_stages_for_admin([
            {
                "stage": "backend_egress",
                "status_code": 200,
                "call_id": "x" * 20000,
                "model_api_base": "https://user:header-secret@example.invalid/v1?api_key=header-key",
            }
        ])
        self.assertEqual(out, [{"stage": "backend_egress", "status_code": 200}])

    def test_nested_stage_list_secret_exfiltration_is_fully_discarded(self) -> None:
        """Reproduces independent-gate-3.md's admin_nested_stage exploit: a
        mapping nested inside a list nested inside a stage-list entry,
        two levels deeper than gateway.redaction's single-level recursion
        reaches."""
        hostile = [
            {"stage": "terminal", "terminal_reason": "completed", "status_code": 200},
            {
                "nested_stage": [{
                    "arguments": "private args",
                    "headers": {"authorization": "Bearer stage-secret"},
                    "result": "private result",
                    "tool_name": "private_tool",
                }],
            },
        ]
        out = sanitize_request_shape_stages_for_admin(hostile)
        self.assertEqual(
            out, [{"stage": "terminal", "terminal_reason": "completed", "status_code": 200}]
        )
        dumped = str(out)
        for leak in ("private args", "stage-secret", "private result", "private_tool"):
            self.assertNotIn(leak, dumped)

    def test_entry_count_is_capped_at_max_admin_stage_count(self) -> None:
        stages = [{"stage": "accepted"} for _ in range(MAX_ADMIN_STAGE_COUNT + 10)]
        out = sanitize_request_shape_stages_for_admin(stages)
        self.assertEqual(len(out), MAX_ADMIN_STAGE_COUNT)

    def test_non_mapping_list_items_are_skipped(self) -> None:
        out = sanitize_request_shape_stages_for_admin(["not a dict", 42, None, {"stage": "accepted"}])
        self.assertEqual(out, [{"stage": "accepted"}])

    def test_status_code_wrong_type_is_discarded(self) -> None:
        out = sanitize_request_shape_stages_for_admin(
            [{"stage": "backend_egress", "status_code": "200"}]
        )
        self.assertEqual(out, [{"stage": "backend_egress"}])

    def test_every_declared_terminal_reason_round_trips(self) -> None:
        for reason in TERMINAL_REASONS:
            out = sanitize_request_shape_stages_for_admin(
                [{"stage": "terminal", "terminal_reason": reason}]
            )
            self.assertEqual(out, [{"stage": "terminal", "terminal_reason": reason}])

    def test_every_declared_stage_name_round_trips(self) -> None:
        for stage in REQUEST_SHAPE_STAGES:
            out = sanitize_request_shape_stages_for_admin([{"stage": stage}])
            self.assertEqual(out, [{"stage": stage}])


class SanitizeAdminMetadataTests(unittest.TestCase):
    """End-to-end trust-boundary tests via gateway.receipts.sanitize_admin_metadata."""

    def test_hostile_litellm_metadata_is_bounded_to_the_safe_key_allowlist(self) -> None:
        from gateway.receipts import sanitize_admin_metadata

        hostile = {
            "litellm": {
                "call_id": "x" * 20000,
                "model_api_base": "https://user:header-secret@example.invalid/v1?api_key=header-key",
                "model_id": "legit-model",
                "arbitrary_injected_key": "should not appear",
            },
        }
        out = sanitize_admin_metadata(hostile)
        self.assertEqual(out, {"litellm": {"model_id": "legit-model"}})

    def test_full_hostile_metadata_shape_from_gate_3_is_fully_contained(self) -> None:
        from gateway.receipts import sanitize_admin_metadata

        hostile = {
            "litellm": {
                "call_id": "x" * 20000,
                "model_api_base": "https://user:header-secret@example.invalid/v1?api_key=header-key",
            },
            "request_shape_stages": [
                {
                    "nested_stage": [{
                        "arguments": "private args",
                        "headers": {"authorization": "Bearer stage-secret"},
                        "result": "private result",
                        "tool_name": "private_tool",
                    }],
                },
            ],
            "not_an_admin_field_at_all": "should never appear",
        }
        out = sanitize_admin_metadata(hostile)
        self.assertEqual(out, {})
        self.assertNotIn("not_an_admin_field_at_all", str(out))


if __name__ == "__main__":
    unittest.main()
