"""The trusted resolver for media inputs (S2 design, D7).

A route gets a file input from a caller as an asset id, an uploaded body, or a URL. This module turns each into one
immutable, stored reference (``StoredInput``) that holds a lease for as long as a run needs it. It is the only place that
decides four things:

- **What the bytes are.** The caller's claimed media type, size and name are never read. The resolver measures the
  bytes itself (``measure``) and refuses a type the input does not accept.
- **Whether the caller may read them.** An asset id passes the asset store's own read check, with the caller's
  authenticated context. An uploaded or downloaded body belongs to the caller who sent it.
- **Whether the selected upstream may receive them.** A local upstream may. A remote one may only when the caller's
  access plan lists that provider; an upstream the registry does not know may not (``check_disclosure``).
- **What lasts.** An ingest that stores a file writes its own marker first, then the file, then the lease
  (``gateway/media_input_leases.py``). A crash between them leaves only a marker, which the startup sweep removes.

The order is fixed, and every refusal comes before any effect: the disclosure check (it needs no bytes), then the read
check, then the measure and the size and type checks, and only then the lease or the ingest. So a denied or invalid
input reaches no store write and no upstream call: "zero upstream effects" at the input boundary.

URLs have one path. The resolver takes a ``fetch`` callable from its caller and calls it; it holds no HTTP client, no
socket and no origin rule of its own. The caller passes the gateway's existing media ingestion
(``gateway/media_fetch.py`` with its origin and redirect rules). A test holds that line.

The module names no workflow, model, engine, intent or modality. The asset store, the access plan and the fetcher
reach it through the ``AssetPort`` and the callables its constructor takes.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from gateway import asset_types
from gateway.media_input_leases import HOLDER_KINDS, MediaInputLedger
from gateway.workflow_call import sniff


class MediaInputError(ValueError):
    """An input was refused. ``code`` is stable; ``message`` is safe to show the caller. No effect has happened."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# The codes. A route maps a code to its own wording when its wire contract fixes the text.
INVALID = "input_invalid"
MISSING = "input_asset_missing"
FORBIDDEN = "input_asset_forbidden"
UNSUPPORTED = "input_type_unsupported"
TOO_LARGE = "input_too_large"
EMPTY = "input_empty"
DISCLOSURE_REFUSED = "input_disclosure_refused"
FETCH_FAILED = "input_fetch_failed"


@dataclass(frozen=True)
class StoredInput:
    """An immutable reference to a stored file, with what the resolver measured. It holds no bytes."""

    asset_id: str
    media_type: str
    size_bytes: int
    sha256: str
    lease_id: str


@dataclass(frozen=True)
class ResolvedInput:
    """The reference, and the bytes to send upstream. A caller that only records the input keeps the reference."""

    ref: StoredInput
    data: bytes


@dataclass(frozen=True)
class Destination:
    """Where the bytes would go. ``known`` is False when the registry has no such service: nothing may go there."""

    service_id: str
    known: bool = True
    remote: bool = False
    provider_id: str | None = None


@dataclass(frozen=True)
class Holder:
    """Who keeps the lease: a job (released when it ends, is deleted or is erased) or one request."""

    kind: str
    id: str

    def __post_init__(self) -> None:
        if self.kind not in HOLDER_KINDS or not self.id:
            raise ValueError(f"a holder is one of {HOLDER_KINDS} with a non-empty id")


@dataclass(frozen=True)
class Measured:
    media_type: str
    suffix: str
    size_bytes: int
    sha256: str


def measure(data: bytes) -> Measured:
    """What the bytes are, from the bytes. Magic numbers only: there is no hint to give, so a caller cannot lie.
    A format the sniffer does not know measures as ``application/octet-stream``."""
    media_type, suffix = sniff(data, None)
    return Measured(media_type, suffix, len(data), hashlib.sha256(data).hexdigest())


