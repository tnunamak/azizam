"""Decision failures retain a terminal outcome in durable Activity receipts."""
from types import SimpleNamespace

import pytest

import proxy
from gateway import decisions_api, media_jobs, receipt_store


@pytest.mark.parametrize(("http_status", "expected"), [
    (200, "succeeded"), (422, "failed"), (529, "failed"), (499, "disconnected"),
])
def test_decision_receipt_records_canonical_outcome(tmp_path, monkeypatch, http_status, expected):
    path = tmp_path / "receipts.db"
    facts_path = tmp_path / "media_jobs.db"
    receipt_store.init_db(path)
    media_jobs.init_db(facts_path)
    original_record = receipt_store.record
    monkeypatch.setattr(proxy.receipt_store, "record", lambda receipt: original_record(receipt, path=path))
    monkeypatch.setattr(proxy.media_jobs, "DB_PATH", facts_path)
    result = decisions_api.DecisionReceipt(
        protocol="typesafe", intent="decision.systemone", request_id="req_decision_outcome",
        path="/v1/systemone", requested_model="decide", resolved_model=None, backend="decisions",
        question_count=1, latency_ms=12.5, status_code=http_status, remote=False,
        cost_usd=0, engine=None,
        error_code="decision_backend_unavailable" if http_status == 529 else None,
    )
    proxy._record_decision_receipt(SimpleNamespace(identity=None), result)
    stored = receipt_store.get(result.request_id, audience="admin", path=path)
    assert stored["status"] == expected
    assert stored["status_code"] == http_status
    assert stored["latency_ms"] == 12.5
    if http_status == 529:
        assert stored["rejected_inputs"]["request"] == "decision_backend_unavailable"
