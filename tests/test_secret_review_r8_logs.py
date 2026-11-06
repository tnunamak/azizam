"""Round 8 of the secret handling, logs: what the review of phase1-r7 (6d7f21d4) found (decision D44).

* HIGH 1: a protocol failure was logged with ``str(UpstreamError)``, which held an excerpt of the engine's reply; the
  structural scanner missed ``getLogger(...).warning``, implicit ``logging.exception`` tracebacks and aliased
  ``traceback.format_exc()``.
* HIGH 2: ``describe_response`` copied 64 characters of the upstream's Content-Type into the line.
* HIGH 3: library loggers bypassed the rule: httpx logs the reason phrase of the reply, httpcore logs its headers.

The fix is a boundary, not a longer list: every record is cleaned when it is made (``gateway/log_egress.py``), the
upstream's words are kept apart in the error (``UpstreamError.excerpt``), and a line names a media type only from a list.
``CANARY`` is a credential the gateway sent and registered; ``WORDS`` is text an upstream wrote, which nothing registered.
"""

from __future__ import annotations

import asyncio
import io
import logging
import sys
import traceback
import warnings

import httpcore
import httpx
import pytest

import proxy
from gateway import log_scrub, runtime_secrets
from gateway.engine_protocol import dispatch, executor, inventory
from gateway.engine_protocol.errors import UpstreamError
from tests.support.fake_upstream import HandlerTransport
from tests.test_engine_protocol_executor import MODELS, conn, make, req, run
from tests.test_engine_protocol_round3 import _registry_of, fresh_inventory, ollama_doc  # noqa: F401

CANARY = "sk-r8-CANARY-credential-4471"
WORDS = "R8-UPSTREAM-WORDS-9912"


@pytest.fixture(autouse=True)
def registered():
    log_scrub.clear()
    log_scrub.remember(CANARY, "r8-test")
    yield
    log_scrub.clear()


def _upstream_error(text: str = WORDS) -> UpstreamError:
    ex, _ = make(MODELS, lambda r: httpx.Response(500, text=text))
    with pytest.raises(UpstreamError) as raised:
        run(ex.invoke(conn(), req("models")))
    return raised.value


# ---------------------------------------------------------------------------
# HIGH 1: the engine's words are in the error, not in its text
# ---------------------------------------------------------------------------

def test_an_upstream_error_never_renders_what_the_engine_said():
    error = _upstream_error()
    assert WORDS not in str(error) and WORDS not in repr(error) and WORDS not in str(error.args)
    assert WORDS in getattr(error, "excerpt", ""), "the engine's words stay available, apart from the text"


def test_a_protocol_failure_response_log_line_holds_no_engine_words(caplog):
    caplog.set_level(logging.DEBUG)
    response = proxy._protocol_failure(_upstream_error())
    assert response.status_code in (400, 404, 409, 422, 429, 502, 503)
    assert WORDS not in caplog.text and WORDS not in response.body.decode()


def test_the_inventory_failure_log_line_holds_no_engine_words(monkeypatch, caplog, fresh_inventory, ollama_doc):
    from gateway.engine_protocol import description

    monkeypatch.setattr(description, "load_registry", lambda directory=None: {"ollama-native": ollama_doc})
    failure = _upstream_error()

    async def invoke(self, op, body):
        raise failure

    monkeypatch.setattr(dispatch.Runtime, "invoke", invoke)
    caplog.set_level(logging.DEBUG)
    assert asyncio.run(inventory.models(_registry_of(1))) == []
    assert inventory.problems()
    assert "Protocol inventory failed" in caplog.text, "the line is still written"
    assert WORDS not in caplog.text


# ---------------------------------------------------------------------------
# HIGH 2: a media type is named from a list
# ---------------------------------------------------------------------------

def test_a_line_about_a_reply_names_a_media_type_only_from_the_fixed_list():
    reply = httpx.Response(502, headers={"content-type": f"{CANARY}/x; charset=utf-8"}, content=b"{}")
    line = log_scrub.describe_response(reply, "refused")
    assert CANARY not in line and WORDS not in line
    assert line == "refused: status 502, other, 2 bytes"
    assert log_scrub.describe_response(httpx.Response(200, headers={"content-type": "application/json; charset=utf-8"}, content=b"{}")) \
        == "status 200, application/json, 2 bytes"
    assert log_scrub.describe_response(httpx.Response(200, content=b"")) == "status 200, no content type, 0 bytes"


def test_a_line_about_a_reply_never_holds_the_reason_phrase():
    reply = httpx.Response(401, extensions={"reason_phrase": WORDS.encode()}, content=b"")
    assert WORDS not in log_scrub.describe_response(reply, "refused")


# ---------------------------------------------------------------------------
# HIGH 3: library loggers
# ---------------------------------------------------------------------------

