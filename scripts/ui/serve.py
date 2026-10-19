#!/usr/bin/env python3
"""Run an isolated gateway and fake engine for the portable browser tests."""
from __future__ import annotations


import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = Path(__file__).resolve().parents[2]
PORT = int(os.environ.get('UI_E2E_PORT', '18180'))
# A scheduled run points the gateway at an upstream it already started (the
# fake upstream, scripts/scheduled/); then no fake engine is spawned here.
EXTERNAL_UPSTREAM_PORT = os.environ.get('UI_E2E_UPSTREAM_PORT', '')
ENGINE_PORT = int(EXTERNAL_UPSTREAM_PORT) if EXTERNAL_UPSTREAM_PORT else PORT + 1
SEED_PROFILE = os.environ.get('UI_SEED_PROFILE', '')
if SEED_PROFILE not in ('', 'household'):
    raise SystemExit('UI_SEED_PROFILE must be household or unset')
# The shipped first-run config (no backends) instead of the fixture's: the
# instance a new operator starts with, for web/e2e/first-run.spec.ts.
FIRST_RUN = os.environ.get('UI_E2E_FIRST_RUN') == '1'
SEED_SCENARIO = os.environ.get('UI_HOUSEHOLD_SCENARIO', 'default')
if not 18000 <= PORT < 18999:
    raise SystemExit('UI_E2E_PORT must be between 18000 and 18998')
# Use a disk-backed, test-owned directory; do not inherit operator environment.
state_base = Path(os.environ.get('UI_E2E_STATE_ROOT', str(REPO / '.ui-e2e-state')))
state_base.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(REPO))
from scripts import scratch_owner  # noqa: E402

scratch_owner.sweep(state_base, 'run-')  # what a SIGKILLed server left
children = []


