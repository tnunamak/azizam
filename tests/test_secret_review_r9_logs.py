"""Round 9 of the secret handling, logs and engine output: defence in depth (decision D45).

Logs are readable on the host only, by the owner whose credentials they are, so these gaps do not block a release. They were
cheap to close, and each closes with a test:

* the engine output pump (``gateway/engine_supervisor.py``) removed raw secret bytes and a fixed key pattern only; a base64
  form of a secret survived in the 0600 engine log;
* a record made before the egress was installed, or by ``logging.makeLogRecord``, reached a handler added after the install
  (a ``propagate = False`` handler has no filter of ours) without being cleaned;
* a credential holding a multi-byte character, written as bytes to ``sys.stderr.buffer`` in two pieces, was decoded piece by
  piece, so the scrubber saw two broken halves.

Not changed, and why (the limit is in D45): a secret of fewer than four characters is removed only where it stands alone
(between characters that are not letters or digits), because removing it everywhere would take letters out of every line;
refusing to register short secrets would leave them in every line.
"""

from __future__ import annotations

import base64
import io
import logging
from urllib.parse import quote, quote_plus

import pytest

from gateway import engine_supervisor, log_egress, log_scrub

SECRET = "sk-r9-ENGINE/secret+value=7741"


@pytest.fixture(autouse=True)
def registered():
    log_scrub.clear()
    log_scrub.remember(SECRET, "r9-test")
    yield
    log_scrub.clear()


def _forms() -> list[str]:
    raw = SECRET.encode()
    out = [SECRET, quote(SECRET, safe=""), quote_plus(SECRET)]
    for encoder in (base64.b64encode, base64.urlsafe_b64encode):
        text = encoder(raw).decode()
        out += [text, text.rstrip("=")]
    return out


# ---------------------------------------------------------------------------
# The engine output pump
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("form", _forms(), ids=[f"form{index}" for index in range(len(_forms()))])
def test_the_engine_output_pump_removes_every_form_the_log_scrubber_knows(tmp_path, form):
    """A base64 form of the secret survived in the engine's 0600 log (the review of r8)."""
    log = tmp_path / "engine.log"
    pump = engine_supervisor.LogPump(log, limit=1 << 20, secrets=[SECRET])
    pump.run(io.BytesIO(f"loading model\nAuthorization echoed: {form}\ndone\n".encode()))
    text = log.read_text()
    assert form not in text and "loading model" in text and "done" in text
    assert "[redacted]" in text


def test_the_engine_output_pump_keeps_removing_the_raw_value_and_the_fixed_key_pattern(tmp_path):
    log = tmp_path / "engine.log"
    pump = engine_supervisor.LogPump(log, limit=1 << 20, secrets=[SECRET, "abc"])
    pump.run(io.BytesIO(f"key {SECRET} and opk_abc123 and abc stays\n".encode()))
    text = log.read_text()
    assert SECRET not in text and "opk_abc123" not in text
    assert "abc stays" in text, "a secret of fewer than four characters is not a pump source"


# ---------------------------------------------------------------------------
# The log egress
# ---------------------------------------------------------------------------

@pytest.fixture
def egress():
    log_egress.uninstall()
    log_egress.install(streams=False)
    yield
    log_egress.uninstall()
    log_egress.install(streams=False)


def _late_handler(name: str) -> tuple[logging.Logger, logging.Handler, io.StringIO]:
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)       # no filter: added after the install
    logger = logging.getLogger(name)
    logger.addHandler(handler)
    logger.propagate = False
    return logger, handler, sink


def test_a_record_made_before_the_install_is_cleaned_at_a_handler_added_after_it(egress):
    """The review's repro: a record from before the install, to a later ``propagate = False`` handler."""
    log_egress.uninstall()                                   # the factory is not in place yet
    record = logging.getLogger("gateway.r9.early").makeRecord("gateway.r9.early", logging.WARNING, __file__, 1,
                                                              "echo %s", (SECRET,), None)
    log_egress.install(streams=False)
    logger, handler, sink = _late_handler("gateway.r9.early")
    try:
        logger.handle(record)
    finally:
        logger.removeHandler(handler)
        logger.propagate = True
    assert "echo" in sink.getvalue() and SECRET not in sink.getvalue()


def test_a_record_from_make_log_record_is_cleaned_at_the_handler(egress):
    """``logging.makeLogRecord`` fills the fields after the factory ran."""
    record = logging.makeLogRecord({"name": "gateway.r9.dict", "levelno": logging.WARNING, "levelname": "WARNING",
                                    "msg": "echo %s", "args": (SECRET,)})
    logger, handler, sink = _late_handler("gateway.r9.dict")
    try:
        logger.handle(record)
    finally:
        logger.removeHandler(handler)
        logger.propagate = True
    assert "echo" in sink.getvalue() and SECRET not in sink.getvalue()


def test_the_patches_come_out_with_the_install():
    log_egress.uninstall()
    plain_make, plain_handle = logging.makeLogRecord, logging.Handler.handle
    log_egress.install(streams=False)
    assert logging.makeLogRecord is not plain_make and logging.Handler.handle is not plain_handle
    log_egress.uninstall()
    assert logging.makeLogRecord is plain_make and logging.Handler.handle is plain_handle
    log_egress.install(streams=False)


def test_a_character_cut_between_two_buffer_writes_is_not_corrupted():
    """A credential with a multi-byte character, written as bytes in two pieces, is found whole."""
    secret = "sk-r9-café-über-7741"
    log_scrub.remember(secret, "r9-utf8")
    encoded = secret.encode("utf-8")
    cut = encoded.index("é".encode("utf-8")) + 1        # in the middle of the two-byte character
    inner = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    stream = log_egress.GuardedStream(inner)
    stream.buffer.write(b"echo " + encoded[:cut])
    stream.buffer.write(encoded[cut:] + b" done\n")
    stream.flush()
    inner.flush()
    out = inner.buffer.getvalue().decode("utf-8")
    assert "echo" in out and "done" in out
    assert secret not in out and "�" not in out, out


def test_a_character_still_cut_when_the_stream_ends_is_written_as_the_replacement_mark():
    inner = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    stream = log_egress.GuardedStream(inner)
    stream.buffer.write("café".encode("utf-8")[:-1])
    stream.flush(final=True)
    inner.flush()
    assert inner.buffer.getvalue().decode("utf-8") == "caf�"
