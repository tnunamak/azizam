"""The release defaults are neutral (docs/design/CONFIG_HOMES.md, 10.2 item 3).

They name no host path of one machine, no address of one network, and no service, workflow or model that this
release does not ship. The files are parsed as data and each address value and each reference is judged on its own,
so a line that holds one allowed address and one deployment address fails.
"""
from __future__ import annotations

import copy
import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / "registry" / "defaults"

HOST_PATH = re.compile(r"(?<![\w.])/(?:media|mnt|home|Users)/[\w.-]+")

# Allowed anywhere, as a host or a network: loopback, the unspecified address, the documentation ranges.
ALLOWED_ANYWHERE = [ipaddress.ip_network(net) for net in (
    "127.0.0.0/8", "0.0.0.0/32", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
    "::1/128", "::/128", "2001:db8::/32",
)]
# Allowed only as a whole token that is exactly one of these canonical networks (a rule such as "trust the private
# ranges"), never as a host in one and never as a host address with a prefix length.
GENERIC_PRIVATE_CIDRS = [ipaddress.ip_network(net) for net in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7", "fe80::/10",
)]


def _allowed(network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    return any(network.version == allowed.version and network.subnet_of(allowed) for allowed in ALLOWED_ANYWHERE)


def host_problem(host: str) -> str | None:
    """Why this host is not neutral, or None. A name that is not an address is not judged here."""
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped  # ::ffff:a.b.c.d is the IPv4 host a.b.c.d
    if _allowed(ipaddress.ip_network(address)):
        return None
    if address.is_private or address.is_link_local:
        return f"{host} is a private address of one network; only a generic private CIDR (as a whole CIDR token) may stand"
    return f"{host} is a public address"


def _network_problem(token: str) -> str | None:
    """Judge a whole ``address/prefix`` token. Only a canonical network may stand: ``10.1.2.3/8`` is a host address."""
    try:
        network = ipaddress.ip_network(token, strict=True)
    except ValueError:
        return host_problem(token.split("/", 1)[0]) or f"{token} is not a canonical network (it has host bits set)"
    if token != str(network):
        return f"{token} is not the canonical form of {network}"
    if _allowed(network) or network in GENERIC_PRIVATE_CIDRS:
        return None
    if network.is_private or network.is_link_local:
        return f"{token} is a private network of one network; only a generic private CIDR may stand"
    return f"{token} is a public network"


# A URL is `scheme://authority`, then a path, query or fragment that runs to white space or a quote. The authority stops at
# closing punctuation (`)`, `,`, `;`), and the path stops where another `scheme://` starts, so "(http://h)" and
# "http://a,http://b" are judged by their hosts. Only the host is judged; no address literal is read inside the path.
_URL = re.compile(r"""(?i)[a-z][a-z0-9+.-]*://([^/?#\s"'<>(),;]*)(?:[/?#](?:(?![a-z][a-z0-9+.-]*://)[^\s"'<>])*)?""")
# Outside URLs, an address literal is one of three shapes. They start with different characters (`[`, a digit then a dot,
# a hex digit or `:` then a colon), so at any position at most one of them can match and it takes all it can:
# `[::ffff:127.0.0.1]:80` and `::ffff:127.0.0.1` are one literal each.
_LITERAL = re.compile(
    r"\[[0-9A-Fa-f:.%\w]*:[0-9A-Fa-f:.%\w]*\](?::\d{1,5})?"                         # bracketed IPv6, optional port
    # IPv4, optional /prefix, optional :port; "1.2.3.4-style" and "build-1.2.3.4" are identifiers, not addresses
    r"|(?<![\w.])(?<![A-Za-z]-)\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,3})?(?::\d{1,5})?(?![\w])(?!\.\d)(?!-[A-Za-z])"
    r"|(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f.]*(?:/\d{1,3})?"                # bare IPv6, optional /prefix
)
_V4_PORT = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}):\d{1,5}")
_BRACKETED = re.compile(r"(\[[^\]]*\])(?::\d{1,5})?")


def _authority_host(authority: str) -> str:
    host = authority.rpartition("@")[2]
    return host[: host.index("]") + 1] if host.startswith("[") and "]" in host else host.split(":", 1)[0]


