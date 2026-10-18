"""``executor: http-v1``: runs a protocol description's operations over HTTP.

A deep module with a small interface. A caller holds a ``ProtocolDescription``
and a ``Connection`` and calls:

    invoke(conn, request)                  -> Reply | StreamReply
    submit / poll / cancel / fetch_assets  -> the async job pattern
    preview(conn, request)                 -> what would be sent, secrets masked

Everything else (path templates, query strings, body mapping, multipart,
framing, selection, auth, redirects, budgets) is internal and driven by the
description. The executor has no engine knowledge: it interprets data.

Rules it enforces on every call
- A credential is bound to the connection's origin: it is sent only there. A
  redirect to another origin is refused before anything is sent to it; a
  same-origin redirect is followed up to ``max_redirects`` times.
- Path values are percent-encoded; a template cannot change the host or climb.
- Compressed answers are refused (identity is requested), so a small answer
  cannot expand past a budget.
- Budgets: request, upload, response, asset, event and stream bytes; JSON
  depth; selected values; assets per job; mapping time; and the connection's
  connect, first-byte and total timeouts. A stream's total time is checked at
  each chunk, and a stalled stream ends at the first-byte timeout.
- Error texts never hold a credential.

Overhead: measured by ``scripts/bench_engine_protocol.py``; the numbers are in
docs/design/ENGINE_PROTOCOL_EXECUTOR.md and ``tests/test_engine_protocol_overhead.py`` guards them.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import re
import secrets
import time
import unicodedata
from typing import Any, AsyncIterator, Callable, Mapping
from urllib.parse import quote, quote_plus, urljoin, urlsplit

from gateway.engine_protocol import framing, pointer
from gateway.engine_protocol.assets import AssetStore, served_media_type
from gateway.engine_protocol.budgets import Budgets
from gateway.engine_protocol.description import (
    Operation, ProtocolDescription, RequestSpec, ResponseSpec, StreamSpec, Terminal,
)
from gateway.engine_protocol.errors import (
    BudgetExceeded, MalformedResponse, MappingError, ProtocolError, RedirectRefused, TransportFailure, UnknownOperation,
    UnsupportedByAdapter, UpstreamError,
)
from gateway.engine_protocol.mapping import json_key, same_json
from gateway.engine_protocol.transport import HttpxTransport, Transport, WireRequest, WireResponse
from gateway.engine_protocol.types import (
    AssetRef, CanonicalRequest, Connection, JobRef, JobState, JobStatus, Reply, StreamReply,
)

MISSING = pointer.MISSING
MASK = "<configured>"
_ERROR_BODY_CAP = 64 * 1024
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_REDIRECTS = (301, 302, 303, 307, 308)
_SNIFF_BYTES = 32
_ENDED = frozenset({JobState.succeeded, JobState.failed, JobState.cancelled})


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or _DEFAULT_PORTS.get(scheme, 0)


def _label(url: str) -> str:
    scheme, host, port = _origin(url)
    return f"{scheme}://{host}:{port}"


class _Redact:
    """Around every public call: an error leaves with no secret in its text and none in the chain a log would print."""

    def __init__(self, secrets_: list[str]) -> None:
        self._secrets = secrets_

    def __enter__(self) -> "_Redact":
        return self

    def __exit__(self, kind: Any, exc: BaseException | None, tb: Any) -> None:
        if self._secrets and isinstance(exc, ProtocolError):
            exc.args = tuple(_scrub(arg, self._secrets) if isinstance(arg, str) else arg for arg in exc.args)
            exc.__cause__ = exc.__context__ = None  # the original may hold the full URL; the typed text says what happened


def _redacting(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    async def wrapper(self: Any, conn: Connection, *args: Any, **kwargs: Any) -> Any:
        with _Redact(_secrets(conn)):
            return await method(self, conn, *args, **kwargs)
    return wrapper


class HttpV1Executor:
    def __init__(self, description: ProtocolDescription, *, transport: Transport | None = None,
                 assets: AssetStore | None = None, budgets: Budgets | None = None) -> None:
        if description.executor != "http-v1":
            raise ValueError(f"'{description.id}' is run by {description.executor}, not http-v1")
        self.description = description
        self._transport = transport or HttpxTransport()
        self._assets = assets
        self.budgets = budgets or description.budgets
        self.boundary_factory: Callable[[], str] = lambda: secrets.token_hex(16)

    # --- public: one request ---------------------------------------------------------------------

    @_redacting
    async def invoke(self, conn: Connection, request: CanonicalRequest) -> Reply | StreamReply:
        op = self._operation(request.op, job=False)
        spec, response = op.request, op.response
        assert spec is not None and response is not None
        source = self._source(request)
        stream = self._stream_choice(response, source)
        started = time.perf_counter()
        deadline = asyncio.get_running_loop().time() + conn.timeouts.total_s
        secrets_ = _secrets(conn)
        try:
            async with asyncio.timeout(conn.timeouts.total_s):
                wire = await self._exchange(conn, spec, op, source, secrets_)
                if stream is not None:
                    return self._open_stream(wire, stream, source, deadline, request, secrets_)
                try:
                    return await self._finish(wire, response, source, request, started, secrets_)
                finally:
                    await wire.aclose()
        except TimeoutError:
            raise TransportFailure("timeout", f"The engine did not finish within {conn.timeouts.total_s:g} s.") from None

    @_redacting
    async def preview(self, conn: Connection, request: CanonicalRequest) -> dict[str, Any]:
        """What would be sent, with credentials masked: the effective request path, headers and body."""
        op = self._operation(request.op, job=False)
        assert op.request is not None
        wire = await self._build(conn, op.request, op, self._source(request), preview=True)
        secrets_ = _secrets(conn)
        headers = {k: (MASK if v in secrets_ or any(s in v for s in secrets_) else v) for k, v in wire.headers.items()}
        url = _scrub(wire.url, secrets_)
        return {"method": wire.method, "url": url, "headers": headers, "body": (wire.body or b"").decode("utf-8", "replace")}

    # --- public: jobs ----------------------------------------------------------------------------

    @_redacting
    async def submit(self, conn: Connection, request: CanonicalRequest) -> JobRef:
        op = self._operation(request.op, job=True)
        assert op.job is not None
        source = self._source(request)
        parsed, _ = await self._json_step(conn, op.job.submit.request, op, source)
        raw = pointer.get(parsed, op.job.submit.id_segments)
        if type(raw) is int:
            raw = str(raw)
        if type(raw) is not str or not raw:
            raise MalformedResponse("The engine accepted the job but its answer holds no job id where the description says.")
        return JobRef(op.name, raw)

    @_redacting
    async def poll(self, conn: Connection, ref: JobRef) -> JobStatus:
        op = self._operation(ref.op, job=True)
        assert op.job is not None
        poll = op.job.poll
        parsed, _ = await self._json_step(conn, poll.request, op, {"body": {}, "params": {"id": ref.id}, "files": {}})
        raw = pointer.get(parsed, poll.status.segments)
        if raw is MISSING or isinstance(raw, (dict, list)):
            raise MalformedResponse("The job status is missing from the engine's answer.")
        key = json_key(raw)
        state = poll.status.states.get(key)
        if state is None:
            raise MalformedResponse(f"The engine reported the job status {key!r}, which the protocol description does not map.")
        detail = None
        if poll.error_segments is not None:
            found = pointer.get(parsed, poll.error_segments)
            if found is not MISSING and found is not None:
                detail = found if type(found) is str else json.dumps(found, separators=(",", ":"))
        handles: tuple[str, ...] = ()
        if poll.assets_segments is not None:
            values = pointer.select_all(parsed, poll.assets_segments, limit=self.budgets.max_assets, budget="max_assets")
            clean = []
            for value in values:
                if type(value) is int:
                    value = str(value)
                if type(value) is not str or not value:
                    raise MalformedResponse("An asset handle in the engine's answer is not a string or number.")
                clean.append(value)
            handles = tuple(clean)
        return JobStatus(ref, JobState(state), None if detail is None else _scrub(detail, _secrets(conn)), handles)

    @_redacting
    async def wait(self, conn: Connection, ref: JobRef) -> JobStatus:
        """Poll every ``interval_s`` until the job ends. ``max_wait_s`` bounds the whole wait, a slow poll included."""
        op = self._operation(ref.op, job=True)
        assert op.job is not None
        interval, limit = op.job.poll.interval_s, op.job.poll.max_wait_s
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit
        too_long = BudgetExceeded("max_wait_s", f"The job did not end within {limit:g} s.")
        while True:
            if loop.time() >= deadline:
                raise too_long
            try:
                async with asyncio.timeout_at(deadline):
                    status = await self.poll(conn, ref)
            except TimeoutError:
                raise too_long from None
            except TransportFailure:
                pass
            except UpstreamError as exc:
                if not 500 <= exc.status < 600:
                    raise
            else:
                if status.state in _ENDED:
                    return status
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise too_long
            await asyncio.sleep(min(interval, remaining))

    @_redacting
    async def cancel(self, conn: Connection, ref: JobRef) -> None:
        """Ask the engine to stop a job that is still running. A job that already ended is left alone: nothing is sent."""
        op = self._operation(ref.op, job=True)
        assert op.job is not None
        if op.job.cancel is None:
            raise UnsupportedByAdapter(f"The protocol does not describe cancelling '{ref.op}'.")
        source = {"body": {}, "params": {"id": ref.id}, "files": {}}
        # Poll has its own total timeout. A failed poll must not consume the
        # cancellation request's deadline or suppress that request.
        try:
            status = await self.poll(conn, ref)
        except ProtocolError:
            pass  # only positive terminal evidence suppresses cancellation
        else:
            if status.state in _ENDED:
                return
        async with _total(conn, "The engine did not finish cancelling"):
            wire = await self._exchange(conn, op.job.cancel, op, source, _secrets(conn))
            await wire.aclose()

    @_redacting
    async def fetch_assets(self, conn: Connection, ref: JobRef, status: JobStatus) -> tuple[AssetRef, ...]:
        op = self._operation(ref.op, job=True)
        assert op.job is not None
        fetch = op.job.fetch
        if fetch is None:
            return ()
        if self._assets is None:
            raise ProtocolError("No asset store is configured, so fetched artifacts cannot be saved.", code="no_asset_store")
        handles: tuple[str | None, ...] = status.assets if "asset" in fetch.placeholders else (None,)
        if len(handles) > self.budgets.max_assets:
            raise BudgetExceeded("max_assets", f"A job may fetch at most {self.budgets.max_assets} assets.")
        out: list[AssetRef] = []
        secrets_ = _secrets(conn)
        total = 0
        async with _total(conn, "The engine did not deliver the assets"):
            for handle in handles:
                params: dict[str, Any] = {"id": ref.id}
                if handle is not None:
                    params["asset"] = handle
                wire = await self._exchange(conn, fetch, op, {"body": {}, "params": params, "files": {}}, secrets_)
                try:
                    asset = await self._save_binary(wire, name=handle, fetch_left=self.budgets.max_fetch_bytes - total)
                finally:
                    await wire.aclose()
                total += asset.size
                out.append(asset)
        return tuple(out)

    # --- operations ------------------------------------------------------------------------------

    def _operation(self, name: str, *, job: bool) -> Operation:
        op = self.description.operations.get(name)
        if op is None or (op.job is not None) != job:
            kind = "job operation" if job else "request operation"
            raise UnknownOperation(f"'{self.description.id}' has no {kind} named '{name}'.")
        return op

    @staticmethod
    def _source(request: CanonicalRequest) -> dict[str, Any]:
        return {"body": request.body, "params": request.params, "files": request.files}

    @staticmethod
    def _stream_choice(response: ResponseSpec, source: Mapping[str, Any]) -> StreamSpec | None:
        if response.framing in ("sse", "ndjson"):
            return StreamSpec(None, response.framing, response.terminal, response.program)
        if response.stream is not None and response.stream.when(source):
            return response.stream
        return None

    # --- building and sending --------------------------------------------------------------------

    async def _timed_exchange(self, conn: Connection, spec: RequestSpec, op: Operation, source: Mapping[str, Any]) -> WireResponse:
        try:
            async with asyncio.timeout(conn.timeouts.total_s):
                return await self._exchange(conn, spec, op, source, _secrets(conn))
        except TimeoutError:
            raise TransportFailure("timeout", f"The engine did not answer within {conn.timeouts.total_s:g} s.") from None

    async def _json_step(self, conn: Connection, spec: RequestSpec, op: Operation, source: Mapping[str, Any]) -> tuple[Any, WireResponse]:
        try:
            async with asyncio.timeout(conn.timeouts.total_s):
                wire = await self._exchange(conn, spec, op, source, _secrets(conn))
                try:
                    return await self._read_json(wire), wire
                finally:
                    await wire.aclose()
        except TimeoutError:
            raise TransportFailure("timeout", f"The engine did not answer within {conn.timeouts.total_s:g} s.") from None

    def _expand_path(self, spec: RequestSpec, params: Mapping[str, Any]) -> str:
        def fill(match: re.Match[str]) -> str:
            value = params.get(match.group(1), MISSING)
            if type(value) is int:
                value = str(value)
            if type(value) is not str or not value:
                raise MappingError(f"The request needs the value '{match.group(1)}' for the path {spec.path}.")
            normalized = unicodedata.normalize("NFKC", value)
            if (normalized in (".", "..")
                    or any(c in "/\\%;" or unicodedata.category(c) == "Cc" for c in normalized)
                    or any(c != "." and "." in unicodedata.normalize("NFKC", c) for c in value)):
                raise MappingError(f"The value '{match.group(1)}' is not a safe single path segment.")
            return quote(value, safe="")
        if not spec.placeholders:
            return spec.path
        path = _PLACEHOLDER.sub(fill, spec.path)
        if any(segment in (".", "..") for segment in path.split("/")):
            raise MappingError(f"A path value would make a '.' or '..' segment in {spec.path}; Azizam does not send it.")
        return path

    def _query(self, spec: RequestSpec, source: Mapping[str, Any]) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for item in spec.query:
            value = pointer.get(source, item.segments)
            if value is MISSING or value is None:
                value = item.default
                if value is MISSING:
                    continue
            if isinstance(value, bool):
                text = "true" if value else "false"
            elif isinstance(value, (dict, list)):
                raise MappingError(f"The query value '{item.name}' must be a plain value.")
            else:
                text = str(value)
            pairs.append((item.name, text))
        return pairs

    async def _build(self, conn: Connection, spec: RequestSpec, op: Operation, source: Mapping[str, Any], *, preview: bool = False) -> WireRequest:
        budgets = self.budgets
        for place in ("body", "params"):  # first, and without recursion: mapping and JSON writing walk these values
            if pointer.depth_exceeds(source[place], budgets.max_json_depth):
                raise BudgetExceeded("max_json_depth", f"The request {place} nests deeper than {budgets.max_json_depth}.")
        path = self._expand_path(spec, source["params"])
        auth = self.description.auth.get("control" if op.op_class == "control" else "inference")
        credential = conn.credentials.get("control" if op.op_class == "control" else "inference")
        headers: dict[str, str] = {"accept-encoding": "identity"}
        query = self._query(spec, source)
        if auth is not None and credential is not None and auth.kind != "none":
            if auth.kind == "query":
                query.append((auth.name or "", MASK if preview else credential.secret))
            else:
                name = credential.header or auth.name or "Authorization"
                scheme = credential.scheme if credential.scheme is not None else (auth.scheme if auth.scheme is not None else ("Bearer" if auth.kind == "bearer" else ""))
                headers[name.lower()] = f"{scheme} {credential.secret}" if scheme else credential.secret
        for name, value in {**conn.headers, **spec.headers}.items():
            headers[name.lower()] = value
        for value in headers.values():
            if "\r" in value or "\n" in value:
                raise MappingError("A request header holds a line break; Azizam does not send it.")
        body: bytes | None = None
        encoding = spec.body.encoding
        if encoding == "json":
            assert spec.body.program is not None
            mapped = spec.body.program.apply(source)
            if pointer.depth_exceeds(mapped, budgets.max_json_depth):
                raise BudgetExceeded("max_json_depth", f"The request body nests deeper than {budgets.max_json_depth}.")
            body = _dump_json(mapped, "The request body")
            headers["content-type"] = "application/json"
        elif encoding == "multipart":
            body, headers["content-type"] = await self._multipart(spec, source)
        if body is not None and len(body) > budgets.max_request_bytes:
            raise BudgetExceeded("max_request_bytes", f"The request body is larger than {budgets.max_request_bytes} bytes.")
        base = conn.address.rstrip("/")
        url = base + path
        if query:
            from urllib.parse import urlencode
            url += "?" + urlencode(query, quote_via=quote)
        if _origin(url) != _origin(conn.address):
            raise MappingError("The request would leave the engine's origin.")  # unreachable by construction; a guard, not a feature
        return WireRequest(spec.method, url, headers, body)

    async def _multipart(self, spec: RequestSpec, source: Mapping[str, Any]) -> tuple[bytes, str]:
        parts: list[framing.Part] = []
        upload = 0
        for part in spec.body.parts:
            if part.kind == "file":
                ref = source["files"].get(part.segments[1], MISSING)
                if ref is MISSING:
                    if part.required:
                        raise MappingError(f"The request needs the file '{part.segments[1]}'.")
                    continue
                if self._assets is None:
                    raise ProtocolError("No asset store is configured, so files cannot be uploaded.", code="no_asset_store")
                data = await self._assets.read(ref, max_bytes=self.budgets.max_upload_bytes - upload)
                upload += len(data)
                parts.append(framing.Part(part.name, data, filename=ref.name or ref.id, content_type=ref.content_type))
                continue
            value = pointer.get(source, part.segments)
            if value is MISSING or value is None:
                value = part.default
            if value is MISSING:
                if part.required:
                    raise MappingError(f"The request needs '{'/'.join(part.segments)}' for the form field '{part.name}'.")
                continue
            if part.kind == "json":
                parts.append(framing.Part(part.name, _dump_json(value, f"The form field '{part.name}'"), content_type="application/json"))
            else:
                if isinstance(value, (dict, list)):
                    raise MappingError(f"The form field '{part.name}' must be a plain value; use kind json for structures.")
                text = ("true" if value else "false") if isinstance(value, bool) else str(value)
                parts.append(framing.Part(part.name, text.encode("utf-8")))
        try:
            return framing.encode_multipart(parts, self.boundary_factory())
        except ValueError as exc:
            raise MappingError(str(exc)) from None

    async def _exchange(self, conn: Connection, spec: RequestSpec, op: Operation, source: Mapping[str, Any], secrets_: list[str]) -> WireResponse:
        """Send, follow same-origin redirects, refuse cross-origin ones, and raise for a non-2xx status."""
        wire = await self._build(conn, spec, op, source)
        method, url, body = wire.method, wire.url, wire.body
        headers = dict(wire.headers)
        hops = 0
        while True:
            response = await self._send(WireRequest(method, url, headers, body), conn, secrets_)
            if response.status not in _REDIRECTS:
                break
            location = response.headers.get("location", "")
            await response.aclose()
            if not location:
                raise MalformedResponse("The engine redirected without saying where.")
            target = urljoin(url, location)
            if _origin(target) != _origin(conn.address):
                raise RedirectRefused(
                    f"The engine redirected to {_label(_scrub(target, secrets_))}, which is another origin. "
                    "Azizam does not follow it, and the key was not sent there."
                )
            hops += 1
            if hops > self.budgets.max_redirects:
                raise RedirectRefused(f"The engine redirected more than {self.budgets.max_redirects} times.")
            if response.status == 303 or (response.status in (301, 302) and method == "POST"):
                method, body = "GET", None
                headers.pop("content-type", None)
            url = target
        if not 200 <= response.status < 300:
            await self._raise_status(response, secrets_)
        return response

    async def _send(self, wire: WireRequest, conn: Connection, secrets_: list[str]) -> WireResponse:
        try:
            return await self._transport.send(wire, conn.timeouts)
        except ProtocolError:
            raise
        except Exception as exc:  # a transport bug or an unexpected client error: typed, and without the URL it may quote
            raise TransportFailure("aborted", _scrub(f"The request to the engine failed ({type(exc).__name__}).", secrets_)) from None

    async def _raise_status(self, response: WireResponse, secrets_: list[str]) -> None:
        raw = bytearray()
        # Bounded lookahead catches a credential crossing the diagnostic cap.
        capture_limit = _ERROR_BODY_CAP + max((len(s.encode()) for s in secrets_), default=1) - 1
        try:
            async for chunk in response.chunks():
                raw.extend(chunk[:capture_limit - len(raw)])
                if len(raw) >= capture_limit:
                    break
        except TransportFailure:
            pass
        finally:
            await response.aclose()
        body = _scrub_cut(bytes(raw), secrets_, _ERROR_BODY_CAP)
        excerpt = _scrub_cut(" ".join(body.decode("utf-8", "replace").split()).encode(), secrets_, 160).decode("utf-8", "replace")
        row = self.description.advice_for(response.status)
        keep = {k: _scrub(v, secrets_) for k, v in response.headers.items() if k in ("retry-after", "content-type")}
        message = f"The engine answered {response.status}" + (f": {excerpt}" if excerpt else ".")
        raise UpstreamError(response.status, message, body=body, headers=keep,
                            advice_code=row.code if row else None, advice=row.advice if row else None)

    # --- reading ---------------------------------------------------------------------------------

    @staticmethod
    def _check_identity(response: WireResponse) -> None:
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in ("", "identity"):
            raise MalformedResponse(f"The engine compressed its answer ({encoding}) although Azizam asked for none. Azizam refuses it.")

    async def _read_all(self, response: WireResponse, limit: int, budget: str) -> bytes:
        self._check_identity(response)
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise BudgetExceeded(budget, f"The engine's answer is larger than {limit} bytes.")
        parts: list[bytes] = []
        total = 0
        async for chunk in response.chunks():
            total += len(chunk)
            if total > limit:
                raise BudgetExceeded(budget, f"The engine's answer is larger than {limit} bytes.")
            parts.append(chunk)
        return b"".join(parts)

    def _loads(self, data: bytes, noun: str) -> Any:
        """Parse JSON under the depth budget. Every JSON the engine sends (answer, job step, stream event) comes through here."""
        try:
            parsed = json.loads(data)
        except RecursionError:
            raise BudgetExceeded("max_json_depth", f"{noun} nests too deep.") from None
        except ValueError:
            raise MalformedResponse(f"{noun} is not JSON.") from None
        limit = self.budgets.max_json_depth
        # Each level of nesting needs an opening bracket, so a value with few of them cannot be too deep: skip the walk.
        if data.count(b"{") + data.count(b"[") > limit and pointer.depth_exceeds(parsed, limit):
            raise BudgetExceeded("max_json_depth", f"{noun} nests deeper than {limit}.")
        return parsed

    def _parse_json(self, data: bytes) -> Any:
        if not data.strip():
            return None
        return self._loads(data, "The engine's answer")

    async def _read_json(self, response: WireResponse) -> Any:
        return self._parse_json(await self._read_all(response, self.budgets.max_response_bytes, "max_response_bytes"))

    async def _save_binary(self, response: WireResponse, *, name: str | None = None, fetch_left: int | None = None) -> AssetRef:
        if self._assets is None:
            raise ProtocolError("No asset store is configured, so a binary answer cannot be saved.", code="no_asset_store")
        self._check_identity(response)
        limit, budget = self.budgets.max_asset_bytes, "max_asset_bytes"
        if fetch_left is not None and fetch_left < limit:
            limit, budget = max(fetch_left, 0), "max_fetch_bytes"
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise BudgetExceeded(budget, f"The engine's answer is larger than {limit} bytes.")
        chunks = response.chunks().__aiter__()
        first: list[bytes] = []
        head = b""
        while len(head) < _SNIFF_BYTES:  # the claimed type is checked against the first bytes before anything is saved
            try:
                chunk = await chunks.__anext__()
            except StopAsyncIteration:
                break
            first.append(chunk)
            head += chunk
        content_type = media_type_for(response.headers.get("content-type"), head)

        async def capped() -> AsyncIterator[bytes]:  # the cap holds whatever the store does with it
            seen = 0
            for chunk in first:
                seen += len(chunk)
                if seen > limit:
                    raise BudgetExceeded(budget, f"The engine's answer is larger than {limit} bytes.")
                yield chunk
            async for chunk in chunks:
                seen += len(chunk)
                if seen > limit:
                    raise BudgetExceeded(budget, f"The engine's answer is larger than {limit} bytes.")
                yield chunk
        return await self._assets.save(capped(), content_type=content_type, max_bytes=limit, name=name)

    async def _finish(self, response: WireResponse, spec: ResponseSpec, source: Mapping[str, Any], request: CanonicalRequest, started: float,
                      secrets_: list[str]) -> Reply:
        headers = _clean_headers(response.headers, secrets_)
        if spec.framing == "binary":
            asset = await self._save_binary(response)
            return Reply(response.status, headers, asset=asset, seconds=time.perf_counter() - started)
        if spec.framing == "none":
            return Reply(response.status, headers, seconds=time.perf_counter() - started)
        parsed = await self._read_json(response)
        selected: dict[str, Any] = {}
        for name, segments in spec.select.items():
            if "*" in segments:
                selected[name] = pointer.select_all(parsed, segments, limit=self.budgets.max_selected)
            else:
                found = pointer.get(parsed, segments)
                if found is not MISSING:
                    selected[name] = found
        body = parsed
        if spec.program is not None:
            body = spec.program.apply({"body": parsed, "status": response.status, "headers": headers, "params": request.params, "event": None})
        return Reply(response.status, headers, body, selected, seconds=time.perf_counter() - started)

    # --- streaming -------------------------------------------------------------------------------

    def _open_stream(self, response: WireResponse, stream: StreamSpec, source: Mapping[str, Any], deadline: float,
                     request: CanonicalRequest, secrets_: list[str]) -> StreamReply:
        try:
            self._check_identity(response)
        except ProtocolError:
            asyncio.ensure_future(response.aclose())
            raise
        events = self._events(response, stream, request, deadline, secrets_)
        return StreamReply(response.status, _clean_headers(response.headers, secrets_), events, response.aclose)

    async def _events(self, response: WireResponse, stream: StreamSpec, request: CanonicalRequest, deadline: float,
                      secrets_: list[str]) -> AsyncIterator[Any]:
        budgets = self.budgets
        terminal: Terminal | None = stream.terminal
        program = stream.program
        sse = stream.framing == "sse"
        decoder: Any = framing.SseDecoder(budgets.max_event_bytes) if sse else framing.NdjsonDecoder(budgets.max_event_bytes)
        sentinel = terminal.sentinel.encode() if terminal is not None and terminal.sentinel is not None else None
        end_event = terminal.event if terminal is not None else None
        end_path = terminal.segments if terminal is not None else None
        end_value = terminal.equals if terminal is not None else None
        parse = program is not None or end_path is not None
        has_terminal = terminal is not None
        mapped_source: dict[str, Any] = {"body": None, "status": response.status, "headers": _clean_headers(response.headers, secrets_),
                                         "params": request.params, "event": None}

        def handle(item: Any) -> tuple[bool, Any, bool]:
            """For one decoded event: (deliver it, what to deliver, it ends the stream)."""
            data, name = (item.data, item.event) if sse else (item, None)
            if sentinel is not None and data == sentinel:
                return False, None, True
            if end_event is not None and name == end_event:
                return False, None, True
            if not parse:
                return True, data, False
            if not data:
                return False, None, False
            obj = self._loads(data, "A stream event")
            last = False
            if end_path is not None:
                found = pointer.get(obj, end_path)
                last = found is not MISSING and same_json(found, end_value)
            value: Any = data
            if program is not None:
                mapped_source["body"] = obj
                mapped_source["event"] = name
                value = program.apply(mapped_source)
            return True, value, last

        loop = asyncio.get_running_loop()
        total = 0
        finished = False
        with _Redact(secrets_):
            try:
                async for chunk in response.chunks():
                    total += len(chunk)
                    if total > budgets.max_stream_bytes:
                        raise BudgetExceeded("max_stream_bytes", f"A stream is larger than {budgets.max_stream_bytes} bytes.")
                    if loop.time() > deadline:
                        raise TransportFailure("timeout", "The stream ran past the total time allowed.")
                    for item in decoder.feed(chunk):
                        deliver, value, finished = handle(item)
                        if deliver:
                            yield value
                        if finished:
                            break
                    if finished:
                        break
                else:
                    for item in decoder.finish():  # the end of the stream completes a held CR, or an NDJSON line with no newline
                        deliver, value, finished = handle(item)
                        if deliver:
                            yield value
                        if finished:
                            break
                if has_terminal and not finished:
                    raise TransportFailure("aborted", "The stream ended before the engine's final event.")
                if not has_terminal and sse and decoder.pending():
                    raise TransportFailure("aborted", "The stream ended in the middle of an event.")
            finally:
                await response.aclose()


def media_type_for(declared: str | None, head: bytes) -> str:
    served = served_media_type(declared, head)
    if served is None:
        raise MalformedResponse(
            "The engine's answer is not an image, audio or video type Azizam serves, or its bytes do not match the type the engine named. "
            "Azizam did not save it."
        )
    return served


def _dump_json(value: Any, what: str) -> bytes:
    try:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except RecursionError:
        raise BudgetExceeded("max_json_depth", f"{what} nests too deep.") from None
    except (TypeError, ValueError) as exc:  # a value JSON has no spelling for: an asset reference, a non-text key, NaN
        raise MappingError(f"{what} cannot be written as JSON: {exc}") from None


@contextlib.asynccontextmanager
async def _total(conn: Connection, what: str) -> AsyncIterator[None]:
    """One total deadline for everything inside, however many exchanges and body reads it holds."""
    try:
        async with asyncio.timeout(conn.timeouts.total_s):
            yield
    except TimeoutError:
        raise TransportFailure("timeout", f"{what} within {conn.timeouts.total_s:g} s.") from None


@functools.lru_cache(maxsize=8)
def _variants(secrets_: tuple[str, ...]) -> list[str]:
    """Every spelling of each secret that can reach a URL, a header or a JSON text, longest first.

    A key sent as a query value is percent-encoded on the wire, and an engine that echoes the URL echoes that spelling.
    """
    forms: set[str] = set()
    for secret in secrets_:
        if secret:  # even a one-character credential must never be echoed
            forms.update((secret, quote(secret), quote(secret, safe=""), quote_plus(secret), json.dumps(secret)[1:-1]))
    return sorted(forms, key=len, reverse=True)


def _secrets(conn: Connection) -> list[str]:
    return _variants(tuple(c.secret for c in conn.credentials.values()))


def _scrub(text: str, secrets_: list[str]) -> str:
    mask = "" if any(secret in MASK for secret in secrets_) else MASK
    for secret in secrets_:
        text = text.replace(secret, mask)
    return text


def _clean_headers(headers: Mapping[str, str], secrets_: list[str]) -> dict[str, str]:
    return {k: _scrub(v, secrets_) for k, v in headers.items()} if secrets_ else dict(headers)


def _scrub_bytes(data: bytes, secrets_: list[str]) -> bytes:
    mask = b"" if any(secret in MASK for secret in secrets_) else MASK.encode()
    for secret in secrets_:
        data = data.replace(secret.encode(), mask)
    return data


def _scrub_cut(data: bytes, secrets_: list[str], cap: int) -> bytes:
    """Scrub before truncation; remove any partial credential prefix at the cut."""
    data = _scrub_bytes(data, secrets_)[:cap]
    # Remove rather than append a mask here: even a full cap must remain bounded.
    cut = len(data)
    for secret in secrets_:
        encoded = secret.encode()
        for size in range(min(len(encoded) - 1, len(data)), 0, -1):
            if data.endswith(encoded[:size]):
                cut = min(cut, len(data) - size)
                break
    return data[:cut]
