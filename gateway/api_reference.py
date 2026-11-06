"""Offline, server-rendered API reference derived from the gateway's OpenAPI."""
from __future__ import annotations

from html import escape
import json

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

_CSS = """
:root{color-scheme:light dark;font-family:system-ui,sans-serif;line-height:1.5}
body{margin:0;background:light-dark(#fafafa,#18181b);color:light-dark(#27272a,#f4f4f5)}
main{max-width:960px;margin:auto;padding:32px 20px}a{color:light-dark(#087f74,#5eead4)}
h1{font-size:28px}h2{font-size:20px;margin-top:32px}summary{cursor:pointer;padding:14px 0}
details{border-bottom:1px solid light-dark(#d4d4d8,#3f3f46)}code,pre{font-family:ui-monospace,monospace}
summary code{overflow-wrap:anywhere}pre{padding:16px;overflow:auto;background:light-dark(#f4f4f5,#27272a);border-radius:8px}
.method{display:inline-block;min-width:68px;font-weight:600;color:light-dark(#087f74,#5eead4)}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto}}
"""


def render_reference(schema: dict) -> str:
    """Escape schema content and render usable endpoint details without JavaScript."""
    sections = []
    for path, methods in sorted(schema.get("paths", {}).items()):
        for method, operation in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete", "options", "head", "trace"}:
                continue
            summary = escape(str(operation.get("summary", "")))
            description = escape(str(operation.get("description", "")))
            details = escape(json.dumps(operation, indent=2, ensure_ascii=False))
            sections.append(
                f'<details><summary><span class="method">{escape(method.upper())}</span> '
                f'<code>{escape(path)}</code> {summary}</summary>'
                f'<p>{description}</p><pre>{details}</pre></details>'
            )
    title = escape(str(schema.get("info", {}).get("title", "Gateway API")))
    components = escape(json.dumps(schema.get("components", {}), indent=2, ensure_ascii=False))
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{title} · API reference</title><style>{_CSS}</style></head><body><main>'
        f'<h1>{title} API reference</h1><p>Requests, responses and authentication from this gateway’s OpenAPI schema.</p>'
        '<p><a href="/openapi.json">Download OpenAPI JSON</a> · <a href="/">Open gateway</a></p>'
        f'<h2>Endpoints</h2>{"".join(sections)}'
        f'<h2>Shared schemas</h2><details><summary>Types and security schemes</summary><pre>{components}</pre></details>'
        '</main></body></html>'
    )


def register_api_reference(app: FastAPI) -> None:
    """Expose both familiar documentation URLs with no external asset requests."""
    @app.get("/docs", include_in_schema=False, response_class=HTMLResponse)
    @app.get("/redoc", include_in_schema=False, response_class=HTMLResponse)
    def reference() -> HTMLResponse:
        return HTMLResponse(render_reference(app.openapi()), headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-cache",
        })
