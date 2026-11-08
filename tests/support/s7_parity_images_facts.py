"""The facts the image parity tests compare: small readers over an ``Obs`` (tests/support/s7_parity_images_rig.py).

Each reader names one observable thing (a status, the error a caller got, the engine's call sequence, a receipt field) so a
failing parity test says which fact differs. Readers never mask: a value is read as the trace holds it.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from tests.support.s7_parity_images_rig import Obs


def _response(obs: Obs, i: int = 0) -> dict[str, Any]:
    return obs.trace["caller"][i]["response"] if len(obs.trace["caller"]) > i else {}


def status(obs: Obs, i: int = 0) -> Any:
    return _response(obs, i).get("status")


def doc(obs: Obs, i: int = 0) -> Any:
    body = _response(obs, i).get("body") or {}
    if body.get("kind") == "json":
        try:
            return json.loads(body["text"])
        except ValueError:
            return None
    return None


def failure(obs: Obs, i: int = 0) -> dict[str, Any] | None:
    """The error object a caller got: message, type, code, param, and the extra keys the route adds."""
    data = doc(obs, i)
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return None
    return {k: error.get(k) for k in ("message", "type", "code", "param", "queue_position", "details") if k in error}


def header(obs: Obs, name: str, i: int = 0) -> str | None:
    headers = _response(obs, i).get("headers") or {}
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def headers_with(obs: Obs, *prefixes: str, i: int = 0) -> dict[str, str]:
    headers = _response(obs, i).get("headers") or {}
    return {k.lower(): v for k, v in headers.items() if any(k.lower().startswith(p.lower()) for p in prefixes)}


def engine(obs: Obs) -> list[tuple[str, str]]:
    return [(r["method"], r["path"]) for r in obs.trace["engine_requests"]]


def engine_without_probes(obs: Obs) -> list[tuple[str, str]]:
    """The engine calls that carry the request itself (the capacity probe ``/system_stats`` is read apart)."""
    return [pair for pair in engine(obs) if pair[1] != "/system_stats"]


def engine_bodies(obs: Obs, path: str) -> list[Any]:
    return [r.get("body") for r in obs.trace["engine_requests"] if r["path"] == path]


def sleeps(obs: Obs) -> list[float]:
    return [s["delay"] for s in obs.trace["gateway_sleeps"]]


def receipts(obs: Obs) -> list[dict[str, Any]]:
    return list((obs.trace["receipts"] or {}).get("receipts") or [])


def receipt_row(obs: Obs, i: int = 0) -> dict[str, Any] | None:
    rows = receipts(obs)
    return rows[i] if len(rows) > i else None


def receipt_summary(obs: Obs) -> list[tuple[Any, ...]]:
    return [(r["status"], r["protocol"], r["intent"], r["path"]) for r in receipts(obs)]


def receipt_fields(obs: Obs, *keys: str, i: int = 0) -> dict[str, Any] | None:
    row = receipt_row(obs, i)
    if row is None:
        return None
    body = row.get("receipt_json") or {}
    return {k: body.get(k) for k in keys}


def media_jobs(obs: Obs) -> list[dict[str, Any]]:
    return list((obs.trace["media_jobs"] or {}).get("media_jobs") or [])


def retention_destinations(obs: Obs) -> int:
    return len((obs.trace["media_jobs"] or {}).get("retention_fact_destinations") or [])


def origins(obs: Obs) -> list[Any]:
    return [a["json"].get("origin") for a in obs.trace["assets"] if isinstance(a.get("json"), dict)]


def asset_files(obs: Obs) -> list[tuple[str, int]]:
    return [(a["suffix"], a.get("size")) for a in obs.trace["assets"] if a["suffix"] != ".json"]


def data_items(obs: Obs, i: int = 0) -> list[dict[str, Any]]:
    data = doc(obs, i)
    return list(data.get("data") or []) if isinstance(data, dict) else []


def top_keys(obs: Obs, i: int = 0) -> list[str]:
    data = doc(obs, i)
    return sorted(k for k in data if k != "data") if isinstance(data, dict) else []


def item_keys(obs: Obs, i: int = 0) -> list[list[str]]:
    return [sorted(item) for item in data_items(obs, i)]


Reader = Callable[[Obs], Any]


def assert_same(old: Obs, row: Obs, *readers: Reader | tuple[str, Reader]) -> None:
    """Every reader gives the same fact for the old route and the row. The old route must have answered at all."""
    assert old.trace["caller"], "the old route did not answer: the scenario is wrong"
    problems = []
    for reader in readers:
        label, read = reader if isinstance(reader, tuple) else (getattr(reader, "__name__", "fact"), reader)
        before, after = read(old), read(row)
        if before != after:
            problems.append(f"{label}:\n  old: {before!r}\n  row: {after!r}")
    assert not problems, "the row path differs from the old route:\n" + "\n".join(problems)


def old_has(old: Obs, label: str, value: Any) -> None:
    """Guard: the scenario really reached the behaviour the item names on the old route."""
    assert value, f"scenario does not reach {label} on the old route (value {value!r})"
