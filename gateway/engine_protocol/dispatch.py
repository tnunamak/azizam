"""Resolve explicitly opted-in engine records and run their declared protocol.

This module owns no routing policy or durable jobs. Public handlers apply those
before invocation. Selection uses the same transaction as other engine paths.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from gateway.engine_contract_view import connection_config, observations_config, offerings_config
from gateway.engine_credentials import credential_headers
from gateway.engine_control import EngineControlRuntime
from gateway.engine_observations import Observation, UNKNOWN
from gateway.engine_protocol import description
from gateway.engine_protocol.adapter import AdapterRegistry, connection_from_record
from gateway.engine_protocol.assets import AssetStore
from gateway.engine_protocol.errors import ProtocolError, UnknownOperation
from gateway.engine_protocol.types import CanonicalRequest, Credential, StreamReply
from gateway.engine_switch import SwitchController, SwitchUnavailable


@dataclass(frozen=True)
class Target:
    engine_id: str
    service: Mapping[str, Any]
    offering: Mapping[str, Any]
    description: description.ProtocolDescription

    @property
    def upstream(self) -> str:
        return str(self.offering.get('upstream') or self.offering['id'])


def opted_in(service: Mapping[str, Any]) -> bool:
    return connection_config(service).get('dispatch') == 'protocol'


def resolve(registry: Mapping[str, Any], model: Any, op: str) -> Target | None:
    """Resolve an offering id (or engine/id); existing records never enter here."""
    if not isinstance(model, str):
        return None
    matches = []
    for engine_id, service in (registry.get('services') or {}).items():
        if not opted_in(service):
            continue
        for offering in offerings_config(service):
            if model in {offering.get('id'), f"{engine_id}/{offering.get('id')}"}:
                matches.append((engine_id, service, offering))
    if not matches:
        return None
    if len(matches) != 1:
        raise ProtocolError('The model names more than one offering. Use engine/id.', code='ambiguous_model')
    engine_id, service, offering = matches[0]
    if service.get('enabled', True) is False:
        raise ProtocolError('The selected engine is disabled.', code='engine_disabled')
    protocol_id = connection_config(service).get('protocol')
    doc = description.load_registry().get(protocol_id)
    if doc is None or doc.executor != 'http-v1':
        raise ProtocolError('Protocol dispatch requires an http-v1 description.', code='unknown_protocol')
    if op not in doc.operations or doc.operations[op].op_class != 'generate':
        raise UnknownOperation(f"The description declares no generation operation '{op}'.")
    # A declared task limits the operation; an absent task list is not a grant.
    tasks = {row.get('task') for row in offering.get('tasks') or [] if isinstance(row, Mapping)}
    aliases = {'images': {'image', 'image.generate'}, 'speech': {'speech', 'speech.synthesis'},
               'embeddings': {'embeddings', 'text.embeddings'}, 'chat': {'chat', 'chat.generate'}}
    if not tasks.intersection(aliases.get(op, {op})):
        raise UnknownOperation('The offering does not declare this task.')
    return Target(str(engine_id), service, offering, doc)


class Runtime:
    def __init__(self, target: Target, *, assets: AssetStore | None = None, transport=None):
        self.target = target
        connection = connection_config(target.service)
        # The origin check and secret lookup are the same as the native lane.
        # Resolve only after dispatch is selected, never while listing records.
        credentials = {}
        for cls, spec in (connection.get('credentials') or {}).items():
            if not spec or not spec.get('secret_ref'):
                continue
            values = credential_headers(target.service, cls)
            value = next(iter(values.values()))
            scheme = spec.get('scheme') or ''
            if scheme:
                value = value[len(scheme)+1:]
            credentials[cls] = Credential(value, header=spec.get('header'), scheme=spec.get('scheme'))
        self.connection = replace(connection_from_record({**connection, 'credentials': {}}, lambda ref: None),
                                  credentials=credentials)
        self.adapter = AdapterRegistry({target.description.id: target.description}, assets=assets,
                                       transport=transport).adapter_for(target.description.id)

    def request(self, op: str, body: Mapping[str, Any]) -> CanonicalRequest:
        return CanonicalRequest(op=op, body={**body, 'model': self.target.upstream},
                                params={'model': self.target.upstream, 'offering': self.target.offering['id']})

    async def invoke(self, op: str, body: Mapping[str, Any]):
        return await self.adapter.invoke(self.connection, op, self.request(op, body))

    async def lease(self, *, drain, lock_for, may_switch: bool = True):
        selection = self.target.offering.get('selection') or {}
        if selection.get('mode', 'request_field') != 'switch':
            return None
        service = {**self.target.service, 'id':self.target.engine_id,
                   'offerings':offerings_config(self.target.service),
                   'observations':observations_config(self.target.service)}

        async def invoke(name, args):
            op = self.target.description.operations.get(name)
            if op is None or op.op_class != 'control' or op.job is not None:
                raise ValueError('selection needs a declared control operation')
            # Retain W2's owner-root checks for host file arguments.
            checker = EngineControlRuntime(service, drain=drain, lock_for=lock_for)
            checked, identities = checker._check_paths(args, frozenset())
            checker._recheck_paths(identities)
            reply = await self.adapter.invoke(self.connection, name, CanonicalRequest(name, body=checked))
            if isinstance(reply, StreamReply):
                await reply.aclose()
                raise ValueError('selection control cannot be streamed')

        async def observe(name):
            spec = service['observations'].get(name) or {}
            op = spec.get('op')
            adapter, connection = self.adapter, self.connection
            if 'http' in spec:
                http = spec['http']
                selector = http.get('select', spec.get('select'))
                if selector is None:
                    return UNKNOWN
                # Compile the documented HTTP observation into the same bounded
                # grammar. No second HTTP client or credential path is needed.
                observation = description.parse_description({
                    'id': 'observation', 'protocol_version': 1, 'executor': 'http-v1',
                    'auth': {cls: {'kind': auth.kind, **({'name': auth.name} if auth.name else {}),
                                   **({'scheme': auth.scheme} if auth.scheme is not None else {})}
                             for cls, auth in self.target.description.auth.items()},
                    'operations': {'observe': {
                        'class': 'inventory' if spec.get('credential_class') == 'inference' else 'control',
                        'request': {'method': 'GET', 'path': http['path']},
                        'response': {'framing': 'json', 'select': {'value': selector}}}},
                }, source='engine observation', budgets=self.target.description.budgets)
                adapter = AdapterRegistry({'observation': observation}).adapter_for('observation')
                seconds = max(0.01, min(float(http.get('timeout_s', 2)), 30))
                connection = replace(connection, timeouts=replace(connection.timeouts, total_s=seconds,
                                       first_byte_s=min(seconds, connection.timeouts.first_byte_s)))
                op = 'observe'
            elif not isinstance(op, str) or op not in self.target.description.operations:
                return UNKNOWN
            try:
                reply = await adapter.invoke(connection, op, CanonicalRequest(op))
                if isinstance(reply, StreamReply):
                    await reply.aclose()
                    return UNKNOWN
                if reply.status != (spec.get('http') or {}).get('expect', 200):
                    return UNKNOWN
                value = reply.selected.get('value')
                return Observation(value is not None, value)
            except ProtocolError:
                return UNKNOWN

        return await SwitchController(drain=drain, invoke=invoke, observe=observe, lock_for=lock_for).select(
            service, str(self.target.offering['id']), selection, may_switch=may_switch)
