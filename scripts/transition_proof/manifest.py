"""The case manifest: load it, check every body against its sha256, and say what the cache check reads.

Nothing here sends a request. ``load_cases`` is the whole of ``--dry-run``'s validation, and the real run calls it
first, so a body that changed on disk is refused before the first request leaves.

Manifest shape (the ``cases`` list is the operator's frozen-case format; ``cache_check`` is the data that tells the
harness how to read a reply)::

    {"cache_check": {"cached_tokens_path": "usage.prompt_tokens_details.cached_tokens",
                     "prompt_tokens_path": "usage.prompt_tokens",
                     "min_cached_fraction": 0.8,
                     "max_timing_ratio": 0.5},          # optional
     "cases": [{"label": "4k", "file": "4k-seed.json", "sha256": "...",
                "followup": "A question.", "count_estimate": {"input_tokens": 4158},
                "cache_check": {"min_cached_fraction": 0.9}}]}   # optional per-case override

A path is dotted; a number selects a list item (``choices.0.message.content``).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


class InputError(Exception):
    """The manifest, a body or an argument is wrong. Nothing was sent."""


_SHA = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_PATHS = ("cached_tokens_path", "prompt_tokens_path")


@dataclass(frozen=True)
class CacheCheck:
    cached_tokens_path: str
    prompt_tokens_path: str
    min_cached_fraction: float
    max_timing_ratio: Optional[float]


@dataclass(frozen=True)
class Case:
    label: str
    file: str
    sha256: str
    body: dict[str, Any]
    followup: str
    estimated_prompt_tokens: Optional[int]
    cache: CacheCheck


def read_path(data: Any, path: str) -> Any:
    """The value at a dotted path, or None when any step is missing."""
    node = data
    for part in path.split("."):
        if isinstance(node, dict):
            if part not in node:
                return None
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _cache_check(label: str, merged: dict[str, Any]) -> CacheCheck:
    where = f"case {label!r} cache_check"
    for name in _REQUIRED_PATHS:
        if not isinstance(merged.get(name), str) or not merged[name]:
            raise InputError(f"{where}: {name} is required (a dotted JSON path into the reply)")
    fraction = _number(merged.get("min_cached_fraction"))
    if fraction is None or not 0 < fraction <= 1:
        raise InputError(f"{where}: min_cached_fraction is required and must be in (0, 1]")
    ratio = None
    if merged.get("max_timing_ratio") is not None:
        ratio = _number(merged["max_timing_ratio"])
        if ratio is None or ratio <= 0:
            raise InputError(f"{where}: max_timing_ratio must be a positive number")
    return CacheCheck(merged["cached_tokens_path"], merged["prompt_tokens_path"], fraction, ratio)


def _body(label: str, raw: bytes, file: str) -> dict[str, Any]:
    try:
        body = json.loads(raw)
    except ValueError as error:
        raise InputError(f"case {label!r}: {file} is not JSON ({error})") from None
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list) or not messages or not all(
        isinstance(m, dict) and "role" in m and "content" in m for m in messages
    ):
        raise InputError(f"case {label!r}: {file} needs a non-empty messages list of role and content")
    if body.get("stream"):
        raise InputError(f"case {label!r}: {file} sets stream; the proof reads usage and timing from one JSON reply")
    return body


def load_cases(manifest_path: Path) -> list[Case]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise InputError(f"cannot read the manifest {manifest_path}: {error.strerror}") from None
    except ValueError as error:
        raise InputError(f"the manifest {manifest_path} is not JSON ({error})") from None
    entries = manifest.get("cases") if isinstance(manifest, dict) else None
    if not isinstance(entries, list) or not entries:
        raise InputError("the manifest needs a non-empty cases list")
    shared = manifest.get("cache_check") or {}
    cases: list[Case] = []
    for index, entry in enumerate(entries):
        label = entry.get("label") if isinstance(entry, dict) else None
        if not isinstance(label, str) or not label:
            raise InputError(f"cases[{index}] needs a label")
        if any(case.label == label for case in cases):
            raise InputError(f"case label {label!r} appears twice")
        file, expected, followup = entry.get("file"), entry.get("sha256"), entry.get("followup")
        if not isinstance(file, str) or not file:
            raise InputError(f"case {label!r} needs a file")
        if not isinstance(expected, str) or not _SHA.match(expected):
            raise InputError(f"case {label!r} needs a 64-hex sha256")
        if not isinstance(followup, str) or not followup.strip():
            raise InputError(f"case {label!r} needs a followup question")
        try:
            raw = (manifest_path.parent / file).read_bytes()
        except OSError as error:
            raise InputError(f"case {label!r}: cannot read {file}: {error.strerror}") from None
        actual = hashlib.sha256(raw).hexdigest()
        if actual != expected:
            raise InputError(f"case {label!r}: {file} sha256 is {actual}, the manifest says {expected}")
        estimate = _number((entry.get("count_estimate") or {}).get("input_tokens"))
        cases.append(Case(
            label=label, file=file, sha256=expected, body=_body(label, raw, file), followup=followup,
            estimated_prompt_tokens=int(estimate) if estimate else None,
            cache=_cache_check(label, {**shared, **(entry.get("cache_check") or {})}),
        ))
    return cases
