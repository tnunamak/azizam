"""Exercise legacy wire fixtures through the common compiled route dispatcher."""
from dataclasses import replace
from types import SimpleNamespace

from gateway import model_routes, registry_snapshot
from gateway.alias_policy import resolve_request_target
from tests.support.fake_upstream.route_dispatch import request_for


async def execute(service, kind, slug, *, surface, body, identity, access, app_profile=None,
                  request_path='/v1/responses', request_id=None):
    sid = kind.model_prefix
    connection_id = service.store.connection_id(kind.id)
    services = {sid: {'id': sid, 'enabled': True, 'config': {
        'credential_source': {'kind': 'connection', 'connection_id': connection_id},
        'request_protocol': kind.protocol, 'billing_mode': kind.billing_mode, 'models': []}}}
    snapshot = registry_snapshot.build({'services': services}, registry_snapshot.BundledRelease({}), generation=1,
        connection_models={connection_id: {'models': {slug: []}}})
    target = resolve_request_target(body['model'], runtime_config={
        'backends': {sid: snapshot.services[sid]['config']}}, route_index=snapshot.model_routes)
    current = replace(identity, access=access)
    async def fresh():
        return current
    async def admit(edge, receipt):
        return None
    async def prepare(edge, original):
        return original
    return await model_routes.dispatch_routes(snapshot=snapshot, target=target,
        request=request_for(body, surface=surface), body=body, surface=surface, identity=current,
        fresh_identity=fresh, admission=admit, prepare_body=prepare, subscriptions=service,
        runtime=SimpleNamespace(enabled=False), ledger_path=None)
