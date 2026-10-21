"""Contract and refusal tests for the pinned substrate's no-content profile."""
from __future__ import annotations

import asyncio
import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from gateway import litellm_content as content
from gateway.litellm_substrate import compile_litellm_substrate_config
from tests.support.fake_upstream import fake_upstreams  # noqa: F401 - fixture

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(('section', 'key', 'value'), [
    ('general_settings', 'disable_error_logs', False),
    ('general_settings', 'disable_spend_logs', False),
    ('general_settings', 'store_model_in_db', True),
    ('general_settings', 'store_prompts_in_spend_logs', True),
    ('litellm_settings', 'success_callback', ['log_file']),
    ('litellm_settings', 'cache', True),
    ('litellm_settings', 'turn_off_message_logging', False),
    ('router_settings', 'cache_responses', True),
])
def test_profile_mutations_are_refused(section, key, value):
    compiled = compile_litellm_substrate_config({})
    assert content.config_problem(compiled) is None
    compiled[section][key] = value
    assert content.config_problem(compiled) == f'{section}.{key}'


def test_environment_is_allowlisted_and_forces_critical():
    env = content.child_environment({'PATH': '/bin', 'DATABASE_URL': 'test-db', 'DECLARED_KEY': 'test',
        'UNRELATED_KEY': 'drop', 'LITELLM_LOG': 'DEBUG', 'DEBUG': 'true', 'STORE_MODEL_IN_DB': 'true',
        'STORE_PROMPTS_IN_SPEND_LOGS': 'true', 'LANGFUSE_PUBLIC_KEY': 'drop', 'OTEL_EXPORTER_OTLP_ENDPOINT': 'drop'},
        declared={'DECLARED_KEY', 'DEBUG', 'STORE_MODEL_IN_DB', 'LANGFUSE_PUBLIC_KEY'})
    assert env == {'PATH': '/bin', 'DATABASE_URL': 'test-db', 'DECLARED_KEY': 'test', **content.profile()['environment']}


def test_compose_has_exact_profile_without_database():
    compose = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
    service = compose['services']['litellm']
    assert service['environment'] == {'LITELLM_MASTER_KEY': '${AZIZAM_LITELLM_API_KEY-sk-local-litellm}', **content.profile()['environment']}
    assert '/app/data/litellm.config.yaml' in service['entrypoint'][-1]
    assert 'DATABASE_URL' not in service['environment']
    assert compose['services']['gateway']['environment']['AZIZAM_LITELLM_COMPOSE_PROFILE'] == 'true'


@pytest.mark.parametrize('flag', ['config_unsafe', 'key_unsafe', 'team_unsafe', None, 'malformed'])
def test_database_check_is_read_only_and_only_returns_flags(monkeypatch, flag):
    calls = []
    row = {'config_unsafe': False, 'key_unsafe': False, 'team_unsafe': False}
    if flag == 'malformed':
        row = {}
    elif flag:
        row[flag] = True

    class Client:
        def __init__(self, **kwargs): pass
        async def connect(self): calls.append('connect')
        async def disconnect(self): calls.append('disconnect')
        def tx(self): return self
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def execute_raw(self, query): calls.append(query)
        async def query_raw(self, query):
            calls.append(query)
            return [row]
    monkeypatch.setitem(sys.modules, 'prisma', SimpleNamespace(Prisma=Client))
    result = asyncio.run(content.database_problem('offline-test-only'))
    assert result == ('database_check_unavailable' if flag == 'malformed' else f'database_{flag}' if flag else None)
    assert calls == ['connect', 'SET TRANSACTION READ ONLY', content.DB_CHECK_SQL, 'disconnect']
    assert "metadata->'callback_settings'" in content.DB_CHECK_SQL
    assert 'callback_settings,' not in content.DB_CHECK_SQL


def test_unmanaged_is_disclosed_and_never_refused(monkeypatch):
    monkeypatch.delenv('AZIZAM_MANAGE_LITELLM', raising=False)
    gate = content.ContentGate()
    assert asyncio.run(gate.check('http://127.0.0.1:4011')) is None
    assert gate.status('http://127.0.0.1:4011')['status'] == 'unmanaged'
    assert 'cannot confirm' in gate.status('http://127.0.0.1:4011')['detail']


