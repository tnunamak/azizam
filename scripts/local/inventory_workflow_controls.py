#!/usr/bin/env python3
"""Inventory bindable controls in a ComfyUI (API-format) workflow JSON.

This is a *tooling* helper for local power users / agents who want to write
honest workflow-extension manifests (see ``manifests/workflows/*.yml``)
without coupling the architecture to one specific graph. It does **not**
emit a manifest. It emits a sanitized report of what controls *could* be
bound, so a human or agent can pick the right semantic names.

Privacy rules (enforced):

  - Never print prompt text, image filenames, URLs, base64, or any other
    long / arbitrary string from the workflow's input values.
  - Strings are summarized as a length bucket plus a coarse category
    (``text`` / ``filename`` / ``path`` / ``url`` / ``base64`` / ``empty``).
  - Numbers, bools, and null are printed as type only; their actual value
    is *not* included in the report. Boolean ``True``/``False`` is
    intentionally not emitted as a value because seemingly-safe booleans
    can encode toggles a workflow author considers private.
  - Graph edges (``[upstream_node, slot]`` lists) are skipped — they are
    not bindable controls.

The "candidate logical name" is a conservative guess only. It is *not*
authoritative; it exists to point a reviewer at likely bindings. The
allowed candidate vocabulary is the small set the gateway already
recognises portably::

    width, height, steps, guidance, cfg, seed, denoise, image, text

Public API:

    inventory_workflow(workflow: dict) -> list[ControlEntry]
    redact_value(value) -> dict
    summarize(entries) -> dict

CLI:

    python scripts/local/inventory_workflow_controls.py <workflow.json>
    python scripts/local/inventory_workflow_controls.py <workflow.json> --json

The CLI never imports the proxy and never talks to any service.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


# Conservative allow-list. Adding a name here means "the gateway treats this
# as a portable logical input". Keep this in lockstep with
# ``gateway/image_extensions.py`` and friends.
CANDIDATE_LOGICAL_NAMES: tuple[str, ...] = (
    "width",
    "height",
    "steps",
    "guidance",
    "cfg",
    "seed",
    "denoise",
    "image",
    "text",
)

# Inputs whose names match these (case-insensitive) map 1:1 to a logical name.
_DIRECT_NAME_ALIASES: dict[str, str] = {
    "width": "width",
    "height": "height",
    "steps": "steps",
    "guidance": "guidance",
    "guidance_scale": "guidance",
    "cfg": "cfg",
    "seed": "seed",
    "denoise": "denoise",
    "denoising_strength": "denoise",
    "image": "image",
    "source_image": "image",
    "text": "text",
    "prompt": "text",
}

# Class-type substrings that, combined with a known text-ish input name,
# bump a guess to a higher confidence. These are heuristics, not facts.
_TEXT_NODE_CLASS_HINTS = ("textencode", "clip", "prompt", "tangoflux", "sampler")

# Class-type substrings that suggest the node carries an image control.
_IMAGE_NODE_CLASS_HINTS = ("loadimage", "imageinput", "image")


# --- redaction --------------------------------------------------------------


_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]{40,}$")
_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_PATH_HINT_RE = re.compile(r"[\\/]")
_FILENAME_EXTS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".tiff",
    ".mp4",
    ".mov",
    ".webm",
    ".mp3",
    ".wav",
    ".flac",
    ".ogg",
    ".safetensors",
    ".ckpt",
    ".gguf",
    ".pt",
    ".bin",
)


def _length_bucket(n: int) -> str:
    if n == 0:
        return "empty"
    if n <= 32:
        return "short"
    if n <= 256:
        return "medium"
    return "long"


def _string_category(value: str) -> str:
    """Coarse category for a string. Never returns the string itself.

    Categories are conservative — when in doubt we say ``"text"`` so the
    reviewer treats the field as potentially sensitive.
    """
    if not value:
        return "empty"
    stripped = value.strip()
    if not stripped:
        return "empty"
    lower = stripped.lower()
    if _URL_RE.match(stripped):
        return "url"
    if any(lower.endswith(ext) for ext in _FILENAME_EXTS):
        return "filename"
    if _PATH_HINT_RE.search(stripped) and len(stripped) <= 512:
        return "path"
    # Base64-ish guard: only flag long, mostly-alphanumeric runs with no
    # spaces. Short euler/dpmpp_2m enum strings must not be misclassified.
    if len(stripped) >= 40 and " " not in stripped and _BASE64_RE.match(stripped):
        return "base64"
    return "text"


def redact_value(value: Any) -> dict[str, Any]:
    """Return a sanitized description of ``value``.

    Never includes the raw value for strings. For numbers/bools/None we
    report only the Python type — not the literal — to keep the output
    safe to paste into chat, issues, or shared docs.
    """
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        # NB: must check bool before int (bool is a subclass of int).
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {
            "type": "string",
            "length_bucket": _length_bucket(len(value)),
            "category": _string_category(value),
        }
    if isinstance(value, list):
        return {"type": "array", "length_bucket": _length_bucket(len(value))}
    if isinstance(value, dict):
        return {"type": "object", "length_bucket": _length_bucket(len(value))}
    return {"type": "unknown"}


# --- inventory --------------------------------------------------------------


def _is_graph_edge(value: Any) -> bool:
    """ComfyUI API JSON encodes graph edges as ``[upstream_node_id, slot]``.

    Those are not bindable controls — they wire nodes together. Treat any
    list whose first element is a string-or-int and whose second is an int
    as an edge. (This matches every workflow shipped in ``workflows/``.)
    """
    if not isinstance(value, list) or len(value) != 2:
        return False
    head, tail = value
    return isinstance(head, (str, int)) and isinstance(tail, int)


def _guess_candidate(
    input_name: str, class_type: str | None, redacted: dict[str, Any]
) -> str | None:
    """Conservative mapping from (input_name, class_type) to logical name.

    Returns ``None`` when the mapping is not obvious. This is deliberately
    pessimistic — the manifest author is expected to fill in the rest.
    """
    name_l = input_name.lower()
    class_l = (class_type or "").lower()

    direct = _DIRECT_NAME_ALIASES.get(name_l)
    if direct is None:
        return None

    if direct == "text":
        # Only flag a string-valued input on a text-ish node. Avoids
        # mislabeling ``prompt`` inputs that secretly carry a config
        # struct or a list of references.
        if redacted.get("type") != "string":
            return None
        if not any(h in class_l for h in _TEXT_NODE_CLASS_HINTS):
            # Generic "text" input on an unknown node: still likely text,
            # but be conservative — surface it but tag the guess lightly.
            return "text"
        return "text"

    if direct == "image":
        # ``image`` as an *input* is bindable when it's a primitive string
        # (a filename); when it's a graph edge we never reach this branch.
        if redacted.get("type") != "string":
            return None
        if any(h in class_l for h in _IMAGE_NODE_CLASS_HINTS):
            return "image"
        return "image"

    return direct


@dataclass(frozen=True)
class ControlEntry:
    """One bindable-input row. All fields are safe to print/serialize."""

    node_id: str
    class_type: str | None
    input_name: str
    value: dict[str, Any]
    candidate_logical_name: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["notes"] = list(self.notes)
        return d


def inventory_workflow(workflow: dict) -> list[ControlEntry]:
    """Return a sanitized inventory of bindable controls in ``workflow``.

    ``workflow`` is a ComfyUI API-format graph: a top-level dict whose keys
    are node ids and whose values are ``{"class_type": ..., "inputs": {...}}``.
    A top-level ``_meta`` key (proxy convention) is ignored. The function
    is pure; it never reads from disk or the network.
    """
    if not isinstance(workflow, dict):
        raise TypeError("workflow must be a dict (ComfyUI API-format graph)")

    entries: list[ControlEntry] = []
    for node_id, node in workflow.items():
        if node_id == "_meta" or node_id.startswith("__"):
            continue
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        if class_type is not None and not isinstance(class_type, str):
            class_type = None
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for input_name, value in inputs.items():
            if not isinstance(input_name, str):
                continue
            if _is_graph_edge(value):
                continue
            redacted = redact_value(value)
            notes: list[str] = []
            # Flag potentially-sensitive shapes so reviewers don't bind them
            # without thinking.
            if redacted.get("type") == "string":
                cat = redacted.get("category")
                if cat in ("base64", "url", "filename", "path"):
                    notes.append(f"string looks like {cat}; review before binding")
                if redacted.get("length_bucket") == "long":
                    notes.append("long string; likely prompt or payload")
            candidate = _guess_candidate(input_name, class_type, redacted)
            entries.append(
                ControlEntry(
                    node_id=str(node_id),
                    class_type=class_type,
                    input_name=input_name,
                    value=redacted,
                    candidate_logical_name=candidate,
                    notes=tuple(notes),
                )
            )
    return entries


def summarize(entries: Iterable[ControlEntry]) -> dict[str, Any]:
    """Aggregate stats useful for a manifest author at a glance."""
    entries = list(entries)
    by_candidate: dict[str, list[str]] = {}
    for e in entries:
        if e.candidate_logical_name is None:
            continue
        by_candidate.setdefault(e.candidate_logical_name, []).append(
            f"{e.node_id}.{e.input_name}"
        )
    return {
        "total_controls": len(entries),
        "controls_with_candidate": sum(
            1 for e in entries if e.candidate_logical_name is not None
        ),
        "candidates": {
            name: sorted(targets) for name, targets in sorted(by_candidate.items())
        },
        "allowed_logical_names": list(CANDIDATE_LOGICAL_NAMES),
    }


# --- CLI --------------------------------------------------------------------


def _render_text(entries: list[ControlEntry], summary: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"controls: {summary['total_controls']}")
    lines.append(
        "with candidate logical name: "
        f"{summary['controls_with_candidate']} / {summary['total_controls']}"
    )
    lines.append("")
    header = f"{'node':>6}  {'class_type':<28}  {'input':<24}  {'type':<10}  candidate"
    lines.append(header)
    lines.append("-" * len(header))
    for e in entries:
        vt = e.value.get("type", "?")
        extra = ""
        if vt == "string":
            extra = f" ({e.value.get('category')}/{e.value.get('length_bucket')})"
        cand = e.candidate_logical_name or "-"
        cls = (e.class_type or "?")[:28]
        lines.append(
            f"{e.node_id:>6}  {cls:<28}  {e.input_name:<24}  "
            f"{(vt + extra):<10}  {cand}"
        )
        for n in e.notes:
            lines.append(f"        note: {n}")
    if summary["candidates"]:
        lines.append("")
        lines.append("candidate roll-up:")
        for name, targets in summary["candidates"].items():
            lines.append(f"  {name}: {', '.join(targets)}")
    return "\n".join(lines)


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Inventory bindable controls in a ComfyUI API-format workflow."
    )
    parser.add_argument("workflow", help="Path to a ComfyUI API-format JSON workflow")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON (machine-readable) instead of the human-readable table",
    )
    args = parser.parse_args(argv)

    path = Path(args.workflow)
    try:
        data = json.loads(path.read_text())
    except OSError as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: {path} is not valid JSON: {exc}", file=sys.stderr)
        return 2

    if not isinstance(data, dict):
        print(
            "error: workflow JSON must be an object keyed by node id",
            file=sys.stderr,
        )
        return 2

    entries = inventory_workflow(data)
    summary = summarize(entries)

    if args.json:
        print(
            json.dumps(
                {
                    "workflow_path": str(path),
                    "summary": summary,
                    "controls": [e.to_dict() for e in entries],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(_render_text(entries, summary))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
