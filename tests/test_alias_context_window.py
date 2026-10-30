"""Stable chat aliases report the served model's context window."""
import asyncio
from unittest.mock import patch

import proxy


def test_alias_window_follows_target_instead_of_same_named_role():
    data = [
        {'id': 'default', 'azizam_resolves_to': {'chat': 'served'}},
        {'id': 'served', 'context_window': 262144},
        {'id': 'old-role'},
    ]
    roles = {'default': {'context_window': 202752}, 'old-role': {'context_window': 4096}}
    with patch.object(proxy, 'ROLES', roles):
        asyncio.run(proxy._annotate_model_context_windows(data))
    assert [item.get('context_window') for item in data] == [262144, 262144, 4096]


def test_alias_without_known_target_window_does_not_claim_old_role_window():
    data = [{'id': 'default', 'context_window': 202752,
             'azizam_resolves_to': {'chat': 'missing-model'}}]
    with patch.object(proxy, 'ROLES', {'default': {'context_window': 202752}}), \
         patch.object(proxy, 'MODELS_CATALOG', {}), \
         patch.object(proxy, 'config', {'backends': {}}):
        asyncio.run(proxy._annotate_model_context_windows(data))
    assert 'context_window' not in data[0]


def test_unavailable_alias_does_not_claim_same_named_role_window():
    data = [{'id': 'default', 'context_window': 202752,
             'azizam_resolves_to': {'chat': None}}]
    with patch.object(proxy, 'ROLES', {'default': {'context_window': 202752}}):
        asyncio.run(proxy._annotate_model_context_windows(data))
    assert 'context_window' not in data[0]
