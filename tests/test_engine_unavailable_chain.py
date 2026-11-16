"""HTTP answers and suppressed exception context must not inherit retryability."""

import pytest

from gateway.engine_protocol.errors import TransportFailure, UpstreamError
from gateway.engine_unavailable import Verdict, classify


def test_engine_json_answer_is_terminal_over_transport_cause():
    cause = TransportFailure("unreachable", "synthetic refusal")
    try:
        raise UpstreamError(503, "Upstream answered.", body=b'{"error":"busy"}') from cause
    except UpstreamError as error:
        assert classify(error) is None


def test_proxy_answer_wins_over_transport_cause():
    try:
        raise UpstreamError(502, "Upstream answered.", body=b"<html>bad gateway</html>") from TransportFailure("dns", "synthetic lookup")
    except UpstreamError as error:
        assert classify(error) == Verdict(True, "proxy")


def test_from_none_does_not_inherit_unreachable_context():
    try:
        try:
            raise TransportFailure("unreachable", "synthetic refusal")
        except TransportFailure:
            raise TransportFailure("timeout", "synthetic timeout") from None
    except TransportFailure as error:
        assert error.__context__ is not None
        assert error.__cause__ is None
        assert error.__suppress_context__
        assert classify(error) is None


@pytest.mark.parametrize("body", [
    b" " * 2048 + b'{"error":"busy"}',
    b" " * 2049,
], ids=["json-after-prefix", "long-whitespace"])
def test_truncated_whitespace_is_inconclusive(body):
    assert classify(UpstreamError(504, "Upstream answered.", body=body)) is None


@pytest.mark.parametrize("body", [
    b'"service unavailable"',
    b"  " + b'"gateway timeout"',
    b'{"error":"bad gateway"}',
    b'["service unavailable"]',
], ids=["string", "spaced-string", "object", "array"])
def test_json_answer_is_not_a_proxy_page(body):
    assert classify(UpstreamError(503, "Upstream answered.", body=body)) is None


@pytest.mark.parametrize("body", [b"", b" " * 2048, b"<html>bad gateway</html>"],
                         ids=["empty", "bounded-whitespace", "html"])
def test_proxy_controls(body):
    assert classify(UpstreamError(502, "Upstream answered.", body=body)) == Verdict(True, "proxy")


@pytest.mark.parametrize("kind,retryable", [("unreachable", True), ("dns", False)])
def test_direct_transport_controls(kind, retryable):
    reason = "refused" if retryable else "dns"
    assert classify(TransportFailure(kind, "synthetic failure")) == Verdict(retryable, reason)


def test_runner_unreachable_control():
    error = RuntimeError("synthetic runner failure")
    error.code = "upstream_unavailable"
    error.upstream_status = 502
    assert classify(error) == Verdict(True, "refused")
    error.upstream_status = 503
    assert classify(error) is None


def test_unsuppressed_context_is_followed():
    try:
        try:
            raise TransportFailure("unreachable", "synthetic refusal")
        except TransportFailure:
            raise RuntimeError("synthetic wrapper")
    except RuntimeError as error:
        assert classify(error) == Verdict(True, "refused")


def test_explicit_cause_wins_over_context():
    error = RuntimeError("synthetic wrapper")
    error.__context__ = TransportFailure("unreachable", "synthetic refusal")
    error.__cause__ = TransportFailure("dns", "synthetic lookup")
    assert classify(error) == Verdict(False, "dns")


def test_cycles_terminate():
    first, second = RuntimeError("first"), RuntimeError("second")
    first.__cause__ = second
    second.__cause__ = first
    assert classify(first) is None
    first.__cause__ = None
    first.__suppress_context__ = False
    first.__context__ = first
    assert classify(first) is None