def test_gate_rechecks_restart_database_mutation_and_failure(monkeypatch):
    monkeypatch.setenv('AZIZAM_MANAGE_LITELLM', 'true')
    gate = content.ContentGate()
    generation = [1]
    calls = []
    problem = [None]
    monkeypatch.setattr(gate, 'generation', lambda _: tuple(generation))
    async def check(_):
        calls.append(tuple(generation))
        return problem[0]
    monkeypatch.setattr(content, 'database_problem', check)
    async def run():
        assert await gate.check('http://localhost:4011') is None
        assert await gate.check('http://localhost:4011') is None
        assert len(calls) == 1
        generation[0] = 2
        problem[0] = 'database_team_unsafe'
        assert await gate.check('http://localhost:4011') == problem[0]
        problem[0] = None
        assert await gate.check('http://localhost:4011', force=True) is None
        async def unavailable(_): raise OSError('private failure')
        monkeypatch.setattr(content, 'database_problem', unavailable)
        assert await gate.check('http://localhost:4011', force=True) == 'profile_check_unavailable'
        assert 'private failure' not in str(gate.status('http://localhost:4011'))
    asyncio.run(run())


def test_unverified_managed_refuses_before_content_leaves(monkeypatch, tmp_path):
    monkeypatch.setenv('AZIZAM_MANAGE_LITELLM', 'true')
    monkeypatch.setenv('AZIZAM_LITELLM_PID_FILE', str(tmp_path / 'absent.pid'))
    monkeypatch.setattr(content, 'gate', content.ContentGate())
    response = asyncio.run(content.execution_refusal('http://localhost:4011'))
    assert response.status_code == 503
    assert json.loads(response.body)['error']['code'] == 'litellm_content_unverified'


def test_launch_proof_is_bound_to_process_and_config(monkeypatch, tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump(compile_litellm_substrate_config({})))
    pid = tmp_path / 'litellm.pid'
    pid.write_text(str(os.getpid()))
    proof = Path(str(pid) + '.retention.json')
    content.write_launch_proof(proof, config, content.profile()['environment'])
    assert json.loads(proof.read_text())['generation'] == list(content.process_generation(os.getpid()))
    assert proof.stat().st_mode & 0o777 == 0o600
    monkeypatch.setenv('AZIZAM_LITELLM_PID_FILE', str(pid))
    # This pytest process is not a LiteLLM command, even with a valid proof.
    with pytest.raises(ValueError, match='command'):
        content.ContentGate().generation('http://localhost:4011')
    with pytest.raises(ValueError, match='environment'):
        content.write_launch_proof(proof, config, {'LITELLM_LOG': 'DEBUG'})


def test_rehearsal_is_offline_and_reports_missing_database_evidence(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump(compile_litellm_substrate_config({})))
    command = [sys.executable, str(ROOT / 'scripts/rehearse-litellm-content'), '--config', str(config)]
    result = subprocess.run(command, capture_output=True, text=True, env={'PATH': '/usr/bin:/bin'})
    assert result.returncode == 2
    assert 'database_unchecked' in result.stdout
    result = subprocess.run(command + ['--no-database'], capture_output=True, text=True, env={'PATH': '/usr/bin:/bin'})
    assert result.returncode == 0
    flags = tmp_path / 'flags.json'
    flags.write_text(json.dumps({'config_unsafe': False, 'key_unsafe': False, 'team_unsafe': True}))
    result = subprocess.run(command + ['--database-check', str(flags)], capture_output=True, text=True, env={'PATH': '/usr/bin:/bin'})
    assert result.returncode == 1


def test_every_substrate_url_caller_is_classified():
    expected = {
        'proxy.py': {'_litellm_models_payload', '_forward_litellm_proxy_request'},
        'gateway/provider_execution.py': {'execute_paid_litellm'},
        'gateway/backends/litellm_admin.py': {'_request'},
    }
    found = {}
    for path in [ROOT / 'proxy.py', *sorted((ROOT / 'gateway').rglob('*.py'))]:
        tree = ast.parse(path.read_text())
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node): parents[child] = node
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'litellm_url':
                parent = node
                while not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    parent = parents[parent]
                found.setdefault(str(path.relative_to(ROOT)), set()).add(parent.name)
    # Model listing and key CRUD carry no request content. Both execution
    # callers must await execution_refusal before they build the URL.
    assert found == expected
    for filename, name in [('proxy.py', '_forward_litellm_proxy_request'), ('gateway/provider_execution.py', 'execute_paid_litellm')]:
        tree = ast.parse((ROOT / filename).read_text())
        function = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
        source = ast.unparse(function)
        assert source.index('await execution_refusal(') < source.index('litellm_url(')


def test_restart_during_check_cannot_reuse_approval(monkeypatch):
    monkeypatch.setenv('AZIZAM_MANAGE_LITELLM', 'true')
    gate = content.ContentGate()
    generation = [1]
    monkeypatch.setattr(gate, 'generation', lambda _: tuple(generation))
    async def check(_):
        generation[0] = 2
        return None
    monkeypatch.setattr(content, 'database_problem', check)
    assert asyncio.run(gate.check('http://localhost:4011')) == 'profile_check_unavailable'
    assert gate.checked is None