def accepted(media_type: str, accepts: Sequence[str]) -> bool:
    """``accepts`` holds exact types (``image/png``) or a whole top-level type (``image/*``). An unidentified file
    (``application/octet-stream``) matches only if the input names that type itself."""
    for pattern in accepts:
        if pattern == media_type:
            return True
        if pattern.endswith("/*") and media_type != asset_types.UNKNOWN_MEDIA_TYPE and media_type.startswith(pattern[:-1]):
            return True
    return False


class AssetPort(Protocol):
    """What the asset store offers the resolver. The gateway builds one over its own store and read check."""

    def parse(self, asset_id: str) -> tuple[str, str] | None:
        """``(kind prefix, digest)`` of a well-formed asset id, else None."""
        ...

    def exists(self, asset_id: str) -> bool: ...

    def size(self, asset_id: str) -> int | None: ...

    def may_read(self, asset_id: str, auth: Any) -> bool:
        """The store's own read check for this caller."""
        ...

    def read(self, asset_id: str) -> bytes:
        """The stored bytes, as they are delivered to an upstream."""
        ...

    def owner_key(self, auth: Any) -> str | None:
        """The key a stored owner claim and a lease use for this caller."""
        ...

    def planned_id(self, data: bytes, suffix: str, kind: str) -> str:
        """The id that ``put`` will give these bytes (the store may rewrite them, so the id is the store's to say)."""
        ...

    def put(self, data: bytes, *, kind: str, suffix: str, owner_key: str | None) -> str: ...

    def discard(self, asset_id: str, owner_key: str) -> None:
        """Remove the owner's claim and, when no owner is left, the file. Quiet if it is already gone."""
        ...


Fetch = Callable[[str], Awaitable[Any]]  # returns an object with ``.data`` (bytes)
RemoteGrant = Callable[[str, Any], bool]  # (provider id, auth) -> may this caller send data to that provider


