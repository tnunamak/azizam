#!/usr/bin/env python3
"""Check local Markdown link targets (including heading fragments), offline.

Pass Markdown paths explicitly to check a documentation change. External URLs,
email links and absolute deployment URLs are outside this filesystem check.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


def prose(text):
    text = re.sub(r"^\s*(`{3,}|~{3,}).*?^\s*\1\s*$", "", text, flags=re.M | re.S)
    return re.sub(r"`[^`\n]+`", "", text)


def anchors(text):
    found = set(re.findall(r'(?:id|name)=[\"\']([^\"\']+)', text))
    duplicates = {}
    for heading in re.findall(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", text, flags=re.M):
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        number = duplicates.get(slug, 0)
        duplicates[slug] = number + 1
        found.add(f"{slug}-{number}" if number else slug)
    return found


def check(paths):
    failures = []
    checked = 0
    for path in paths:
        text = prose(path.read_text())
        targets = re.findall(r"!?\[[^\]\n]*\]\(\s*(<[^>]+>|[^\s)]+)", text)
        targets += re.findall(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]+>|\S+)", text, flags=re.M)
        for target in targets:
            url = urlsplit(target.strip("<>"))
            if url.scheme or url.netloc or url.path.startswith("/"):
                continue
            local = path.parent / unquote(url.path) if url.path else path
            checked += 1
            if not local.exists():
                failures.append(f"{path}: missing target {target}")
            elif url.fragment and local.suffix.lower() == ".md":
                if unquote(url.fragment) not in anchors(local.read_text()):
                    failures.append(f"{path}: missing heading {target}")
    return checked, failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    checked, failures = check(args.files)
    for failure in failures:
        print(failure)
    print(f"Checked {checked} local links in {len(args.files)} files; {len(failures)} failures")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