def _literal_problem(literal: str) -> str | None:
    """Judge one whole address literal: a host (with or without a port) or an ``address/prefix`` network."""
    bracketed = _BRACKETED.fullmatch(literal)
    if bracketed:
        return host_problem(bracketed.group(1))
    if "/" in literal:
        try:
            ipaddress.ip_address(literal.split("/", 1)[0])
        except ValueError:
            return None
        return _network_problem(literal)
    with_port = _V4_PORT.fullmatch(literal)
    return host_problem(with_port.group(1) if with_port else literal)


def address_problems(text: str) -> list[str]:
    """Every non-neutral address in a string, found by scanning the whole text (never by splitting it). A URL is judged by
    its host only; every address literal outside the URLs is judged whole."""
    problems: list[str] = []
    outside: list[str] = []
    position = 0
    for url in _URL.finditer(text):
        outside.append(text[position:url.start()])
        position = url.end()
        problems.append(host_problem(_authority_host(url.group(1))))
    outside.append(text[position:])
    for chunk in outside:
        problems += [_literal_problem(literal.group(0)) for literal in _LITERAL.finditer(chunk)]
    return [problem for problem in problems if problem]


# Keys whose value names a thing the release must ship (used to keep the reference fields below complete).
SERVICE_KEY = re.compile(r"^(?:backend|service|service_id|engine|[a-z_]+_(?:service|service_id|engine|backend))$")
WORKFLOW_KEY = re.compile(r"^(?:workflow|[a-z_]+_workflow)$")
MODEL_KEY = re.compile(r"^(?:model|model_id|default_model|[a-z_]+_model)$")


def _stable_names(document: Any) -> set[str]:
    """The stable names of a model_aliases document: the keys of each operation's ``stable`` table."""
    return {str(name) for operation in (document or {}).values() if isinstance(operation, dict)
            for name in (operation.get("stable") or {})}


def _engine_ids(document: Any) -> set[str]:
    return {str(name) for operation in (document or {}).values() if isinstance(operation, dict)
            for name in (operation.get("engines") or {})}


def shipped_ids() -> dict[str, set[str]]:
    """What this release ships: service ids (registry), workflow ids (graph files and manifests), model names.

    A model name is a workflow id or a stable name (a key of ``<operation>.stable``); a container name such as
    ``stable`` or ``engines`` is a field of the section and is never an id."""
    services = {record["id"] for record in yaml.safe_load((ROOT / "registry" / "services.yml").read_text(encoding="utf-8"))["records"]}
    for item in json.loads((ROOT / "registry" / "engine_definitions.json").read_text(encoding="utf-8"))["definitions"]:
        services.add(str(item["definition"]))
        services.add(str(item.get("record", {}).get("id", "")))
    workflows = {path.name.removesuffix(".json") for path in (ROOT / "workflows").rglob("*.json") if not path.name.endswith(".ui.json")}
    for path in (ROOT / "manifests" / "workflows").glob("*.yml"):
        manifest_id = str(yaml.safe_load(path.read_text(encoding="utf-8")).get("id", ""))
        workflows.update({manifest_id, manifest_id.rsplit(".", 1)[-1]})
    models = set(workflows) | _stable_names(yaml.safe_load((DEFAULTS / "model_aliases.yml").read_text(encoding="utf-8")))
    return {"service": services - {""}, "workflow": workflows - {""}, "model": models - {""}}


# --- where each section keeps an id: data, one table, read by the checker and by the coverage tests -------------------
# A path is a tuple of steps from the document root: a name is a key; ``*`` is every key of a mapping; ``name[]`` is every
# element of the list under ``name``. The ids are the values at the end of the path (a string, or a list of strings), or,
# with ``keys=True``, the keys of the mapping the path ends at.

@dataclass(frozen=True)
class Ref:
    kind: str
    path: tuple[str, ...]
    keys: bool = False


@dataclass(frozen=True)
class Section:
    refs: tuple[Ref, ...] = ()
    not_references: dict[tuple[str, ...], str] = field(default_factory=dict)  # reference-looking schema path -> why it is none
    open_paths: dict[tuple[str, ...], str] = field(default_factory=dict)  # declared path the schema leaves open -> why


_AUDIO_TABLE = Section(refs=(
    Ref("workflow", ("default_workflow",)), Ref("workflow", ("workflows",), keys=True), Ref("workflow", ("aliases", "*")),
))
_VIDEO_TABLE = Section(refs=(
    *_AUDIO_TABLE.refs, Ref("workflow", ("extend_workflow",)), Ref("workflow", ("remix_workflow",)),
    Ref("workflow", ("workflows", "*", "i2v_workflow")), Ref("workflow", ("workflows", "*", "chain_workflow")),
))