class MediaInputResolver:
    def __init__(
        self,
        *,
        assets: AssetPort,
        ledger: MediaInputLedger,
        may_send_remote: RemoteGrant,
        max_bytes: int,
        fetch: Fetch | None = None,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._assets = assets
        self._ledger = ledger
        self._may_send_remote = may_send_remote
        self._max_bytes = max_bytes
        self._fetch = fetch

    # -- the disclosure check ------------------------------------------------

    def check_disclosure(self, destination: Destination, auth: Any) -> None:
        """Raise unless this caller's data may go to ``destination``. It needs no bytes, so it runs first."""
        if not destination.known:
            raise MediaInputError(DISCLOSURE_REFUSED, "The selected service is not one this gateway knows, so no input can be sent to it.")
        if not destination.remote:
            return
        if not destination.provider_id or not self._may_send_remote(destination.provider_id, auth):
            raise MediaInputError(
                DISCLOSURE_REFUSED,
                "Your access plan does not allow sending files to the provider this model runs on.",
            )

    # -- the three kinds of input -------------------------------------------

    def resolve_asset(self, auth: Any, asset_id: str, *, holder: Holder, destination: Destination,
                      accepts: Sequence[str]) -> ResolvedInput:
        """An asset the caller names by id. Nothing is stored: the lease keeps it for the holder. The size limit does
        not apply: it bounds what the gateway fetches or receives, not what it already holds."""
        self.check_disclosure(destination, auth)
        if not isinstance(asset_id, str) or self._assets.parse(asset_id) is None:
            raise MediaInputError(INVALID, "asset_id is not a stored asset id")
        if not self._assets.exists(asset_id):
            raise MediaInputError(MISSING, "asset_id not found (may have been evicted)")
        if not self._assets.may_read(asset_id, auth):
            raise MediaInputError(FORBIDDEN, "asset_id is not accessible for this API key")
        # The size limit is for bytes the gateway fetches or receives. A file already in the store got in under that
        # rule, and a route that served it before this resolver existed never measured it, so it is not refused here.
        data = self._assets.read(asset_id)
        measured = self._check_bytes(data, accepts, capped=False)
        owner = self._assets.owner_key(auth) or ""
        lease_id = self._lease(asset_id, owner, holder, created=False, effect_id=None)
        return ResolvedInput(StoredInput(asset_id, measured.media_type, measured.size_bytes, measured.sha256, lease_id), data)

    def resolve_upload(self, auth: Any, body: bytes, *, holder: Holder, destination: Destination,
                       accepts: Sequence[str]) -> ResolvedInput:
        """An uploaded body. The type, size and name that came with it are not parameters here: they are never read."""
        self.check_disclosure(destination, auth)
        return self._ingest(auth, body, holder, accepts)

    async def resolve_url(self, auth: Any, url: str, *, holder: Holder, destination: Destination,
                          accepts: Sequence[str]) -> ResolvedInput:
        """A URL, fetched by the gateway's media ingestion and nothing else. Its origin rules run inside ``fetch``."""
        self.check_disclosure(destination, auth)
        if self._fetch is None:
            raise MediaInputError(FETCH_FAILED, "this gateway does not fetch media from a URL here")
        try:
            fetched = await self._fetch(url)
        except Exception as exc:  # noqa: BLE001 - the fetcher's own refusal (origin, redirect, size) is the reason
            raise MediaInputError(FETCH_FAILED, f"failed to download the input: {exc}") from exc
        return self._ingest(auth, bytes(fetched.data), holder, accepts)

    # -- ingest: marker, then file, then lease ------------------------------

    def _ingest(self, auth: Any, data: bytes, holder: Holder, accepts: Sequence[str]) -> ResolvedInput:
        measured = self._check_bytes(data, accepts)
        owner = self._assets.owner_key(auth)
        kind = asset_types.asset_prefix_for_media_type(measured.media_type)
        planned = self._assets.planned_id(data, measured.suffix, kind)
        created = not self._assets.exists(planned)
        effect_id = f"mie_{uuid.uuid4().hex}"
        self._ledger.begin_effect(effect_id, owner_key=owner or "", asset_id=planned, created_asset=created)
        asset_id = self._assets.put(data, kind=kind, suffix=measured.suffix, owner_key=owner)
        lease_id = self._lease(asset_id, owner or "", holder, created=created, effect_id=effect_id)
        return ResolvedInput(StoredInput(asset_id, measured.media_type, measured.size_bytes, measured.sha256, lease_id), data)

    def _check_bytes(self, data: bytes, accepts: Sequence[str], *, capped: bool = True) -> Measured:
        if not data:
            raise MediaInputError(EMPTY, "the input is empty")
        if capped and len(data) > self._max_bytes:
            raise MediaInputError(TOO_LARGE, "the input is larger than the size limit")
        measured = measure(data)
        if not accepted(measured.media_type, accepts):
            raise MediaInputError(UNSUPPORTED, "the input is not a file type this model accepts")
        return measured

    def _lease(self, asset_id: str, owner: str, holder: Holder, *, created: bool, effect_id: str | None) -> str:
        lease_id = f"mil_{uuid.uuid4().hex}"
        self._ledger.lease(lease_id, asset_id=asset_id, owner_key=owner, holder_kind=holder.kind,
                           holder_id=holder.id, created_asset=created, effect_id=effect_id)
        return lease_id

    # -- ending -------------------------------------------------------------

    def release(self, holder: Holder) -> int:
        """The holder is done (a request ended). A job's leases end with the job, in the jobs database."""
        return self._ledger.release_holder(holder.kind, holder.id)

    def sweep(self, *, effect_grace_s: float = 3600, request_ttl_s: float = 3600) -> int:
        """Remove what nothing holds: orphaned markers, leases without a holder, and the files they created."""
        return self._ledger.sweep(self._assets.discard, effect_grace_s=effect_grace_s, request_ttl_s=request_ttl_s)
