"""The release defaults are neutral (docs/design/CONFIG_HOMES.md, 10.2 item 3).

They name no host path of one machine, no address of one network, and no service, workflow or model that this
release does not ship. The files are parsed as data and each address value and each reference is judged on its own,
so a line that holds one allowed address and one deployment address fails.
"""
from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

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


_V4_PORT = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}):\d{1,5}")
_V6_PORT = re.compile(r"(\[[0-9A-Fa-f:.%\w]+\]):\d{1,5}")


def _literal(token: str) -> tuple[bool, str | None]:
    """(recognised, problem) for one whole literal: a URL, host:port, address/prefix or a bare address."""
    if "://" in token:
        try:
            host = urlsplit(token).hostname
        except ValueError:
            return False, None
        return (True, host_problem(host)) if host else (False, None)
    for pattern in (_V4_PORT, _V6_PORT):
        match = pattern.fullmatch(token)
        if match:
            return True, host_problem(match.group(1))
    if "/" in token:
        head = token.split("/", 1)[0]
        try:
            ipaddress.ip_address(head)
        except ValueError:
            return False, None
        return True, _network_problem(token)
    try:
        ipaddress.ip_address(token.strip("[]"))
    except ValueError:
        return False, None
    return True, host_problem(token)


def address_problems(text: str) -> list[str]:
    """Every non-neutral address in a string. The whole string is tried as one literal first (a URL is judged by its
    host, never read as a CIDR); a string that is not one literal is split into tokens and each token is tried the same way."""
    recognised, problem = _literal(text.strip())
    if recognised:
        return [problem] if problem else []
    found = []
    for token in re.split(r"[\s,;]+", text):
        for candidate in (token, token.strip("[]()\"'.:")):
            recognised, problem = _literal(candidate)
            if recognised:
                if problem:
                    found.append(problem)
                break
    return found


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


# --- where each section keeps an id (read from registry/defaults/*.schema.json and the shipped records) ---------------
# Each handler yields (kind, id, where). A section with no entry here holds no reference; the completeness tests below
# fail when a section is missing from REFERENCE_SHAPES or a schema grows a reference-looking field the handler lacks.

def _fields(entry: Any, where: str, fields: dict[str, str]) -> Iterator[tuple[str, str, str]]:
    if isinstance(entry, dict):
        for field, kind in fields.items():
            value = entry.get(field)
            if isinstance(value, str) and value.strip():
                yield kind, value.strip(), f"{where}/{field}"


def _workflow_table(document: Any) -> Iterator[tuple[str, str, str]]:
    """audio.yml and videos.yml: ``*_workflow`` fields, ``workflows`` (keyed by workflow id) and ``aliases`` (name -> workflow id)."""
    document = document or {}
    yield from _fields(document, "", {"default_workflow": "workflow", "extend_workflow": "workflow", "remix_workflow": "workflow"})
    for workflow_id, entry in (document.get("workflows") or {}).items():
        yield "workflow", str(workflow_id), f"/workflows/{workflow_id} (key)"
        yield from _fields(entry, f"/workflows/{workflow_id}", {"i2v_workflow": "workflow", "chain_workflow": "workflow"})
    for alias, target in (document.get("aliases") or {}).items():
        if isinstance(target, str):
            yield "workflow", target.strip(), f"/aliases/{alias}"


def _images(document: Any) -> Iterator[tuple[str, str, str]]:
    document = document or {}
    yield from _fields(document.get("variations"), "/variations", {"default_workflow": "workflow"})
    yield from _fields(document.get("edits"), "/edits", {"multiref_default_workflow": "workflow"})


def _voices(document: Any) -> Iterator[tuple[str, str, str]]:
    yield from _fields(document, "", {"quality_engine": "service", "design_engine": "service"})


def _keyed(kind: str, field_kinds: dict[str, str]):
    """A map keyed by id of a ``kind`` (``None``: the keys are defined here) whose entries may hold reference fields."""
    def handler(document: Any) -> Iterator[tuple[str, str, str]]:
        for key, entry in (document or {}).items():
            if kind:
                yield kind, str(key), f"/{key} (key)"
            yield from _fields(entry, f"/{key}", field_kinds)
    return handler


def _model_compatibility(document: Any) -> Iterator[tuple[str, str, str]]:
    for operation, model in ((document or {}).get("defaults") or {}).items():
        if isinstance(model, str) and model.strip():
            yield "model", model.strip(), f"/defaults/{operation}"


def _model_aliases(document: Any) -> Iterator[tuple[str, str, str]]:
    """Per operation: ``stable`` targets (a string, or a list of engines) name a workflow, model, service, another stable
    name or an engine of this document; ``engines`` is keyed by engine id and each holds a ``service`` and a ``model``;
    ``openai`` patterns point (``to``) at a stable name of this document."""
    for operation, body in (document or {}).items():
        if not isinstance(body, dict):
            continue
        for name, target in (body.get("stable") or {}).items():
            for item in ([target] if isinstance(target, str) else target if isinstance(target, list) else []):
                if isinstance(item, str) and item.strip():
                    yield "alias_target", item.strip(), f"/{operation}/stable/{name}"
        for engine_id, entry in (body.get("engines") or {}).items():
            yield "service", str(engine_id), f"/{operation}/engines/{engine_id} (key)"
            yield from _fields(entry, f"/{operation}/engines/{engine_id}", {"service": "service", "model": "model"})
        for index, pattern in enumerate(body.get("openai") or []):
            yield from _fields(pattern, f"/{operation}/openai/{index}", {"to": "stable_name"})


