"""Exercise the actual browser-header middleware without booting live proxy config."""
import ast
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient


def test_browser_media_csp_allows_generated_media_without_widening_scripts():
    tree = ast.parse((Path(__file__).parents[1] / "proxy.py").read_text())
    names = {"_HTML_CONTENT_TYPES", "_BROWSER_UI_PREFIXES", "_SECURITY_HEADERS", "_BROWSER_AUTH_PATHS"}
    nodes = [node for node in tree.body if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id in names for target in node.targets)]
    nodes += [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
              and node.name in {"_security_headers_middleware", "_mark_browser_auth_response"}]
    app = FastAPI()
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "proxy-security-headers", "exec"),
         {"app": app, "Request": Request, "Response": Response})

    @app.get("/try/")
    def try_page():
        return HTMLResponse("<audio controls></audio>")

    @app.get("/me/api/home")
    def home():
        return {"ok": True}

    with TestClient(app) as client:
        for path in ("/try/", "/me/api/home"):
            response = client.get(path)
            assert response.status_code == 200
            directives = dict(part.strip().split(" ", 1) for part in response.headers["content-security-policy"].split(";") if part.strip())
            assert directives["media-src"] == "'self' blob: data:"
            # Try shows a workflow's picture bytes from a blob: URL.
            assert directives["img-src"] == "'self' data: blob:"
            assert directives["script-src"] == "'self' 'unsafe-inline'"
            assert directives["frame-ancestors"] == "'none'"
            assert response.headers["x-content-type-options"] == "nosniff"