SECTIONS: dict[str, Section] = {
    "audio": _AUDIO_TABLE,
    "videos": _VIDEO_TABLE,
    "images": Section(refs=(Ref("workflow", ("variations", "default_workflow")), Ref("workflow", ("edits", "default_workflow")),
                             Ref("workflow", ("edits", "multiref_default_workflow")))),
    "voices": Section(refs=(Ref("service", ("quality_engine",)), Ref("service", ("design_engine",)))),
    "models": Section(refs=(Ref("model", (), keys=True), Ref("service", ("*", "backend")))),
    "roles": Section(refs=(Ref("service", ("*", "backend")), Ref("model", ("*", "model")))),
    "service_availability": Section(refs=(Ref("service", (), keys=True),)),
    "model_compatibility": Section(refs=(Ref("model", ("defaults", "*")),)),
    # per operation: a stable target names a workflow, model, service, stable name or engine; an engine holds a service
    # and a model; an openai pattern points at a stable name
    "model_aliases": Section(refs=(
        Ref("alias_target", ("*", "stable", "*")), Ref("service", ("*", "engines"), keys=True),
        Ref("service", ("*", "engines", "*", "service")), Ref("model", ("*", "engines", "*", "model")),
        Ref("stable_name", ("*", "openai[]", "to")),
    ), open_paths={
        ("*", "engines", "*", "service"): "an engine is an open object in the schema; the data holds a service id here",
        ("*", "engines", "*", "model"): "an engine is an open object in the schema; the data holds a model id here",
        ("*", "openai[]", "to"): "a pattern is an open object in the schema; the data holds a stable name here",
    }),
    # no id of a service, workflow or model lives in these sections
    "identity": Section(not_references={("face_service",): "the CPU face sidecar's own config object, not a registry service id"}),
    "instance": Section(), "model_routing": Section(), "reasoning": Section(),
    "llm": Section(not_references={("pin_loaded_model",): "a boolean"}),
    "resource_admission": Section(), "workflow_execution": Section(),
}


def _walk(node: Any, path: tuple[str, ...], where: str) -> Iterator[tuple[Any, str]]:
    if not path:
        yield node, where
        return
    step, rest = path[0], path[1:]
    if not isinstance(node, dict):
        return
    if step == "*":
        for key, child in node.items():
            yield from _walk(child, rest, f"{where}/{key}")
    elif step.endswith("[]"):
        items = node.get(step[:-2])
        for index, child in enumerate(items if isinstance(items, list) else []):
            yield from _walk(child, rest, f"{where}/{step[:-2]}/{index}")
    elif step in node:
        yield from _walk(node[step], rest, f"{where}/{step}")


def references(name: str, document: Any) -> Iterator[tuple[str, str, str]]:
    """(kind, id, where) for every reference to a service, workflow or model in the data document ``name``."""
    stem = name.split(".")[0]
    section = SECTIONS.get(stem)
    if section is None:
        yield "unmapped", stem, f"no reference shape is declared for {name}"
        return
    for ref in section.refs:
        for target, where in _walk(document or {}, ref.path, ""):
            if ref.keys:
                for key in target if isinstance(target, dict) else ():
                    yield ref.kind, str(key), f"{where}/{key} (key)"
            else:
                for item in [target] if isinstance(target, str) else target if isinstance(target, list) else []:
                    if isinstance(item, str) and item.strip():
                        yield ref.kind, item.strip(), where


def _resolve(node: Any, root: Any) -> Any:
    """Follow a local ``$ref`` (``#/$defs/x``) to its target."""
    seen = 0
    while isinstance(node, dict) and isinstance(node.get("$ref"), str) and node["$ref"].startswith("#/") and seen < 20:
        target: Any = root
        for part in node["$ref"][2:].split("/"):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        node, seen = target, seen + 1
    return node


