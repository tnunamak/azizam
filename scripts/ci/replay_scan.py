"""Policy scan and size limits for replay transcripts (TEST_STRATEGY 4.7).

Stdlib only: scripts/ci/policy.py imports it, and so do the recorder and tests.
Scrubbing is incomplete by nature, so this scan is a backstop behind the
per-protocol allowlists. A finding names a category and a file, never the value.
"""

from __future__ import annotations

import ipaddress
import datetime
import functools
import json
import re
import tomllib
from pathlib import Path
from urllib.parse import unquote

METADATA_LIMIT = 256 * 1024
CORPUS_LIMIT = 20 * 1024 * 1024
# Transcripts are scenario files whose provenance.source is "transcript".
SCENARIO_DIR = "tests/support/fake_upstream/scenarios"
MEDIA_DIR = "tests/support/fake_upstream/media"

_KEY = re.compile(r"(?<![A-Za-z0-9])(?:(?:opk|oak)_[A-Za-z0-9]{4,}|sk-[A-Za-z0-9_-]{6,})")
# A token holds a digit; plain prose ("bearer credentials") does not.
_BEARER = re.compile(r"\bbearer[ \t]+(?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]{8,}", re.I)
# Case-sensitive: home directories are /home/<user>, /Users/<user> (macOS) and
# /root; a case-insensitive match also hit JSON key paths such as "/users/[]".
_HOME = re.compile(r"/(?:home|Users)/[^/\s\"'\\]+|/root(?:/|\b)|[A-Za-z]:[\\/]+[Uu][Ss][Ee][Rr][Ss][\\/]+")
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_IPV6 = re.compile(r"(?<![\w:])(?:[a-fA-F0-9]{0,4}:){2,}[a-fA-F0-9:.%]+")
_PRIVATE = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10", "fc00::/7", "fe80::/10"))


# Reviewed exceptions: a file, a category, and why. Never a directory or a glob.
EXCEPTIONS = {
    # A copy of the public Ollama API document; its example Modelfile path is /Users/matt.
    ("tests/fixtures/ollama-api.md", "home-path"),
}


def categories(raw: bytes) -> set[str]:
    """Which kinds of private data ``raw`` holds: credential, home-path, lan-address."""
    return set(_categories(raw))


@functools.lru_cache(maxsize=256)
def _categories(raw: bytes) -> frozenset[str]:
    """Scans the raw text, every decoded JSON string and key (so escapes cannot hide
    a value), and up to four rounds of percent-decoding. A pure function of the bytes,
    cached because the policy self-tests scan the same unchanged corpus in every run.
    """
    texts = [raw.decode("utf-8", "replace")]
    try:
        _strings(json.loads(texts[0]), texts)
    except (ValueError, RecursionError):
        pass
    found: set[str] = set()
    for text in texts:
        for _ in range(4):
            if _KEY.search(text) or _BEARER.search(text):
                found.add("credential")
            if _HOME.search(text):
                found.add("home-path")
            for candidate in _IPV4.findall(text) + _IPV6.findall(text):
                if _is_private(candidate):
                    found.add("lan-address")
            decoded = unquote(text)
            if decoded == text:
                break
            text = decoded
    return frozenset(found)


