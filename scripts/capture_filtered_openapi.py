#!/usr/bin/env python3
"""Emit the projected OpenAPI document used by the route-graph digest test."""
import json
import os
from pathlib import Path
import sys
if os.environ.get('HERMETIC_TEST_NAMESPACE') != '1':
    raise SystemExit('Use the hermetic runner')
root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / 'tests')]
import conftest  # noqa: F401
import pytest
from tests import test_route_graph_freeze as test

def emit(app):
    print('FILTERED_OPENAPI_JSON=' + json.dumps(app.openapi(), sort_keys=True))
    return test._frozen()['openapi_sha256']

test.route_graph.openapi_digest = emit
with pytest.MonkeyPatch.context() as patch:
    test.test_the_openapi_document_is_unchanged(patch)