def schema_paths(schema: Any) -> tuple[set[tuple[str, ...]], set[tuple[str, ...]]]:
    """(every path, the paths named by a ``properties`` key) of a JSON schema, in the step language of ``Ref``: a property
    name is its name; ``additionalProperties`` and ``patternProperties`` are ``*``; the items of an array under a property
    are ``name[]``. A local ``$ref`` is followed; ``oneOf``/``anyOf``/``allOf`` branches add to the same path."""
    every: set[tuple[str, ...]] = set()
    named: set[tuple[str, ...]] = set()

    def visit(node: Any, path: tuple[str, ...], active: frozenset) -> None:
        node = _resolve(node, schema)
        if not isinstance(node, dict) or (id(node), path) in active:
            return
        active = active | {(id(node), path)}
        every.add(path)
        for keyword in ("oneOf", "anyOf", "allOf"):
            for branch in node.get(keyword) or []:
                visit(branch, path, active)
        for name, child in (node.get("properties") or {}).items():
            named.add((*path, str(name)))
            visit(child, (*path, str(name)), active)
        for keyword in ("patternProperties",):
            for child in (node.get(keyword) or {}).values():
                visit(child, (*path, "*"), active)
        extra = node.get("additionalProperties")
        if isinstance(extra, dict):
            visit(extra, (*path, "*"), active)
        items = node.get("items")
        if isinstance(items, dict) and path and not path[-1].endswith("[]"):
            every.add((*path[:-1], path[-1] + "[]"))
            visit(items, (*path[:-1], path[-1] + "[]"), active)

    visit(schema, (), frozenset())
    return every, named


def _looks_like_a_reference(name: str) -> bool:
    return bool(SERVICE_KEY.match(name) or WORKFLOW_KEY.match(name) or MODEL_KEY.match(name))


def uncovered_reference_paths(section: Section, schema: Any) -> set[tuple[str, ...]]:
    """Schema paths of one section whose last step looks like a reference name and that its table neither reads (at that
    path) nor excuses (at that path)."""
    _, named = schema_paths(schema)
    looking = {path for path in named if _looks_like_a_reference(path[-1])}
    return looking - {ref.path for ref in section.refs} - set(section.not_references)


def undeclared_schema_paths(section: Section, schema: Any) -> set[tuple[str, ...]]:
    """Declared paths that the schema does not have (a typo, or a container that does not exist), less the paths the section
    says the schema leaves open."""
    every, _ = schema_paths(schema)
    return {ref.path for ref in section.refs if ref.path and ref.path not in every and ref.path not in section.open_paths}


SENTINEL = "sentinel-unshipped-id"


def with_sentinel(document: Any, ref: Ref) -> Any:
    """A copy of ``document`` with an id this release does not ship put where ``ref`` says an id lives."""
    root = copy.deepcopy(document) if isinstance(document, dict) else {}
    node = root
    steps = ref.path if ref.keys else ref.path[:-1]
    for step in steps:
        if step == "*":
            key = next(iter(node), "probe")
            child = node.get(key)
        elif step.endswith("[]"):
            key = step[:-2]
            child = node.get(key)
            if not isinstance(child, list) or not child or not isinstance(child[0], dict):
                child = node[key] = [{}]
            node = child[0]
            continue
        else:
            key = step
            child = node.get(key)
        if not isinstance(child, dict):
            child = node[key] = {}
        node = child
    if ref.keys:
        node[SENTINEL] = {}
    else:
        node[next(iter(node), "probe") if ref.path[-1] == "*" else ref.path[-1]] = SENTINEL
    return root


def strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def _allowed_ids(kind: str, name: str, document: Any, shipped: dict[str, set[str]]) -> set[str]:
    if kind == "alias_target":
        local = _stable_names(document) | _engine_ids(document) if name.startswith("model_aliases") else set()
        return shipped["workflow"] | shipped["model"] | shipped["service"] | local
    if kind == "stable_name":
        return _stable_names(document)
    return shipped[kind]


def document_problems(name: str, document: Any, shipped: dict[str, set[str]]) -> list[str]:
    """Address and path problems of any document; reference problems of a data document (a schema describes keys)."""
    problems: list[str] = []
    for text in strings(document):
        problems += [f"{name}: {why}" for why in address_problems(text)]
        if HOST_PATH.search(text):
            problems.append(f"{name}: host path in {text[:80]!r}")
    for kind, ident, where in ([] if name.endswith(".schema.json") else references(name, document)):
        if kind == "unmapped":
            problems.append(f"{name}: {where}")
        elif ident not in _allowed_ids(kind, name, document, shipped):
            problems.append(f"{name}: {where} names the {kind.replace('_', ' ')} {ident!r}, which this release does not ship")
    return problems


def release_documents() -> dict[str, Any]:
    documents: dict[str, Any] = {}
    for path in sorted(DEFAULTS.glob("*.yml")):
        documents[path.name] = yaml.safe_load(path.read_text(encoding="utf-8"))
    for path in sorted(DEFAULTS.glob("*.schema.json")):
        documents[path.name] = json.loads(path.read_text(encoding="utf-8"))
    return documents


