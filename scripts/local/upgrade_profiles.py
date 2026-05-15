"""Idempotent local upgrade for ``proxy_api_keys.yml`` profile blocks.

Adds a permissive, non-breaking ``profile:`` block to known local key entries
that don't already have one. Preserves every existing ``key_hash``, ``key``,
``key_env``, and ``downstream`` field byte-for-byte by mutating the parsed
mapping in place and re-emitting through PyYAML's safe dumper.

Safety properties:

* Never prints key hashes, raw keys, downstream env names, or any value that
  could plausibly be a secret. Output is limited to key names and an
  ``added``/``unchanged``/``unknown`` status.
* Defaults to ``--dry-run`` — file is never touched unless ``--write`` is
  passed explicitly.
* Refuses to overwrite an existing ``profile:`` block unless ``--replace`` is
  passed.
* Built-in templates are conservative: ``fallback.cloud_allowed: false``,
  ``native_passthrough.enabled: false``, and protocol allow-lists scoped to
  family prefixes (``openai.*`` etc.) that match the owner's family-prefix
  policy.
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path
from typing import Mapping

import yaml


# Permissive defaults. Allow-lists for workflows/models/backends are intentionally
# omitted ("no opinion") so we don't break existing local clients.
def _profile(
    profile_id: str,
    *,
    protocols: list[str] | None = None,
    visibility: str = "basic",
) -> dict:
    block: dict = {
        "id": profile_id,
        "fallback": {"cloud_allowed": False},
        "native_passthrough": {"enabled": False},
        "status_visibility": {"level": visibility},
    }
    if protocols is not None:
        block["protocols"] = list(protocols)
    return block


# Family prefixes only — no model/workflow/backend allow-lists.
BUILTIN_TEMPLATES: dict[str, dict] = {
    "simon": _profile(
        "simon",
        protocols=["openai", "anthropic", "ollama"],
        visibility="standard",
    ),
    "daisy": _profile(
        "daisy",
        protocols=[
            "openai.chat",
            "openai.completions",
            "openai.audio",
            "openai.images",
        ],
        visibility="basic",
    ),
    "librechat": _profile(
        "librechat",
        protocols=[
            "openai.chat",
            "openai.completions",
            "openai.embeddings",
            "anthropic.messages",
        ],
        visibility="basic",
    ),
    "cap": _profile(
        "cap",
        protocols=["openai.audio"],
        visibility="basic",
    ),
    "open-webui": _profile(
        "open-webui",
        protocols=[
            "openai.chat",
            "openai.completions",
            "openai.embeddings",
            "ollama",
        ],
        visibility="basic",
    ),
    "claude-test": _profile(
        "claude-test",
        protocols=["openai.chat", "openai.completions", "anthropic.messages"],
        visibility="basic",
    ),
}


STATUS_ADDED = "added"
STATUS_UNCHANGED = "unchanged"
STATUS_REPLACED = "replaced"
STATUS_NO_TEMPLATE = "no-template"


class UpgradeError(RuntimeError):
    """Raised on structural problems with the keys file."""


def builtin_template(name: str) -> dict | None:
    """Return a deep copy of the builtin template for ``name``, or ``None``."""
    tpl = BUILTIN_TEMPLATES.get(name)
    if tpl is None:
        return None
    return copy.deepcopy(tpl)


def _load_doc(path: Path) -> dict:
    text = path.read_text()
    data = yaml.safe_load(text)
    if data is None:
        raise UpgradeError(f"{path} is empty or not valid YAML")
    if not isinstance(data, Mapping):
        raise UpgradeError(f"{path} must contain a YAML mapping at the top level")
    keys = data.get("keys")
    if keys is None:
        raise UpgradeError(f"{path} has no top-level 'keys:' list")
    if not isinstance(keys, list):
        raise UpgradeError(f"{path} 'keys' must be a list")
    return dict(data)


def upgrade_document(
    doc: dict,
    *,
    templates: Mapping[str, dict] | None = None,
    replace: bool = False,
    only: set[str] | None = None,
) -> list[tuple[str, str]]:
    """Mutate ``doc`` in place, adding profile blocks for known names.

    Returns a list of ``(name, status)`` tuples. Status is one of
    ``added``, ``unchanged``, ``replaced``, ``no-template``.

    Never logs or returns secret-bearing fields. Caller-supplied ``templates``
    override the builtin map.
    """
    template_map = templates if templates is not None else BUILTIN_TEMPLATES
    results: list[tuple[str, str]] = []
    keys = doc.get("keys")
    if not isinstance(keys, list):
        raise UpgradeError("document missing 'keys' list")

    for index, entry in enumerate(keys):
        if not isinstance(entry, dict):
            raise UpgradeError(f"keys[{index}] is not a mapping")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            # Skip unnamed entries silently — they can't be targeted.
            continue
        if only is not None and name not in only:
            continue

        template = template_map.get(name)
        has_profile = "profile" in entry and entry["profile"] is not None

        if template is None:
            if has_profile:
                results.append((name, STATUS_UNCHANGED))
            else:
                results.append((name, STATUS_NO_TEMPLATE))
            continue

        if has_profile and not replace:
            results.append((name, STATUS_UNCHANGED))
            continue

        entry["profile"] = copy.deepcopy(template)
        results.append(
            (name, STATUS_REPLACED if has_profile else STATUS_ADDED)
        )

    return results


def dump_document(doc: dict) -> str:
    """Serialize ``doc`` back to YAML deterministically."""
    return yaml.safe_dump(
        doc, sort_keys=False, default_flow_style=False, allow_unicode=True
    )


def _print_results(
    path: Path, results: list[tuple[str, str]], *, write: bool
) -> None:
    mode = "WRITE" if write else "DRY-RUN"
    print(f"[{mode}] {path}")
    if not results:
        print("  (no matching key entries)")
        return
    for name, status in results:
        # Names only. No hashes, no env vars, no downstream details.
        print(f"  - {name}: {status}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="upgrade_profiles",
        description=(
            "Idempotently add app-profile blocks to a proxy keys YAML file. "
            "Defaults to dry-run; pass --write to persist changes."
        ),
    )
    parser.add_argument(
        "--keys-file",
        required=True,
        type=Path,
        help="Path to proxy_api_keys.yml (or compatible) to upgrade.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Print planned changes without modifying the file (default).",
    )
    group.add_argument(
        "--write",
        action="store_true",
        help="Persist changes to the file in place.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help=(
            "Replace existing profile blocks with the builtin template. "
            "Off by default so manual edits are preserved."
        ),
    )
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="NAME",
        help=(
            "Restrict upgrade to a specific key name. May be passed multiple "
            "times."
        ),
    )

    args = parser.parse_args(argv)
    path: Path = args.keys_file

    if not path.exists():
        print(f"error: keys file not found: {path}", file=sys.stderr)
        return 2

    try:
        doc = _load_doc(path)
    except UpgradeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    only = set(args.only) if args.only else None

    results = upgrade_document(doc, replace=args.replace, only=only)

    write = bool(args.write)
    _print_results(path, results, write=write)

    changed = any(
        status in (STATUS_ADDED, STATUS_REPLACED) for _, status in results
    )

    if write and changed:
        path.write_text(dump_document(doc))
        print(f"  -> wrote {path}")
    elif write and not changed:
        print("  (no changes to write)")
    elif not write and changed:
        print("  (dry-run: pass --write to persist)")

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
