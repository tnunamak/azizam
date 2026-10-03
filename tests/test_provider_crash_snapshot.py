"""Crash recovery over the actual paid execution/admission path."""

import json
import sqlite3
from unittest.mock import AsyncMock, patch

from test_provider_reconciliation import ReconciliationExecutionFixture
from test_provider_execution import _Client, _Response
from gateway import providers
from gateway.policy import AppProfile, RemoteProviderPolicy
from decimal import Decimal


class CrashSnapshotExecutionTests(ReconciliationExecutionFixture):
    async def test_restored_mid_request_snapshot_cannot_admit_a_second_charge(self):
        self.profile = AppProfile(id='paid-app', remote=RemoteProviderPolicy(
            providers=frozenset({'openrouter'}), budget_usd_per_day=Decimal('0.02')))
        snapshot = self.ledger.with_name('mid_request.db')
        billed = []
        async def billed_before_settlement(execution, **kwargs):
            billed.append(Decimal('0.03'))
            # Snapshot has only the estimate; the provider has already billed.
            with sqlite3.connect(self.ledger) as source, sqlite3.connect(snapshot) as target:
                source.backup(target)
            return _Client(), _Response(body=json.dumps({'usage': {'cost': '0.03'}}).encode())
        first, _ = await self._execute(send=AsyncMock(side_effect=billed_before_settlement))
        assert first.status_code == 200
        with sqlite3.connect(snapshot) as source, sqlite3.connect(self.ledger) as target:
            now = source.execute("SELECT created_at FROM provider_spend").fetchone()[0]
            source.backup(target)
        # A restored row may belong to this process in a test; expiry still
        # catches it in admission, even without a startup event.
        with patch.object(providers.time, 'time', return_value=now + 121):
            second, dispatch = await self._execute()
        assert second.status_code == 402
        assert b'provider_needs_reconciliation' in second.body
        dispatch.assert_not_awaited()
        assert sum(billed) == Decimal('0.03')
        assert providers.list_provider_reconciliations(path=self.ledger)[0]['status'] == 'needs_reconciliation'
