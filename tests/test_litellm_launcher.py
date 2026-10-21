"""An upstream upgrade must re-verify the pinned no-content shim."""
import importlib.metadata

import pytest

from gateway import litellm_launcher as launcher


def test_installed_version_and_patched_symbol_are_the_verified_pin(monkeypatch):
    from litellm.proxy.db.db_spend_update_writer import DBSpendUpdateWriter
    original = DBSpendUpdateWriter._enqueue_tool_registry_upsert
    monkeypatch.setattr(DBSpendUpdateWriter, '_enqueue_tool_registry_upsert', original)
    assert importlib.metadata.version('litellm') == launcher.SUPPORTED_VERSION == '1.86.2'
    launcher.install_no_content_shim()
    assert DBSpendUpdateWriter._enqueue_tool_registry_upsert is not original
    assert DBSpendUpdateWriter()._enqueue_tool_registry_upsert(
        kwargs={'tools': [{'function': {'name': 'must-not-be-queued'}}]}, completion_response=None) is None


def test_version_bump_fails_loudly_before_startup(monkeypatch, capsys):
    monkeypatch.setattr(launcher.importlib.metadata, 'version', lambda _: '1.86.3')
    assert launcher.main(['--config', 'not-opened.yaml']) == 78
    assert 'version or tool queue changed' in capsys.readouterr().err


def test_changed_internal_symbol_refuses_instead_of_silently_skipping(monkeypatch, capsys):
    from litellm.proxy.db.db_spend_update_writer import DBSpendUpdateWriter
    monkeypatch.setattr(DBSpendUpdateWriter, '_enqueue_tool_registry_upsert', None)
    assert launcher.main(['--config', 'not-opened.yaml']) == 78
    assert 'version or tool queue changed' in capsys.readouterr().err


def test_changed_internal_implementation_refuses(monkeypatch, capsys):
    from litellm.proxy.db.db_spend_update_writer import DBSpendUpdateWriter
    def changed(self, **kwargs): return None
    monkeypatch.setattr(DBSpendUpdateWriter, '_enqueue_tool_registry_upsert', changed)
    assert launcher.main(['--config', 'not-opened.yaml']) == 78
    assert 'version or tool queue changed' in capsys.readouterr().err


@pytest.mark.parametrize('argv,workers', [(['--num_workers', '2'], None), (['--num_workers=3'], None),
    (['--reload'], None), ([], '2'), (['--num_workers', '1', '--num_workers=2'], None)])
@pytest.mark.parametrize('entrypoint', ['launcher', 'sidecar'])
def test_spawn_modes_refused_before_imports_or_database(monkeypatch, capsys, argv, workers, entrypoint):
    from gateway import provider_sidecar
    monkeypatch.delenv('NUM_WORKERS', raising=False)
    if workers:
        monkeypatch.setenv('NUM_WORKERS', workers)
    def must_not_run():
        pytest.fail('shim imported before unsafe process mode refused')
    monkeypatch.setattr(launcher, 'install_no_content_shim', must_not_run)
    if entrypoint == 'launcher':
        assert launcher.main(argv) == 78
        assert 'single_process_required' in capsys.readouterr().err
    else:
        with pytest.raises(ValueError, match='single_process_required'):
            provider_sidecar.main(['run', '--db', '/not-opened.db', '--', 'litellm', *argv])


def test_launcher_sets_effective_logger_levels_before_server(monkeypatch):
    import logging
    import sys
    from types import SimpleNamespace
    monkeypatch.setattr(sys, 'argv', ['launcher-test'])
    names = ('LiteLLM', 'LiteLLM Router', 'LiteLLM Proxy')
    for name in names:
        monkeypatch.setattr(logging.getLogger(name), 'level', logging.WARNING)
    monkeypatch.setattr(launcher, 'install_no_content_shim', lambda: None)
    def start():
        assert all(logging.getLogger(name).getEffectiveLevel() == logging.CRITICAL for name in names)
    monkeypatch.setitem(sys.modules, 'litellm', SimpleNamespace(run_server=start))
    assert launcher.main([]) == 0
