"""Run one revocation regression with its invalidation line deleted in memory.

Invoke through run-hermetic-pytest.sh --python-script. Exit 1 (the regression
sees 200 instead of 403) proves that the test detects the missing invalidation.
Production source files and the committed tree are never changed.
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import proxy
from gateway import signin_admin

if sys.argv[1:] == ["signin"]:
    module, name = signin_admin, "apply_changes"
    test = "test_signin_rules_admin_loss_ends_warm_asset_family"
elif sys.argv[1:] == ["provider"]:
    module, name = proxy, "_end_provider_access_in"
    test = "test_provider_access_end_ends_warm_asset_family"
else:
    raise SystemExit("supply signin or provider")
source = inspect.getsource(getattr(module, name))
lines = source.splitlines(keepends=True)
needle = "tx.after_commit(accounts.invalidate_asset_families)"
assert sum(needle in line for line in lines) == 1
mutant = "".join(line for line in lines if needle not in line)
exec(compile(mutant, f"<temporary {module.__name__}.{name} without invalidation>", "exec"), vars(module))
raise SystemExit(pytest.main(["-q", f"tests/test_session_asset_urls.py::{test}"]))
