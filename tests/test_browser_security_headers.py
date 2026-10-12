"""Exercise the actual browser-header middleware without booting live proxy config."""
import ast
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient


def test_browser_media_csp_allows_generated_media_without_widening_scripts():
    tree = ast.parse((Path(__file__).parents[1] / "proxy.py").read_text())
    names = {"_HTML_CONTENT_TYPES", "_BROWSER_UI_PREFIXES", "_SECURITY_HEADERS"}
    nodes = [node for node in tree.body if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id in names for target in node.targets)]
    nodes += [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
              and node.name == "_security_headers_middleware"]
    app = FastAPI()
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "proxy-security-headers", "exec"),
         {"app": app, "Request": Request})

    @app.get("/ui/try/")
    def try_page():
        return HTMLResponse("<audio controls></audio>")

    @app.get("/me/api/home")
    def home():
        return {"ok": True}

    with TestClient(app) as client:
        for path in ("/ui/try/", "/me/api/home"):
            response = client.get(path)
            assert response.status_code == 200
            directives = dict(part.strip().split(" ", 1) for part in response.headers["content-security-policy"].split(";") if part.strip())
            assert directives["media-src"] == "'self' blob: data:"
            # Try shows a workflow's picture bytes from a blob: URL.
            assert directives["img-src"] == "'self' data: blob:"
            assert directives["script-src"] == "'self' 'unsafe-inline'"
            assert directives["frame-ancestors"] == "'none'"
            assert response.headers["x-content-type-options"] == "nosniff"
