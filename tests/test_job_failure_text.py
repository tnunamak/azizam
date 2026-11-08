"""Which text a failed job stores is data of the frontend row (``runtime_policy.job_failure_text``).

``adapter`` (the default) stores the adapter's own message, as the 3D route always did. ``category_copy`` stores the fixed
sentence of the failure's category, as the image job routes did: an engine's words may echo a prompt, and the job row is
read by listings and History.
"""
from __future__ import annotations

import copy

import pytest
import yaml
from starlette.responses import JSONResponse

import proxy
from gateway import frontend_shapes
from gateway.frontend_shapes import media_job


def _row(row_id):
    return {row.id: row for row in frontend_shapes.bundled_rows()}[row_id]


def test_the_image_job_row_stores_the_category_sentence_and_every_other_row_the_adapter_message():
    assert _row("image-jobs").job_failure_text == "category_copy"
    others = {row.id: row.job_failure_text for row in frontend_shapes.bundled_rows() if row.id != "image-jobs"}
    assert set(others.values()) == {"adapter"}, others


def test_a_row_value_that_is_not_one_of_the_two_words_is_refused_at_load():
    document = yaml.safe_load(frontend_shapes.ROWS_PATH.read_text(encoding="utf-8"))
    for bad in ("both", "", None, 3):
        broken = copy.deepcopy(document)
        broken[frontend_shapes.ROWS_KEY][0]["routes"][0]["runtime_policy"]["job_failure_text"] = bad
        with pytest.raises(frontend_shapes.RowError, match="job_failure_text"):
            frontend_shapes.parse_rows(broken)


def test_two_routes_of_one_row_cannot_disagree():
    document = yaml.safe_load(frontend_shapes.ROWS_PATH.read_text(encoding="utf-8"))
    row = next(r for r in document[frontend_shapes.ROWS_KEY] if len(r["routes"]) > 1)
    row["routes"][0].setdefault("runtime_policy", {})["job_failure_text"] = "category_copy"
    with pytest.raises(frontend_shapes.RowError, match="job_failure_text"):
        frontend_shapes.parse_rows(document)


@pytest.mark.parametrize(("status", "category", "upstream_status"), [(502, "upstream", 502), (504, "timeout", None), (429, "capacity", None)])
def test_category_copy_matches_what_the_old_image_job_stored(status, category, upstream_status):
    old_message, old_info = proxy._media_job_failure(JSONResponse({"error": {"message": "engine words"}}, status_code=status))
    message, code = media_job.stored_failure("category_copy", category, "engine words", "upstream_job_failed", upstream_status)
    assert (message, code) == (old_message, old_info["error_code"])


def test_the_adapter_mode_keeps_the_adapters_words_and_a_category_without_a_sentence_keeps_them_too():
    assert media_job.stored_failure("adapter", "upstream", "engine words", "x_code", 502) == ("engine words", "x_code")
    assert media_job.stored_failure("category_copy", "internal", "our words", "internal_error", None) == ("our words", "internal_error")
