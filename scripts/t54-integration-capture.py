"""Temporary hermetic integration diagnostics; never opens operator stores."""
import os
assert os.environ.get('HERMETIC_TEST_NAMESPACE') == '1'
import sys, json, tempfile
from pathlib import Path
sys.path[:0] = [str(Path.cwd()), str(Path.cwd() / 'tests')]
import conftest
import proxy
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