def test_httpx_info_lines_hold_no_reason_phrase_or_query(caplog):
    caplog.set_level(logging.INFO, logger="httpx")

    def handler(request):
        return httpx.Response(401, extensions={"reason_phrase": CANARY.encode()}, content=b"")

    with httpx.Client(transport=HandlerTransport(handler)) as client:
        client.get(f"http://host.invalid:8080/path?token={CANARY}")
    assert any(record.name == "httpx" for record in caplog.records), "the line is still written"
    assert CANARY not in caplog.text
    assert "host.invalid" in caplog.text and "401" in caplog.text, "method, host and status are kept"


def test_httpcore_debug_lines_hold_no_response_headers(caplog):
    caplog.set_level(logging.DEBUG, logger="httpcore")
    backend = httpcore.MockBackend([f"HTTP/1.1 401 Unauthorized\r\nwww-authenticate: Bearer {CANARY}\r\ncontent-length: 0\r\n\r\n".encode()])
    with httpcore.ConnectionPool(network_backend=backend) as pool:
        pool.request("GET", f"http://host.invalid/path?token={CANARY}")
    assert any(record.name.startswith("httpcore") for record in caplog.records), "httpcore wrote its trace"
    assert CANARY not in caplog.text


def test_the_http_client_loggers_are_pinned_to_warning():
    for name in ("httpx", "httpcore", "urllib3", "h11", "h2", "hpack"):
        assert logging.getLogger(name).level >= logging.WARNING, name


# ---------------------------------------------------------------------------
# A: one test through every logger family
# ---------------------------------------------------------------------------

def _raise_with(text: str) -> ValueError:
    try:
        raise ValueError(text)
    except ValueError as error:
        return error


