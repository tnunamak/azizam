"""Resolve explicit catalog choices into one existing registry transaction."""
from collections.abc import Mapping
from uuid import uuid4

from gateway.model_routes import compile_model_routes, model_records, route_step, RouteEdge


def _copy(value):
    if isinstance(value, Mapping):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy(item) for item in value]
    return value


def records(services):
    return {(sid, row["id"]): row for sid, service in services.items()
            for row in model_records(service.get("config") or {}) if row.get("id") and not row.get("base_model_id")}


def draft(document, services, anchor, targets, *, discoveries=None):
    """Copy selected execution facts only; never alter another owner's route group."""
    catalog = records(services)
    stored = records(document.get("services") or {})
    if anchor not in catalog or not targets or len(targets) > 32 or len(set(targets)) != len(targets):
        raise ValueError("Choose a model and distinct routes from the current catalog.")
    if catalog[anchor].get("execution_only"):
        raise ValueError("Choose a public model as the route group.")
    existing = {(edge["service_id"], edge["model_id"]): edge["id"] for edge in catalog[anchor].get("routes", [])}
    candidate = _copy(document)
    for pair in [anchor, *targets]:
        row = catalog.get(pair)
        if row is None:
            raise ValueError("A selected route is no longer available. Reload the catalog.")
        routes = row.get("routes") or []
        if pair != anchor and routes and not (len(routes) == 1 and (routes[0]["service_id"], routes[0]["model_id"]) == pair):
            raise ValueError("A selected model owns another route group. Choose its leaf target.")
        sid, mid = pair
        if sid not in candidate["services"] or not services[sid].get("enabled", True):
            raise ValueError("A selected route is unavailable. Reload the catalog.")
        if pair in stored:
            continue
        config = candidate["services"][sid].setdefault("config", {})
        rows = _copy(model_records(config))
        promoted = _copy(row)
        source = (services[sid].get("config") or {}).get("credential_source") or {}
        if source.get("kind") in {"connection", "cli-session"}:
            if discoveries is not None:
                discovery = discoveries.get(source.get("connection_id")) or {}
                if discovery.get("status") != "connected" or mid not in (discovery.get("models") or {}):
                    raise ValueError("A selected discovery route is unavailable. Reload the catalog.")
            promoted["discovery_mapping"] = {"connection_id": source["connection_id"], "model_id": mid,
                                             "reasoning_efforts": _copy(row.get("reasoning_efforts") or [])}
        promoted.pop("routes", None)
        if pair != anchor:
            promoted["execution_only"] = True
        rows.append(promoted)
        config["models"] = rows
        stored[pair] = promoted
    sid, mid = anchor
    config = candidate["services"][sid]["config"]
    config["models"] = _copy(model_records(config))
    base = next(row for row in config["models"] if row["id"] == mid)
    base["routes"] = [{"id": existing.get(pair) or f"route/{uuid4().hex}", "service_id": pair[0], "model_id": pair[1]}
                      for pair in targets]
    # Validate references against promoted records before any durable write.
    compile_model_routes(candidate["services"])
    return candidate


def view(services, anchor, generation, *, discoveries=None):
    catalog = records(services)
    if anchor not in catalog:
        raise ValueError("Model not found in the current catalog.")
    index = compile_model_routes(services)
    edges = index.by_model[anchor]
    def choice(pair, pin=None):
        sid, mid = pair
        row = catalog[pair]
        service = services[sid]
        available = bool(service.get("enabled", True))
        source = row.get("discovery_mapping") or (service.get("config") or {}).get("credential_source") or {}
        if discoveries is not None and source.get("connection_id"):
            discovery = discoveries.get(source["connection_id"]) or {}
            offered = (discovery.get("models") or {}).get(source.get("model_id", mid))
            available = available and discovery.get("status") == "connected" and offered is not None
            if row.get("discovery_mapping"):
                available = available and list(offered or []) == list(source.get("reasoning_efforts") or [])
        step = route_step(services, RouteEdge(pin or "choice", sid, mid))
        return {"service_id": sid, "model_id": mid, "name": str(service.get("display_name") or service.get("name") or sid),
                "upstream_model": str(row.get("upstream_model") or mid), "billing_class": step.kind,
                "available": available, **({"route_id": pin} if pin else {})}
    targets = []
    for pair, row in catalog.items():
        routes = row.get("routes") or []
        if pair == anchor or not routes or (len(routes) == 1 and (routes[0]["service_id"], routes[0]["model_id"]) == pair):
            targets.append(choice(pair))
    return {"generation": generation, "service_id": anchor[0], "model_id": anchor[1],
            "routes": [choice(edge.target, edge.id) for edge in edges], "targets": targets}
