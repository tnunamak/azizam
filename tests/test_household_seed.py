"""Household fixture invariants against its actual persisted schemas."""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.ui.household_seed import FIXED_NOW


def _seed(tmp_path, scenario="default"):
    """Run the seed in a subprocess against isolated stores; return its manifest."""
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
        [sys.executable, "-m", "scripts.ui.household_seed", str(tmp_path), "18184", scenario],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        timeout=120,
    )
    return json.loads((tmp_path / "household-manifest.json").read_text())


def _receipts(tmp_path):
    with sqlite3.connect(tmp_path / "receipts.db") as conn:
        return [
            (row[0], json.loads(row[1]))
            for row in conn.execute("SELECT created_at,receipt_json FROM receipts")
        ]


def _spend(tmp_path, profile_id, since):
    with sqlite3.connect(tmp_path / "provider_spend.db") as conn:
        return sum(
            Decimal(row[0])
            for row in conn.execute(
                "SELECT actual_usd FROM provider_spend WHERE profile_id=? AND created_at>=?",
                (profile_id, since),
            )
        )


def test_household_consistency(tmp_path):
    manifest = _seed(tmp_path)
    assert manifest["scenario"] == "default"
    assert manifest["now"] == FIXED_NOW
    with sqlite3.connect(tmp_path / "receipts.db") as conn:
        rows = conn.execute("SELECT created_at,receipt_json FROM receipts").fetchall()
    receipts = [json.loads(row[1]) for row in rows]
    assert manifest["request_count"] == len(receipts)
    assert sum(receipt["status"] != "succeeded" for receipt in receipts) == 5
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
        if receipt["app_id"] == manifest["apps"]["codex"]:
            assert receipt["intent"] in {"chat.completions", "embeddings"}
        if receipt["app_id"] == manifest["apps"]["home"]:
            assert receipt["intent"] in {
                "chat.completions",
                "audio.speech",
                "audio.transcribe",
            }
            assert not receipt["remote"]
        if receipt["intent"] == "embeddings":
            assert receipt["app_id"] == manifest["apps"]["codex"]
        if receipt["remote"]:
            assert receipt["intent"] == "chat.completions"
        if (
            receipt["status"] == "succeeded"
            and receipt["app_id"] == manifest["apps"]["alex"]
        ):
            assert not receipt["remote"]
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
    # The video's job asset names a poster that is cached and owned like the video.
    with sqlite3.connect(tmp_path / "media_jobs.db") as conn:
        jobs = [json.loads(row[0]) for row in conn.execute("SELECT job_json FROM media_jobs")]
    posters = [a["poster_asset_id"] for job in jobs for a in job.get("assets", []) if a.get("poster_asset_id")]
    assert len(posters) == 1
    digest = posters[0].removeprefix("img_")
    assert (tmp_path / "images" / f"{digest}.webp").is_file()
    assert json.loads((tmp_path / "images" / f"{digest}.meta.json").read_text())["asset_id"] == posters[0]
    with sqlite3.connect(tmp_path / "admin.db") as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {row[0] for row in conn.execute("SELECT name FROM profiles")} == {
            "Everyone",
            "Teen",
            "Builder",
        }
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


