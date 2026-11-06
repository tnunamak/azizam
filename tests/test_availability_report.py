from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


def load_report():
    path = Path(__file__).resolve().parents[1] / "scripts/local/availability_report.py"
    spec = importlib.util.spec_from_file_location("availability_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_reads_only_capabilities_and_prints_evidence(monkeypatch, capsys):
    report = load_report()
    calls = []

    def get_json(url, headers):
        calls.append((url, headers))
        return {"capabilities": [{"intent": "chat.completions", "model": "chat",
            "availability": {"status": "ready", "reason": "Recent request succeeded",
                "evidence": {"source": "request", "observed_at_s": 123}}}]}

    monkeypatch.setattr(report, "_get_json", get_json)
    monkeypatch.setattr(sys, "argv", ["availability_report", "http://127.0.0.1:18080"])
    assert report.main() == 0
    assert [url for url, _ in calls] == ["http://127.0.0.1:18080/gateway/v1/capabilities"]
    assert "chat.completions / chat: ready" in capsys.readouterr().out


def test_report_returns_failure_without_leaking_response(monkeypatch, capsys):
    report = load_report()

    def fail(url, headers):
        raise RuntimeError("HTTP 401")

    monkeypatch.setattr(report, "_get_json", fail)
    monkeypatch.setattr(sys, "argv", ["availability_report", "http://127.0.0.1:18080"])
    assert report.main() == 1
    assert "HTTP 401" in capsys.readouterr().err


def test_report_rejects_invalid_capability_payload(monkeypatch):
    report = load_report()
    monkeypatch.setattr(report, "_get_json", lambda url, headers: {"error": "denied"})
    monkeypatch.setattr(sys, "argv", ["availability_report", "http://127.0.0.1:18080"])
    assert report.main() == 1
