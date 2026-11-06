"""scripts/transition-proof against a fake gateway that serves the transition routes and a chat route with a cache.

Each test runs the real command and reads its exit code, its output and its result JSON. The fake's attributes
(tests/support/fake_upstream/transition_gateway.py) decide whether the cache survives, whether park or wake fails,
whether an operation hangs or the gate is held.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

from scripts.transition_proof.manifest import InputError, load_cases, read_path
from scripts.transition_proof.runner import _bust_cache
from tests.support.fake_upstream.transition_gateway import DOMAIN, KEY, FakeTransitionGateway

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "transition-proof"
STANDARD = {"cached_tokens_path": "usage.prompt_tokens_details.cached_tokens", "prompt_tokens_path": "usage.prompt_tokens",
            "min_cached_fraction": 0.8, "max_timing_ratio": 0.5}
ALTERNATE = {"cached_tokens_path": "timings.cache_n", "prompt_tokens_path": "timings.prompt_n",
             "min_cached_fraction": 0.8}


@pytest.fixture
def gateway() -> Iterator[FakeTransitionGateway]:
    fake = FakeTransitionGateway().start()
    try:
        yield fake
    finally:
        fake.stop()


def write_cases(directory: Path, cache_check: dict[str, Any] = STANDARD, *, labels: tuple[str, ...] = ("small",)) -> Path:
    cases = []
    for label in labels:
        body = {"model": "frozen-name", "stream": False, "seed": 1, "temperature": 0, "max_tokens": 8,
                "messages": [{"role": "system", "content": "You keep records."},
                             {"role": "user", "content": f"record {label} " * 400}]}
        raw = json.dumps(body).encode()
        (directory / f"{label}.json").write_bytes(raw)
        cases.append({"label": label, "file": f"{label}.json", "sha256": hashlib.sha256(raw).hexdigest(),
                      "followup": "What is record 7?", "count_estimate": {"input_tokens": 1000}})
    manifest = directory / "cases.json"
    manifest.write_text(json.dumps({"cache_check": cache_check, "cases": cases}))
    return manifest


def run(gateway: FakeTransitionGateway, tmp_path: Path, *extra: str, manifest: Path | None = None,
        key_file: Path | None = None) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    if key_file is None:
        key_file = tmp_path / "owner.key"
        key_file.write_text(KEY + "\n")
    out = tmp_path / "result.json"
    process = subprocess.run(
        [sys.executable, str(SCRIPT), "--base-url", gateway.base_url, "--domain", DOMAIN, "--model", "alias",
         "--cases", str(manifest or write_cases(tmp_path)), "--owner-key-file", str(key_file), "--out", str(out),
         "--poll-interval-s", "0.02", "--poll-timeout-s", "1", "--request-timeout-s", "10", "--run-id", "t", *extra],
        capture_output=True, text=True, check=False, timeout=60)
    return process, json.loads(out.read_text()) if out.exists() else {}


def assertions(result: dict[str, Any], case: int = 0) -> dict[str, bool]:
    return {a["name"]: a["passed"] for a in result["cases"][case]["assertions"]}


def steps(result: dict[str, Any], case: int = 0) -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in result["cases"][case]["steps"]}


def test_kept_prefix_passes_with_every_step_recorded(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    process, result = run(gateway, tmp_path)
    assert process.returncode == 0, process.stdout + process.stderr
    assert result["status"] == "passed" and result["exit_code"] == 0 and result["assertions_failed"] == 0
    case = result["cases"][0]
    assert [s["name"] for s in case["steps"]] == ["seed", "park", "wake", "followup", "cold_baseline"]
    assert assertions(result) == {
        "seed_replied": True, "park_succeeded": True, "wake_succeeded": True, "gate_open_after_wake": True,
        "followup_replied": True, "cached_tokens_reported": True, "seed_prompt_tokens_known": True,
        "cached_prefix_kept": True, "cold_baseline_replied": True, "followup_faster_than_cold": True}
    park, wake, followup = steps(result)["park"], steps(result)["wake"], steps(result)["followup"]
    assert (park["op_id"], park["idempotency_key"], park["states"]) == ("op-1", "proof-t-park", ["running", "succeeded"])
    assert (wake["op_id"], wake["idempotency_key"]) == ("op-2", "proof-t-wake")
    assert [t["gate"] for t in case["gate_trace"]] == ["open", "open", "open", "open"]
    assert steps(result)["seed"]["reply_text"] == "answer" and steps(result)["seed"]["usage"]["prompt_tokens"] > 0
    assert followup["cached_tokens"] == steps(result)["seed"]["prompt_tokens"] and followup["seconds"] > 0
    assert gateway.paths()[:3] == ["POST /v1/chat/completions", "GET /admin/engines/domains/proof-domain/gate",
                                   "POST /admin/engines/domains/proof-domain/transitions"]
    sent = [r for r in gateway.requests if r["path"] == "/v1/chat/completions"]
    assert {r["body"]["model"] for r in sent} == {"alias"}  # the frozen body's own model name is never sent
    assert sent[1]["body"]["messages"][:-1] == sent[0]["body"]["messages"]
    assert sent[1]["body"]["messages"][-1] == {"role": "user", "content": "What is record 7?"}
    assert {r["authorization"] for r in gateway.requests} == {f"Bearer {KEY}"}


def test_request_mode_lets_the_followup_trigger_the_wake(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    process, result = run(gateway, tmp_path, "--wake-mode", "request")
    assert process.returncode == 0, process.stdout + process.stderr
    posts = [r["body"]["kind"] for r in gateway.requests if r["method"] == "POST" and r["path"].endswith("/transitions")]
    assert posts == ["park", "wake"]  # the second is the gateway's own wake, key wake-auto
    assert [r["body"]["idempotency_key"] for r in gateway.requests if r["body"] and "kind" in r["body"]] == [
        "proof-t-park", "wake-auto"]
    assert steps(result)["wake"]["mode"] == "request" and assertions(result)["gate_open_after_followup"]


def test_lost_prefix_fails_on_the_cache_assertion_only(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.cache = "lost"
    process, result = run(gateway, tmp_path)
    assert process.returncode == 1
    failed = {name for name, passed in assertions(result).items() if not passed}
    assert failed == {"cached_prefix_kept", "followup_faster_than_cold"}
    detail = next(a for a in result["cases"][0]["assertions"] if a["name"] == "cached_prefix_kept")
    assert detail["cached_tokens"] == 0 and detail["fraction"] == 0 and detail["denominator_source"] == "seed_reply"
    assert result["status"] == "failed" and "FAIL small cached_prefix_kept" in process.stdout


def test_cached_count_that_is_not_faster_fails_the_timing_ratio(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.slow_when_cached = True
    process, result = run(gateway, tmp_path)
    assert process.returncode == 1
    assert {name for name, passed in assertions(result).items() if not passed} == {"followup_faster_than_cold"}


def test_cache_paths_are_data_not_code(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.usage_shape = "alternate"
    process, result = run(gateway, tmp_path, manifest=write_cases(tmp_path, ALTERNATE))
    assert process.returncode == 0, process.stdout
    assert "cold_baseline" not in steps(result) or steps(result)["cold_baseline"]["status"] == "skipped"
    # The same reply read through the standard paths reports no count, so the run fails on that.
    process, result = run(gateway, tmp_path, manifest=write_cases(tmp_path, {**STANDARD, "max_timing_ratio": None}))
    assert process.returncode == 1 and assertions(result)["cached_tokens_reported"] is False


def test_a_failed_park_stops_before_wake_and_followup(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.park = "fail"
    process, result = run(gateway, tmp_path)
    assert process.returncode == 1
    assert assertions(result)["park_succeeded"] is False
    assert steps(result)["park"]["final_state"] == "failed_closed"
    assert [s["status"] for n, s in steps(result).items() if n in ("wake", "followup", "cold_baseline")] == ["skipped"] * 3
    assert [t["gate"] for t in result["cases"][0]["gate_trace"]] == ["open", "failed_closed"]
    assert sum(r["path"] == "/v1/chat/completions" for r in gateway.requests) == 1  # only the seed


def test_a_gate_that_stays_reconciling_is_reported_not_waited_out(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.park = "reconcile"
    process, result = run(gateway, tmp_path, "--poll-timeout-s", "30")
    assert process.returncode == 1
    park = steps(result)["park"]
    assert park["final_state"] == "reconciling" and "owner" in park["error"]
    assert result["cases"][0]["gate_trace"][-1] == {"at": "after_park", "gate": "reconciling"}
    assert result["cases"][0]["status"] == "failed"


def test_a_wake_that_never_finishes_times_out(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.wake = "hang"
    process, result = run(gateway, tmp_path, "--poll-timeout-s", "0.3")
    assert process.returncode == 1
    wake = steps(result)["wake"]
    assert wake["states"] == ["running"] and "still running after 0.3s" in wake["error"]
    assert assertions(result)["wake_succeeded"] is False and "followup_replied" not in assertions(result)


def test_a_conflicting_operation_is_reported_with_its_id(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.park = "conflict"
    process, result = run(gateway, tmp_path)
    assert process.returncode == 1
    assert "op-other" in steps(result)["park"]["error"] and steps(result)["park"]["http_status"] == 409


def test_a_failed_case_leaves_later_cases_not_run(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.cache = "lost"
    process, result = run(gateway, tmp_path, manifest=write_cases(tmp_path, labels=("a", "b")))
    assert process.returncode == 1
    assert [(c["label"], c["status"]) for c in result["cases"]] == [("a", "failed"), ("b", "not_run")]


def test_every_case_runs_its_own_park_wake_cycle(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    process, result = run(gateway, tmp_path, manifest=write_cases(tmp_path, labels=("a", "b")))
    assert process.returncode == 0, process.stdout
    assert [c["status"] for c in result["cases"]] == ["passed", "passed"]
    assert result["assertions_total"] == 20


def test_a_body_that_changed_is_refused_before_anything_is_sent(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    manifest = write_cases(tmp_path, labels=("a", "b"))
    (tmp_path / "b.json").write_text((tmp_path / "b.json").read_text().replace("record", "recorD"))
    process, result = run(gateway, tmp_path, manifest=manifest)
    assert process.returncode == 2
    assert result["status"] == "invalid_input" and "case 'b'" in result["error"] and "sha256" in process.stderr
    assert gateway.requests == []  # not even the valid first case was sent


def test_a_missing_key_file_sends_nothing_and_names_no_secret(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    process, result = run(gateway, tmp_path, key_file=tmp_path / "absent.key")
    assert process.returncode == 2 and gateway.requests == []
    assert "owner key file" in process.stderr and result["status"] == "invalid_input"


def test_an_empty_key_file_is_refused(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    empty = tmp_path / "empty.key"
    empty.write_text("\n")
    process, _ = run(gateway, tmp_path, key_file=empty)
    assert process.returncode == 2 and gateway.requests == []


def test_the_key_never_reaches_output_or_results_even_when_the_gateway_echoes_it(
        gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    gateway.echo_auth = True
    gateway.park = "conflict"  # the 409 body repeats the Authorization header
    process, result = run(gateway, tmp_path)
    assert process.returncode == 1
    text = (tmp_path / "result.json").read_text()
    assert "[redacted]" in text, "the echo reached the result, so this test must prove the redaction"
    for surface in (process.stdout, process.stderr, text):
        assert KEY not in surface
    process, _ = run(gateway, tmp_path, "--dry-run")
    assert KEY not in process.stdout + process.stderr + (tmp_path / "result.json").read_text()


def test_dry_run_validates_and_sends_nothing(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    process, result = run(gateway, tmp_path, "--dry-run")
    assert process.returncode == 0 and gateway.requests == []
    assert result["status"] == "validated" and result["dry_run"] is True
    assert result["cases"][0]["messages"] == 2 and result["cases"][0]["cache_check"]["min_cached_fraction"] == 0.8


def test_dry_run_rejects_a_manifest_with_no_cache_check(gateway: FakeTransitionGateway, tmp_path: Path) -> None:
    process, result = run(gateway, tmp_path, "--dry-run", manifest=write_cases(tmp_path, {}))
    assert process.returncode == 2 and "cached_tokens_path" in result["error"] and gateway.requests == []


def test_a_gateway_that_is_down_is_a_failed_step_not_a_crash(tmp_path: Path) -> None:
    dead = FakeTransitionGateway().start()
    dead.stop()
    process, result = run(dead, tmp_path)
    assert process.returncode == 1 and "Traceback" not in process.stderr
    assert assertions(result) == {"seed_replied": False} and "ConnectError" in steps(result)["seed"]["error"]


def test_dotted_paths_read_lists_and_report_missing_as_none() -> None:
    data = {"a": {"b": [{"c": 4}]}}
    assert read_path(data, "a.b.0.c") == 4
    assert read_path(data, "a.b.1.c") is None and read_path(data, "a.x") is None and read_path(data, "a.b.c") is None


def test_cold_baseline_body_shares_no_prefix_with_the_original() -> None:
    for first in ("text", [{"type": "text", "text": "text"}]):
        body = {"messages": [{"role": "user", "content": first}, {"role": "user", "content": "q"}]}
        busted = _bust_cache(body, "r", "x")
        assert busted["messages"][0] != body["messages"][0] and busted["messages"][1:] == body["messages"][1:]
        assert body["messages"][0]["content"] == first  # the original is untouched


def test_a_streaming_body_is_refused(tmp_path: Path) -> None:
    manifest = write_cases(tmp_path)
    body = json.loads((tmp_path / "small.json").read_text())
    body["stream"] = True
    raw = json.dumps(body).encode()
    (tmp_path / "small.json").write_bytes(raw)
    data = json.loads(manifest.read_text())
    data["cases"][0]["sha256"] = hashlib.sha256(raw).hexdigest()
    manifest.write_text(json.dumps(data))
    with pytest.raises(InputError, match="stream"):
        load_cases(manifest)
