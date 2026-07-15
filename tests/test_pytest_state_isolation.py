"""Regression coverage for collection-time gateway state isolation."""

from __future__ import annotations

from pathlib import Path

import gateway.admin_store as admin_store
import gateway.media_jobs as media_jobs
import gateway.receipt_store as receipt_store
import metrics_store
import proxy
import responses_store


def test_gateway_persistent_stores_use_the_pytest_state_directory(
    gateway_test_state_dir: Path,
) -> None:
    actual = {
        "AI_GATEWAY_ADMIN_DB": proxy.ADMIN_DB_PATH,
        "admin_store default": admin_store.DB_PATH,
        "AI_GATEWAY_METRICS_DB": metrics_store.DB_PATH,
        "AI_GATEWAY_RESPONSES_DB": responses_store.DB_PATH,
        "AI_GATEWAY_RECEIPTS_DB": receipt_store.DB_PATH,
        "AI_GATEWAY_MEDIA_JOBS_DB": media_jobs.DB_PATH,
    }
    assert actual == {
        "AI_GATEWAY_ADMIN_DB": gateway_test_state_dir / "admin.db",
        "admin_store default": gateway_test_state_dir / "admin.db",
        "AI_GATEWAY_METRICS_DB": gateway_test_state_dir / "metrics.db",
        "AI_GATEWAY_RESPONSES_DB": gateway_test_state_dir / "responses.db",
        "AI_GATEWAY_RECEIPTS_DB": gateway_test_state_dir / "receipts.db",
        "AI_GATEWAY_MEDIA_JOBS_DB": gateway_test_state_dir / "media_jobs.db",
    }
