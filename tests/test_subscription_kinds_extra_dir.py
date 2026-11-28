"""A second directory of subscription kind rows, named by AZIZAM_SUBSCRIPTION_KINDS_DIR."""

from __future__ import annotations

import pytest
import yaml

from gateway import subscription_kinds


def _row(kind_id: str) -> dict:
    row = yaml.safe_load((subscription_kinds.KINDS_DIR / "chatgpt.yml").read_text(encoding="utf-8"))
    row["id"] = kind_id
    row["model_prefix"] = f"{kind_id}-plan"
    return row


def test_the_extra_directory_adds_kinds_after_the_shipped_ones(tmp_path, monkeypatch):
    (tmp_path / "other.yml").write_text(yaml.safe_dump(_row("other-kind")), encoding="utf-8")
    monkeypatch.setenv(subscription_kinds.EXTRA_KINDS_DIR_ENV, str(tmp_path))
    kinds = subscription_kinds.load_kinds()
    assert "chatgpt" in kinds and kinds["other-kind"].model_prefix == "other-kind-plan"


def test_without_the_setting_only_the_shipped_kinds_load(monkeypatch):
    monkeypatch.delenv(subscription_kinds.EXTRA_KINDS_DIR_ENV, raising=False)
    assert set(subscription_kinds.load_kinds()) == {"chatgpt", "claude"}


def test_a_repeated_kind_id_fails_instead_of_replacing_the_shipped_row(tmp_path, monkeypatch):
    (tmp_path / "again.yml").write_text(yaml.safe_dump(_row("chatgpt")), encoding="utf-8")
    monkeypatch.setenv(subscription_kinds.EXTRA_KINDS_DIR_ENV, str(tmp_path))
    with pytest.raises(ValueError, match="duplicate subscription kind"):
        subscription_kinds.load_kinds()


def test_an_explicit_directory_ignores_the_setting(tmp_path, monkeypatch):
    other = tmp_path / "extra"
    other.mkdir()
    (other / "x.yml").write_text(yaml.safe_dump(_row("x-kind")), encoding="utf-8")
    monkeypatch.setenv(subscription_kinds.EXTRA_KINDS_DIR_ENV, str(other))
    assert subscription_kinds.load_kinds(tmp_path) == {}
