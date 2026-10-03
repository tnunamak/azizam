#!/usr/bin/env python3
"""Render registered public routes without starting servers or reading host state.

The CLI delegates imports to the project's hermetic pytest runner. Commit changes
first; run this script with --write, then commit the generated document.
"""
from __future__ import annotations

import argparse
import inspect
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BEGIN = "<!-- support-matrix-begin -->"
END = "<!-- support-matrix-end -->"


def public_routes(app):
    """Include HTTP and WS APIs, including schema-hidden compatibility routes.

    Account/admin browser surfaces and static mounts are outside this API matrix.
    FastAPI versions can wrap included routers; traverse those as well.
    """
    def walk(routes):
        for route in routes:
            if getattr(route, "path", None) is not None:
                yield route
            else:
                router = getattr(route, "original_router", None) or getattr(route, "router", None)
                if router is not None:
                    yield from walk(router.routes)

    for route in walk(app.routes):
        path = route.path
        if path.startswith(("/admin", "/me", "/ui", "/setup", "/login", "/auth", "/showcase", "/internal")):
            continue
        if getattr(route, "endpoint", None) is not None:
            yield route


def classification(route):
    """Use shipped unsupported markers and conservative protocol-subset labels."""
    endpoint = inspect.unwrap(route.endpoint)
    try:
        source = inspect.getsource(endpoint)
    except (OSError, TypeError):
        source = ""
    # A1111's authentication wrapper closes over its real handler.
    for cell in getattr(endpoint, "__closure__", None) or ():
        value = cell.cell_contents
        if inspect.isfunction(value) and value.__name__ == "handler":
            source += inspect.getsource(value)
    if "_ollama_unsupported_model_mutation(" in source or "raise A1111Error(501," in source:
        return "unsupported", "501 after authentication and policy checks"
    if "_openai_unsupported_api(" in source:
        return "unsupported", "400 unsupported_operation (not 501)"
    if endpoint.__name__ == "reserved_openai_decisions":
        return "partial", "501 without configured provider passthrough; no local schema"
    if endpoint.__name__ in {"count_response_input_tokens", "cancel_response"}:
        return "unsupported", "400 unsupported_operation; no local tokenizer or cancellation"
    if endpoint.__name__ == "mcp_no_stream":
        return "unsupported", "405; stateless POST transport only"
    if route.path == "/{path:path}":
        return "unsupported", "404 for unmatched public paths"
    if route.path.startswith(("/anthropic/", "/ollama/", "/api/", "/sdapi/", "/mcp")) or any(
        marker in route.path for marker in ("/messages", "/responses", "/realtime", "/videos")
    ):
        return "partial", "Protocol subset or configured-backend feature; see vendor facades"
    return "supported", "Implemented route; requires applicable policy and backend configuration"


def render(app):
    rows = {}
    for route in public_routes(app):
        methods = getattr(route, "methods", None) or {"WS"}
        status, note = classification(route)
        endpoint = inspect.unwrap(route.endpoint)
        filename = inspect.getsourcefile(endpoint)
        try:
            relative = Path(filename).resolve().relative_to(ROOT).as_posix() if filename else None
        except ValueError:
            relative = None
        evidence = f"[{relative}](../{relative}) `{endpoint.__name__}`" if relative else "FastAPI built-in"
        for method in sorted(methods):
            rows[(route.path, method)] = f"| `{method}` | `{route.path}` | {status} | {note} | {evidence} |"
    return "\n".join([
        "# Public API support matrix", "",
        "Generated from the registered FastAPI app, including routes hidden from OpenAPI.",
        "No servers are started. Account/admin, setup, showcase, internal routes and static mounts are excluded.",
        "Public discovery and diagnostic pages are included alongside JSON and WebSocket APIs.", "",
        "**Supported** means the handler exists, not complete vendor-spec conformance or backend availability.",
        "**Partial** identifies protocol subsets and configuration-dependent vendor features.",
        "**Unsupported** identifies explicit rejection routes; the actual rejection status is shown.",
        "Authentication or policy rejection can occur before the listed unsupported response.",
        "All execution routes still need a configured, available backend and access-plan permission.", "",
        "See [vendor facades](VENDOR_FACADES.md) for protocol limits and [configuration ownership](CONFIG_SOURCE_OF_TRUTH.md).", "",
        "Regenerate from a clean committed tree: `python3 scripts/gen_support_matrix.py --write`.",
        "Check: `scripts/run-hermetic-pytest.sh -q tests/test_support_matrix.py`.", "",
        "| Method | Route | Support | Limits | Code evidence |",
        "|---|---|---|---|---|", *[rows[key] for key in sorted(rows)], "",
    ])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="update docs/SUPPORT_MATRIX.md")
    args = parser.parse_args()
    result = subprocess.run([
        str(ROOT / "scripts/run-hermetic-pytest.sh"), "-q", "-s",
        "tests/test_support_matrix.py::test_emit_support_matrix",
    ], cwd=ROOT, text=True, capture_output=True)
    if result.returncode:
        sys.stderr.write(result.stdout + result.stderr)
        return result.returncode
    document = result.stdout.split(BEGIN, 1)[1].split(END, 1)[0].strip() + "\n"
    if args.write:
        (ROOT / "docs/SUPPORT_MATRIX.md").write_text(document)
        print("Updated docs/SUPPORT_MATRIX.md")
    else:
        print(document, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
