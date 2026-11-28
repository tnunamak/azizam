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
if sys.argv[1:] == ["signin"]:
    module_name, name = "gateway.signin_admin", "apply_changes"
    test = "test_signin_rules_admin_loss_ends_warm_asset_family"
elif sys.argv[1:] == ["provider"]:
    module_name, name = "proxy", "_end_provider_access_in"
    test = "test_provider_access_end_ends_warm_asset_family"
else:
    raise SystemExit("supply signin or provider")


class DeleteInvalidation:
    def pytest_collection_modifyitems(self):
        # conftest must first install its test-owned paths and hermetic guard.
        import importlib
        module = importlib.import_module(module_name)
        source = inspect.getsource(getattr(module, name))
        lines = source.splitlines(keepends=True)
        needle = "tx.after_commit(accounts.invalidate_asset_families)"
        assert sum(needle in line for line in lines) == 1
        mutant = "".join(line for line in lines if needle not in line)
        exec(compile(mutant, f"<temporary {module.__name__}.{name} without invalidation>", "exec"), vars(module))


raise SystemExit(pytest.main(["-q", f"tests/test_session_asset_urls.py::{test}"], plugins=[DeleteInvalidation()]))
