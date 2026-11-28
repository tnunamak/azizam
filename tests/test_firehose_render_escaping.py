import pytest

import firehose_render
from test_firehose_render import _Document, _text


@pytest.mark.parametrize("section", ["backends", "keys", "events"])
def test_untrusted_status_values_remain_visible_inert_text(section):
    taint = '<img src=x onerror="alert(1)"><svg onload="alert(2)">&'
    if section == "backends":
        rendered = firehose_render._render_backends([{
            "name": taint, "role": taint, "live_status": "down",
            "url": taint, "last_error": taint, "note": taint,
        }])
        expected_occurrences = 5
    elif section == "keys":
        rendered = firehose_render._render_auth_keys([{
            "name": taint, "active": False, "last_seen": None,
            "requests_24h": 0, "errors_4xx_24h": 0, "errors_5xx_24h": 0,
            "latency_ms_p50": None, "latency_ms_p95": None,
            "top_endpoints": [{"path": taint, "count": 1}],
        }])
        expected_occurrences = 2
    else:
        rendered = firehose_render._render_recent_events([{
            "ts": 1, "type": taint, "summary": taint,
            "status": 503, "latency_ms": 1500, "key": taint,
            "model": taint, "diagnostic": taint,
        }])
        expected_occurrences = 5

    document = _Document(rendered)
    visible = "\n".join(_text(node) for node in document.find("td"))
    assert visible.count(taint) == expected_occurrences
    assert not document.find("img")
    assert not document.find("svg")
    assert not document.find("script")
    assert all(
        not name.lower().startswith("on")
        for node in document.nodes for name in node["attrs"]
    )
