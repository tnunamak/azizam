#!/usr/bin/env python3
"""Run an isolated gateway and fake engine for the portable browser tests."""
from __future__ import annotations

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
ENGINE_PORT = PORT + 1
if not 18000 <= PORT < 18999:
    raise SystemExit('UI_E2E_PORT must be between 18000 and 18998')
# Use a disk-backed, test-owned directory; do not inherit operator environment.
state_base = Path(os.environ.get('UI_E2E_STATE_ROOT', str(REPO / '.ui-e2e-state')))
state_base.mkdir(parents=True, exist_ok=True)
children = []


def stop(_signum=None, _frame=None):
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
with tempfile.TemporaryDirectory(prefix='run-', dir=state_base) as temp:
    state = Path(temp)
    home = state / 'home'
    home.mkdir()
    (state / 'images').mkdir()
    config = state / 'config.yml'
    # Backend definitions are seed data, not engine-specific implementation.
    disabled = '\n'.join(f'  {name}: {{enabled: false, host: 127.0.0.1, port: {ENGINE_PORT}}}'
                         for name in ('tabby', 'kobold', 'comfyui', 'embeddings'))
    config.write_text(f'proxy:\n  host: 127.0.0.1\n  port: {PORT}\nbackends:\n{disabled}\n  litellm: {{enabled: true, base_url: "http://127.0.0.1:{ENGINE_PORT}", host: 127.0.0.1, port: {ENGINE_PORT}}}\n'
                      'admin:\n  oidc:\n    cookie:\n      secure: false\n'
                      'model_aliases:\n  chat:\n    stable:\n      fast: demo-chat\n'
                      f'images:\n  cache_dir: {state / "images"}\n  vram_estimates_file: {state / "vram.json"}\n')
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(home),
           'PYTHONPATH': str(REPO), 'PYTHONDONTWRITEBYTECODE': '1',
           'AI_GATEWAY_CONFIG_PATH': str(config), 'AI_GATEWAY_GRACEFUL_SHUTDOWN_S': '1',
           'AI_GATEWAY_LITELLM_BASE_URL': f'http://127.0.0.1:{ENGINE_PORT}',
           'XDG_CONFIG_HOME': str(home / '.config'), 'XDG_CACHE_HOME': str(home / '.cache')}
    if os.environ.get('UI_E2E_ALLOWED_PORTS'):
        env['UI_E2E_ALLOWED_PORTS'] = os.environ['UI_E2E_ALLOWED_PORTS']
    for name, filename in {
        'AI_GATEWAY_ADMIN_DB': 'admin.db', 'AI_GATEWAY_METRICS_DB': 'metrics.db',
        'AI_GATEWAY_RESPONSES_DB': 'responses.db', 'AI_GATEWAY_RECEIPTS_DB': 'receipts.db',
        'AI_GATEWAY_MEDIA_JOBS_DB': 'media_jobs.db',
    }.items():
        env[name] = str(state / filename)
    try:
        for module, port in [('scripts.ui.fake_engine:app', ENGINE_PORT), ('proxy:app', PORT)]:
            log = (state / f'{port}.log').open('w')
            command = ([sys.executable, '-m', 'scripts.ui.fixture_gateway', str(PORT), str(ENGINE_PORT)]
                       if module == 'proxy:app' else [sys.executable, '-m', 'uvicorn', module, '--host',
                                                    '127.0.0.1', '--port', str(port)])
            children.append(subprocess.Popen(command, cwd=state, env=env,
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