def test_default_household_edge_cases_and_realism(tmp_path):
    manifest = _seed(tmp_path)
    with sqlite3.connect(tmp_path / "admin.db") as conn:
        names = [row[0] for row in conn.execute("SELECT display_name FROM users")]
        app_names = [row[0] for row in conn.execute("SELECT name FROM apps")]
    assert any(len(name) >= 35 for name in names)
    assert any(len(name) >= 60 for name in app_names)
    assert any(not name.isascii() for name in names)
    receipts = _receipts(tmp_path)
    # Findings 9-11: no fixed grid, no per-day cycle, capability-specific timings.
    stamps = [created for created, _ in receipts]
    assert len({ts % 900 for ts in stamps}) > 100
    per_day = {}
    for ts in stamps:
        per_day[ts // 86400] = per_day.get(ts // 86400, 0) + 1
    assert len(set(per_day.values())) > 6
    by_intent = {}
    for _, receipt in receipts:
        if receipt["status"] == "succeeded":
            by_intent.setdefault(receipt["intent"], []).append(receipt["latency_ms"])
    median = lambda values: sorted(values)[len(values) // 2]
    assert median(by_intent["video.generate"]) > 10 * median(by_intent["audio.speech"])
    assert len(set(by_intent["audio.speech"])) > 20
    created = sorted(
        r["accepted_at_ms"] // 1000 for _, r in receipts if r["request_id"].startswith("req_household_asset_")
    )
    asset_gaps = {later - earlier for earlier, later in zip(created, created[1:])}
    assert len(asset_gaps) > 10
    # Finding 10: a request refused before execution generated nothing.
    denied = [r for _, r in receipts if r["status"] == "rejected"]
    assert denied and all(r["usage"]["completion_tokens"] == 0 for r in denied)
    # Finding 2: the budget rejection really exceeds the $0.10 per-request cap.
    over_cap = next(r for r in denied if r["policy_reasons"] == ["budget_exceeded"])
    assert Decimal(over_cap["usage"]["prompt_tokens"]) * Decimal("0.000003") > Decimal("0.10")
    assert manifest["scenario_details"] == {}


def test_running_scenario(tmp_path):
    manifest = _seed(tmp_path, "running")
    receipts = dict((r["request_id"], r) for _, r in _receipts(tmp_path))
    live = {rid for rid, r in receipts.items() if r["status"] == "in_progress"}
    assert live == set(manifest["scenario_details"]["in_progress_receipts"])
    for rid in live:  # updated within the stale window, so they render as running
        assert FIXED_NOW * 1000 - receipts[rid]["updated_at_ms"] < 5 * 60 * 1000
    with sqlite3.connect(tmp_path / "media_jobs.db") as conn:
        jobs = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT json_extract(job_json,'$.status'),json_extract(job_json,'$.request_id'),"
                "json_extract(job_json,'$.assets'),json_extract(job_json,'$.error'),"
                "json_extract(job_json,'$.progress') FROM media_jobs "
                "WHERE json_extract(job_json,'$.status') IN ('running','failed')"
            )
        }
    assert jobs["running"][0] == manifest["scenario_details"]["running_job"]
    assert 0 < jobs["running"][3] < 100
    assert jobs["failed"][0] == manifest["scenario_details"]["failed_job"]
    assert json.loads(jobs["failed"][1]) == [] and jobs["failed"][2]
    assert manifest["request_count"] == len(receipts)


def test_budget_exhausted_scenario(tmp_path):
    manifest = _seed(tmp_path, "budget-exhausted")
    details = manifest["scenario_details"]
    with sqlite3.connect(tmp_path / "admin.db") as conn:
        profile_id = conn.execute("SELECT id FROM profiles WHERE name='Everyone'").fetchone()[0]
    month_start = int(datetime.fromtimestamp(FIXED_NOW, timezone.utc).replace(day=1, hour=0, minute=0, second=0).timestamp())
    # Both windows the gateway enforces are at or over their cap.
    assert _spend(tmp_path, profile_id, FIXED_NOW - 86400) >= Decimal(1)
    assert _spend(tmp_path, profile_id, month_start) >= Decimal(8)
    receipts = [r for _, r in _receipts(tmp_path)]
    charged = {
        r["request_id"]: Decimal(r["cost"]["usd"])
        for r in receipts
        if r["remote"] and r["status"] == "succeeded"
    }
    with sqlite3.connect(tmp_path / "provider_spend.db") as conn:
        ledger = dict(conn.execute("SELECT request_id,actual_usd FROM provider_spend"))
    assert {k: Decimal(v) for k, v in ledger.items()} == charged  # ledger and receipts agree
    assert all(Decimal((r.get("cost") or {"usd": "0"})["usd"]) <= Decimal("0.10") for r in receipts)
    rejected = [
        r for r in receipts
        if r["request_id"].startswith("req_household_budget_rejected_")
    ]
    assert len(rejected) == details["rejections"] == 3
    for r in rejected:  # each refusal comes after the charge that exhausted the balance
        assert r["accepted_at_ms"] // 1000 > details["exhausted_at"]
        assert r["status"] == "rejected" and r["metadata"]["status_code"] == 429
        assert r["usage"]["completion_tokens"] == 0
        assert r["app_id"] == manifest["apps"]["robin"]


