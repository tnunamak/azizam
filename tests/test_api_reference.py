"""The offline documentation must show the real API without CDN or script dependencies."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from gateway.api_reference import register_api_reference, render_reference


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_reference_renders_actual_routes_without_external_assets(path):
    app = FastAPI(docs_url=None, redoc_url=None)

    @app.post("/v1/example", summary="Create an example")
    def example(name: str):
        return {"name": name}

    register_api_reference(app)
    response = TestClient(app).get(path)
    assert response.status_code == 200
    assert '<h1>' in response.text
    assert '/v1/example' in response.text
    assert 'Create an example' in response.text
    assert 'parameters' in response.text
    assert '<script' not in response.text
    assert 'https://' not in response.text
    assert "default-src 'none'" in response.headers['content-security-policy']


def test_schema_copy_is_escaped():
    html = render_reference({'info': {'title': '<script>alert(1)</script>'}, 'paths': {
        '/unsafe<path>': {'get': {'summary': '<img src=x onerror=alert(1)>', 'description': '</pre><script>bad</script>'}}
    }})
    assert '<script>' not in html
    assert '<img' not in html
    assert '&lt;script&gt;' in html
    assert '/unsafe&lt;path&gt;' in html
