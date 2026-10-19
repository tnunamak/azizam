"""The one place every log line leaves the gateway (decision D44).

A credential reaches a log by being sent upstream: a server echoes what it was sent, in a body, an error, a reason
phrase or a header, and some code or some library logs it. Finding each such call (rounds r3 to r7) was never finished,
because the next call is always one the finder did not know. This module is the boundary that does not depend on finding
them. It is installed once, at startup, and it covers every logger, every handler and every stream, including the ones
added later (uvicorn builds its own after startup), because it works where all of them start: when a record is made.

* **Records.** The record factory cleans every record the moment it exists, before any handler sees it: the message is
  formatted once and scrubbed (``gateway.log_scrub``); the exception is reduced to its type and the file, line and
  function of each frame (never the exception text, never a local value: they are where an upstream body ends up); the
  stack text and any text passed in ``extra`` are scrubbed. ``logging.exception``, ``logger.warning(..., exc_info=True)``,
  ``logging.getLogger(...).error`` and every library logger go through it.
* **HTTP client libraries.** ``httpx``, ``httpcore`` and their kin are pinned to WARNING, and whatever they still emit is cut
  to a method, an origin and a status: their INFO and DEBUG lines carry the upstream reason phrase, the response headers
  and the URL with its query.
* **Access lines.** ``uvicorn.access`` loses the query string of the request path (a client may put a key in it).
* **Streams.** ``sys.stdout`` and ``sys.stderr`` are wrapped so that what ``print`` writes (and what the interpreter writes
  for an uncaught exception, through the hooks below) is scrubbed a line at a time.
* **Warnings and uncaught exceptions.** ``warnings`` is routed into ``logging``; ``sys.excepthook`` and
  ``threading.excepthook`` log through it.

What this does not do, and what the other layers are for: a message that is about an upstream is cut down to its
registered secrets only (the scrubber can remove only what it was told), so a gateway call must not put upstream text in a
message at all (``log_scrub.describe_*``; ``tests/test_no_upstream_text_in_logs.py``), and an exception that carries an
upstream excerpt must keep it out of ``str()`` (``gateway.engine_protocol.errors``).
"""

from __future__ import annotations

import codecs
import logging
import sys
import threading
import traceback
from typing import Any, Mapping
from urllib.parse import urlsplit

from gateway import log_scrub

LIMIT = 4000
WITHHELD_RECORD = "[log line withheld: it could not be cleaned]"

# Third-party HTTP stacks. Their lines carry what an upstream said (reason phrases, headers) and what was sent (URLs).
CLIENT_LOGGERS = ("httpx", "httpcore", "h11", "h2", "hpack", "hyperframe", "urllib3", "requests", "aiohttp", "websockets",
                  "openai", "anthropic", "anyio", "asyncio.selector_events")

_CLEAN = "_azizam_egress_clean"
_STANDARD = frozenset(logging.LogRecord("x", logging.INFO, "x", 1, "x", (), None).__dict__) | {"message", "asctime", "taskName"}


# -- exceptions -------------------------------------------------------------------------------------------------------


def _blank(root: traceback.TracebackException) -> None:
    """Remove from a built traceback (and from every cause, context and group member) the text an exception holds."""
    pending, seen = [root], set()
    while pending:
        te = pending.pop()
        if te is None or id(te) in seen:
            continue
        seen.add(id(te))
        for name, value in (("_str", ""), ("__notes__", None), ("msg", ""), ("text", None)):
            if hasattr(te, name):
                try:
                    setattr(te, name, value)
                except AttributeError:
                    pass
        pending += [getattr(te, "__cause__", None), getattr(te, "__context__", None), *(getattr(te, "exceptions", None) or [])]


def exception_text(exc_info: Any) -> str:
    """The traceback of ``exc_info`` as type and location only (an empty string when there is none).

    Each exception is its type; each frame is its file, line and function and the source line of the code (ours, not data).
    ``str(exc)`` (where an upstream's body or an HTTP error's URL ends up), a syntax error's text, an exception's notes and
    every local value are dropped, for the exception and for each exception it was raised from or during.
    """
    if not exc_info or not isinstance(exc_info, tuple) or exc_info[1] is None:
        return ""
    try:
        te = traceback.TracebackException(exc_info[0], exc_info[1], exc_info[2], capture_locals=False)
        _blank(te)
        return "".join(te.format()).rstrip("\n")
    except Exception:  # noqa: BLE001 - an exception that cannot be described names only its class
        return type(exc_info[1]).__name__


