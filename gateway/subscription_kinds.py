"""Subscription connection kinds: how the gateway's owner connects a plan once, loaded from data.

A kind is a row in ``providers/subscriptions/*.yml``. The gateway is the plan's client: the owner signs in once, and
other people reach the plan only through the access plans the owner gives them (owner only by default). The gateway
code is generic over these rows; the engines and plans behind it are data.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml

from gateway import transport_budgets

KINDS_DIR = Path(__file__).resolve().parent.parent / "providers" / "subscriptions"
# A second directory of kind rows, read after the shipped ones (browser test fixtures use it for a kind that points at a fake upstream).
EXTRA_KINDS_DIR_ENV = "AZIZAM_SUBSCRIPTION_KINDS_DIR"


class BillingMode(str, Enum):
    PLAN = "plan"


@dataclass(frozen=True)
class Refusal:
    status: int
    code: str
    message: str


@dataclass(frozen=True)
class SubscriptionKind:
    id: str
    display_name: str
    mechanism: str
    protocol: str
    billing_mode: str
    model_prefix: str
    oauth: Mapping[str, Any]
    upstream: Mapping[str, Any]
    unsupported_fields: frozenset[str]
    errors: Mapping[str, Refusal]
    status_errors: Mapping[Any, Refusal]
    terminal_refresh_errors: frozenset[str]
    dropped_fields: frozenset[str] = frozenset()
    request: Mapping[str, Any] = field(default_factory=dict)
    # Error codes of a failed response event (no HTTP status of its own) and the status each one stands for.
    failed_codes: Mapping[str, int] = field(default_factory=dict)
    # The whole row: a mechanism or protocol module reads its own section (validated by that module) from here.
    row: Mapping[str, Any] = field(default_factory=dict, repr=False)
    # What the owner reads beside a grant of this plan in an access plan (a terms reminder); empty when none.
    grant_note: str = ""
    # The plan's usage windows: for each, the response header names of its numbers (see usage_snapshot).
    usage_windows: tuple[Mapping[str, str], ...] = ()
    # The provider's own page where the owner reads the plan's usage; empty when none. An https address only.
    usage_page_url: str = ""

    @property
    def read_timeout(self) -> float:
        """How long the plan call waits for the upstream between bytes (a reasoning model can think a long time)."""
        return float(self.upstream.get("read_timeout_seconds") or self.row.get("read_timeout_seconds") or 300)

    @property
    def refresh_margin(self) -> int:
        return int(self.oauth.get("refresh_margin_seconds") or 300)

    @property
    def upstream_origin(self) -> str:
        parts = urlsplit(str(self.upstream["base_url"]))
        return f"{parts.scheme}://{parts.netloc}"

    def pin_id(self, slug: str) -> str:
        return f"{self.model_prefix}/{slug}"

    def refusal_for(self, status: int, code: str | None) -> Refusal:
        """The gateway's own refusal for an upstream status and error code. The code is only a key into the kind's table."""
        if code and code in self.errors:
            return self.errors[code]
        return self.status_errors.get(status) or self.status_errors["default"]

    def refusal_for_failed(self, code: str | None) -> Refusal:
        """The refusal for a ``response.failed`` event: its code names the status, else a plain upstream error."""
        return self.refusal_for(self.failed_codes.get(code or "", 502), code)

    def usage_snapshot(self, headers: Mapping[str, str], *, now: int) -> dict[str, Any] | None:
        """The plan's usage windows a response reports, as numbers only; None when it reports none.

        A window needs a readable percent used. A value that does not parse, or is out of range, is dropped, never
        raised. As in Codex CLI, a window with 0% used, no length and no reset time carries no data and is left out.
        """
        windows = []
        for names in self.usage_windows:
            used = usage_percent(headers.get(names["used_percent"]))
            if used is None:
                continue
            minutes = _whole(headers.get(names.get("window_minutes", "")), 1, _MAX_WINDOW_MINUTES)
            resets_at = usage_reset_at(headers.get(names.get("resets_at", "")), now=now)
            if resets_at is None:
                after = _whole(headers.get(names.get("resets_after_seconds", "")), 0, _MAX_RESET_SECONDS)
                resets_at = None if after is None else now + after
            if used == 0 and minutes is None and resets_at is None:
                continue
            windows.append({"used_percent": used, "window_minutes": minutes, "resets_at": resets_at})
        return {"captured_at": now, "windows": windows} if windows else None


# Beyond these bounds a plan's number is not a reading (registry/transport_budgets.yml, read at import).
_BOUNDS = transport_budgets.section("plan_usage_bounds")
_MAX_PERCENT = _BOUNDS["max_percent"]
_MAX_WINDOW_MINUTES = _BOUNDS["max_window_minutes"]
_MAX_RESET_SECONDS = _BOUNDS["max_reset_seconds"]
_USAGE_WINDOW_KEYS = frozenset({"used_percent", "window_minutes", "resets_at", "resets_after_seconds"})


def usage_percent(raw: Any) -> float | None:
    """A finite plan utilization percentage within the shared reading bounds."""
    used = _finite(raw)
    return used if used is not None and 0 <= used <= _MAX_PERCENT else None


def usage_reset_at(raw: Any, *, now: int) -> int | None:
    """A plan reset epoch within the shared reading bounds."""
    return _whole(raw, 1, now + _MAX_RESET_SECONDS)


def _finite(raw: str | None) -> float | None:
    try:
        value = float(str(raw).strip()) if raw is not None else None
    except ValueError:
        return None
    return value if value is not None and math.isfinite(value) else None


def _whole(raw: str | None, low: int, high: int) -> int | None:
    try:
        value = int(str(raw).strip()) if raw is not None else None
    except ValueError:
        return None
    return value if value is not None and low <= value <= high else None


def _usage_windows(raw: Any, source: Path | str) -> tuple[Mapping[str, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(
            isinstance(row, Mapping) and set(row) <= _USAGE_WINDOW_KEYS and isinstance(row.get("used_percent"), str)
            and all(isinstance(name, str) and name for name in row.values()) for row in raw):
        raise ValueError(f"{source}: usage_windows must be a list of header names, each with used_percent")
    return tuple({key: name.lower() for key, name in row.items()} for row in raw)


def _refusal(raw: Any, where: str) -> Refusal:
    if not isinstance(raw, Mapping) or not all(isinstance(raw.get(key), (str, int)) for key in ("status", "code", "message")):
        raise ValueError(f"{where}: a refusal needs status, code and message")
    return Refusal(int(raw["status"]), str(raw["code"]), str(raw["message"]))


def normalize_kind(raw: Mapping[str, Any], *, source: Path | str = "<memory>") -> SubscriptionKind:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{source}: a subscription kind must be a mapping")
    kind_id = str(raw.get("id") or "").strip()
    if not kind_id or not kind_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"{source}: invalid subscription kind id")
    # Static imports here avoid coupling the data types to implementation import order.
    from gateway.provider_connections import CONNECT_MECHANISMS
    from gateway.provider_protocols import REQUEST_PROTOCOLS

    mechanism = str(raw.get("mechanism") or "")
    protocol = str(raw.get("protocol") or "")
    if mechanism not in CONNECT_MECHANISMS:
        raise ValueError(f"{source}: unknown connection mechanism {mechanism!r}")
    if protocol not in REQUEST_PROTOCOLS:
        raise ValueError(f"{source}: unknown request protocol {protocol!r}")
    CONNECT_MECHANISMS[mechanism].validate(raw, source=source)
    REQUEST_PROTOCOLS[protocol].validate(raw, source=source)
    try:
        billing_mode = BillingMode(raw.get("billing_mode")).value
    except ValueError:
        raise ValueError(f"{source}: unsupported billing mode {raw.get('billing_mode')!r}") from None
    dropped_fields = raw.get("dropped_fields", [])
    if (not isinstance(dropped_fields, list) or any(
            not isinstance(name, str) or not name.isascii() or not name.isidentifier()
            for name in dropped_fields)):
        raise ValueError(f"{source}: dropped_fields must be a list of parameter names")
    oauth, upstream = raw.get("oauth") or {}, raw.get("upstream") or {}
    prefix = str(raw.get("model_prefix") or "").strip()
    if not prefix or "/" in prefix:
        raise ValueError(f"{source}: model_prefix must be one path segment")
    errors = {str(code): _refusal(row, f"{source}: errors.{code}") for code, row in (raw.get("errors") or {}).items()}
    status_errors = {key: _refusal(row, f"{source}: status_errors.{key}") for key, row in (raw.get("status_errors") or {}).items()}
    if "default" not in status_errors:
        raise ValueError(f"{source}: status_errors needs a default")
    return SubscriptionKind(
        id=kind_id, display_name=str(raw.get("display_name") or kind_id), mechanism=mechanism,
        protocol=protocol, billing_mode=billing_mode, model_prefix=prefix,
        oauth=dict(oauth), upstream=dict(upstream),
        unsupported_fields=frozenset(str(item) for item in raw.get("unsupported_fields") or ()),
        dropped_fields=frozenset(dropped_fields),
        errors=errors, status_errors=status_errors,
        terminal_refresh_errors=frozenset(str(item) for item in raw.get("terminal_refresh_errors") or ()),
        request=dict(raw.get("request") or {}),
        failed_codes={str(code): int(status) for code, status in (raw.get("failed_codes") or {}).items()},
        row=dict(raw), grant_note=str(raw.get("grant_note") or "").strip(),
        usage_windows=_usage_windows(raw.get("usage_windows"), source),
        usage_page_url=_usage_page_url(raw.get("usage_page_url"), source),
    )


def _usage_page_url(raw: Any, source: Path | str) -> str:
    if raw is None:
        return ""
    parts = urlsplit(raw) if isinstance(raw, str) else None
    if parts is None or parts.scheme != "https" or not parts.netloc:
        raise ValueError(f"{source}: usage_page_url must be an https address")
    return raw


def load_kinds(path: Path = KINDS_DIR) -> dict[str, SubscriptionKind]:
    directories = [path]
    extra = os.environ.get(EXTRA_KINDS_DIR_ENV)
    if extra and path == KINDS_DIR:
        directories.append(Path(extra))
    kinds: dict[str, SubscriptionKind] = {}
    for directory in directories:
        for file_path in sorted(directory.glob("*.yml")):
            kind = normalize_kind(yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}, source=file_path)
            if kind.id in kinds:
                raise ValueError(f"duplicate subscription kind {kind.id!r}")
            kinds[kind.id] = kind
    return kinds
