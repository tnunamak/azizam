"""Write the route/method matrix of this tree to the path in argv[1].

Runs outside the hermetic sandbox (which mounts the checkout read-only), with
every gateway store in a fresh temporary directory under ~/.tmp:

    venv/bin/python tests/route_matrix_generate.py /tmp/matrix.json

The r18 baseline (tests/fixtures/route_method_baseline.json) was written this
way before the UI moved to the root. Do not regenerate it to make a test pass;
add the reviewed difference to the test instead.
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

import proxy  # noqa: E402
import route_matrix  # noqa: E402
import test_proxy_admin_integration as admin_integration  # noqa: E402
from gateway import accounts  # noqa: E402

case = admin_integration.ProxyAdminIntegrationTests(methodName="runTest")
case.setUp()
try:
    accounts.initialize(case.db)
    data = route_matrix.baseline(proxy.app, case.client, case.admin_key, route_matrix.mint_app_key(case))
finally:
    case.tearDown()
    shutil.rmtree(STATE, ignore_errors=True)
Path(sys.argv[1]).write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
print(len(data["routing"]), "paths,", len(data["live"]), "live requests")