def stop(_signum=None, _frame=None):
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
with tempfile.TemporaryDirectory(prefix='run-', dir=state_base) as temp:
    state = Path(temp)
    scratch_owner.mark(state)
    home = state / 'home'
    home.mkdir()
    (state / 'images').mkdir()
    config = state / 'config.yml'
    if FIRST_RUN:
        import yaml
        document = yaml.safe_load((REPO / 'first-run.yml').read_text())
        document['proxy']['port'] = PORT
        config.write_text(yaml.safe_dump(document))
    else:
        # Backend definitions are seed data, not engine-specific implementation.
        disabled = '\n'.join(f'  {name}: {{enabled: false, host: 127.0.0.1, port: {ENGINE_PORT}}}'
                             for name in ('kobold', 'comfyui', 'embeddings'))
        config.write_text(f'proxy:\n  host: 127.0.0.1\n  port: {PORT}\nbackends:\n{disabled}\n  litellm: {{enabled: true, base_url: "http://127.0.0.1:{ENGINE_PORT}", host: 127.0.0.1, port: {ENGINE_PORT}}}\n'
                          'admin:\n  oidc:\n    cookie:\n      secure: false\n'
                          'model_aliases:\n  chat:\n    stable:\n      fast: demo-chat\n'
                          f'images:\n  cache_dir: {state / "images"}\n  vram_estimates_file: {state / "vram.json"}\n')
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(home),
           'PYTHONPATH': str(REPO), 'PYTHONDONTWRITEBYTECODE': '1',
           'AZIZAM_CONFIG_PATH': str(config), 'AZIZAM_GRACEFUL_SHUTDOWN_S': '1',
           'AZIZAM_LITELLM_BASE_URL': f'http://127.0.0.1:{ENGINE_PORT}',
           'XDG_CONFIG_HOME': str(home / '.config'), 'XDG_CACHE_HOME': str(home / '.cache')}
    if os.environ.get('UI_E2E_AVATAR_PICTURE'):
        env['UI_E2E_AVATAR_PICTURE'] = os.environ['UI_E2E_AVATAR_PICTURE']
    if os.environ.get('UI_E2E_ALLOWED_PORTS'):
        env['UI_E2E_ALLOWED_PORTS'] = os.environ['UI_E2E_ALLOWED_PORTS']
    for name, filename in {
        'AZIZAM_ADMIN_DB': 'admin.db', 'AZIZAM_METRICS_DB': 'metrics.db',
        'AZIZAM_RESPONSES_DB': 'responses.db', 'AZIZAM_RECEIPTS_DB': 'receipts.db',
        'AZIZAM_MEDIA_JOBS_DB': 'media_jobs.db',
    }.items():
        env[name] = str(state / filename)
    if SEED_PROFILE == 'household':
        env['UI_SEED_PROFILE'] = 'household'
        env['UI_HOUSEHOLD_SCENARIO'] = SEED_SCENARIO
        env['UI_HOUSEHOLD_PROVIDER_KEY'] = 'isolated-household-provider'
        env['AZIZAM_ADMIN_SESSION_KEY'] = 'household-fixture-session-not-production'
        env['AZIZAM_PROVIDER_LEDGER_DB'] = str(state / 'provider_spend.db')
        # A household shows the address its members would use, not the loopback test port.
        env['AZIZAM_PUBLIC_URL'] = 'https://ai.household.example'
        if os.environ.get('UI_HOUSEHOLD_PROTOCOL_FRONTENDS'):
            env['UI_HOUSEHOLD_PROTOCOL_FRONTENDS'] = os.environ['UI_HOUSEHOLD_PROTOCOL_FRONTENDS']
        if os.environ.get('UI_HOUSEHOLD_MANIFEST_PATH'):
            env['UI_HOUSEHOLD_MANIFEST_PATH'] = os.environ['UI_HOUSEHOLD_MANIFEST_PATH']
        subprocess.run([sys.executable, '-m', 'scripts.ui.household_seed', str(state), str(PORT), SEED_SCENARIO],
                       cwd=state, env=env, check=True)
        manifest_path = Path(env.get('UI_HOUSEHOLD_MANIFEST_PATH', state / 'household-manifest.json'))
        manifest = json.loads(manifest_path.read_text())
        manifest['fixture_state'] = state.name
        manifest_path.write_text(json.dumps(manifest))
    # Extra top-level configuration for a disposable server that needs more than
    # the browser seed: JSON data merged into config.yml (web/e2e/fixtures/oracle.ts
    # adds a video workflow and a transcription alias), after any household seed.
    if os.environ.get('UI_E2E_EXTRA_CONFIG'):
        import yaml

        def merge(into, extra):
            for key, value in extra.items():
                if isinstance(value, dict) and isinstance(into.get(key), dict):
                    merge(into[key], value)
                else:
                    into[key] = value

        document = yaml.safe_load(config.read_text())
        merge(document, json.loads(Path(os.environ['UI_E2E_EXTRA_CONFIG']).read_text()))
        config.write_text(yaml.safe_dump(document))
    try:
        servers = [('scripts.ui.fake_engine:app', ENGINE_PORT, {}), ('proxy:app', PORT, {})]
        if FIRST_RUN:
            # The engine at ENGINE_PORT stands in for LiteLLM and sends each chat to the
            # address in the config the gateway writes; the operator adds the engine
            # one port above it (inside the harness's strict-port window), which has a
            # model and a reply of its own.
            servers[0][2].update(FAKE_SUBSTRATE_CONFIG=str(state / 'litellm.config.yaml'))
            servers.insert(1, ('scripts.ui.fake_engine:app', ENGINE_PORT + 1,
                               {'FAKE_ENGINE_MODEL': 'marker-engine-chat',
                                'FAKE_ENGINE_REPLY': 'Answered by the engine you just added: marker-engine-chat.'}))
        for module, port, extra_env in servers:
            if module != 'proxy:app' and EXTERNAL_UPSTREAM_PORT:
                continue
            log = (state / f'{port}.log').open('w')
            command = ([sys.executable, '-m', 'scripts.ui.fixture_gateway', str(PORT), str(ENGINE_PORT)]
                       if module == 'proxy:app' else [sys.executable, '-m', 'uvicorn', module, '--host',
                                                    '127.0.0.1', '--port', str(port),
                                                    '--timeout-keep-alive', '3600'])
            children.append(subprocess.Popen(command, cwd=state, env={**env, **extra_env},
                                             stdout=log, stderr=subprocess.STDOUT))
            log.close()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if any(p.poll() is not None for p in children):
                # Logs can include key material; retain privately until cleanup, do not print.
                raise RuntimeError('An isolated server exited before readiness')
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{PORT}/health', timeout=1).close()
                print(f'Isolated UI gateway ready on port {PORT}', flush=True)
                break
            except OSError:
                time.sleep(.2)
        else:
            raise RuntimeError('Isolated gateway readiness timed out')
        while True:
            time.sleep(.5)
            if any(p.poll() is not None for p in children):
                raise RuntimeError('An isolated server stopped during browser tests')
    except KeyboardInterrupt:
        pass
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