# -- records ----------------------------------------------------------------------------------------------------------


def _is_client_logger(name: str) -> bool:
    return any(name == known or name.startswith(known + ".") for known in CLIENT_LOGGERS)


def _client_line(record: logging.LogRecord) -> str:
    """What a library HTTP client's record may say: a method, an origin and a status, or only that it spoke."""
    args = record.args
    # httpx: 'HTTP Request: %s %s "%s %d %s"' % (method, url, http_version, status_code, reason_phrase)
    if record.name == "httpx" and isinstance(args, tuple) and len(args) == 5 and isinstance(args[3], int):
        return f"HTTP request: {str(args[0])[:16]} {log_scrub.describe_origin(args[1])} -> {args[3]}"
    return f"{record.name}: {record.levelname.lower()} event (details withheld)"


def _strip_query(args: Any) -> Any:
    """``uvicorn.access`` args are (client, method, full_path, http_version, status); the path loses its query."""
    if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
        return (args[0], args[1], urlsplit(args[2]).path or "/", args[3], args[4])
    return args


def clean(record: logging.LogRecord) -> logging.LogRecord:
    """Reduce ``record`` to scrubbed text: no exception text, no raw arguments, no unscrubbed extra. Idempotent."""
    if record.__dict__.get(_CLEAN):
        return record
    if record.name == "uvicorn.access":
        record.args = _strip_query(record.args)
    if _is_client_logger(record.name):
        record.msg, record.args = _client_line(record), None
    else:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a message that cannot be formatted says only that
            message = f"{record.name}: a log message that could not be formatted"
        record.msg, record.args = log_scrub.scrub(message, limit=LIMIT), None
    if record.exc_info:
        record.exc_text = log_scrub.scrub(exception_text(record.exc_info), limit=LIMIT)
        record.exc_info = None
    elif record.exc_text:
        record.exc_text = log_scrub.scrub(record.exc_text, limit=LIMIT)
    if record.stack_info:
        record.stack_info = log_scrub.scrub(record.stack_info, limit=LIMIT)
    for key in [key for key in record.__dict__ if key not in _STANDARD]:
        value = record.__dict__[key]
        record.__dict__[key] = log_scrub.scrub(value, limit=LIMIT) if isinstance(value, (str, bytes, Mapping, list, tuple, set)) else value
    record.__dict__[_CLEAN] = True
    return record


def _clean_or_withhold(record: logging.LogRecord) -> logging.LogRecord:
    try:
        return clean(record)
    except Exception:  # noqa: BLE001 - a record that cannot be cleaned is not emitted as it is
        record.msg, record.args, record.exc_info, record.exc_text, record.stack_info = WITHHELD_RECORD, None, None, None, None
        record.__dict__[_CLEAN] = True
        return record


class EgressFilter(logging.Filter):
    """``clean`` as a ``logging.Filter``, for a handler that is built without the record factory (a test's own)."""

    def filter(self, record: logging.LogRecord) -> bool:
        clean(record)
        return True


# -- streams ----------------------------------------------------------------------------------------------------------


