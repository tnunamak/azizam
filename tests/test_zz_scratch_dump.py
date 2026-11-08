import json
import proxy

def test_dump():
    proxy.app.openapi_schema = None
    rows = proxy.app.openapi()["x-azizam-workflows"]
    row = rows[16]
    al = proxy._gateway_aliases_provider()
    lines = [f"{row['id']} {row['aliases']}"]
    for op, sec in al.items():
        for e in (sec or {}).get("stable") or []:
            if e.get("resolves_to") and any(t in str(e["resolves_to"]) for t in row["short_id"].split("-")[:2]):
                lines.append(f"{op}: {e.get('alias')} -> {e.get('resolves_to')}")
    assert False, "\n".join(lines)