def _strings(value, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            out.append(str(key))
            _strings(item, out)
    elif isinstance(value, list):
        for item in value:
            _strings(item, out)


def _is_private(candidate: str) -> bool:
    try:
        address = ipaddress.ip_address(candidate.split("%", 1)[0])
    except ValueError:
        return False
    address = getattr(address, "ipv4_mapped", None) or address
    return any(address.version == net.version and address in net for net in _PRIVATE)


def is_transcript(path: Path) -> bool:
    if path.suffix != ".json":
        return False
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and isinstance(data.get("provenance"), dict) and data["provenance"].get("source") == "transcript"


def check_text(label: str, raw: bytes) -> list[str]:
    return [f"replay: {label}: disallowed {category}" for category in sorted(categories(raw))]


def check_root(root: Path) -> list[str]:
    """Problems in the committed corpus under ``root``, one line each."""
    problems: list[str] = []
    scenarios, media = root / SCENARIO_DIR, root / MEDIA_DIR
    corpus = 0
    scanned: list[Path] = []
    if scenarios.is_dir():
        for path in sorted(scenarios.rglob("*.json")):
            if is_transcript(path):
                size = path.stat().st_size
                corpus += size
                scanned.append(path)
                if size > METADATA_LIMIT:
                    problems.append(f"replay: {path.relative_to(root).as_posix()}: metadata exceeds 256 KiB")
    if media.is_dir():
        for path in sorted(media.rglob("*")):
            if path.is_symlink():
                problems.append(f"replay: {path.relative_to(root).as_posix()}: symlink in corpus")
            elif path.is_file():
                corpus += path.stat().st_size
                scanned.append(path)
    # Every fixture directory is scanned too, so a planted value cannot hide in one.
    for base in (root / "tests", root / "web"):
        if base.is_dir():
            scanned += [p for p in base.rglob("*") if p.is_file() and not p.is_symlink()
                        and "fixtures" in p.relative_to(base).parts and "node_modules" not in p.parts]
    for path in dict.fromkeys(scanned):
        label = path.relative_to(root).as_posix()
        problems += [p for p in check_text(label, path.read_bytes())
                     if (label, p.rsplit("disallowed ", 1)[1]) not in EXCEPTIONS]
    if corpus > CORPUS_LIMIT:
        problems.append("replay: corpus exceeds 20 MiB")
    return problems


CELLS = "tests/live/rp_cells.toml"
REQUEST_SETS = "tests/live/request_sets.json"
PENDING_WINDOW_DAYS = 90
LOCAL_DEFINITIONS = "registry/engine_definitions.json"


def local_upstream_protocols(root: Path) -> set[str]:
    """Upstream protocols the shipped engine definitions speak (registry/engine_definitions.json).

    Read as data, without importing the gateway. Remote providers (the provider
    catalog) are not counted: replay recordings are made against local engines
    only (owner rule, TEST_STRATEGY 4.7), and remote upstreams belong to the
    budgeted live lanes.
    """
    source = root / LOCAL_DEFINITIONS
    if not source.is_file():
        return set()
    data = json.loads(source.read_text(encoding="utf-8"))
    return {str(row["record"]["protocol"]) for row in data.get("definitions", ()) if row.get("record", {}).get("protocol")}


def check_cells(root: Path, today: datetime.date) -> list[str]:
    """Every request set has one RP cell: ``recorded`` with its transcript, an unexpired
    ``pending``, or ``not-applicable``.

    A pending cell names an owner and a reason and expires within 90 days; an
    expired one fails, and a renewal states why in its PR (TEST_STRATEGY 4.2.7).
    A not-applicable cell names the registry ``upstream_protocol`` it stands for
    and a reason; it is valid only while no shipped engine definition speaks
    that protocol upstream. When one does, the cell fails until it is recorded
    (or pending), and so does a local upstream protocol with no request set.
    """
    cells_path, sets_path = root / CELLS, root / REQUEST_SETS
    if not sets_path.is_file():
        return []
    sets = json.loads(sets_path.read_text(encoding="utf-8"))
    cells = tomllib.loads(cells_path.read_text(encoding="utf-8")).get("cell", []) if cells_path.is_file() else []
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    local = local_upstream_protocols(root)
    for cell in cells:
        key = (cell.get("protocol"), cell.get("set"))
        name = f"{key[0]}/{key[1]}"
        if key in seen:
            problems.append(f"replay cell {name}: listed twice")
        seen.add(key)
        if key[1] not in sets.get(key[0], {}):
            problems.append(f"replay cell {name}: no such request set")
        status = cell.get("status")
        if status == "recorded":
            path = root / SCENARIO_DIR / str(key[0]) / f"rp-{key[1]}.json"
            if not path.is_file() or not is_transcript(path):
                problems.append(f"replay cell {name}: recorded, but its transcript is missing")
        elif status == "pending":
            expires = cell.get("expires_on")
            if not (isinstance(cell.get("owner"), str) and cell["owner"].strip() and isinstance(cell.get("reason"), str) and cell["reason"].strip()):
                problems.append(f"replay cell {name}: pending needs owner and reason")
            if not isinstance(expires, datetime.date):
                problems.append(f"replay cell {name}: pending needs expires_on")
            elif expires < today:
                problems.append(f"replay cell {name}: pending cell expired on {expires}")
            elif expires > today + datetime.timedelta(days=PENDING_WINDOW_DAYS):
                problems.append(f"replay cell {name}: expires_on is more than {PENDING_WINDOW_DAYS} days ahead")
        elif status == "not-applicable":
            upstream = cell.get("upstream_protocol")
            if not (isinstance(upstream, str) and upstream.strip() and isinstance(cell.get("reason"), str) and cell["reason"].strip()):
                problems.append(f"replay cell {name}: not-applicable needs upstream_protocol and reason")
            elif upstream in local:
                problems.append(f"replay cell {name}: an engine definition speaks {upstream} upstream; record it (status recorded or pending)")
        else:
            problems.append(f"replay cell {name}: status must be 'recorded', 'pending' or 'not-applicable'")
    problems += [f"replay cell {protocol}/{name}: request set has no cell"
                 for protocol, named in sets.items() if isinstance(named, dict) and not protocol.startswith("_")
                 for name in named if (protocol, name) not in seen]
    problems += [f"replay: an engine definition speaks {protocol} upstream, but no request set covers it"
                 for protocol in sorted(local) if protocol not in sets]
    return problems
