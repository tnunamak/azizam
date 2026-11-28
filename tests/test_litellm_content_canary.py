"""Single-protection mutations prove that the pinned sinks are exercised."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.litellm_content import child_environment
from gateway.log_scrub import forms
from tests.support.litellm_content_canary import CONTENT_MARKERS


MUTATIONS = ['none', 'tool_inventory', 'default_log', 'spend_without_prompts', 'spend_content']


@pytest.fixture(scope='module')
def canary(tmp_path_factory):
    """Each mutation's run: one interpreter imports the pinned proxy once and forks a child per mutation.

    The log level is read when the package is imported, so the default-log mutation (no LITELLM_LOG) is the only
    one that needs an interpreter of its own.
    """
    root = Path(__file__).resolve().parents[1]
    runs = {}
    for names in (['none', 'tool_inventory', 'spend_without_prompts', 'spend_content'], ['default_log']):
        base = tmp_path_factory.mktemp('canary')
        home = base / 'home'
        home.mkdir()
        env = child_environment({'PATH': '/usr/bin:/bin', 'HOME': str(home), 'TMPDIR': str(home),
            'LITELLM_LOCAL_MODEL_COST_MAP': 'True', 'NO_PROXY': '*'}, declared=set())
        env.update({'PYTHONPATH': str(root), 'PYTHONDONTWRITEBYTECODE': '1'})
        if names == ['default_log']:
            env.pop('LITELLM_LOG')
        done = subprocess.run([sys.executable, '-m', 'tests.support.litellm_content_canary', '--fork', str(base / 'run'), *names],
                              cwd=home, env=env, capture_output=True, timeout=150)
        assert done.returncode == 0, (done.stdout + done.stderr).decode(errors='replace')[-3000:]
        for name in names:
            io = base / 'run' / '_io'
            runs[name] = {'returncode': int((io / f'{name}.status').read_text()), 'stdout': (io / f'{name}.stdout').read_bytes(),
                          'stderr': (io / f'{name}.stderr').read_bytes(), 'private': base / 'run' / name,
                          # What the importing interpreter itself wrote: it handles no request, so it holds no content.
                          'shared': base / 'home'}
    return runs


@pytest.mark.parametrize('mutation', MUTATIONS)
def test_pinned_proxy_keeps_no_content_and_still_meters(canary, mutation):
    run = canary[mutation]
    private = run['private']
    result = SimpleNamespace(returncode=run['returncode'], stdout=run['stdout'], stderr=run['stderr'])
    assert result.returncode == 0, (result.stdout + result.stderr).decode(errors='replace')[-7000:]
    assert not [path for path in run['shared'].rglob('*') if path.is_file()], 'the shared interpreter wrote files'
    scans = {'stdout': result.stdout, 'stderr': result.stderr}
    scans.update({str(path.relative_to(private)): path.read_bytes() for path in private.rglob('*') if path.is_file()})
    def contains(data, marker):
        return any(item.encode() in data for item in forms(CONTENT_MARKERS[marker]))
    matches = {name for name, data in scans.items() if any(contains(data, marker) for marker in CONTENT_MARKERS)}
    tables = json.loads((private / 'database.json').read_text())
    budget = json.loads((private / 'budget.json').read_text())
    assert budget['budget_refused'] is True
    assert bool(tables['LiteLLM_ToolTable']) is (mutation == 'tool_inventory')
    assert tables['LiteLLM_SpendLogGuardrailIndex'] == []
    assert {'key', 'user', 'team'} <= {row['entity_type'] for row in budget['updates']}
    if mutation == 'tool_inventory':
        assert contains(json.dumps(tables['LiteLLM_ToolTable']).encode(), 'tool_name')
        assert matches == {'database.json'}
    elif mutation == 'default_log':
        assert contains(result.stderr, 'prompt')
        assert 'database.json' not in matches
    elif mutation == 'spend_content':
        spend = json.dumps(tables['LiteLLM_SpendLogs']).encode()
        for marker in ('tool_name', 'arguments', 'description', 'schema'):
            assert contains(spend, marker), marker
        assert matches == {'database.json'}
    else:
        assert not matches, matches
        assert bool(tables['LiteLLM_SpendLogs']) is (mutation == 'spend_without_prompts')
        assert not any(rows for name, rows in tables.items() if 'error' in name.lower())
        daily = json.dumps({name: rows for name, rows in tables.items() if name.startswith('daily_')})
        for term in ('canary-user-agent', 'client-end-user', 'request_id', 'canary', '/chat/completions'):
            assert term in daily, term
        assert any('client-end-user' in json.dumps(rows) for name, rows in tables.items() if 'end_user' in name)
