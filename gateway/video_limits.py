"""Published video limits: what a client may ask for, read from ``videos`` config.

The gateway enforces these in ``proxy.py``; this module only reports them so clients
(``/gateway/v1/capabilities``, ``x-azizam-workflows``) need not hard-code them.
"""

from __future__ import annotations

from typing import Any, Mapping

DEFAULT_MAX_TOTAL_SECONDS = 40.0
DEFAULT_CHAIN_SEGMENT_SECONDS = 8.0


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if float(value).is_integer() else float(value)


def _chain_workflow(workflows: Mapping[str, Any], spec: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]] | None:
    """The chain workflow a workflow's long requests run on (itself, or its ``chain_workflow``)."""
    if spec.get("builder"):
        return None  # itself a chain; handled by the caller
    target = spec.get("chain_workflow")
    chain = workflows.get(target) if isinstance(target, str) else None
    if isinstance(chain, Mapping) and chain.get("builder"):
        return str(target), chain
    return None


def video_limits(videos_cfg: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Per-workflow limits keyed by workflow id.

    * ``max_total_seconds`` - cap on the stitched length of a long video, source included
      (``videos.max_total_seconds``). Only workflows that can chain have it.
    * ``segment_seconds`` - longest single segment of that chain (``chain_segment_seconds``,
      per workflow, else the global default).
    * ``chain_workflow`` - the workflow that serves long requests, when it is not this one.
    * ``min_seconds`` / ``max_seconds`` - declared length range of a plain (non-chain) workflow.
    * ``frame_rate``, ``default_size``, ``max_size`` - as declared.
    """
    cfg = videos_cfg or {}
    workflows = cfg.get("workflows") or {}
    total = _number(cfg.get("max_total_seconds", DEFAULT_MAX_TOTAL_SECONDS))
    default_segment = _number(cfg.get("chain_segment_seconds", DEFAULT_CHAIN_SEGMENT_SECONDS))
    out: dict[str, dict[str, Any]] = {}
    for workflow_id, raw in workflows.items():
        if not isinstance(workflow_id, str) or not isinstance(raw, Mapping):
            continue
        limits: dict[str, Any] = {}
        is_chain = bool(raw.get("builder"))
        linked = _chain_workflow(workflows, raw)
        chain_spec = raw if is_chain else (linked[1] if linked else None)
        if chain_spec is not None:
            limits["max_total_seconds"] = total
            limits["segment_seconds"] = _number(chain_spec.get("chain_segment_seconds", default_segment))
            if linked:
                limits["chain_workflow"] = linked[0]
        if not is_chain:
            for key in ("min_seconds", "max_seconds"):
                value = _number(raw.get(key))
                if value is not None:
                    limits[key] = value
        for key in ("frame_rate",):
            value = _number(raw.get(key))
            if value is not None:
                limits[key] = value
        for key in ("default_size", "max_size"):
            if isinstance(raw.get(key), str):
                limits[key] = raw[key]
        out[workflow_id] = limits
    return out


def limits_for_workflow(
    published: Mapping[str, Mapping[str, Any]], workflow: str | None
) -> Mapping[str, Any]:
    """Limits for a workflow named by its config key (``video-ltx-2.5-long``) or its registry id
    (``video.generate.ltx-2.5-long``); empty when it has none."""
    if not workflow:
        return {}
    if workflow in published:
        return published[workflow]
    for prefix in ("video.generate.", "video.edit."):
        if workflow.startswith(prefix):
            return published.get(f"video-{workflow[len(prefix):]}", {})
    return {}
