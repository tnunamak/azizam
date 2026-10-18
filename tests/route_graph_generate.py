"""Write the route graph of this tree to the path in argv[1].

Run outside the hermetic sandbox, from a tree whose frontends are the baseline
being frozen, with every gateway store in a fresh temporary directory under ~/.tmp:

    venv/bin/python tests/route_graph_generate.py tests/fixtures/route_graph_frozen.json

Do not regenerate the fixture to make a test pass. A change to the route graph
is a deliberate addition: list it in tests/test_route_graph_freeze.py with its reason.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(tempfile.mkdtemp(dir=Path.home() / ".tmp"))
for _name, _file in {"AZIZAM_ADMIN_DB": "admin.db", "AZIZAM_METRICS_DB": "metrics.db",
                     "AZIZAM_RESPONSES_DB": "responses.db", "AZIZAM_RECEIPTS_DB": "receipts.db",
                     "AZIZAM_MEDIA_JOBS_DB": "media_jobs.db"}.items():
    os.environ[_name] = str(STATE / _file)
shutil.copy(ROOT / "proxy_config.yml", STATE / "proxy_config.yml")
os.environ["AZIZAM_CONFIG_PATH"] = str(STATE / "proxy_config.yml")
os.chdir(ROOT)
sys.path[:0] = [str(ROOT / "tests"), str(ROOT)]

try:
    import proxy  # noqa: E402
    import route_graph  # noqa: E402

    data = route_graph.capture(proxy)
    Path(sys.argv[1]).write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    print(len(data["routes"]), "routes,", len(data["dispatch"]), "probed paths")
finally:
    shutil.rmtree(STATE, ignore_errors=True)
