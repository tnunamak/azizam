"""Temporary hermetic integration diagnostics; never opens operator stores."""
import os
assert os.environ.get('HERMETIC_TEST_NAMESPACE') == '1'
import sys, json, tempfile
from pathlib import Path
sys.path[:0] = [str(Path.cwd()), str(Path.cwd() / 'tests')]
import conftest
import proxy
if sys.argv[1:] == ['--catalog-proof']:
    import tests.test_engine_contract_oracle as oracle
    original = oracle._digest
    def check(value):
        if isinstance(value, dict) and set(value) == {'models', 'capabilities', 'engine'}:
            wanted = 'f358d972e72f0a2ad15acdb6b89ff724b6c8743643afa7724803b2aff82e5517'
            rows = value['models']
            possibilities = [(kind, inputs, outputs) for kind in ('image', 'language', None) for inputs in (['image'], ['text','image'], ['text']) for outputs in (['text'], ['image'], [])]
            matched = []
            for kind, inputs, outputs in possibilities:
                retired = ('face-likeness', kind, {'input_modalities': inputs, 'output_modalities': outputs})
                for i in range(len(rows)+1):
                    if original({**value, 'models': [*rows[:i], retired, *rows[i:]]}) == wanted:
                        matched.append((i, retired))
            matches = matched
            print('CATALOG_PROOF=' + json.dumps({'old_digest_restored_by_face_likeness_only': matches}))
            assert len(matches) == 1
        return original(value)
    oracle._digest = check
    oracle.test_public_catalog_surfaces_are_frozen(Path(tempfile.mkdtemp()))
    raise SystemExit(0)
from gateway import policy_protocol
for intent in ('chat.completions', 'provider.native'):
    try:
        print('PROTOCOL', intent, policy_protocol.protocol_for_intent(intent))
    except Exception as e:
        print('PROTOCOL', intent, str(e), policy_protocol._row_protocols().get(intent))
from tests.test_content_access_inventory import _observed
print('INVENTORY_JSON=' + json.dumps(_observed(), sort_keys=True))
from tests.test_workflow_request_map_rows import registry, MODES, request_mapping
print('REQUEST_MAP_JSON=' + json.dumps({f'{r.id} {m}': list(map(list, request_mapping(r,m).items())) for r in registry().records() for m in MODES},sort_keys=True))
from tests.config_homes_golden import capture
print('CONFIG_HOMES_JSON=' + json.dumps(capture(proxy, Path(tempfile.mkdtemp())),sort_keys=True))
from tests.workflow_reader_outputs import read_all
print('WORKFLOW_READERS_JSON=' + json.dumps(read_all(registry().records()),sort_keys=True))
from gateway import workflow_schema_migration as migration
from gateway.discovery import bundled_workflow_manifests
from tests.support.section_edits import without_section_additions
for key, rec in migration.frozen_release_records().items():
    plan=migration.plan_workflow_schema_2({'workflows':{key:{'manifest':rec['manifest'], 'enabled':True, 'editable':True, 'source':'ui'}}},{'workflows':{}})
    stored=plan.stored['workflows'][key]['manifest']
    want=without_section_additions(bundled_workflow_manifests()[key],stored)
    if stored != want:
        print('MIGRATION_JSON='+json.dumps({'id':key,'converted':stored,'bundled':want},sort_keys=True))
