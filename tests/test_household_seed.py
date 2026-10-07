"""Household fixture invariants against its actual persisted schemas."""

import json
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path


def test_household_consistency(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    for name, filename in [
        ("AZIZAM_ADMIN_DB", "admin.db"),
        ("AZIZAM_METRICS_DB", "metrics.db"),
        ("AZIZAM_RECEIPTS_DB", "receipts.db"),
        ("AZIZAM_MEDIA_JOBS_DB", "media_jobs.db"),
        ("AZIZAM_PROVIDER_LEDGER_DB", "provider_spend.db"),
    ]:
        env[name] = str(tmp_path / filename)
    env.update(
        PYTHONPATH=str(repo),
        HOME=str(tmp_path),
        AZIZAM_CONFIG_PATH=str(tmp_path / "config.yml"),
    )
    subprocess.run(
        [sys.executable, "-m", "scripts.ui.household_seed", str(tmp_path), "18184"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        timeout=120,
    )
    manifest = json.loads((tmp_path / "household-manifest.json").read_text())
    with sqlite3.connect(tmp_path / "receipts.db") as conn:
        rows = conn.execute("SELECT created_at,receipt_json FROM receipts").fetchall()
    receipts = [json.loads(row[1]) for row in rows]
    assert manifest["request_count"] == len(receipts)
    assert max(row[0] for row in rows) - min(row[0] for row in rows) >= 29 * 86400
    assert {
        r["metadata"].get("status_code") for r in receipts if r["status"] != "succeeded"
    } == {400, 403, 429, 503, 504}
    for receipt in receipts:
        assert receipt["resolved_model"]
        if receipt["metadata"].get("status_code") == 403:
            assert receipt["app_id"] == manifest["apps"]["alex"]
            assert receipt["remote"]
        if receipt["metadata"].get("status_code") == 429:
            assert receipt["remote"]
    with sqlite3.connect(tmp_path / "metrics.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM samples WHERE metric='request'"
        ).fetchone()[0] == len(receipts)
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM samples WHERE metric='service_up'"
            ).fetchone()[0]
            == 30 * 24 * 6
        )
    receipt_cost = sum(Decimal(r["cost"]["usd"]) for r in receipts if r.get("cost"))
    with sqlite3.connect(tmp_path / "provider_spend.db") as conn:
        spend = sum(
            Decimal(row[0])
            for row in conn.execute("SELECT actual_usd FROM provider_spend")
        )
    assert receipt_cost == spend
    assert Decimal(1) < spend < Decimal(8)
    assets = manifest["assets"]
    assert len(assets) == len({asset["asset_id"] for asset in assets}) == 24
    assert {asset["owner"] for asset in assets} == {"sam", "robin", "alex"}
    assert {asset["kind"] for asset in assets} == {"image", "audio", "video"}
    with sqlite3.connect(tmp_path / "admin.db") as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM users WHERE email='june@example.test'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM account_links WHERE kind='invite' AND used_at IS NULL AND revoked_at IS NULL"
            ).fetchone()[0]
            == 2
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM credentials WHERE last_used_at IS NULL AND revoked_at IS NULL"
            ).fetchone()[0]
            == 1
        )
        teen = json.loads(
            conn.execute(
                "SELECT profile_json FROM profiles WHERE name='Teen'"
            ).fetchone()[0]
        )
        assert "claude-sonnet" not in teen["models"]
        assert teen["remote"]["providers"] == []
        from gateway.policy import PolicyRequest, evaluate
        from gateway.profiles import load_profile_from_entry

        assert evaluate(
            load_profile_from_entry({"profile": teen}),
            PolicyRequest(remote_provider="household-remote"),
        ).denied


def test_seed_refuses_unscoped_state(tmp_path, monkeypatch):
    from scripts.ui.household_seed import seed

    monkeypatch.delenv("AZIZAM_ADMIN_DB", raising=False)
    import pytest

    with pytest.raises(ValueError, match="isolated"):
        seed(tmp_path, 18184)
    assert not list(tmp_path.iterdir())


def test_household_media_process_boundary(tmp_path):
    import pytest

    from scripts.ui.fixture_guard import check_event

    cache = tmp_path / "images"
    cache.mkdir(exist_ok=True)
    asset = cache / "fixture.mp3"
    asset.write_bytes(b"fixture")
    probe = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_entries",
        "format_tags:stream_tags",
        str(asset),
    ]
    with pytest.raises(RuntimeError):
        check_event("subprocess.Popen", ("ffprobe", probe, None, None), {18184})
    check_event("subprocess.Popen", ("ffprobe", probe, None, None), {18184}, cache)
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"fixture")
    with pytest.raises(RuntimeError):
        check_event(
            "subprocess.Popen",
            ("ffprobe", probe[:-1] + [str(outside)], None, None),
            {18184},
            cache,
        )
    with pytest.raises(RuntimeError):
        check_event(
            "subprocess.Popen",
            ("systemctl", ["systemctl", "restart", "service"], None, None),
            {18184},
            cache,
        )


def test_household_model_declarations_override_language_discovery():
    from gateway import model_metadata
    from gateway.model_presentation import LISTED_METADATA_KEY, product_capability
    from scripts.ui.household_seed import registry

    expected = {
        "krea2-turbo": "image",
        "qwen-image-edit": "image",
        "tts-1": "speech",
        "whisper-large": "transcribe",
        "musicgen": "music",
        "audio-sfx": "sfx",
        "ltx-2.5": "video",
        "bge-m3": "embed",
    }
    rows = {
        model["id"]: model
        for service in registry(18185)["services"].values()
        for model in service["config"].get("models", [])
    }
    for model_id, capability in expected.items():
        row = rows[model_id]
        declared = model_metadata.apply_declared(
            model_metadata.from_capability("chat.completions"), row["model_metadata"]
        )
        assert (
            product_capability(
                "speech.synthesis" if model_id == "tts-1" else "audio.generate",
                {**row, LISTED_METADATA_KEY: declared.to_fields()},
            )
            == capability
        )