def test_hourly_recheck_catches_new_callbacks(monkeypatch):
    monkeypatch.setenv('AZIZAM_MANAGE_LITELLM', 'true')
    gate = content.ContentGate()
    monkeypatch.setattr(gate, 'generation', lambda _: ('same-process',))
    clock = [1.0]
    monkeypatch.setattr(content.time, 'monotonic', lambda: clock[0])
    calls = []
    async def check(_):
        calls.append(1)
        return None if len(calls) == 1 else 'database_key_unsafe'
    monkeypatch.setattr(content, 'database_problem', check)
    async def run():
        assert await gate.check('http://localhost:4011') is None
        clock[0] += 3601
        assert await gate.check('http://localhost:4011') == 'database_key_unsafe'
    asyncio.run(run())


@pytest.mark.parametrize('unseen', [True, False])
def test_compose_database_visibility_is_disclosed(monkeypatch, unseen):
    monkeypatch.setenv('AZIZAM_LITELLM_COMPOSE_PROFILE', 'true')
    monkeypatch.delenv('DATABASE_URL', raising=False)
    gate = content.ContentGate()
    async def visibility(_): return unseen
    monkeypatch.setattr(content, 'compose_database_unseen', visibility)
    assert asyncio.run(gate.check('http://litellm:4000')) is None
    assert gate.status('http://litellm:4000')['status'] == ('unmanaged' if unseen else 'verified')


def test_unexpected_profile_sections_cannot_load_callbacks():
    config = compile_litellm_substrate_config({})
    config['environment_variables'] = {'STORE_MODEL_IN_DB': 'true'}
    assert content.config_problem(config) == 'unexpected_section'


def test_compose_checks_authenticated_effective_diagnostics(monkeypatch, fake_upstreams):
    from tests.support.fake_upstream import scenario_from_data
    fake = fake_upstreams.asgi(scenario_from_data({'protocol': 'litellm-admin', 'name': 'no-content-readiness',
        'provenance': {'source': 'hand-written', 'reference': 'LiteLLM 1.86.2 readiness/details'},
        'exchanges': [{'id': 'readiness', 'match': {'method': 'GET', 'path': '/health/readiness/details'},
            'response': {'json': {'db': 'Not connected', 'log_level': 'CRITICAL', 'cache': None, 'success_callbacks': []}}}]}),
        host='readiness.fk.test')
    assert asyncio.run(content.compose_database_unseen(fake.base_url)) is False


def test_real_gateway_refuses_and_recovers_without_dispatching_content(monkeypatch, tmp_path, fake_upstreams):
    from tests.support.fake_upstream import load_scenario
    from tests.support.gateway import gateway_client, register_litellm, use_admin_db
    fake = fake_upstreams.asgi(load_scenario('openai-compatible/chat-text', model='fk-model', content='ok'), host='retention.fk.test')
    register_litellm(monkeypatch, use_admin_db(monkeypatch, tmp_path), fake.base_url)
    gate = content.ContentGate()
    monkeypatch.setattr(content, 'gate', gate)
    monkeypatch.setattr(gate, 'mode', lambda _: 'managed')
    monkeypatch.setattr(gate, 'generation', lambda _: ('fixture-generation',))
    problem = ['database_team_unsafe']
    async def check(_): return problem[0]
    monkeypatch.setattr(content, 'database_problem', check)
    async def run():
        async with gateway_client() as client:
            body = {'model': 'fk-model', 'messages': [{'role': 'user', 'content': 'private-test-content'}]}
            refused = await client.post('/v1/chat/completions', json=body)
            assert refused.status_code == 503
            assert refused.json()['error']['code'] == 'litellm_content_unverified'
            assert fake.requests == []
            problem[0] = None
            await gate.check(fake.base_url, force=True)
            accepted = await client.post('/v1/chat/completions', json=body)
            assert accepted.status_code == 200
    asyncio.run(run())


def test_endpoint_changes_are_disclosed_from_the_effective_registry(monkeypatch):
    monkeypatch.setenv('AZIZAM_MANAGE_LITELLM', 'true')
    monkeypatch.setenv('AZIZAM_LITELLM_BASE_URL', 'http://127.0.0.1:4011')
    gate = content.ContentGate()
    gate.problem = None
    monkeypatch.setattr(content, 'gate', gate)
    def registry(address):
        return {'services': {'router': {'id': 'router', 'kind': 'llm-substrate', 'enabled': True,
            'config': {'base_url': address}}}}
    assert content.deployment_status(registry('http://127.0.0.1:4011'))['status'] == 'verified'
    assert content.deployment_status(registry('https://owner-run.example'))['status'] == 'unmanaged'


def test_compiled_callback_lists_cannot_mutate_the_expected_profile():
    compiled = compile_litellm_substrate_config({})
    compiled['litellm_settings']['callbacks'].append('log_file')
    assert content.config_problem(compiled) == 'litellm_settings.callbacks'
    assert compile_litellm_substrate_config({})['litellm_settings']['callbacks'] == []