def test_the_release_defaults_are_neutral():
    shipped = shipped_ids()
    found = [problem for name, document in release_documents().items() for problem in document_problems(name, document, shipped)]
    assert not found, "\n".join(found)


def test_every_workflow_file_the_release_names_ships_in_the_repository():
    # The neutrality check above reads ids, not files: a household graph path would pass it while its id is shipped.
    documents = release_documents()
    declared = {entry["file"] for section in ("audio", "videos") for entry in documents[f"{section}.yml"]["workflows"].values()}
    declared |= set(documents["images.yml"]["graph_roles"])
    bundled = {path.relative_to(ROOT).as_posix() for path in (ROOT / "workflows").rglob("*.json") if not path.name.endswith(".ui.json")}
    assert declared and declared <= bundled, sorted(declared - bundled)


def test_the_release_names_no_model_listing_but_openais_and_no_owner_choice():
    documents = release_documents()
    assert documents["model_compatibility.yml"]["openai_models_api_url"] == "https://api.openai.com/v1/models"
    # These sections hold only an owner's choices; the release ships them empty.
    assert all(documents[f"{name}.yml"] == {} for name in ("identity", "models", "roles", "service_availability"))


def comment_problems(name: str, text: str) -> list[str]:
    """Host paths and addresses on any line of a default file, comments included."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if HOST_PATH.search(line):
            found.append(f"{name}:{number}: host path")
        found += [f"{name}:{number}: {why}" for why in address_problems(line)]
    return found


def test_the_comments_of_the_default_files_name_no_host_path_or_address():
    found = [problem for path in sorted(DEFAULTS.glob("*.yml")) for problem in comment_problems(path.name, path.read_text(encoding="utf-8"))]
    assert not found, "\n".join(found)


# --- the checks catch what they exist to catch (negative fixtures) ---------------------------------------------------

SHIPPED = {"service": {"litellm"}, "workflow": {"video-a"}, "model": {"video-a", "fast"}}


def _problems(document: Any, name: str = "instance.yml") -> list[str]:
    return document_problems(name, document, SHIPPED)


def _built(*parts: str) -> str:
    return ".".join(parts)  # built, so that this file holds no literal LAN address


LAN_HOST = _built("192", "168", "1", "23")
LAN_NET = _built("192", "168", "1", "0") + "/24"


def _section_files() -> set[str]:
    return {path.name.split(".")[0] for path in DEFAULTS.glob("*.yml")}


def test_every_default_file_has_a_declared_section_and_no_section_is_none():
    assert _section_files() == set(SECTIONS)
    assert all(isinstance(section, Section) for section in SECTIONS.values())


def _schema_of(name: str) -> Any:
    return json.loads((DEFAULTS / f"{name}.schema.json").read_text(encoding="utf-8"))


def test_every_reference_looking_schema_path_of_a_section_is_declared_there_or_not_a_reference():
    """Per section and per path: a field called ``backend`` that one path reads says nothing about another path or section."""
    for name in sorted(_section_files()):
        assert uncovered_reference_paths(SECTIONS[name], _schema_of(name)) == set(), \
            f"{name}: a schema path looks like a reference and the section's table neither reads nor excuses it"


def test_every_declared_path_exists_in_the_schema_of_its_section():
    for name in sorted(_section_files()):
        assert undeclared_schema_paths(SECTIONS[name], _schema_of(name)) == set(), f"{name}: a declared path is not in the schema"
        assert set(SECTIONS[name].open_paths) <= {ref.path for ref in SECTIONS[name].refs}, f"{name}: an excuse for no declared path"


def test_the_declared_paths_are_found_in_the_shipped_defaults_or_catch_a_sentinel():
    """Every declared (section, path): an id this release does not ship, put at that path in a copy of the shipped record,
    is reported by the checker (and the shipped record itself is clean)."""
    shipped = shipped_ids()
    for name in sorted(_section_files()):
        record = yaml.safe_load((DEFAULTS / f"{name}.yml").read_text(encoding="utf-8"))
        assert document_problems(f"{name}.yml", record, shipped) == [], name
        for ref in SECTIONS[name].refs:
            problems = document_problems(f"{name}.yml", with_sentinel(record, ref), shipped)
            assert any(SENTINEL in problem for problem in problems), (name, ref)


def test_a_new_reference_field_in_one_section_is_not_excused_by_another_sections_handler():
    schema = _schema_of("images")
    schema["properties"]["backend"] = {"type": "string"}
    assert uncovered_reference_paths(SECTIONS["images"], schema) == {("backend",)}
    assert _problems({"backend": "my-private-engine"}, "images.yml") == []  # nothing reads it, so the table must grow
    assert uncovered_reference_paths(SECTIONS["roles"], _schema_of("roles")) == set()  # roles reads ("*", "backend"), not ("backend",)
    assert uncovered_reference_paths(SECTIONS["roles"], {"properties": {"backend": {}}}) == {("backend",)}


def test_a_section_that_declares_no_reference_leaves_its_reference_paths_uncovered():
    assert uncovered_reference_paths(Section(), _schema_of("audio")) == {("default_workflow",)}


def test_the_schema_paths_are_read_in_the_step_language():
    schema = {"$defs": {"entry": {"type": "object", "properties": {"backend": {}, "list": {"type": "array", "items": {
                  "type": "object", "properties": {"to": {}}}}}}},
              "type": "object", "patternProperties": {"^x$": {"$ref": "#/$defs/entry"}},
              "properties": {"one": {"oneOf": [{"type": "object", "properties": {"model": {}}}, {"allOf": [{"properties": {"default_model": {}}}]}]},
                             "table": {"type": "object", "additionalProperties": {"type": "object", "properties": {"service": {}}}}}}
    every, named = schema_paths(schema)
    assert {("*", "backend"), ("*", "list"), ("*", "list[]", "to"), ("one", "model"), ("one", "default_model"),
            ("table", "*", "service")} <= named
    assert {("table", "*"), ("*", "list[]")} <= every


def test_a_declared_path_that_is_not_in_the_schema_is_found():
    roles = _schema_of("roles")
    wrong = Section(refs=(Ref("service", ("*", "not_a_real_container", "backend")), Ref("model", ("*", "model"))))
    assert undeclared_schema_paths(wrong, roles) == {("*", "not_a_real_container", "backend")}
    assert undeclared_schema_paths(Section(refs=(Ref("model", ("*", "model")),)), roles) == set()
    assert undeclared_schema_paths(Section(refs=(Ref("service", ("*", "gone"), keys=True),)), roles) == {("*", "gone")}
    assert undeclared_schema_paths(Section(refs=(Ref("service", ("*", "gone")),), open_paths={("*", "gone"): "open"}), roles) == set()


def test_a_second_nested_reference_field_in_a_section_that_reads_the_first_is_found():
    roles = _schema_of("roles")
    roles["additionalProperties"]["properties"]["fallback"] = {"type": "object", "properties": {"backend": {"type": "string"}}}
    assert uncovered_reference_paths(SECTIONS["roles"], roles) == {("*", "fallback", "backend")}


def test_a_second_address_on_a_line_with_an_allowed_one_is_found():
    assert _problems({"hosts": f"[127.0.0.1, {LAN_HOST}]"})
    assert _problems({"hosts": ["127.0.0.1", LAN_HOST]})
    assert _problems({"hosts": f"127.0.0.1 and {LAN_HOST}:8080"})


def test_every_address_in_a_text_is_found_not_only_the_first_token():
    other = "http://" + LAN_HOST + ":8080/v1"
    assert _problems({"value": "http://127.0.0.1/health " + other})  # the second URL is not the first one's path
    assert _problems({"value": ["x", {"nested": ["http://127.0.0.1/health " + other]}]})
    assert _problems({"value": "http://127.0.0.1/health http://127.0.0.1:4011/ok"}) == []
    assert _problems({"value": "network=10.4.0.0/16"})
    assert _problems({"value": ["a", {"n": "network=10.4.0.0/16"}]})
    assert _problems({"value": "endpoint=" + LAN_HOST + ":8080"})
    assert _problems({"value": "[" + LAN_HOST + "]"}) and _problems({"value": "see (" + LAN_HOST + ")."})


def test_a_url_ended_by_closing_punctuation_or_joined_to_another_url_is_judged_by_its_host():
    for text in (f"see (http://{LAN_HOST})", f"see (http://{LAN_HOST}:8080/x).", f"endpoint=http://{LAN_HOST}, next",
                 f"a=http://{LAN_HOST};b=1", f"http://127.0.0.1/ok,http://{LAN_HOST}/v1", f"(http://127.0.0.1) and (http://{LAN_HOST})",
                 f"http://127.0.0.1,{LAN_HOST}", "http://[2a01:4f8::1]:8080, next"):
        assert _problems({"v": text}), text
    for text in ("see (http://127.0.0.1)", "endpoint=http://127.0.0.1:8000, next", "http://127.0.0.1/a,http://[::1]:80/b",
                 "http://[::1]:8000/x", "(http://example.test/10.0.0.0/8)"):
        assert _problems({"v": text}) == [], text


def test_a_dotted_quad_joined_to_a_word_by_a_hyphen_is_an_identifier_not_an_address():
    for text in ("version 1.2.3.4-style", "build-1.2.3.4", "tool 8.8.8.8-rc1 released", "pkg-8.8.8.8-beta"):
        assert _problems({"v": text}) == [], text
    for text in (f"{LAN_HOST}", f"range {LAN_HOST}-{_built('192', '168', '1', '99')}", f"- {LAN_HOST}", f"host {LAN_HOST} - up",
                 "8.8.8.8", "dns 8.8.8.8, 1.1.1.1"):
        assert _problems({"v": text}), text


def test_an_address_in_a_comment_line_is_found():
    assert comment_problems("x.yml", "a: 1\n# endpoint=" + LAN_HOST + ":8080\n")[0].startswith("x.yml:2:")
    assert comment_problems("x.yml", "# network=10.4.0.0/16 and 127.0.0.1\n")
    assert comment_problems("x.yml", "# the gateway listens on 127.0.0.1:8000, trusting 10.0.0.0/8 and [::1]\n") == []
    assert comment_problems("x.yml", "# see http://example.test/10.0.0.0/8\n") == []


def test_a_mapped_address_is_one_literal_and_a_url_path_is_not_scanned():
    assert _problems({"v": "[::ffff:" + LAN_HOST + "]:80"}) and _problems({"v": "::ffff:" + LAN_HOST})
    assert _problems({"v": "[::ffff:127.0.0.1]:80"}) == [] and _problems({"v": "::ffff:127.0.0.1"}) == []
    assert _problems({"v": "https://user@127.0.0.1/" + LAN_HOST}) == []


def test_ipv6_is_checked():
    assert _problems({"host": "fd12:3456:789a::7"})
    assert _problems({"host": "http://[2a01:4f8::1]:8080"})
    assert _problems({"host": "fe80::1/128"})


def test_a_public_address_and_a_private_cidr_that_is_not_generic_are_found():
    assert _problems({"host": "8.8.8.8"})
    assert _problems({"net": LAN_NET})
    assert _problems({"net": "10.4.0.0/16"})


def test_loopback_unspecified_documentation_and_generic_private_cidrs_pass():
    for allowed in ("127.0.0.1", "127.0.0.0/8", "0.0.0.0", "::1", "::", "192.0.2.7", "198.51.100.0/24", "2001:db8::5",
                    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7", "fe80::/10", "http://127.0.0.1:4011/health",
                    "http://[::ffff:127.0.0.1]:8080", "::ffff:127.0.0.1", "http://[::1]:8000/x", "127.0.0.1:8000"):
        assert _problems({"value": allowed}) == [], allowed


def test_a_generic_private_range_as_a_single_host_is_found():
    assert _problems({"value": "10.0.0.0/32"}) and _problems({"value": _built("192", "168", "0", "0")})


def test_a_host_address_with_a_prefix_length_is_not_a_generic_private_cidr():
    for host_with_prefix in ("10.1.2.3/8", _built("192", "168", "12", "34") + "/16", "fd12::1/7", "172.16.5.5/12", "FC00::/7"):
        assert _problems({"value": host_with_prefix}), host_with_prefix
        assert _problems({"value": [{"nested": host_with_prefix}]}), host_with_prefix


def test_a_url_is_judged_by_its_host_and_its_path_is_not_a_prefix_length():
    assert _problems({"value": "http://10.0.0.0/8"})  # the host is a private address; "/8" is a path
    assert _problems({"value": [["http://10.0.0.0/8"]]})
    assert _problems({"value": {"urls": ["http://10.0.0.0/8"]}})
    assert _problems({"value": f"http://{LAN_HOST}:8080/v1"})
    assert _problems({"value": "http://[::ffff:" + LAN_HOST + "]:8080"})  # a mapped private address is still private
    assert _problems({"value": "http://example.test/10.0.0.0/8"}) == []  # a path is not a host


def test_a_reference_the_release_does_not_ship_is_found_in_every_section_shape():
    assert _problems({"image": {"backend": "my-box-comfy"}}, "images.yml") == []  # no such field in images: judged by shape
    assert _problems({"default_workflow": "video-b"}, "videos.yml")
    assert _problems({"extend_workflow": "video-b"}, "videos.yml")
    assert _problems({"workflows": {"video-b": {"file": "x"}}}, "videos.yml")
    assert _problems({"workflows": {"video-a": {"i2v_workflow": "video-b"}}}, "videos.yml")
    assert _problems({"workflows": {"video-a": {}}, "aliases": {"old": "video-b"}}, "videos.yml")
    assert _problems({"variations": {"default_workflow": "video-b"}}, "images.yml")
    assert _problems({"design_engine": "tts-on-my-box"}, "voices.yml")
    assert _problems({"mine": {"backend": "my-private-engine", "path": "x"}}, "roles.yml")
    assert _problems({"mine": {"backend": "litellm", "model": "my-private-gguf"}}, "roles.yml")
    assert _problems({"my-private-gguf": {"backend": "litellm", "path": "x"}}, "models.yml")
    assert _problems({"my-box": {"state": "disabled"}}, "service_availability.yml")
    assert _problems({"defaults": {"chat": "my-private-gguf"}}, "model_compatibility.yml")
    assert _problems({"anything": 1}, "no_such_section.yml")


def test_an_alias_target_a_stable_name_an_engine_key_and_a_list_target_are_checked():
    assert _problems({"chat": {"stable": {"default": "private-model"}}}, "model_aliases.yml")
    assert _problems({"speech": {"stable": {"tts": ["private-engine"]}}}, "model_aliases.yml")
    assert _problems({"speech": {"stable": {"tts": ["litellm", "private-engine"]}}}, "model_aliases.yml")
    assert _problems({"speech": {"engines": {"private-engine": {"service": "litellm"}}}}, "model_aliases.yml")
    assert _problems({"speech": {"engines": {"litellm": {"service": "my-box-tts"}}}}, "model_aliases.yml")
    assert _problems({"chat": {"stable": {"fast": "video-a"}, "openai": [{"match": "gpt-*", "to": "private-name"}]}}, "model_aliases.yml")
    assert _problems({"chat": {"stable": {"fast": "video-a"}, "openai": [{"match": "gpt-*", "to": "video-a"}]}}, "model_aliases.yml"), \
        "an openai pattern points at a stable name, not at a workflow"


def test_a_model_called_by_a_container_name_is_not_shipped():
    # "stable" and "engines" are fields of the model_aliases section, not model ids.
    for container in ("stable", "engines", "openai", "voice_aliases", "chat"):
        assert _problems({"mine": {"backend": "litellm", "model": container}}, "roles.yml"), container
    assert "stable" not in shipped_ids()["model"] and "default" not in shipped_ids()["model"] - _stable_names(yaml.safe_load((DEFAULTS / "model_aliases.yml").read_text(encoding="utf-8")))
    assert "image" in shipped_ids()["model"]  # a stable name the release ships


def test_stable_names_and_engine_ids_are_read_from_the_section_shape():
    document = {"chat": {"stable": {"fast": None, "default": "x"}, "engines": {"eng": {}}, "voice_aliases": {"v": "w"}},
                "speech": {"stable": {"tts": ["eng"]}}}
    assert _stable_names(document) == {"fast", "default", "tts"}
    assert _engine_ids(document) == {"eng"}


def test_the_release_stable_names_resolve_to_shipped_ids_or_nothing():
    # Per operation: one name (image, image-fast) can appear under two operations, and a flattened map would keep one.
    document = yaml.safe_load((DEFAULTS / "model_aliases.yml").read_text(encoding="utf-8"))
    targets = [target for body in document.values() for target in body["stable"].values()]
    assert targets and all(target is None for target in targets)


def test_a_shipped_service_workflow_or_model_passes():
    assert _problems({"default_workflow": "video-a", "workflows": {"video-a": {"i2v_workflow": "video-a"}},
                      "aliases": {"old": "video-a"}}, "videos.yml") == []
    assert _problems({"mine": {"backend": "litellm", "model": "fast"}}, "roles.yml") == []
    assert _problems({"defaults": {"chat": "fast"}}, "model_compatibility.yml") == []
    assert _problems({"chat": {"stable": {"fast": "video-a", "default": "fast", "none": None}, "openai": [{"match": "gpt-*", "to": "fast"}]},
                      "speech": {"engines": {"litellm": {"service": "litellm"}}, "stable": {"tts": ["litellm"]}}}, "model_aliases.yml") == []


def test_a_host_path_is_found():
    assert _problems({"path": "/" + "home/someone/models"})
