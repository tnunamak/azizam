"""scripts/ci/policy.py's time rule: a per-case ceiling and a whole-tier budget for the Python tier.

A new slow case fails the gate unless tests/budgets.toml [time.slow] lists it with a reason, and the tier's summed
test time stays under its budget, so the tier cannot slowly grow back to tens of minutes unseen."""
from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ci_policy_time", ROOT / "scripts" / "ci" / "policy.py")
policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(policy)

FILES = ["tests/test_a.py", "tests/test_b.py"]


def _case(case: str, seconds: float, *, framework: str = "pytest", attempt: int = 0) -> dict:
    return {"framework": framework, "case": case, "attempt": attempt, "duration_s": seconds}


def _budgets(case_max: float = 5.0, total: float = 100.0, slow: dict | None = None) -> dict:
    return {"time": {"pytest_case_max_s": case_max, "pytest_total_s": total, "slow": slow or {}}}


def test_cases_within_the_ceiling_and_budget_pass() -> None:
    problems, summary = policy.check_time([_case("tests/test_a.py::t1", 4.9), _case("tests/test_b.py::t2", 1.0)],
                                          _budgets(), FILES)
    assert problems == []
    assert summary == "time pytest 6/100 s, slowest 4.9 s"


def test_an_unlisted_case_over_the_ceiling_fails_and_names_the_case() -> None:
    problems, _ = policy.check_time([_case("tests/test_a.py::slow", 5.1)], _budgets(), FILES)
    assert problems == ["time: tests/test_a.py::slow took 5.1 s, over the 5 s case ceiling; make it faster, or list it "
                        "in tests/budgets.toml [time.slow] with max_s and a reason"]


def test_a_listed_case_may_take_its_max_s_and_not_more() -> None:
    slow = {"tests/test_a.py::crash[*]": {"max_s": 12, "reason": "real SIGKILL on host disk"}}
    ok, _ = policy.check_time([_case("tests/test_a.py::crash[0]", 11.0)], _budgets(slow=slow), FILES)
    assert ok == []
    over, _ = policy.check_time([_case("tests/test_a.py::crash[1]", 12.5)], _budgets(slow=slow), FILES)
    assert over == ["time: tests/test_a.py::crash[1] took 12.5 s, over its [time.slow] max_s 12"]


def test_the_tier_budget_counts_every_first_attempt_pytest_case_and_nothing_else() -> None:
    cases = [_case(f"tests/test_a.py::t{i}", 4.0) for i in range(30)]
    cases += [_case("web/x.test.ts::t", 50.0, framework="vitest"), _case("tests/test_a.py::t0", 50.0, attempt=1)]
    problems, summary = policy.check_time(cases, _budgets(total=100.0), FILES)
    assert problems == ["time: the pytest cases took 120 s together, over the tier budget pytest_total_s 100"]
    assert summary.startswith("time pytest 120/100 s")


def test_a_slow_entry_needs_a_reason_and_an_existing_file() -> None:
    slow = {"tests/test_a.py::x": {"max_s": 9}, "tests/test_gone.py::y": {"max_s": 9, "reason": "gone"}}
    problems, _ = policy.check_time([], _budgets(slow=slow), FILES)
    assert problems == ["time: [time.slow] 'tests/test_a.py::x' needs a numeric max_s and a reason",
                        "time: [time.slow] 'tests/test_gone.py::y' names a file that no longer exists; remove it"]


def test_the_limits_only_fall_against_the_target_branch() -> None:
    problems, _ = policy.check_time([], _budgets(case_max=6.0, total=120.0), FILES, base=_budgets(case_max=5.0, total=100.0))
    assert problems == ["time: pytest_case_max_s is 6.0, above 5.0 on the target branch; it only falls",
                        "time: pytest_total_s is 120.0, above 100.0 on the target branch; it only falls"]


def test_a_missing_time_section_fails() -> None:
    assert policy.check_time([], {}, FILES)[0] == ["time: tests/budgets.toml [time] needs pytest_case_max_s and pytest_total_s"]


def test_the_repository_budget_has_a_valid_time_section() -> None:
    budgets = tomllib.loads((ROOT / "tests" / "budgets.toml").read_text(encoding="utf-8"))
    files = [path.relative_to(ROOT).as_posix() for path in (ROOT / "tests").rglob("*.py")]
    problems, _ = policy.check_time([], budgets, files)
    assert problems == []


def test_a_case_over_its_limit_but_within_the_fail_factor_is_a_warning_not_a_failure() -> None:
    budgets = _budgets()
    budgets["time"]["pytest_case_fail_factor"] = 3
    slow = {"tests/test_b.py::guard": {"max_s": 10, "reason": "walks every route"}}
    budgets["time"]["slow"] = slow
    problems, summary = policy.check_time([_case("tests/test_a.py::loaded", 14.0), _case("tests/test_b.py::guard", 29.0)],
                                          budgets, FILES)
    assert problems == []
    assert summary.endswith("; 2 over their ceiling under 3x (warning): tests/test_a.py::loaded, tests/test_b.py::guard")
    problems, _ = policy.check_time([_case("tests/test_a.py::stuck", 15.5), _case("tests/test_b.py::guard", 31.0)], budgets, FILES)
    assert problems == ["time: tests/test_a.py::stuck took 15.5 s, over the 5 s case ceiling; make it faster, or list it "
                        "in tests/budgets.toml [time.slow] with max_s and a reason (fails past 3x)",
                        "time: tests/test_b.py::guard took 31.0 s, over its [time.slow] max_s 10 (fails past 3x)"]


def test_the_fail_factor_only_falls_against_the_target_branch() -> None:
    budgets, base = _budgets(), _budgets()
    budgets["time"]["pytest_case_fail_factor"] = 4
    base["time"]["pytest_case_fail_factor"] = 3
    assert policy.check_time([], budgets, FILES, base=base)[0] == [
        "time: pytest_case_fail_factor is 4, above 3 on the target branch; it only falls"]
