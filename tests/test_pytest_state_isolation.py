"""Regression coverage for collection-time gateway state isolation."""

from __future__ import annotations

from pathlib import Path

import firehose_metrics
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
        "AZIZAM_ADMIN_DB": proxy.ADMIN_DB_PATH,
        "admin_store default": admin_store.DB_PATH,
        "AZIZAM_METRICS_DB": metrics_store.DB_PATH,
        "AZIZAM_RESPONSES_DB": responses_store.DB_PATH,
        "AZIZAM_RECEIPTS_DB": receipt_store.DB_PATH,
        "AZIZAM_MEDIA_JOBS_DB": media_jobs.DB_PATH,
        "AZIZAM_KEY_ACTIVITY_LOG": firehose_metrics._DEFAULT_CACHE_PATH,
    }
    assert actual == {
        "AZIZAM_ADMIN_DB": gateway_test_state_dir / "admin.db",
        "admin_store default": gateway_test_state_dir / "admin.db",
        "AZIZAM_METRICS_DB": gateway_test_state_dir / "metrics.db",
        "AZIZAM_RESPONSES_DB": gateway_test_state_dir / "responses.db",
        "AZIZAM_RECEIPTS_DB": gateway_test_state_dir / "receipts.db",
        "AZIZAM_MEDIA_JOBS_DB": gateway_test_state_dir / "media_jobs.db",
        "AZIZAM_KEY_ACTIVITY_LOG": gateway_test_state_dir / "key_activity.jsonl",
    }
