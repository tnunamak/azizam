from dataclasses import replace
from decimal import Decimal
import sqlite3
import unittest
import httpx
from unittest.mock import AsyncMock

import pytest

from gateway import providers
from tests.support.access import identity as identity_of, plan_for  # noqa: E402
import test_provider_execution as execution_tests
from test_provider_execution import _Client, _Response, _body


class ReconciliationExecutionFixture(unittest.IsolatedAsyncioTestCase):
    setUp = execution_tests.ProviderExecutionTests.setUp
    tearDown = execution_tests.ProviderExecutionTests.tearDown
    _execute = execution_tests.ProviderExecutionTests._execute


class UnknownChargeExecutionTests(ReconciliationExecutionFixture):
    async def test_malformed_completed_charge_blocks_second_dispatch(self):
        self.profile = plan_for('paid-app', paid=['openrouter'], usd_per_day='0.02')
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
                                'paid-remote', {}, {'credential_origin': 'https://provider.test', 'spend_caps_usd': {'daily': '0.02'}})
    plan = identity_of(plan_for('one-plan', paid=['one', 'two']))
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


@pytest.mark.parametrize('scope', ['provider', 'person', 'global'])
def test_reconciliation_barrier_does_not_age_out(scope, tmp_path, monkeypatch):
    from gateway import admin_store
    monkeypatch.setenv('RECON_TEST_KEY', 'test')
    ledger = tmp_path / 'spend.db'
    admin_db = tmp_path / 'admin.db'
    admin_store.init_db(admin_db)
    ref = providers.ProviderRef('one', 'https://provider.test', ('openai',), 'RECON_TEST_KEY',
                                'paid-remote', {}, {'credential_origin': 'https://provider.test'})
    plan = plan_for('one-plan', paid=['one', 'two'])
    if scope == 'provider':
        ref = replace(ref, config={'credential_origin': 'https://provider.test', 'spend_caps_usd': {'monthly': '0.02'}})
    elif scope == 'person':
        plan = plan_for('one-plan', paid=['one', 'two'], usd_per_month='0.02')
    else:
        admin_store.put_operator_setting('provider_admin', {'global_caps': {'monthly_usd': '0.02'}}, path=admin_db)
    plan = identity_of(plan)
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


def test_direct_estimator_resolves_upstream_price_and_preserves_alias_override(monkeypatch):
    seen = []

    def fixed_token_count(payload):
        seen.append(payload)
        return 1000

    monkeypatch.setattr(providers, '_estimate_tokens_from_json', fixed_token_count)
    ref = providers.ProviderRef('one', 'https://provider.test', ('openai',), 'KEY', 'paid-remote',
        {'input_usd_per_million_tokens':'1', 'output_usd_per_million_tokens':'1', 'models': {
            'upstream': {'output_usd_per_million_tokens':'100'},
            'one/alias': {'input_usd_per_million_tokens':'5'}}},
        {'credential_origin': 'https://provider.test', 'models':[{'id':'alias', 'upstream_model':'upstream'}]})
    cost = providers.estimate_chat_cost(ref, {'model':'one/alias', 'messages':[], 'max_tokens':100})
    assert seen == [{'messages': []}]
    # 1000 input tokens at the alias rate 5 plus 100 output tokens at the upstream rate 100.
    assert cost.usd == Decimal('0.015000000')
    assert cost.source == 'price-table'
    assert cost.provider == 'one'


class RejectedChargeCleanupTests(ReconciliationExecutionFixture):
    async def test_cleanup_cancellation_cannot_leave_official_sdk_charge_unsettled(self):
        import asyncio
        class CancelledClose(_Response):
            async def aclose(self):
                raise asyncio.CancelledError()
        self.provider = providers.ProviderRef(
            'openai', 'https://api.openai.com/v1', ('openai',),
            'OPENROUTER_API_KEY', 'paid-remote', {'cost_model': 'openai/gpt-4.1-mini'},
            {'preset_id': 'openai', 'credential_origin': 'https://api.openai.com',
             'models': [{'id': 'gpt-4.1-mini'}]},
        )
        self.body = {**self.body, 'model': 'openai/gpt-4.1-mini'}
        self.profile = plan_for('paid-app', paid=['openai'], usd_per_day='0.02')
        send = AsyncMock(return_value=(_Client(), CancelledClose(status_code=500,
            headers={'content-type':'application/json', 'x-litellm-response-cost':'0.03'})))
        with self.assertRaises(asyncio.CancelledError):
            await self._execute(send=send)
        next_response, next_send = await self._execute()
        assert next_response.status_code == 402
        next_send.assert_not_awaited()
        with sqlite3.connect(self.ledger) as conn:
            assert conn.execute('SELECT status,actual_usd FROM provider_spend').fetchone() == ('settled','0.030000000')


class MalformedHeaderChargeTests(ReconciliationExecutionFixture):
    async def test_unrepresentable_header_bill_cannot_leave_reserved_charge(self):
        send = AsyncMock(return_value=(_Client(), _Response(
            headers={'content-type':'application/json', 'x-litellm-response-cost':'1e1000'}, body=b'{}')))
        response, _ = await self._execute(send=send)
        assert response.status_code == 200
        second, second_send = await self._execute()
        assert second.status_code == 402
        second_send.assert_not_awaited()


class AliasPriceAdmissionTests(ReconciliationExecutionFixture):
    async def test_alias_price_override_is_not_replaced_by_upstream_default(self):
        from unittest.mock import patch
        self.provider = replace(self.provider,
            config={
                **self.provider.config,
                'models':[{'id':'basic','upstream_model':'upstream'}],
            },
            price={'input_usd_per_million_tokens':'1', 'output_usd_per_million_tokens':'1', 'models': {
                'upstream': {'output_usd_per_million_tokens':'1'},
                'openrouter/basic': {'output_usd_per_million_tokens':'100'}}})
        self.body = {**self.body, 'model':'openrouter/basic', 'max_tokens':100}
        estimates = []
        estimate = providers.estimate_request_cost
        def capture(provider, body, **kwargs):
            cost = estimate(provider, body, **kwargs)
            estimates.append(cost)
            return cost
        with patch('gateway.providers.estimate_request_cost', side_effect=capture):
            response, _ = await self._execute()
        assert response.status_code == 200
        assert estimates[0].usd >= Decimal('0.01')
