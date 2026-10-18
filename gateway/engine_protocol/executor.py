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
import json
import re
import secrets
import time
from typing import Any, AsyncIterator, Callable, Mapping
from urllib.parse import quote, urljoin, urlsplit

from gateway.engine_protocol import framing, pointer
from gateway.engine_protocol.assets import AssetStore
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
_ENCODED_SEPARATOR = re.compile(r"%(?:2f|5c)", re.IGNORECASE)  # a pre-encoded slash or backslash: some servers decode twice
_DEFAULT_PORTS = {"http": 80, "https": 443}
_REDIRECTS = (301, 302, 303, 307, 308)


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), parts.port or _DEFAULT_PORTS.get(scheme, 0)


def _label(url: str) -> str:
    scheme, host, port = _origin(url)
    return f"{scheme}://{host}:{port}"


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
                    return self._open_stream(wire, stream, source, deadline, request)
                try:
                    return await self._finish(wire, response, source, request, started)
                finally:
                    await wire.aclose()
        except TimeoutError:
            raise TransportFailure("timeout", f"The engine did not finish within {conn.timeouts.total_s:g} s.") from None

    async def preview(self, conn: Connection, request: CanonicalRequest) -> dict[str, Any]:
        """What would be sent, with credentials masked: the effective request path, headers and body."""
        op = self._operation(request.op, job=False)
        assert op.request is not None
        wire = await self._build(conn, op.request, op, self._source(request))
        secrets_ = _secrets(conn)
        headers = {k: (MASK if v in secrets_ or any(s in v for s in secrets_) else v) for k, v in wire.headers.items()}
        url = _scrub(wire.url, secrets_)
        return {"method": wire.method, "url": url, "headers": headers, "body": (wire.body or b"").decode("utf-8", "replace")}

    # --- public: jobs ----------------------------------------------------------------------------

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
        return JobStatus(ref, JobState(state), detail, handles)

    async def cancel(self, conn: Connection, ref: JobRef) -> None:
        op = self._operation(ref.op, job=True)
        assert op.job is not None
        if op.job.cancel is None:
            raise UnsupportedByAdapter(f"The protocol does not describe cancelling '{ref.op}'.")
        source = {"body": {}, "params": {"id": ref.id}, "files": {}}
        wire = await self._timed_exchange(conn, op.job.cancel, op, source)
        await wire.aclose()

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
        for handle in handles:
            params: dict[str, Any] = {"id": ref.id}
            if handle is not None:
                params["asset"] = handle
            wire = await self._timed_exchange(conn, fetch, op, {"body": {}, "params": params, "files": {}})
            try:
                out.append(await self._save_binary(wire, name=handle))
            finally:
                await wire.aclose()
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
            if _ENCODED_SEPARATOR.search(value):
                raise MappingError(f"The value '{match.group(1)}' holds an encoded slash; Azizam does not send it in a path.")
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

    async def _build(self, conn: Connection, spec: RequestSpec, op: Operation, source: Mapping[str, Any]) -> WireRequest:
        budgets = self.budgets
        path = self._expand_path(spec, source["params"])
        auth = self.description.auth.get("control" if op.op_class == "control" else "inference")
        credential = conn.credentials.get("control" if op.op_class == "control" else "inference")
        headers: dict[str, str] = {"accept-encoding": "identity"}
        query = self._query(spec, source)
        if auth is not None and credential is not None and auth.kind != "none":
            if auth.kind == "query":
                query.append((auth.name or "", credential.secret))
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
            try:
                body = json.dumps(mapped, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
            except RecursionError:
                raise BudgetExceeded("max_json_depth", "The request body nests too deep.") from None
            except ValueError as exc:
                raise MappingError(f"The request body cannot be written as JSON: {exc}") from None
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
                parts.append(framing.Part(part.name, json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode(), content_type="application/json"))
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
            response = await self._transport.send(WireRequest(method, url, headers, body), conn.timeouts)
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

    async def _raise_status(self, response: WireResponse, secrets_: list[str]) -> None:
        raw = b""
        try:
            async for chunk in response.chunks():
                raw += chunk
                if len(raw) >= _ERROR_BODY_CAP:
                    break
        except TransportFailure:
            pass
        finally:
            await response.aclose()
        body = _scrub_bytes(raw[:_ERROR_BODY_CAP], secrets_)
        excerpt = _scrub(" ".join(body.decode("utf-8", "replace").split())[:160], secrets_)
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

    def _parse_json(self, data: bytes) -> Any:
        if not data.strip():
            return None
        try:
            parsed = json.loads(data)
        except RecursionError:
            raise BudgetExceeded("max_json_depth", "The engine's answer nests too deep.") from None
        except ValueError:
            raise MalformedResponse("The engine's answer is not JSON.") from None
        if pointer.depth_exceeds(parsed, self.budgets.max_json_depth):
            raise BudgetExceeded("max_json_depth", f"The engine's answer nests deeper than {self.budgets.max_json_depth}.")
        return parsed

    async def _read_json(self, response: WireResponse) -> Any:
        return self._parse_json(await self._read_all(response, self.budgets.max_response_bytes, "max_response_bytes"))

    async def _save_binary(self, response: WireResponse, *, name: str | None = None) -> AssetRef:
        if self._assets is None:
            raise ProtocolError("No asset store is configured, so a binary answer cannot be saved.", code="no_asset_store")
        self._check_identity(response)
        limit = self.budgets.max_asset_bytes
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise BudgetExceeded("max_asset_bytes", f"The engine's answer is larger than {limit} bytes.")
        content_type = response.headers.get("content-type", "application/octet-stream")
        return await self._assets.save(response.chunks(), content_type=content_type, max_bytes=limit, name=name)

    async def _finish(self, response: WireResponse, spec: ResponseSpec, source: Mapping[str, Any], request: CanonicalRequest, started: float) -> Reply:
        headers = dict(response.headers)
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
                     request: CanonicalRequest) -> StreamReply:
        try:
            self._check_identity(response)
        except ProtocolError:
            asyncio.ensure_future(response.aclose())
            raise
        events = self._events(response, stream, request, deadline)
        return StreamReply(response.status, dict(response.headers), events, response.aclose)

    async def _events(self, response: WireResponse, stream: StreamSpec, request: CanonicalRequest, deadline: float) -> AsyncIterator[Any]:
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
        mapped_source: dict[str, Any] = {"body": None, "status": response.status, "headers": dict(response.headers),
                                         "params": request.params, "event": None}
        loop = asyncio.get_running_loop()
        total = 0
        finished = False
        try:
            async for chunk in response.chunks():
                total += len(chunk)
                if total > budgets.max_stream_bytes:
                    raise BudgetExceeded("max_stream_bytes", f"A stream is larger than {budgets.max_stream_bytes} bytes.")
                if loop.time() > deadline:
                    raise TransportFailure("timeout", "The stream ran past the total time allowed.")
                for item in decoder.feed(chunk):
                    data, name = (item.data, item.event) if sse else (item, None)
                    if sentinel is not None and data == sentinel:
                        finished = True
                        break
                    if end_event is not None and name == end_event:
                        finished = True
                        break
                    last = False
                    value: Any = data
                    if parse:
                        if not data:
                            continue
                        try:
                            obj = json.loads(data)
                        except RecursionError:
                            raise BudgetExceeded("max_json_depth", "A stream event nests too deep.") from None
                        except ValueError:
                            raise MalformedResponse("A stream event is not JSON.") from None
                        if end_path is not None:
                            found = pointer.get(obj, end_path)
                            last = found is not MISSING and same_json(found, end_value)
                        if program is not None:
                            mapped_source["body"] = obj
                            mapped_source["event"] = name
                            value = program.apply(mapped_source)
                    yield value
                    if last:
                        finished = True
                        break
                if finished:
                    break
            else:
                if not sse:
                    for line in decoder.finish():
                        if parse:
                            try:
                                obj = json.loads(line)
                            except ValueError:
                                raise MalformedResponse("A stream event is not JSON.") from None
                            if end_path is not None:
                                found = pointer.get(obj, end_path)
                                if found is not MISSING and same_json(found, end_value):
                                    finished = True
                            if program is not None:
                                mapped_source["body"] = obj
                                yield program.apply(mapped_source)
                            else:
                                yield line
                        else:
                            yield line
            if has_terminal and not finished:
                raise TransportFailure("aborted", "The stream ended before the engine's final event.")
            if not has_terminal and sse and decoder.pending():
                raise TransportFailure("aborted", "The stream ended in the middle of an event.")
        finally:
            await response.aclose()


def _secrets(conn: Connection) -> list[str]:
    return [c.secret for c in conn.credentials.values() if len(c.secret) >= 4]


def _scrub(text: str, secrets_: list[str]) -> str:
    for secret in secrets_:
        text = text.replace(secret, MASK)
    return text


def _scrub_bytes(data: bytes, secrets_: list[str]) -> bytes:
    for secret in secrets_:
        data = data.replace(secret.encode(), MASK.encode())
    return data
