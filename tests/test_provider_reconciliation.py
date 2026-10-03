from dataclasses import replace
from decimal import Decimal
import sqlite3
from unittest.mock import AsyncMock

import pytest

from gateway import providers
from gateway.policy import AppProfile, RemoteProviderPolicy
from test_provider_execution import ProviderExecutionTests, _Client, _Response, _body


class UnknownChargeExecutionTests(ProviderExecutionTests):
    async def test_malformed_completed_charge_blocks_second_dispatch(self):
        self.profile = AppProfile(id='paid-app', remote=RemoteProviderPolicy(
            providers=frozenset({'openrouter'}), budget_usd_per_day=Decimal('0.02')))
        self.body = {**self.body, 'stream': True}
        send = AsyncMock(return_value=(_Client(), _Response(
            headers={'content-type': 'text/event-stream'}, chunks=[
                b'data: malformed\n\n',
                b'data: {"id":"generation-1","usage":{"cost":0.03}}\n\n',
                b'data: [DONE]\n\n'])))
        first, _ = await self._execute(send=send)
        await _body(first)
        second, _ = await self._execute(send=send)
        assert second.status_code == 402
        assert b'provider_needs_reconciliation' in second.body
        assert send.await_count == 1
        assert self.receipts[0].cost['reconciliation_status'] == 'needs_reconciliation'


def test_unknown_charge_blocks_only_affected_caps_until_owner_reconciles(tmp_path, monkeypatch):
    monkeypatch.setenv('RECON_TEST_KEY', 'test')
    ledger = tmp_path / 'spend.db'
    ref = providers.ProviderRef('one', 'https://provider.test', ('openai',), 'RECON_TEST_KEY',
                                'paid-remote', {}, {'spend_caps_usd': {'daily': '0.02'}})
    plan = AppProfile(id='one-plan', remote=RemoteProviderPolicy(providers=frozenset({'one','two'})))
    estimate = providers.Cost(Decimal('0.001'), 'price-table', 'one')
    reservation = providers.reserve_provider_spend(plan, ref, estimate, path=ledger)
    providers.settle_provider_spend(reservation, providers.Cost(None, 'unknown', 'one'), path=ledger)
    with pytest.raises(providers.BudgetExceeded, match='reconcil'):
        providers.reserve_provider_spend(plan, ref, estimate, path=ledger)
    other = replace(ref, id='two')
    providers.reserve_provider_spend(plan, other, estimate, path=ledger)
    providers.reconcile_provider_spend(reservation.id, '0.03', provider_id='one', path=ledger)
    with pytest.raises(providers.BudgetExceeded, match='exceed'):
        providers.reserve_provider_spend(plan, ref, estimate, path=ledger)
    assert providers.list_provider_reconciliations(path=ledger) == []
