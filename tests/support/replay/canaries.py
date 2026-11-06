"""``canaries.toml`` schema and validator (TEST_STRATEGY 1.4, 4.7).

The file is operator data: which services to probe, with which models and
which limits. Committed code reads it generically. A row without finite limits
fails validation, so no recording or live run can be unbounded.
"""

from __future__ import annotations

import datetime as dt
import math
import tomllib
from pathlib import Path
from typing import Any

from tests.support.fake_upstream.protocols import PROTOCOLS

FORBIDDEN_PORTS = frozenset({5000, 15000})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "[::1]"})
REVIEW_WINDOW_DAYS = 90
SERVICE_KEYS = {"id", "protocol", "capabilities", "target_kind", "base_url", "owner", "review_by", "models", "limits"}
OPTIONAL_SERVICE_KEYS = {"engine_header", "version_header"}
MODEL_KEYS = {"id", "size_bytes", "features"}
# name -> (smallest, largest) allowed value; every limit is required.
LIMITS = {
    "max_requests": (1, 20), "max_submits": (0, 20), "max_polls": (0, 60), "poll_interval_s": (0, 60),
    "max_retries": (0, 0), "max_downloads": (0, 5), "max_cancels": (0, 5),
    "wall_s": (1, 600), "max_response_bytes": (1, 1024 * 1024),
}


def load(path: Path, today: dt.date | None = None) -> dict[str, Any]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    problems = validate(data, today)
    if problems:
        raise ValueError("canaries invalid:\n" + "\n".join(problems))
    return data


def validate(data: Any, today: dt.date | None = None) -> list[str]:
    today = today or dt.date.today()
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return ["schema_version must be 1"]
    problems = [f"unknown top-level key {key!r}" for key in data if key not in {"schema_version", "services"}]
    services = data.get("services")
    if not isinstance(services, list) or not services:
        return problems + ["services must be a non-empty list"]
    seen: set[str] = set()
    for index, service in enumerate(services):
        where = f"services[{index}]"
        if not isinstance(service, dict):
            problems.append(f"{where}: must be a table")
            continue
        where = f"{where} ({service.get('id')!r})"
        if service.get("id") in seen:
            problems.append(f"{where}: duplicate id")
        seen.add(service.get("id"))
        problems += _service(service, where, today)
    return problems


def _service(service: dict[str, Any], where: str, today: dt.date) -> list[str]:
    out = [f"{where}: unknown key {key!r}" for key in service if key not in SERVICE_KEYS | OPTIONAL_SERVICE_KEYS]
    out += [f"{where}: missing {key!r}" for key in sorted(SERVICE_KEYS - service.keys())]
    for key in ("id", "owner"):
        if key in service and not _text(service[key]):
            out.append(f"{where}: {key} must be a non-empty string")
    for key in OPTIONAL_SERVICE_KEYS & service.keys():
        if not _text(service[key]):
            out.append(f"{where}: {key} must be a non-empty string")
    if "protocol" in service and service["protocol"] not in PROTOCOLS:
        out.append(f"{where}: unknown protocol {service['protocol']!r}")
    if "target_kind" in service and service["target_kind"] != "direct-local-engine":
        out.append(f"{where}: target_kind must be 'direct-local-engine'")
    if "capabilities" in service and not (isinstance(service["capabilities"], list) and service["capabilities"]
                                          and all(_text(c) for c in service["capabilities"])):
        out.append(f"{where}: capabilities must be a non-empty list of strings")
    if "base_url" in service:
        out += _base_url(service["base_url"], where)
    if "review_by" in service:
        review = service["review_by"]
        if not isinstance(review, dt.date) or isinstance(review, dt.datetime):
            out.append(f"{where}: review_by must be a TOML date")
        elif review < today:
            out.append(f"{where}: review_by {review} has passed")
        elif review > today + dt.timedelta(days=REVIEW_WINDOW_DAYS):
            out.append(f"{where}: review_by is more than {REVIEW_WINDOW_DAYS} days ahead")
    if "models" in service:
        out += _models(service["models"], where)
    if "limits" in service:
        out += _limits(service["limits"], where)
    return out


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _base_url(value: Any, where: str) -> list[str]:
    if not _text(value):
        return [f"{where}: base_url must be a string"]
    from urllib.parse import urlsplit
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        return [f"{where}: base_url is not a URL"]
    host = f"[{url.hostname}]" if url.hostname and ":" in url.hostname else url.hostname
    problems = []
    if url.scheme != "http" or host not in LOOPBACK_HOSTS:
        problems.append(f"{where}: base_url must be http on a literal loopback address (127.0.0.1 or [::1])")
    if port is None:
        problems.append(f"{where}: base_url needs an explicit port")
    elif port in FORBIDDEN_PORTS:
        problems.append(f"{where}: base_url uses forbidden gateway port {port}")
    if url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
        problems.append(f"{where}: base_url must be an origin only")
    return problems


def _models(models: Any, where: str) -> list[str]:
    if not isinstance(models, list) or not models:
        return [f"{where}: models must be a non-empty list"]
    out = []
    for model in models:
        if not isinstance(model, dict) or set(model) != MODEL_KEYS:
            out.append(f"{where}: each model needs exactly {sorted(MODEL_KEYS)}")
        elif not (_text(model["id"]) and _number(model["size_bytes"], integer=True) and model["size_bytes"] > 0
                  and isinstance(model["features"], list) and all(_text(f) for f in model["features"])):
            out.append(f"{where}: model {model.get('id')!r} has an invalid id, size_bytes or features")
    return out


def _number(value: Any, *, integer: bool = False) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and (not integer or isinstance(value, int))


def _limits(limits: Any, where: str) -> list[str]:
    if not isinstance(limits, dict):
        return [f"{where}: limits must be a table"]
    out = [f"{where}: unknown limit {key!r}" for key in limits if key not in LIMITS]
    for key, (low, high) in LIMITS.items():
        if key not in limits:
            out.append(f"{where}: limits.{key} is required")
        elif not _number(limits[key]) or not low <= limits[key] <= high:
            out.append(f"{where}: limits.{key} must be a finite number from {low} to {high}")
    if isinstance(limits.get("max_polls"), int) and limits["max_polls"] > 0 and limits.get("poll_interval_s") == 0:
        out.append(f"{where}: limits.poll_interval_s must be positive when max_polls is")
    return out
