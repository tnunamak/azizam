"""Capture each document behind the route-graph digest in the pytest environment."""
import json
from tests import test_route_graph_freeze as frozen


def test_capture_filtered_openapi(monkeypatch, record_testsuite_property):
    original = frozen.route_graph.openapi_digest
    captured = []
    def capture(app):
        captured.append(json.dumps(app.openapi(), sort_keys=True))
        record_testsuite_property(f'filtered_openapi_{len(captured)}', captured[-1])
        return original(app)
    monkeypatch.setattr(frozen.route_graph, 'openapi_digest', capture)
    frozen.test_the_openapi_document_is_unchanged(monkeypatch)