def test_provider_down_scenario(tmp_path):
    manifest = _seed(tmp_path, "provider-down")
    details = manifest["scenario_details"]
    assert details["last_success_at"] == FIXED_NOW - 4 * 3600
    assert "503" in details["error"] and set(details["affected_models"]) == {"claude-sonnet", "gpt-mini"}
    with sqlite3.connect(tmp_path / "metrics.db") as conn:
        history = dict(
            conn.execute(
                "SELECT ts,value FROM samples WHERE metric='service_up' "
                "AND json_extract(tags,'$.backend')='household-remote'"
            )
        )
        assert history[FIXED_NOW - 4 * 3600] == 1
        assert sorted(ts for ts, up in history.items() if not up) == sorted(FIXED_NOW - h * 3600 for h in range(4))
    failed = [r for _, r in _receipts(tmp_path) if r["request_id"].startswith("req_household_down_")]
    assert len(failed) == details["failed_requests"] == 5
    assert all(r["remote"] and r["metadata"]["status_code"] == 503 for r in failed)
    assert all(r["accepted_at_ms"] // 1000 > details["last_success_at"] for r in failed)
    with sqlite3.connect(tmp_path / "admin.db") as conn:
        setting = json.loads(conn.execute("SELECT value_json FROM operator_settings WHERE key='provider_admin'").fetchone()[0])
    assert setting["providers"]["household-remote"]["connection"]["reachable"] is False
    from scripts.ui.household_seed import registry

    caps = registry(18185, "provider-down")
    services = caps["services"]["household-remote"]["config"]
    assert services["health_endpoint"] == "/fixture/down"
    assert registry(18185)["services"]["household-remote"]["config"]["health_endpoint"] == "/health"
    assert {c["health"] for c in caps["capabilities"].values() if c["backend"] == "household-remote"} == {"unhealthy"}


def test_stale_health_scenario(tmp_path):
    _seed(tmp_path, "stale-health")
    with sqlite3.connect(tmp_path / "metrics.db") as conn:
        def latest(service):
            return conn.execute(
                "SELECT MAX(ts) FROM samples WHERE metric='service_up' AND json_extract(tags,'$.backend')=?",
                (service,),
            ).fetchone()[0]
        assert latest("household-voice") == FIXED_NOW - 7 * 3600  # hours old
        assert latest("household-embedding") is None  # never observed
        assert latest("household-chat") == FIXED_NOW  # the rest are fresh
    # The voice service must not answer a live probe, or the UI would say "checked just now".
    from scripts.ui.household_seed import registry

    voice = lambda scenario: registry(18185, scenario)["services"]["household-voice"]["config"]["health_endpoint"]
    assert voice("stale-health") == "/fixture/no-health"
    assert voice("default") == "/health"


def test_new_member_scenario(tmp_path):
    manifest = _seed(tmp_path, "new-member")
    assert manifest["member_email"] == "noor@example.test"
    noor = manifest["people"]["noor"]
    with sqlite3.connect(tmp_path / "admin.db") as conn:
        row = conn.execute("SELECT display_name,last_login_at FROM users WHERE id=?", (noor,)).fetchone()
        assert not row[0].isascii() and row[1] is not None  # signed in, nothing else yet
        assert conn.execute("SELECT COUNT(*) FROM account_access WHERE user_id=? AND disabled_at IS NULL", (noor,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM apps WHERE owner_user_id=?", (noor,)).fetchone()[0] == 0
    assert "noor" not in manifest["apps"]
    assert {asset["owner"] for asset in manifest["assets"]} <= {"sam", "robin", "alex"}


def test_unknown_scenario_is_refused(tmp_path):
    from scripts.ui.household_seed import seed

    with pytest.raises(ValueError, match="scenario"):
        seed(tmp_path, 18184, "no-such-scenario")


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


def test_budget_history_respects_every_applicable_cap(tmp_path):
    _seed(tmp_path, "budget-exhausted")
    with sqlite3.connect(tmp_path / "admin.db") as conn:
        profiles = {
            ident: json.loads(document)["remote"]
            for ident, document in conn.execute("SELECT id,profile_json FROM profiles")
        }
        settings = json.loads(conn.execute(
            "SELECT value_json FROM operator_settings WHERE key='provider_admin'"
        ).fetchone()[0])
    paid = []
    rejected = 0
    for ts, receipt in sorted(_receipts(tmp_path), key=lambda row: (row[0], row[1]["request_id"])):
        if not receipt.get("remote"):
            continue
        month = int(datetime.fromtimestamp(ts, timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
        profile = profiles[receipt["app_profile"]]
        provider = settings["providers"][receipt["backend"]]["caps"]
        global_caps = settings["global_caps"]
        exhausted = False
        for matches, daily, monthly in (
            (lambda r: r["app_profile"] == receipt["app_profile"],
             profile["budget_usd_per_day"], profile["budget_usd_per_month"]),
            (lambda r: r["backend"] == receipt["backend"],
             provider["daily_usd"], provider["monthly_usd"]),
            (lambda r: True, global_caps["daily_usd"], global_caps["monthly_usd"]),
        ):
            for since, cap in ((ts - 86400, daily), (month, monthly)):
                spent = sum((Decimal(r["cost"]["usd"]) for when, r in paid
                             if when >= since and matches(r)), Decimal(0))
                exhausted |= spent >= Decimal(str(cap))
        if receipt["status"] == "succeeded":
            assert not exhausted, receipt["request_id"]
            paid.append((ts, receipt))
        elif receipt["metadata"].get("status_code") == 429:
            rejected += 1
            # The base household also has an independent per-request rejection.
            estimate = Decimal(receipt["usage"]["prompt_tokens"]) * Decimal("0.000003")
            assert exhausted or estimate > Decimal(profile["max_usd_per_request"])
            assert receipt["status"] == "rejected"
            assert receipt["usage"]["completion_tokens"] == 0
            assert Decimal(receipt["cost"]["usd"]) == 0
        if exhausted:
            assert receipt["status"] == "rejected"
            assert receipt["metadata"]["status_code"] == 429
    assert paid and rejected >= 3
    with sqlite3.connect(tmp_path / "provider_spend.db") as conn:
        ledger = {rid: Decimal(cost) for rid, cost in conn.execute(
            "SELECT request_id,actual_usd FROM provider_spend")}
    assert ledger == {r["request_id"]: Decimal(r["cost"]["usd"]) for _, r in paid}


def test_provider_outage_has_no_paid_successes(tmp_path):
    manifest = _seed(tmp_path, "provider-down")
    details = manifest["scenario_details"]
    affected = [
        r for ts, r in _receipts(tmp_path)
        if ts > details["last_success_at"] and r.get("remote")
        and r.get("backend") == details["provider"]
    ]
    assert affected
    assert all(r["status"] != "succeeded" for r in affected)
    for receipt in affected:
        assert receipt["usage"]["completion_tokens"] == 0
        assert Decimal(receipt["cost"]["usd"]) == 0
    with sqlite3.connect(tmp_path / "provider_spend.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM provider_spend WHERE provider=? AND created_at>?",
            (details["provider"], details["last_success_at"]),
        ).fetchone()[0] == 0


@pytest.fixture
def repeated_household_seeds(tmp_path):
    paths = [tmp_path / "first", tmp_path / "second"]
    for path in paths:
        path.mkdir()
        _seed(path, "running")
    return paths


def test_health_timings_are_varied_and_repeatable(repeated_household_seeds):
    def timings(path):
        with sqlite3.connect(path / "metrics.db") as conn:
            return conn.execute(
                "SELECT ts,json_extract(tags,'$.backend'),value FROM samples "
                "WHERE metric='service_latency_ms' ORDER BY ts,tags"
            ).fetchall()

    first, second = map(timings, repeated_household_seeds)
    assert first == second
    for backend in {row[1] for row in first}:
        values = [value for _, sid, value in first if sid == backend]
        assert len(set(values)) > 20
        assert any(a != b for a, b in zip(values, values[14:]))
    down = [value for _, backend, value in first if backend == "household-embedding"]
    assert len({value for value in down if value > 100}) > 20


def test_capture_isolation_restarts_before_each_try_and_checks_state_identity():
    repo = Path(__file__).resolve().parents[1]
    source = (repo / "web/e2e/household-capture.mjs").read_text()
    body = source.split("async function captureTryResult(", 1)[1].split(
        "async function capturePeopleViews(", 1
    )[0]
    steps = [
        "await stopServer();",
        "await removeIfPresent(rm, manifestPath);",
        "server = startServer();",
        "const seed = await readySeed();",
        "await newContext(seed,",
        "await run.click();",
    ]
    positions = [body.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "seenFixtureStates.has(seed.fixture_state)" in source
    assert "seenFixtureStates.add(seed.fixture_state)" in source
    assert 'throw new Error("Try capture requires a fresh disposable fixture state")' in source
    serve = (repo / "scripts/ui/serve.py").read_text()
    assert "manifest['fixture_state'] = state.name" in serve


def test_capture_isolation_clock_covers_late_imports_and_existing_aliases(tmp_path):
    # Run the real clock installer alone in a subprocess: never freeze pytest's clock.
    probe = """import time as clock
from time import time as wall_now, time_ns as wall_ns
import datetime as dates
from datetime import datetime as stamp, date as day
def readings():
    return (clock.time(), wall_now(), wall_ns(),
            dates.datetime.now(dates.timezone.utc).timestamp(),
            stamp.now(dates.timezone.utc).timestamp(),
            stamp.utcnow(), day.today(), clock.gmtime(), clock.localtime(),
            clock.ctime(), clock.strftime('%Y-%m-%d'))
"""
    for name in ("capture_clock_before", "capture_clock_after"):
        (tmp_path / (name + ".py")).write_text(probe)
    repo = Path(__file__).resolve().parents[1]
    script = """
import ast
import importlib
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0, sys.argv[2])
before = importlib.import_module('capture_clock_before')
assert 'capture_clock_after' not in sys.modules
fixed = int(sys.argv[3])
expected = (float(fixed), float(fixed), fixed * 1_000_000_000,
            float(fixed), float(fixed),
            datetime.fromtimestamp(fixed, timezone.utc).replace(tzinfo=None),
            datetime.fromtimestamp(fixed).date(), time.gmtime(fixed),
            time.localtime(fixed), time.ctime(fixed),
            time.strftime('%Y-%m-%d', time.localtime(fixed)))
monotonic = time.monotonic
perf_counter = time.perf_counter
source = ast.parse(Path(sys.argv[1]).read_text())
install = next(node for node in source.body
               if isinstance(node, ast.FunctionDef) and node.name == '_freeze_household_clock')
namespace = {'sys': sys}
exec(compile(ast.Module(body=[install], type_ignores=[]), sys.argv[1], 'exec'), namespace)
namespace['_freeze_household_clock'](fixed)
after = importlib.import_module('capture_clock_after')
assert before.readings() == expected
assert after.readings() == expected
assert time.monotonic is monotonic
assert time.perf_counter is perf_counter
assert time.gmtime(0).tm_year == 1970
assert time.time() == fixed
assert time.time_ns() == fixed * 1_000_000_000
"""
    subprocess.run(
        [sys.executable, "-c", script, str(repo / "scripts/ui/fixture_gateway.py"),
         str(tmp_path), str(FIXED_NOW)],
        check=True, capture_output=True, timeout=30,
    )
