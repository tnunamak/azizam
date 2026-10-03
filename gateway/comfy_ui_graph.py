"""Derive a ComfyUI frontend (UI / "workflow") graph from an API-format graph.

Pure module: no network, no proxy imports. The caller supplies a lookup for
``/object_info`` entries. Best effort: returns ``None`` when the graph cannot be
described faithfully (unknown node class, dangling link, cycle), so the caller
can omit the ``workflow`` PNG chunk instead of embedding a broken graph.

Slot semantics mirror the ComfyUI frontend (schema version 0.4):

* A widget is a combo (list or ``COMBO``) or an INT/FLOAT/STRING/BOOLEAN input
  without ``forceInput``. Everything else is a connection slot.
* ``widgets_values`` holds one value per widget in object_info order. A widget
  with ``control_after_generate`` is followed by the string ``"fixed"``; an
  ``image_upload`` / ``video_upload`` / ``audio_upload`` widget is followed by
  its upload-button value.
* ``inputs`` lists connection slots first, then widget-backed slots (each with
  ``"widget": {"name": ...}``). A widget whose API value is a link is wired
  through its widget-backed slot ("converted to input").
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Mapping, Optional

WIDGET_TYPES = frozenset({"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "COLORCODE"})
_UPLOAD_SUFFIX = {"image_upload": "image", "video_upload": "video", "audio_upload": "audio"}
_NODE_WIDTH = 315
_COL_GAP = 80
_ROW_GAP = 40
_ORIGIN = (40, 40)
_ID_NAMESPACE = uuid.UUID("6f1c2f9e-6b0a-5c3e-9a57-0d1e5a7a8b11")


def _is_link(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    )


def _parse_spec(spec: Any) -> tuple[Any, dict]:
    """Return (type_or_options, config) from an object_info input spec."""
    if isinstance(spec, (list, tuple)) and spec:
        cfg = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
        return spec[0], cfg
    return spec, {}


def _ordered_inputs(info: Mapping) -> list[tuple[str, Any, dict, bool]]:
    """(name, type_or_options, config, required) in frontend order."""
    inputs = info.get("input") or {}
    order = info.get("input_order")
    sections: list[tuple[str, list[str]]] = []
    for sec in ("required", "optional"):
        declared = inputs.get(sec) or {}
        names = list(declared)
        if isinstance(order, Mapping) and isinstance(order.get(sec), list):
            names = [n for n in order[sec] if n in declared]
            names += [n for n in declared if n not in names]
        sections.append((sec, names))
    out = []
    for sec, names in sections:
        for name in names:
            typ, cfg = _parse_spec((inputs.get(sec) or {})[name])
            out.append((name, typ, cfg, sec == "required"))
    return out


def _combo_options(typ: Any, cfg: dict) -> list:
    if isinstance(typ, (list, tuple)):
        return list(typ)
    opts = cfg.get("options")
    return list(opts) if isinstance(opts, (list, tuple)) else []


def _is_widget(typ: Any, cfg: dict) -> bool:
    if cfg.get("forceInput"):
        return False
    if isinstance(typ, str) and "," in typ:
        # Multi-type input such as "FLOAT,INT": a widget only when the node names its widget type.
        return cfg.get("widgetType") in WIDGET_TYPES
    return isinstance(typ, (list, tuple)) or (isinstance(typ, str) and typ in WIDGET_TYPES)


def _default_value(typ: Any, cfg: dict) -> Any:
    if "default" in cfg:
        return cfg["default"]
    if isinstance(typ, (list, tuple)) or typ == "COMBO":
        opts = _combo_options(typ, cfg)
        return opts[0] if opts else ""
    return {"INT": 0, "FLOAT": 0.0, "STRING": "", "BOOLEAN": False, "COLORCODE": "#000000"}.get(typ, 0)


def _spec_entries(section: Any) -> list[tuple[str, Any, dict, bool]]:
    out = []
    for name, spec in (section or {}).items():
        typ, cfg = _parse_spec(spec)
        out.append((name, typ, cfg, True))
    return out


def _plan_inputs(entries: list, api_inputs: Mapping, prefix: str, plan: dict) -> None:
    """Append connection slots, widget slots and widget values for ``entries``.

    ``prefix`` is the dotted parent name for nested (V3 dynamic) inputs, which
    ComfyUI's API format flattens as ``parent.child``.
    """
    conns, widgets, values = plan["conns"], plan["widgets"], plan["values"]
    for name, typ, cfg, required in entries:
        full = prefix + name
        raw = api_inputs.get(full)
        linked = _is_link(raw)
        if typ == "COMFY_DYNAMICCOMBO_V3":
            options = cfg.get("options") or []
            keys = [o.get("key") for o in options if isinstance(o, Mapping)]
            value = raw if full in api_inputs and not linked else (keys[0] if keys else "")
            widgets.append({"name": full, "type": "COMBO", "link_src": raw if linked else None,
                            "multiline": False})
            values.append(value)
            chosen = next((o for o in options if isinstance(o, Mapping) and o.get("key") == value), None)
            if chosen:
                sub = chosen.get("inputs") or {}
                nested = _spec_entries(sub.get("required")) + [
                    (n, t, c, False) for n, t, c, _ in _spec_entries(sub.get("optional"))]
                _plan_inputs(nested, api_inputs, full + ".", plan)
        elif typ == "COMFY_AUTOGROW_V3":
            template = cfg.get("template") or {}
            tmpl_in = _spec_entries((template.get("input") or {}).get("required")) + _spec_entries(
                (template.get("input") or {}).get("optional"))
            names = list(template.get("names") or [])
            grown = [k for k in api_inputs if k.startswith(full + ".")]
            grown.sort(key=lambda k: (names.index(k[len(full) + 1:]) if k[len(full) + 1:] in names else len(names), k))
            for k in grown:
                if _is_link(api_inputs[k]):
                    conns.append({"name": k, "type": str(tmpl_in[0][1]) if tmpl_in else "*",
                                  "link_src": api_inputs[k], "shape": 7})
        elif typ == "COMFY_MATCHTYPE_V3":
            if linked or required:
                conns.append({"name": full, "type": "*", "link_src": raw if linked else None,
                              "shape": None if required else 7, "match": True})
        elif _is_widget(typ, cfg):
            wtype = "COMBO" if isinstance(typ, (list, tuple)) else (
                cfg["widgetType"] if "," in typ else typ)
            value = _default_value(typ if not isinstance(typ, str) or "," not in typ else wtype, cfg) if (linked or full not in api_inputs) else raw
            widgets.append({"name": full, "type": wtype, "link_src": raw if linked else None,
                            "multiline": bool(cfg.get("multiline"))})
            values.append(value)
            if cfg.get("control_after_generate"):
                values.append("fixed")
            for flag, button in _UPLOAD_SUFFIX.items():
                if cfg.get(flag):
                    values.append(button)
                    widgets.append({"name": "upload", "type": flag.split("_")[0].upper() + "UPLOAD",
                                    "link_src": None, "multiline": False, "no_value": True})
        elif linked or required:
            conns.append({"name": full, "type": str(typ), "link_src": raw if linked else None,
                          "shape": None if required else 7})


def _outputs_of(info: Mapping) -> list[tuple[str, str]]:
    types = list(info.get("output") or [])
    names = list(info.get("output_name") or [])
    out = []
    for i, t in enumerate(types):
        t = "COMBO" if isinstance(t, (list, tuple)) else str(t)
        n = names[i] if i < len(names) and names[i] else t
        out.append((str(n), t))
    return out


def _graph_id(nodes: Mapping[str, Mapping]) -> str:
    canon = json.dumps(nodes, sort_keys=True, separators=(",", ":"), default=str)
    return str(uuid.uuid5(_ID_NAMESPACE, canon))


def _frontend_ids(api: Mapping[str, Any]) -> dict[str, int]:
    """Map API node ids to the integer ids the frontend needs.

    Numeric ids keep their value, so links and titles match the API graph.
    Catalog templates also use readable ids such as ``s0_text``; those get the
    next free integers in sorted order, which keeps the result deterministic.
    """
    ids: dict[str, int] = {}
    for key in api:
        try:
            ids[key] = int(key)
        except ValueError:
            pass
    taken = set(ids.values())
    nxt = max(taken, default=0)
    for key in sorted(k for k in api if k not in ids):
        nxt += 1
        ids[key] = nxt
    return ids


def derive_ui_workflow(
    api_graph: Mapping[str, Any],
    object_info_for: Callable[[str], Optional[Mapping]],
) -> Optional[dict]:
    """Return a version-0.4 UI workflow dict, or None if it cannot be derived."""
    api: dict[str, Mapping] = {}
    for key, node in api_graph.items():
        if isinstance(key, str) and key.startswith("_"):
            continue
        if not isinstance(node, Mapping) or "class_type" not in node:
            continue
        api[str(key)] = node
    if not api:
        return None
    uid = _frontend_ids(api)

    infos: dict[str, Mapping] = {}
    for key, node in api.items():
        info = object_info_for(node["class_type"])
        if not info:
            return None
        infos[key] = info

    # Per node: slot plan.
    plans: dict[str, dict] = {}
    for key, node in api.items():
        plan: dict[str, list] = {"conns": [], "widgets": [], "values": []}
        _plan_inputs(_ordered_inputs(infos[key]), node.get("inputs") or {}, "", plan)
        plan["outputs"] = _outputs_of(infos[key])
        plans[key] = plan

    # Edges / validation.
    preds: dict[str, set[str]] = {k: set() for k in api}
    for key, plan in plans.items():
        for slot in plan["conns"] + plan["widgets"]:
            src = slot["link_src"]
            if src is None:
                continue
            from_id, from_slot = str(src[0]), src[1]
            if from_id not in api or not 0 <= from_slot < len(plans[from_id]["outputs"]):
                return None
            preds[key].add(from_id)

    # Topological order + longest-path depth (deterministic: numeric id tie-break).
    depth: dict[str, int] = {}
    order: list[str] = []
    remaining = set(api)
    while remaining:
        ready = sorted((k for k in remaining if preds[k] <= set(order)), key=uid.__getitem__)
        if not ready:
            return None  # cycle
        for k in ready:
            depth[k] = 1 + max((depth[p] for p in preds[k]), default=-1)
            order.append(k)
        remaining -= set(ready)
    # `order` is layer-ish; make it a true topological order sorted by (depth, id).
    order.sort(key=lambda k: (depth[k], uid[k]))
    position = {k: i for i, k in enumerate(order)}

    # Nodes with empty link lists.
    ui_nodes: dict[str, dict] = {}
    for key in order:
        node, plan = api[key], plans[key]
        ins = []
        match_type = "*"
        for c in plan["conns"]:
            ctype = c["type"]
            if c.get("match"):
                src = c["link_src"]
                if src is not None:
                    ctype = ui_nodes[str(src[0])]["outputs"][src[1]]["type"]
                match_type = ctype
            slot = {"name": c["name"], "localized_name": c["name"], "type": ctype, "link": None}
            if c["shape"]:
                slot["shape"] = c["shape"]
            ins.append(slot)
        for w in plan["widgets"]:
            ins.append({"name": w["name"], "localized_name": w["name"], "type": w["type"],
                        "widget": {"name": w["name"]}, "link": None})
        outs = [{"name": n, "localized_name": n,
                 "type": match_type if t == "COMFY_MATCHTYPE_V3" else t,
                 "links": None, "slot_index": i}
                for i, (n, t) in enumerate(plan["outputs"])]
        n_widgets = len(plan["widgets"])
        extra = 100 if any(w["multiline"] for w in plan["widgets"]) else 0
        height = max(80, 26 * max(len(ins), len(outs)) + 30 + extra)
        ui = {"id": uid[key], "type": node["class_type"], "pos": [0, 0],
              "size": [_NODE_WIDTH, height], "flags": {}, "order": position[key], "mode": 0,
              "inputs": ins, "outputs": outs,
              "properties": {"Node name for S&R": node["class_type"]},
              "widgets_values": plan["values"]}
        if str(uid[key]) != key:
            ui["properties"]["api_node_id"] = key
            ui.setdefault("title", key)
        title = (node.get("_meta") or {}).get("title") if isinstance(node.get("_meta"), Mapping) else None
        if title:
            ui["title"] = title
        ui_nodes[key] = ui

    # Layout: column by depth, stacked vertically.
    cols: dict[int, list[str]] = {}
    for k in order:
        cols.setdefault(depth[k], []).append(k)
    for d, keys in cols.items():
        y = _ORIGIN[1]
        x = _ORIGIN[0] + d * (_NODE_WIDTH + _COL_GAP)
        for k in keys:
            ui_nodes[k]["pos"] = [x, y]
            y += ui_nodes[k]["size"][1] + _ROW_GAP

    # Links, in (target position, input slot) order.
    links: list[list] = []
    for key in order:
        tnode = ui_nodes[key]
        plan = plans[key]
        slots = plan["conns"] + plan["widgets"]
        for idx, slot in enumerate(slots):
            src = slot["link_src"]
            if src is None:
                continue
            from_id, from_slot = str(src[0]), src[1]
            lid = len(links) + 1
            fnode = ui_nodes[from_id]
            ltype = fnode["outputs"][from_slot]["type"]
            links.append([lid, uid[from_id], from_slot, uid[key], idx, ltype])
            tnode["inputs"][idx]["link"] = lid
            out = fnode["outputs"][from_slot]
            out["links"] = (out["links"] or []) + [lid]

    return {
        "id": _graph_id({k: {"class_type": v["class_type"], "inputs": v.get("inputs"),
                             "_meta": v.get("_meta")} for k, v in sorted(api.items(), key=lambda kv: uid[kv[0]])}),
        "revision": 0,
        "last_node_id": max(uid.values()),
        "last_link_id": len(links),
        "nodes": [ui_nodes[k] for k in sorted(api, key=uid.__getitem__)],
        "links": links,
        "groups": [],
        "config": {},
        "extra": {},
        "version": 0.4,
    }


def validate_ui_workflow(api_graph: Mapping[str, Any], ui: Mapping[str, Any]) -> list[str]:
    """Compare a derived UI workflow with the API graph it came from.

    Returns human-readable problems; an empty list means every API node, every
    link and every literal widget value is present in ``ui``. Pure, so tests and
    the offline audit can share it.
    """
    api = {str(k): v for k, v in api_graph.items()
           if isinstance(v, Mapping) and "class_type" in v and not str(k).startswith("_")}
    uid = _frontend_ids(api)
    problems: list[str] = []
    nodes = {n["id"]: n for n in ui.get("nodes", [])}
    for key, node in api.items():
        ui_node = nodes.get(uid[key])
        if ui_node is None:
            problems.append(f"node {key} ({node['class_type']}) missing from UI graph")
            continue
        if ui_node["type"] != node["class_type"]:
            problems.append(f"node {key}: type {ui_node['type']} != {node['class_type']}")
        slot_names = [i["name"] for i in ui_node.get("inputs", [])]
        values = ui_node.get("widgets_values", [])
        for name, raw in (node.get("inputs") or {}).items():
            if name not in slot_names:
                problems.append(f"node {key} ({node['class_type']}): input {name!r} not in UI graph")
            elif _is_link(raw):
                idx = slot_names.index(name)
                want = [uid[str(raw[0])] if str(raw[0]) in uid else None, raw[1], uid[key], idx]
                if not any(l[1:5] == want for l in ui.get("links", [])):
                    problems.append(f"node {key}: link into {name!r} from {raw[0]}:{raw[1]} missing")
            elif raw not in values:
                problems.append(f"node {key} ({node['class_type']}): value of {name!r} missing from widgets_values")
    n_api_links = sum(1 for n in api.values() for v in (n.get("inputs") or {}).values() if _is_link(v))
    if len(ui.get("links", [])) != n_api_links:
        problems.append(f"link count {len(ui.get('links', []))} != API link count {n_api_links}")
    return problems