def test_no_canary_reaches_a_log_handler_through_any_logger_family(caplog, monkeypatch):
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG)
    emitted: list[str] = []

    def emit(label, call):
        emitted.append(label)
        call(f"[{label}]")

    def opened(name):
        """The logger as a bare process has it: records reach the root handlers (another test may have configured uvicorn's)."""
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "propagate", True)
        monkeypatch.setattr(logger, "level", logging.DEBUG)
        monkeypatch.setattr(logger, "disabled", False)
        return logger

    named = logging.getLogger("gateway.r8.family")
    alias = logging.getLogger("gateway.r8.alias")
    caplog.set_level(logging.DEBUG, logger="gateway.r8.family")
    emit("getLogger(...).warning", lambda t: logging.getLogger("gateway.r8.inline").warning("%s upstream said %s", t, CANARY))
    emit("logger alias", lambda t: alias.error(f"{t} upstream said {CANARY}"))
    emit("logger.info %-args", lambda t: named.info("%s upstream said %s and %s", t, CANARY, {"key": CANARY}))
    emit("logging.warning module function", lambda t: logging.warning("%s upstream said %s", t, CANARY))

    def exception_in_handler(t):
        try:
            raise ValueError(f"{WORDS} {CANARY}")
        except ValueError:
            named.exception("%s the call failed", t)
    emit("logger.exception (implicit traceback)", exception_in_handler)
    emit("exc_info=<exception>", lambda t: named.error("%s the call failed", t, exc_info=_raise_with(f"{WORDS} {CANARY}")))

    def chained_exception(t):
        try:
            try:
                raise ValueError(f"{WORDS} inner {CANARY}")
            except ValueError as inner:
                raise RuntimeError(f"{WORDS} outer") from inner
        except RuntimeError:
            named.exception("%s chained", t)
    emit("chained exceptions", chained_exception)

    def aliased_format_exc(t):
        fmt = traceback.format_exc
        try:
            raise ValueError(f"{CANARY}")
        except ValueError:
            named.error("%s failed:\n%s", t, fmt())
    emit("traceback.format_exc alias", aliased_format_exc)
    emit("stack_info and extra", lambda t: named.warning("%s where", t, stack_info=True, extra={"echo": CANARY}))

    def warned(t):
        logging.captureWarnings(False)  # pytest records warnings; route them into logging again, as the egress does at startup
        logging.captureWarnings(True)
        with warnings.catch_warnings():
            warnings.simplefilter("always")
            warnings.warn(f"{t} upstream said {CANARY}", UserWarning, stacklevel=1)
    emit("warnings", warned)
    emit("uvicorn.access query", lambda t: opened("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", f"/v1/models?tag={t}&api_key={CANARY}", "1.1", 200))

    def uvicorn_error(t):
        try:
            raise ValueError(f"{WORDS} {CANARY}")
        except ValueError:
            opened("uvicorn.error").exception("%s Exception in ASGI application", t)
    emit("uvicorn.error", uvicorn_error)
    emit("asyncio", lambda t: logging.getLogger("asyncio").error("%s Task exception was never retrieved\n%s", t, CANARY))

    def httpx_info(t):
        logging.getLogger("httpx").setLevel(logging.INFO)
        with httpx.Client(transport=HandlerTransport(
                lambda r: httpx.Response(401, extensions={"reason_phrase": CANARY.encode()}))) as client:
            client.get(f"http://host.invalid/x?k={CANARY}")
    emit("httpx INFO", httpx_info)

    def httpcore_debug(t):
        logging.getLogger("httpcore").setLevel(logging.DEBUG)
        backend = httpcore.MockBackend([f"HTTP/1.1 401 x\r\nx-echo: {CANARY}\r\ncontent-length: 0\r\n\r\n".encode()])
        with httpcore.ConnectionPool(network_backend=backend) as pool:
            pool.request("GET", f"http://host.invalid/x?k={CANARY}")
    emit("httpcore DEBUG", httpcore_debug)
    def urllib3_debug(t):
        logging.getLogger("urllib3.connectionpool").setLevel(logging.DEBUG)
        logging.getLogger("urllib3.connectionpool").debug("%s reply %s", t, CANARY)
    emit("urllib3 DEBUG", urllib3_debug)

    # print and the standard streams go through the same scrub
    from gateway import log_egress

    for stream_name in ("stdout", "stderr"):
        monkeypatch.setattr(sys, stream_name, io.StringIO())
    log_egress.uninstall()
    log_egress.install()
    try:
        emit("print", lambda t: print(f"{t} upstream said {CANARY}"))
        emit("print to stderr", lambda t: print(f"{t} upstream said {CANARY}", file=sys.stderr))
        emit("stderr.write split", lambda t: (sys.stderr.write(f"{t} " + CANARY[:9]), sys.stderr.write(CANARY[9:] + "\n")))

        def print_exc(t):
            try:
                raise ValueError(f"{t} {CANARY}")
            except ValueError:
                traceback.print_exc()
        emit("traceback.print_exc", print_exc)
        sys.stdout.flush()
        sys.stderr.flush()
        streams = sys.stdout.inner.getvalue() + sys.stderr.inner.getvalue()
    finally:
        log_egress.uninstall()
        log_egress.install(streams=False)

    def crash(t):
        try:
            raise ValueError(f"{WORDS} {CANARY}")
        except ValueError:
            sys.excepthook(*sys.exc_info())
            logging.getLogger("gateway.crash").error("%s", t)
    emit("sys.excepthook (uncaught exception)", crash)

    handlers_text = sink.getvalue() + "\n" + caplog.text
    root.removeHandler(handler)
    seen = handlers_text + streams
    # these families are rewritten to a method, a host and a status (or withheld), so their tag is not kept; a record is
    reached = {"uvicorn.access query": any(r.name == "uvicorn.access" for r in caplog.records),
               "httpx INFO": any(r.name == "httpx" for r in caplog.records),
               "httpcore DEBUG": any(r.name.startswith("httpcore") for r in caplog.records),
               "urllib3 DEBUG": any(r.name.startswith("urllib3") for r in caplog.records)}
    silent = [label for label in emitted if (f"[{label}]" not in seen if label not in reached else not reached[label])]
    leaked = {"canary": CANARY in handlers_text, "words": WORDS in handlers_text, "streams_canary": CANARY in streams}
    assert len(emitted) >= 20, emitted
    assert not [label for label in silent if label not in ("sys.excepthook (uncaught exception)",)], \
        f"these families never reached a handler, so the test proves nothing for them: {silent}\n{handlers_text}"
    assert not any(leaked.values()), f"leaked: {leaked}\n--- log output ---\n{handlers_text}\n--- streams ---\n{streams}"


def test_an_exception_is_logged_as_its_type_and_its_locations_only(caplog):
    named = logging.getLogger("gateway.r8.types")
    caplog.set_level(logging.DEBUG)
    try:
        raise ValueError(f"{WORDS}")
    except ValueError:
        named.exception("the call failed")
    assert "ValueError" in caplog.text and "test_an_exception_is_logged_as_its_type_and_its_locations_only" in caplog.text
    assert WORDS not in caplog.text


def test_the_withheld_line_replaces_a_message_when_a_credential_is_too_long_to_remove():
    log_scrub.remember("L" * (64 * 1024 + 1), "too-long")
    assert log_scrub.scrub("any line") == log_scrub.WITHHELD


def test_every_log_record_is_cleaned_by_the_record_factory_not_by_a_filter_on_one_handler(caplog):
    # a handler added later, with no filter, still gets a clean record
    extra = io.StringIO()
    handler = logging.StreamHandler(extra)
    logger = logging.getLogger("gateway.r8.late")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("late handler %s", CANARY)
    finally:
        logger.removeHandler(handler)
        logger.propagate = True
    assert "late handler" in extra.getvalue() and CANARY not in extra.getvalue()
    assert runtime_secrets.scrub(CANARY) == "[removed]"
