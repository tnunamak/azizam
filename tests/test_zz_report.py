import asyncio
from tests.test_request_budget import ENDPOINTS, _measure, _p, media_store  # noqa
from tests.test_event_loop_guard import gateway  # noqa

def test_report(gateway, media_store):
    lines = []
    for path, auth in ENDPOINTS:
        headers = gateway["bearer"] if auth == "bearer" else {}
        cookies = gateway["cookies"] if auth == "cookie" else {}
        fast, counts, opened = asyncio.run(_measure(path, headers, cookies, 0))
        slow, _, _ = asyncio.run(_measure(path, headers, cookies, 0.1))
        f = fast[3:]
        lines.append(f"{path} opens={max(counts[2:])} p50={_p(f,.5)*1000:.1f} p95={_p(f,.95)*1000:.1f} max={max(f)*1000:.1f} slow_p50={_p(slow[1:],.5)*1000:.0f} slow_max={max(slow[1:])*1000:.0f}")
    assert False, "\n" + "\n".join(lines)