class GuardedStream:
    """A text stream that scrubs what is written to it, a line at a time (a value split across two ``write`` calls is found)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._pending = ""
        self._lock = threading.Lock()
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")   # for ``buffer`` writes
        self.decoder_lock = threading.Lock()

    @property
    def inner(self) -> Any:
        return self._inner

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError(f"write() argument must be str, not {type(text).__name__}")
        with self._lock:
            self._pending += text
            if "\n" in self._pending:
                head, _, self._pending = self._pending.rpartition("\n")
                self._emit(head + "\n")
            elif len(self._pending) > 1 << 16:
                head, self._pending = self._pending, ""
                self._emit(head)
        return len(text)

    def _emit(self, text: str) -> None:
        self._inner.write(log_scrub.scrub(text, limit=1 << 30))

    def flush(self, *, final: bool = False) -> None:
        if final:   # the process is done with this stream: a character still cut is written as the replacement mark
            with self.decoder_lock:
                tail = self.decoder.decode(b"", final=True)
            self.write(tail)
        with self._lock:
            if self._pending:
                head, self._pending = self._pending, ""
                self._emit(head)
        self._inner.flush()

    def writelines(self, lines: Any) -> None:
        for line in lines:
            self.write(line)

    @property
    def buffer(self) -> Any:
        return _GuardedBytes(self)

    def writable(self) -> bool:
        return True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _GuardedBytes:
    """``stream.buffer`` of a guarded stream: bytes are decoded, scrubbed and written through the text path.

    A character cut between two ``write`` calls is held until its end arrives (one decoder per stream), so a credential
    that has a multi-byte character in it is found whole.
    """

    def __init__(self, stream: GuardedStream) -> None:
        self._stream = stream

    def write(self, data: bytes) -> int:
        with self._stream.decoder_lock:
            text = self._stream.decoder.decode(bytes(data))
        self._stream.write(text)
        return len(data)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream.inner, name)


# -- install ----------------------------------------------------------------------------------------------------------

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {}


def installed() -> bool:
    return bool(_STATE)


def _excepthook(kind: type, value: BaseException, tb: Any) -> None:
    logging.getLogger("gateway.crash").critical("uncaught exception", exc_info=(kind, value, tb))


def _thread_excepthook(args: Any) -> None:
    logging.getLogger("gateway.crash").critical(
        "uncaught exception in a thread", exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def install(*, streams: bool = True) -> None:
    """Install the egress (idempotent). Called once at startup, before anything can log."""
    with _LOCK:
        if _STATE:
            return
        previous_factory = logging.getLogRecordFactory()

        def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            return _clean_or_withhold(previous_factory(*args, **kwargs))

        logging.setLogRecordFactory(factory)
        _STATE["factory"] = (previous_factory, factory)
        # A record can skip the factory (``logging.makeLogRecord`` fills the fields after the factory ran) and reach a
        # handler that was added after this install (a later ``propagate = False`` handler has no filter of ours). Both
        # are closed at the places every record passes: ``makeLogRecord`` and ``Handler.handle``.
        previous_make, previous_handle = logging.makeLogRecord, logging.Handler.handle

        def make_log_record(dictionary: Any) -> logging.LogRecord:
            record = previous_make(dictionary)
            record.__dict__.pop(_CLEAN, None)
            return _clean_or_withhold(record)

        def handle(self: logging.Handler, record: logging.LogRecord) -> Any:
            return previous_handle(self, _clean_or_withhold(record))

        logging.makeLogRecord, logging.Handler.handle = make_log_record, handle  # type: ignore[method-assign]
        _STATE["patches"] = (previous_make, make_log_record, previous_handle, handle)
        # The factory has cleaned every record already. The filter on the handlers that exist now is the second line for a
        # record made some other way (``logging.makeLogRecord``, a record handed over from another process).
        for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
            for handler in logging.getLogger(name).handlers:
                if not any(isinstance(existing, EgressFilter) for existing in handler.filters):
                    handler.addFilter(EgressFilter())
        _STATE["levels"] = {}
        for name in CLIENT_LOGGERS:
            library = logging.getLogger(name)
            _STATE["levels"][name] = library.level
            library.setLevel(logging.WARNING)
        _STATE["warnings_were_captured"] = getattr(logging, "_warnings_showwarning", None) is not None
        logging.captureWarnings(True)
        _STATE["hooks"] = (sys.excepthook, threading.excepthook)
        sys.excepthook, threading.excepthook = _excepthook, _thread_excepthook
        _STATE["streams"] = None
        if streams:
            originals = (sys.stdout, sys.stderr)
            wrapped = tuple(GuardedStream(stream) if stream is not None and not isinstance(stream, GuardedStream) else stream
                            for stream in originals)
            sys.stdout, sys.stderr = wrapped
            _STATE["streams"] = (originals, wrapped)


def uninstall() -> None:
    """Take the egress out (a test)."""
    with _LOCK:
        if not _STATE:
            return
        previous_factory, factory = _STATE["factory"]
        if logging.getLogRecordFactory() is factory:
            logging.setLogRecordFactory(previous_factory)
        previous_make, make_log_record, previous_handle, handle = _STATE["patches"]
        if logging.makeLogRecord is make_log_record:
            logging.makeLogRecord = previous_make
        if logging.Handler.handle is handle:
            logging.Handler.handle = previous_handle  # type: ignore[method-assign]
        for name, level in _STATE["levels"].items():
            logging.getLogger(name).setLevel(level)
        if not _STATE["warnings_were_captured"]:
            logging.captureWarnings(False)
        sys.excepthook, threading.excepthook = _STATE["hooks"]
        if _STATE["streams"] is not None:
            originals, wrapped = _STATE["streams"]
            for stream in wrapped:
                if isinstance(stream, GuardedStream):
                    stream.flush(final=True)
            if (sys.stdout, sys.stderr) == wrapped:
                sys.stdout, sys.stderr = originals
        _STATE.clear()
