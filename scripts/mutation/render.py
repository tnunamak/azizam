"""Render a scripts/mutate JSON report as markdown: render.py report.json"""

import json
import sys


def main(path: str) -> None:
    report = json.load(open(path))
    print(f"# Mutation report, {report.get('sha', '')[:12]}\n")
    if report.get("refused"):
        print(f"**REFUSED:** {report['refused']}\n")
    base = report.get("baseline")
    if base:
        print(f"Baseline: {base['tests']} tests in {base['seconds']} s; "
              f"failing anywhere (outside a refused module's selection they do not count): {len(base['failed_anywhere'])}\n")
    print("| module | scope | generated | killed | timeout | survived | no-cov | equivalent | error | unproductive | score |")
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for m in report.get("modules", []):
        if "refused" in m:
            print(f"| {m['module']} | {m['scope']} | REFUSED: {m['refused']} |||||||||")
            continue
        c = m["counts"]
        score = "-" if m["score"] is None else f"{100 * m['score']:.1f}%"
        print(f"| {m['module']} | {m['scope']} | {m['generated']} | {c['killed']} | {c['timeout']} | {c['survived']} "
              f"| {c['no-cov']} | {c['equivalent']} | {c['error']} | {m['unproductive']} | {score} |")
    print("\nScore = (killed + timeout) / (generated - equivalent).\n")
    for m in report.get("modules", []):
        if "refused" in m:
            print(f"## {m['module']}: refused\n\nFailing selected tests: {m['baseline']['failed']}\n")
            continue
        print(f"## {m['module']}\n")
        for label, key in (("Unclassified survivors", "unclassified"),
                           ("Classified but now killed (stale)", "stale_classifications"),
                           ("Classifications with no matching mutant", "classifications_without_mutant")):
            if m[key]:
                print(f"{label}: {len(m[key])}\n")
        rows = [r for r in m["mutants"] if r["status"] in ("survived", "no-cov", "equivalent", "error")]
        if rows:
            print("| mutant | status | class | tests | reason / detail |")
            print("|---|---|---|---:|---|")
            for r in rows:
                note = r.get("killed_by") or r.get("reason") or r.get("detail", "")
                print(f"| `{r['id']}` | {r['status']} | {r.get('class', '')} | {r.get('tests', 0)} | {note} |")
            print()


if __name__ == "__main__":
    main(sys.argv[1])
