from dataclasses import replace
from decimal import Decimal
import sqlite3
import unittest
import httpx
from unittest.mock import AsyncMock

import pytest

from gateway import providers
from gateway.policy import AppProfile, RemoteProviderPolicy
import test_provider_execution as execution_tests
from test_provider_execution import _Client, _Response, _body


class ReconciliationExecutionFixture(unittest.IsolatedAsyncioTestCase):
    setUp = execution_tests.ProviderExecutionTests.setUp
    tearDown = execution_tests.ProviderExecutionTests.tearDown
    _execute = execution_tests.ProviderExecutionTests._execute


class UnknownChargeExecutionTests(ReconciliationExecutionFixture):
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


class AutomaticReconciliationTests(ReconciliationExecutionFixture):
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


class InterruptedChargeTests(ReconciliationExecutionFixture):
    async def test_nonstream_read_error_blocks_next_admission(self):
        from unittest.mock import patch
        with patch('gateway.provider_execution.read_llm_response_body', side_effect=httpx.ReadError('interrupted')):
            response, _ = await self._execute()
        assert response.status_code == 502
        second, send = await self._execute()
        assert second.status_code == 402
        send.assert_not_awaited()

    async def test_concurrent_owner_settlement_is_reported_from_ledger(self):
        from unittest.mock import patch
        self.body = {**self.body, 'stream': True}
        send = AsyncMock(return_value=(_Client(), _Response(headers={'content-type': 'text/event-stream'},
            chunks=[b'data: {"id":"generation-1"}\n\n', b'data: [DONE]\n\n'])))
        async def owner_reconciles(provider, generation_id):
            pending = providers.list_provider_reconciliations(path=self.ledger)
            providers.reconcile_provider_spend(pending[0]['id'], '0.05', provider_id=provider.id, path=self.ledger)
            return providers.Cost(Decimal('0.03'), 'provider-generation-stats', provider.id)
        with patch('gateway.provider_reconciliation.lookup_generation_cost', side_effect=owner_reconciles):
            response, _ = await self._execute(send=send)
            await _body(response)
        assert self.receipts[0].cost['usd'] == '0.050000000'
        assert self.receipts[0].cost['source'] == 'owner-reported'


def test_legacy_unknown_reservations_migrate_conservatively(tmp_path):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE provider_spend (id INTEGER PRIMARY KEY,provider TEXT,profile_id TEXT,status TEXT,estimated_usd TEXT,actual_usd TEXT,cost_source TEXT,created_at INTEGER,settled_at INTEGER)')
        conn.execute("INSERT INTO provider_spend VALUES (1,'one','plan','reserved','0.001',NULL,'price-table',1,NULL)")
    providers.ensure_ledger(path)
    assert providers.list_provider_reconciliations(path=path)[0]['status'] == 'needs_reconciliation'


class LaterStatsRetryTests(ReconciliationExecutionFixture):
    async def test_later_admission_retries_unavailable_generation_stats(self):
        from unittest.mock import patch
        self.provider = replace(self.provider, config={**self.provider.config, 'generation_stats': {
            'path': '/generation', 'id_param': 'id', 'cost_path': ['data', 'total_cost']}})
        self.body = {**self.body, 'stream': True}
        send = AsyncMock(return_value=(_Client(), _Response(headers={'content-type': 'text/event-stream'},
            chunks=[b'data: {"id":"generation-1"}\n\n', b'data: [DONE]\n\n'])))
        with patch('gateway.provider_reconciliation.lookup_generation_cost',
                   AsyncMock(return_value=providers.Cost(None, 'unknown', 'openrouter'))):
            first, _ = await self._execute(send=send)
            await _body(first)
        with patch('gateway.provider_reconciliation.lookup_generation_cost',
                   AsyncMock(return_value=providers.Cost(Decimal('0.03'), 'provider-generation-stats', 'openrouter'))):
            next_response, next_send = await self._execute()
        assert next_response.status_code == 200
        next_send.assert_awaited_once()
        assert not providers.list_provider_reconciliations(path=self.ledger)
