"""Non-retryable job failures must override status-based client retries."""
import pytest

from gateway.frontend_shapes._base import failure_response
from gateway.workflow_runtime_types import RunFailure


def test_timeout_disables_client_retry():
    response = failure_response(RunFailure("timeout", "timeout", "Timed out", retryable=False),
                                fallback="Failed", retry_after_s=5)
    assert response.status_code == 504
    assert response.headers["x-should-retry"] == "false"
    assert "retry-after" not in response.headers


def test_retryable_capacity_preserves_retry_after():
    response = failure_response(RunFailure("capacity", "capacity", "Busy", retryable=True),
                                fallback="Failed", retry_after_s=5)
    assert response.status_code == 503
    assert "x-should-retry" not in response.headers
    assert response.headers["retry-after"] == "5"


def test_invalid_input_keeps_client_failure_unchanged():
    response = failure_response(RunFailure("invalid_input", "client", "Invalid input", retryable=False),
                                fallback="Failed")
    assert response.status_code == 400
    assert "x-should-retry" not in response.headers


def test_upstream_status_disables_client_retry():
    response = failure_response(RunFailure("upstream", "upstream", "Unavailable",
                                          retryable=False, upstream_status=503), fallback="Failed")
    assert response.status_code == 503
    assert response.headers["x-should-retry"] == "false"


@pytest.mark.parametrize("status", [200, 400, 403, 408, 409, 429, 499, 500, 599])
def test_only_final_retry_status_adds_header_without_changing_body(status):
    fixed = failure_response(RunFailure("failure", "upstream", "Failed", upstream_status=status),
                             fallback="Fallback")
    retryable = failure_response(RunFailure("failure", "upstream", "Failed",
                                           retryable=True, upstream_status=status), fallback="Fallback")
    expected = status if status >= 400 else 502
    assert fixed.status_code == retryable.status_code == expected
    assert fixed.body == retryable.body
    assert {k: v for k, v in fixed.headers.items() if k != "x-should-retry"} == dict(retryable.headers)
    assert "x-should-retry" not in retryable.headers
    assert fixed.headers.get("x-should-retry") == (
        "false" if expected in (408, 409, 429) or expected >= 500 else None
    )


def test_missing_failure_keeps_fallback_unchanged():
    response = failure_response(None, fallback="Fallback", retry_after_s=5)
    assert response.status_code == 500
    assert "x-should-retry" not in response.headers
    assert "retry-after" not in response.headers
