"""Pinned proxy canaries with a fresh process, cwd, environment and fake DB."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gateway.litellm_content import child_environment
from gateway.log_scrub import forms
from tests.support.litellm_content_canary import MARKER


@pytest.mark.parametrize('positive', [False, True], ids=['no-content-profile', 'leaky-positive-control'])
def test_pinned_proxy_keeps_no_content_and_still_meters(tmp_path, positive):
    root = Path(__file__).resolve().parents[1]
    private = tmp_path / 'scratch'
    private.mkdir()
    env = child_environment({'PATH': '/usr/bin:/bin', 'HOME': str(private), 'TMPDIR': str(private)}, declared=set())
    env.update({'PYTHONPATH': str(root), 'PYTHONDONTWRITEBYTECODE': '1', 'LITELLM_LOCAL_MODEL_COST_MAP': 'True',
                'NO_PROXY': '*', 'CANARY_POSITIVE': str(positive).lower()})
    if positive:
        env.pop('LITELLM_LOG')  # measured default-level echoed error is a positive control
    # Keep interpreter library discovery only. No host service/key/DB environment.
    result = subprocess.run([sys.executable, '-m', 'tests.support.litellm_content_canary'],
                            cwd=private, env=env, capture_output=True, timeout=75)
    assert result.returncode == 0, (result.stdout + result.stderr).decode(errors='replace')[-7000:]
    scans = {'stdout': result.stdout, 'stderr': result.stderr}
    scans.update({str(path.relative_to(private)): path.read_bytes() for path in private.rglob('*') if path.is_file()})
    markers = [item.encode() for item in forms(MARKER)]
    matches = {name for name, data in scans.items() if any(marker in data for marker in markers)}
    tables = json.loads((private / 'database.json').read_text())
    budget = json.loads((private / 'budget.json').read_text())
    assert budget['budget_refused'] is True
    assert {'key', 'user', 'team'} <= {row['entity_type'] for row in budget['updates']}
    if positive:
        assert matches & {'stdout', 'stderr'}, matches
        assert 'database.json' in matches
        assert len(tables['LiteLLM_SpendLogs']) > 0
    else:
        assert not matches, matches
        assert tables['LiteLLM_SpendLogs'] == []
        assert not any(rows for name, rows in tables.items() if 'error' in name.lower())
