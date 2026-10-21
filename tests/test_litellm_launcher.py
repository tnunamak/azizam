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
    assert 'version changed' in capsys.readouterr().err


def test_changed_internal_symbol_refuses_instead_of_silently_skipping(monkeypatch, capsys):
    from litellm.proxy.db.db_spend_update_writer import DBSpendUpdateWriter
    monkeypatch.setattr(DBSpendUpdateWriter, '_enqueue_tool_registry_upsert', None)
    assert launcher.main(['--config', 'not-opened.yaml']) == 78
    assert 'symbol changed' in capsys.readouterr().err


def test_changed_internal_implementation_refuses(monkeypatch, capsys):
    from litellm.proxy.db.db_spend_update_writer import DBSpendUpdateWriter
    def changed(self, **kwargs): return None
    monkeypatch.setattr(DBSpendUpdateWriter, '_enqueue_tool_registry_upsert', changed)
    assert launcher.main(['--config', 'not-opened.yaml']) == 78
    assert 'implementation changed' in capsys.readouterr().err
