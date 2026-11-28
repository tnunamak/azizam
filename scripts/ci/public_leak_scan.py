#!/usr/bin/env python3
"""Public-repository leak scan: private data must not enter the tracked tree.

Stdlib only: scripts/ci/policy.py imports it, and so does its test.

Generic rules, checked on the text of every tracked file and on every tracked
path (a file name can leak too):

- lan-address: an IPv4 host address in a private (RFC 1918) or carrier-grade
  NAT (RFC 6598) range. CIDR notation (10.0.0.0/8) and the bounds of those
  ranges name a network, not a host, so they pass.
- home-path: /home/<user>/ or /Users/<user>/.
- email: an address whose domain is not reserved for documentation
  (example.com, example.org, example.net, .example, .test, .invalid,
  .localhost, localhost; RFC 2606 and RFC 6761).

Each generic finding can be excused by an entry in ALLOWLIST: a tracked file,
a rule and why. An entry is exact (never a directory or a glob), and an entry
that excuses nothing is stale and fails, so the list can only shrink.

Private rules: one regular expression per line in the file named by
$AZIZAM_PUBLIC_LEAK_PATTERNS, for values the repository must not name, even in
this scan (an owner's address, host names, family names). Blank lines and
lines starting with "#" are skipped. Private findings have no allowlist. When
the variable is unset (or names a file that does not exist) the scan reads
nothing, says "private patterns: not configured" and checks the generic rules
only. scripts/ci-local sets the variable from
${XDG_CONFIG_HOME:-~/.config}/azizam/public-leak-patterns.txt when that file exists.

A finding names the file, the line and the rule, never the value.

The subject is the files git tracks. A root that is not a git checkout (the
copied trees of the policy tests) is walked instead, without dependency and
output directories.

Usage: public_leak_scan.py [--root CHECKOUT] [--patterns FILE]
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ALLOWLIST = "scripts/ci/public_leak_allowlist.tsv"
PATTERNS_ENV = "AZIZAM_PUBLIC_LEAK_PATTERNS"
RULES = ("lan-address", "home-path", "email")
# Read at most this much of one file; larger tracked files are lock files and media.
MAX_BYTES = 8 * 1024 * 1024

_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d|/\d)")
_PRIVATE = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"))
# The first and last address of each range: prose names a range by them.
_RANGE_BOUNDS = {str(a) for n in _PRIVATE for a in (n.network_address, n.broadcast_address)}
_HOME = re.compile(r"/(?:home|Users)/[A-Za-z0-9_][A-Za-z0-9_.-]*/")
_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.([A-Za-z]{2,24}))\b")
_DOC_DOMAIN = re.compile(r"(?:^|\.)(?:example\.(?:com|org|net)|example|test|invalid|localhost)$|^localhost$", re.I)
# Not an address: a retina asset (icon@2x.png) or a systemd instance (user@1000.service).
_NOT_EMAIL_TLD = {"png", "jpg", "jpeg", "webp", "gif", "svg", "service", "scope", "slice", "socket", "target", "timer"}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int  # 0 for a finding in the path itself
    rule: str  # a generic rule, or "private #N" (the pattern's line number)

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else f"{self.path} (file name)"
        return f"leaks: {where}: {self.rule}"


def generic_rules(text: str) -> set[str]:
    """The generic rules ``text`` breaks."""
    found = set()
    for candidate in _IPV4.findall(text):
        if candidate in _RANGE_BOUNDS:
            continue
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if any(address in network for network in _PRIVATE):
            found.add("lan-address")
            break
    if _HOME.search(text):
        found.add("home-path")
    for match in _EMAIL.finditer(text):
        domain, tld = match.group(1), match.group(2).lower()
        if tld not in _NOT_EMAIL_TLD and not re.match(r"[23]x$", domain.split(".")[0]) \
                and not _DOC_DOMAIN.search(domain):
            found.add("email")
            break
    return found


def load_patterns(path: Path | None) -> tuple[list[tuple[int, re.Pattern[str]]] | None, list[str]]:
    """Private patterns as (line number, regex); None when the file is absent."""
    if path is None or not path.is_file():
        return None, []
    patterns, problems = [], []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            patterns.append((number, re.compile(line)))
        except re.error as error:
            problems.append(f"leaks: private pattern on line {number} does not compile ({error})")
    return patterns, problems


def load_allowlist(root: Path) -> tuple[dict[tuple[str, str], str], list[str]]:
    entries: dict[tuple[str, str], str] = {}
    problems = []
    path = root / ALLOWLIST
    if not path.is_file():
        return entries, problems
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 3 or not all(part.strip() for part in parts):
            problems.append(f"leaks: {ALLOWLIST}:{number} needs path<TAB>rule<TAB>reason")
            continue
        relative, rule, reason = (part.strip() for part in parts)
        if rule not in RULES:
            problems.append(f"leaks: {ALLOWLIST}:{number} names unknown rule {rule!r}; use one of {', '.join(RULES)}")
        elif (relative, rule) in entries:
            problems.append(f"leaks: {ALLOWLIST}:{number} repeats {relative} {rule}")
        entries[(relative, rule)] = reason
    return entries, problems


# Skipped when the root is not a git checkout and has to be walked.
_WALK_PRUNED = {".git", "node_modules", "__pycache__", "venv", ".venv", ".next", "test-results"}


def tracked_files(root: Path) -> list[str]:
    """The files git tracks under root; every file when root is not a git checkout."""
    if not (root / ".git").exists():
        found = []
        for directory, subdirectories, names in os.walk(root):
            subdirectories[:] = [name for name in subdirectories if name not in _WALK_PRUNED]
            found += [Path(directory, name).relative_to(root).as_posix() for name in names]
        return sorted(found)
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True)
    return sorted(name for name in result.stdout.decode("utf-8", "surrogateescape").split("\0") if name)


def _text(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_BYTES)
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None  # binary: only its name is scanned
    return raw.decode("utf-8", "replace")


def scan(root: Path, files: list[str], patterns: list[tuple[int, re.Pattern[str]]] | None) -> list[Finding]:
    findings = []
    for relative in files:
        for rule in sorted(generic_rules(relative)):
            findings.append(Finding(relative, 0, rule))
        for number, pattern in patterns or ():
            if pattern.search(relative):
                findings.append(Finding(relative, 0, f"private #{number}"))
        text = _text(root / relative)
        if text is None:
            continue
        # Most files hold none of the trigger characters; skip the per-line pass.
        quick = generic_rules(text)
        private_hits = [(n, p) for n, p in patterns or () if p.search(text)]
        if not quick and not private_hits:
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            for rule in sorted(generic_rules(line) & quick):
                findings.append(Finding(relative, line_number, rule))
            for number, pattern in private_hits:
                if pattern.search(line):
                    findings.append(Finding(relative, line_number, f"private #{number}"))
    return findings


def check_root(root: Path, patterns_path: Path | None = None, files: list[str] | None = None) -> tuple[list[str], str]:
    """Problems for policy.py, and a summary that says whether private patterns ran."""
    if patterns_path is None:
        configured = os.environ.get(PATTERNS_ENV)
        patterns_path = Path(configured).expanduser() if configured else None
    patterns, problems = load_patterns(patterns_path)
    allowed, allowlist_problems = load_allowlist(root)
    problems += allowlist_problems
    files = tracked_files(root) if files is None else files
    used: set[tuple[str, str]] = set()
    for finding in scan(root, files, patterns):
        key = (finding.path, finding.rule)
        if key in allowed:
            used.add(key)
        else:
            problems.append(str(finding))
    for relative, rule in sorted(set(allowed) - used):
        problems.append(f"leaks: {ALLOWLIST} excuses {relative} {rule}, which has no such finding; remove the entry")
    private = "not configured" if patterns is None else f"{len(patterns)} checked"
    return problems, f"private patterns: {private}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--patterns", type=Path, help=f"private patterns (default: the file named by ${PATTERNS_ENV})")
    args = parser.parse_args()
    problems, summary = check_root(args.root.resolve(), args.patterns)
    for problem in problems:
        print(problem)
    print(f"leaks: {'FAILED' if problems else 'ok'}: {len(problems)} findings; {summary}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
