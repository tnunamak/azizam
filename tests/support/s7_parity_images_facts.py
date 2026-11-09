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
    """The gateway's waits that belong to the request. The receipt heartbeat of a handed-off job is infrastructure both
    routes share; it sleeps one fixed interval and the rig's instant clock makes it spin, so it is left out."""
    from gateway.receipts import HEARTBEAT_INTERVAL_S
    return [s["delay"] for s in obs.trace["gateway_sleeps"] if s["delay"] != HEARTBEAT_INTERVAL_S]


def receipts(obs: Obs) -> list[dict[str, Any]]:
    return list((obs.trace["receipts"] or {}).get("receipts") or [])


def receipt_row(obs: Obs, i: int = 0) -> dict[str, Any] | None:
    rows = receipts(obs)
    return rows[i] if len(rows) > i else None


# Allowed delta ``receipt_status_words``: a row receipt says succeeded where the old route said ok, and failed (with
# terminal_reason) where it said error. A refusal says rejected on both. The readers name the row's words.
RECEIPT_WORDS = {"ok": "succeeded", "error": "failed"}


def receipt_status(row: dict[str, Any]) -> Any:
    return RECEIPT_WORDS.get(row["status"], row["status"])


def receipt_summary(obs: Obs) -> list[tuple[Any, ...]]:
    return [(receipt_status(r), r["protocol"], r["intent"], r["path"]) for r in receipts(obs)]


def receipt_fields(obs: Obs, *keys: str, i: int = 0) -> dict[str, Any] | None:
    row = receipt_row(obs, i)
    if row is None:
        return None
    body = row.get("receipt_json") or {}
    return {k: body.get(k) for k in keys}


# Receipt metadata the lifecycle owner adds to every receipt it closes; the old image routes wrote none (delta receipt_status_words).
LIFECYCLE_METADATA = ("terminal_reason",)


def receipt_work_fields(obs: Obs, *keys: str, i: int = 0) -> dict[str, Any] | None:
    """``receipt_fields`` with the lifecycle owner's own metadata left out."""
    found = receipt_fields(obs, *keys, i=i)
    if found and isinstance(found.get("metadata"), dict):
        found["metadata"] = {k: v for k, v in found["metadata"].items() if k not in LIFECYCLE_METADATA}
    return found


def media_jobs(obs: Obs) -> list[dict[str, Any]]:
    return list((obs.trace["media_jobs"] or {}).get("media_jobs") or [])


def job_ids(obs: Obs) -> list[str]:
    return [str(j["job_id"]) for j in media_jobs(obs)]


def canon_jobs(obs: Obs, value: Any) -> Any:
    """``value`` with each job id of this observation replaced by its place (``<job:1>``, ``<job:2>``, in store order), in every
    string it appears in. A job id is random, so two observations are compared by where an id is used, not by its text: an id
    that differs from the stored job's anywhere (the column, the job document, an answer that names it) stays raw text here and
    makes the observations differ."""
    text = json.dumps(value, sort_keys=True, default=str)
    for position, job_id in sorted(enumerate(job_ids(obs), 1), key=lambda pair: -len(pair[1])):
        text = text.replace(job_id, f"<job:{position}>")
    return json.loads(text)


def job_rows(obs: Obs) -> list[dict[str, Any]]:
    """Every stored job row in full, ids mapped (``canon_jobs``)."""
    return canon_jobs(obs, media_jobs(obs))


# The run core's own bookkeeping on a job it runs (allowed delta ``job_row_metadata``, J07), with the exact values every image
# job document carries. ``mode`` names the run core where the old routes named the route. ``run_receipt_id`` and
# ``protocol_submitting`` are the run core's receipt link and its submit marker. Nothing else is left out of a job document.
RUN_CORE_METADATA = {
    "binding_key": "comfyui", "failure_text": "category_copy", "frontend": "image-jobs", "resource_class": "image",
    "retries": "binding", "run_receipt_path": "/v1/images/generations", "protocol_submitting": False,
}


def _without_run_core(value: Any) -> Any:
    """``value`` with the run core's bookkeeping leaves of every job document metadata removed, each only when it holds its exact
    value (a different value stays and makes the observations differ); the run-core mode becomes the old routes' mode."""
    if isinstance(value, list):
        return [_without_run_core(v) for v in value]
    if not isinstance(value, dict):
        return value
    out = {k: _without_run_core(v) for k, v in value.items()}
    meta = out.get("metadata")
    if isinstance(meta, dict) and meta.get("mode") == "workflow.run":
        meta = {k: v for k, v in meta.items() if not (k in RUN_CORE_METADATA and v == RUN_CORE_METADATA[k])}
        for key, kind in (("run_receipt_id", str), ("protocol_job", str), ("result_metadata", dict)):   # the run core's link, its adapter pointer, the graph runner's result facts
            if isinstance(meta.get(key), kind):
                del meta[key]
        meta["mode"] = "async_image_generation"
        out["metadata"] = meta
    return out


def job_document(obs: Obs, i: int = 1) -> Any:
    """The whole body of answer ``i`` (a job document, results or events), ids mapped, the run core's bookkeeping leaves
    (``RUN_CORE_METADATA``) left out when they hold their exact values."""
    return _without_run_core(canon_jobs(obs, doc(obs, i)))


def retention_destinations(obs: Obs) -> int:
    return len((obs.trace["media_jobs"] or {}).get("retention_fact_destinations") or [])


def origins(obs: Obs) -> list[Any]:
    return [a["json"].get("origin") for a in obs.trace["assets"] if isinstance(a.get("json"), dict)]


def asset_files(obs: Obs) -> list[tuple[str, int, str | None]]:
    """The stored files by suffix, size and the sha256 of their bytes: same-size wrong bytes differ."""
    return [(a["suffix"], a.get("size"), a.get("sha256")) for a in obs.trace["assets"] if a["suffix"] != ".json"]


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
    assert old.trace["caller"] and "exception" not in old.extra, f"the old route did not answer: the scenario is wrong ({old.extra.get('exception')})"
    problems = []
    if "exception" in row.extra:
        problems.append(f"the row path crashed: {row.extra['exception']}")
    for reader in readers:
        label, read = reader if isinstance(reader, tuple) else (getattr(reader, "__name__", "fact"), reader)
        before, after = read(old), read(row)
        if before != after:
            problems.append(f"{label}:\n  old: {before!r}\n  row: {after!r}")
    assert not problems, "the row path differs from the old route:\n" + "\n".join(problems)


def old_has(old: Obs, label: str, value: Any) -> None:
    """Guard: the scenario really reached the behaviour the item names on the old route."""
    assert value, f"scenario does not reach {label} on the old route (value {value!r})"
