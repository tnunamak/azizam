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


@pytest.mark.parametrize('scope', ['provider', 'plan', 'global'])
def test_reconciliation_barrier_does_not_age_out(scope, tmp_path, monkeypatch):
    from gateway import admin_store
    monkeypatch.setenv('RECON_TEST_KEY', 'test')
    ledger = tmp_path / 'spend.db'
    admin_db = tmp_path / 'admin.db'
    admin_store.init_db(admin_db)
    ref = providers.ProviderRef('one', 'https://provider.test', ('openai',), 'RECON_TEST_KEY',
                                'paid-remote', {}, {})
    plan = AppProfile(id='one-plan', remote=RemoteProviderPolicy(providers=frozenset({'one', 'two'})))
    if scope == 'provider':
        ref = replace(ref, config={'spend_caps_usd': {'monthly': '0.02'}})
    elif scope == 'plan':
        plan = replace(plan, remote=replace(plan.remote, budget_usd_per_month=Decimal('0.02')))
    else:
        admin_store.put_operator_setting('provider_admin', {'global_caps': {'monthly_usd': '0.02'}}, path=admin_db)
    estimate = providers.Cost(Decimal('0.001'), 'price-table', 'one')
    reservation = providers.reserve_provider_spend(plan, ref, estimate, path=ledger, admin_db_path=admin_db, now=1)
    providers.settle_provider_spend(reservation, None, path=ledger, now=2)
    target = replace(ref, id='two') if scope != 'provider' else ref
    with pytest.raises(providers.ProviderNeedsReconciliation):
        providers.reserve_provider_spend(plan, target, estimate, path=ledger, admin_db_path=admin_db, now=2000000000)
    providers.reconcile_provider_spend(reservation.id, '0', provider_id='one', path=ledger)
    providers.reserve_provider_spend(plan, target, estimate, path=ledger, admin_db_path=admin_db, now=2000000000)


@pytest.mark.parametrize('amount', ['NaN', 'Infinity', '-1', True, '1e1000', None])
def test_manual_reconciliation_rejects_invalid_cost(amount, tmp_path):
    with pytest.raises(ValueError):
        providers.reconcile_provider_spend(1, amount, provider_id='one', path=tmp_path/'spend.db')


class AutomaticReconciliationTests(ProviderExecutionTests):
    async def test_malformed_stream_reconciles_by_provider_generation_id(self):
        from unittest.mock import patch
        self.body = {**self.body, 'stream': True}
        send = AsyncMock(return_value=(_Client(), _Response(
            headers={'content-type': 'text/event-stream'}, chunks=[
                b'data: malformed\n\n',
                b'data: {"id":"generation-1","usage":{"cost":0.03}}\n\n',
                b'data: [DONE]\n\n'])))
        lookup = AsyncMock(return_value=providers.Cost(Decimal('0.03'), 'provider-generation-stats', 'openrouter'))
        with patch('gateway.provider_reconciliation.lookup_generation_cost', lookup):
            response, _ = await self._execute(send=send)
            await _body(response)
        lookup.assert_awaited_once_with(self.provider, 'generation-1')
        with sqlite3.connect(self.ledger) as conn:
            row = conn.execute('SELECT status,actual_usd,generation_id,request_id FROM provider_spend').fetchone()
        assert row[:3] == ('settled', '0.030000000', 'generation-1')
        assert row[3] == self.receipts[0].request_id
        assert self.receipts[0].cost['source'] == 'provider-generation-stats'
        assert self.receipts[0].cost['reconciliation_status'] == 'settled'
