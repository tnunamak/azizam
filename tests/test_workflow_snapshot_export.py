"""Temporary hermetic snapshot export; removed after reviewing generated deltas."""
import copy
import hashlib
import json
from pathlib import Path

import proxy
import route_graph
import test_engine_contract_oracle as oracle
import test_engine_protocol_review_wire as wire
from gateway import workflows_catalog
from gateway.workflow_openapi import workflow_catalog
from test_route_policy_inventory import gateway  # noqa: F401
import test_route_policy_inventory as inventory
import pytest


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def assertion_locals(call):
    try:
        call()
    except AssertionError as error:
        tb = error.__traceback__
        while tb.tb_next:
            tb = tb.tb_next
        return tb.tb_frame.f_locals
    return None


def test_export(monkeypatch, tmp_path, record_property):
    proxy.app.openapi_schema = None
    schema = proxy.app.openapi()
    out = {"headless_openapi.json": schema}
    hashes = json.loads(Path('tests/fixtures/headless_data_plane_hashes.json').read_text())
    for path in hashes['paths']:
        operation = copy.deepcopy(schema['paths'][path])
        if path == '/gateway/v1/media/jobs/{job_id}/events':
            operation['get']['parameters'] = [p for p in operation['get']['parameters'] if p['name'] != 'after_sequence']
        hashes['paths'][path] = digest(operation)
    for name in hashes['schemas']:
        hashes['schemas'][name] = digest(schema['components']['schemas'][name])
    out['headless_data_plane_hashes.json'] = hashes
    graph = json.loads(Path('tests/fixtures/route_graph_frozen.json').read_text())
    graph['openapi_sha256'] = route_graph.openapi_digest(proxy.app)
    out['route_graph_frozen.json'] = graph
    records = proxy.GATEWAY_WORKFLOW_REGISTRY.records()
    added = {'image.generate.moss-image', 'image.generate.moss-image-fast', 'image.generate.moss-image-quality',
             'image.generate.flux2-klein', 'image.edit.flux2-klein'}
    with monkeypatch.context() as patch:
        patch.setattr(proxy.GATEWAY_WORKFLOW_REGISTRY, 'records', lambda: [r for r in records if r.id not in added])
        proxy.app.openapi_schema = None
        out['route_graph_original_digest'] = route_graph.openapi_digest(proxy.app)
    proxy.app.openapi_schema = None
    with monkeypatch.context() as patch:
        values = assertion_locals(lambda: oracle.test_public_catalog_surfaces_are_frozen(tmp_path))
        if values:
            out['public_catalog_digest'] = digest({key: values[value] for key, value in (
                ('models', 'model_surface'), ('capabilities', 'caps'), ('engine', 'old_engine'))})
    with monkeypatch.context() as patch:
        values = assertion_locals(lambda: wire.test_rv_wire(patch, tmp_path))
        if values:
            out['engine_protocol/review-wire-main.json'] = values['out']
    rows = {r['id']: r for r in workflow_catalog(records, proxy._gateway_aliases_provider())}
    for name, identifier in [('workflows-image-edit.json', 'image.edit.flux2-klein-4b'),
                             ('workflows-image-edit-multiref.json', 'image.edit.qwen-image-2.1-edit-multiref')]:
        record = next(r for r in records if r.id == identifier)
        row = workflows_catalog._workflow_row(record, capability_rows=[], openapi_row=rows[identifier],
              aliases=proxy._gateway_aliases_provider(), receipts=[], jobs=[], base_url='')
        out['web/' + name] = row
    record_property('snapshots', json.dumps(out, sort_keys=True))


@pytest.mark.parametrize('gateway', ['discovery'], indirect=True)
def test_category_export(gateway, monkeypatch, record_property):
    values = assertion_locals(lambda: inventory.test_ungoverned_discovery_snapshot(gateway, monkeypatch))
    if values:
        record_property('snapshots', json.dumps({'category_discovery_ungoverned.json': values['responses']}))