REFERENCE_SHAPES = {
    "audio": _workflow_table, "videos": _workflow_table, "images": _images, "voices": _voices,
    "models": _keyed("model", {"backend": "service"}), "roles": _keyed(None, {"backend": "service", "model": "model"}),
    "service_availability": _keyed("service", {}), "model_compatibility": _model_compatibility,
    "model_aliases": _model_aliases,
    # no id of a service, workflow or model lives here
    "identity": None, "instance": None, "llm": None, "model_routing": None, "reasoning": None,
    "resource_admission": None, "workflow_execution": None,
}
# Schema fields whose name looks like a reference and is not one (and why).
NOT_REFERENCES = {
    "pin_loaded_model": "a boolean",
    "face_service": "the CPU face sidecar's own config object, not a registry service id",
}
# Reference-looking schema fields that the handlers above read.
READ_REFERENCE_FIELDS = {
    "default_workflow", "extend_workflow", "remix_workflow", "multiref_default_workflow", "i2v_workflow", "chain_workflow",
    "quality_engine", "design_engine", "backend", "model",
}


def references(name: str, document: Any) -> Iterator[tuple[str, str, str]]:
    """(kind, id, where) for every reference to a service, workflow or model in the data document ``name``."""
    stem = name.split(".")[0]
    if stem not in REFERENCE_SHAPES:
        yield "unmapped", stem, f"no reference shape is declared for {name}"
        return
    handler = REFERENCE_SHAPES[stem]
    if handler is not None:
        yield from handler(document)


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


def test_the_comments_of_the_default_files_name_no_host_path_or_address():
    found = []
    for path in sorted(DEFAULTS.glob("*.yml")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if HOST_PATH.search(line):
                found.append(f"{path.name}:{number}: host path")
            found += [f"{path.name}:{number}: {why}" for why in address_problems(line)]
    assert not found, "\n".join(found)


# --- the checks catch what they exist to catch (negative fixtures) ---------------------------------------------------

SHIPPED = {"service": {"litellm"}, "workflow": {"video-a"}, "model": {"video-a", "fast"}}


def _problems(document: Any, name: str = "instance.yml") -> list[str]:
    return document_problems(name, document, SHIPPED)


def _built(*parts: str) -> str:
    return ".".join(parts)  # built, so that this file holds no literal LAN address


LAN_HOST = _built("192", "168", "1", "23")
LAN_NET = _built("192", "168", "1", "0") + "/24"


def test_every_default_file_has_a_declared_reference_shape():
    assert {path.name.split(".")[0] for path in DEFAULTS.glob("*.yml")} == set(REFERENCE_SHAPES)


def test_every_reference_looking_schema_field_is_read_or_declared_not_a_reference():
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key == "properties" and isinstance(item, dict):
                    names.update(str(n) for n in item)
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for name, document in release_documents().items():
        if name.endswith(".schema.json"):
            walk(document)
    looking = {n for n in names if SERVICE_KEY.match(n) or WORKFLOW_KEY.match(n) or MODEL_KEY.match(n)}
    assert looking - READ_REFERENCE_FIELDS - set(NOT_REFERENCES) == set(), "a schema field looks like a reference and no handler reads it"
    assert READ_REFERENCE_FIELDS <= names


def test_a_second_address_on_a_line_with_an_allowed_one_is_found():
    assert _problems({"hosts": f"[127.0.0.1, {LAN_HOST}]"})
    assert _problems({"hosts": ["127.0.0.1", LAN_HOST]})
    assert _problems({"hosts": f"127.0.0.1 and {LAN_HOST}:8080"})


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
    stable = {n: t for body in yaml.safe_load((DEFAULTS / "model_aliases.yml").read_text(encoding="utf-8")).values() for n, t in body["stable"].items()}
    assert stable and all(target is None for target in stable.values())


def test_a_shipped_service_workflow_or_model_passes():
    assert _problems({"default_workflow": "video-a", "workflows": {"video-a": {"i2v_workflow": "video-a"}},
                      "aliases": {"old": "video-a"}}, "videos.yml") == []
    assert _problems({"mine": {"backend": "litellm", "model": "fast"}}, "roles.yml") == []
    assert _problems({"defaults": {"chat": "fast"}}, "model_compatibility.yml") == []
    assert _problems({"chat": {"stable": {"fast": "video-a", "default": "fast", "none": None}, "openai": [{"match": "gpt-*", "to": "fast"}]},
                      "speech": {"engines": {"litellm": {"service": "litellm"}}, "stable": {"tts": ["litellm"]}}}, "model_aliases.yml") == []


def test_a_host_path_is_found():
    assert _problems({"path": "/" + "home/someone/models"})
