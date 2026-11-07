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

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / "registry" / "defaults"

HOST_PATH = re.compile(r"(?<![\w.])/(?:media|mnt|home|Users)/[\w.-]+")
IPV4 = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?(?![\w.])")
IPV6 = re.compile(r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:/\d{1,3})?(?![\w:])")

# Allowed anywhere, as a host or a network: loopback, the unspecified address, the documentation ranges.
ALLOWED_ANYWHERE = [ipaddress.ip_network(net) for net in (
    "127.0.0.0/8", "0.0.0.0/32", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
    "::1/128", "::/128", "2001:db8::/32",
)]
# Allowed only as exactly this CIDR (a rule such as "trust the private ranges"), never as a host in it.
GENERIC_PRIVATE_CIDRS = [ipaddress.ip_network(net) for net in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7", "fe80::/10",
)]


def address_problem(text: str) -> str | None:
    """Why this address or CIDR is not neutral, or None when it is."""
    try:
        network = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return None  # not an address at all
    if any(network.version == allowed.version and network.subnet_of(allowed) for allowed in ALLOWED_ANYWHERE):
        return None
    if "/" in text and network in GENERIC_PRIVATE_CIDRS:
        return None
    if network.is_private or network.is_link_local:
        return f"{text} is a private address of one network; only a generic private CIDR (as a CIDR) may stand"
    return f"{text} is a public address"


def addresses_in(text: str) -> Iterator[str]:
    for pattern in (IPV4, IPV6):
        yield from (match.group(0) for match in pattern.finditer(text))


# Keys whose value names a thing the release must ship.
SERVICE_KEY = re.compile(r"^(?:backend|service|service_id|engine|[a-z_]+_(?:service|service_id|engine|backend))$")
WORKFLOW_KEY = re.compile(r"^(?:workflow|[a-z_]+_workflow)$")
MODEL_KEY = re.compile(r"^(?:model|model_id|default_model|[a-z_]+_model)$")


def shipped_ids() -> dict[str, set[str]]:
    """What this release ships: service ids (registry), workflow ids (graph files and manifests), model names."""
    services = {record["id"] for record in yaml.safe_load((ROOT / "registry" / "services.yml").read_text(encoding="utf-8"))["records"]}
    for item in json.loads((ROOT / "registry" / "engine_definitions.json").read_text(encoding="utf-8"))["definitions"]:
        services.add(str(item["definition"]))
        services.add(str(item.get("record", {}).get("id", "")))
    workflows = {path.name.removesuffix(".json") for path in (ROOT / "workflows").rglob("*.json") if not path.name.endswith(".ui.json")}
    for path in (ROOT / "manifests" / "workflows").glob("*.yml"):
        manifest_id = str(yaml.safe_load(path.read_text(encoding="utf-8")).get("id", ""))
        workflows.update({manifest_id, manifest_id.rsplit(".", 1)[-1]})
    models = set(workflows)
    for name in ("model_aliases",):
        models.update(_keys(yaml.safe_load((DEFAULTS / f"{name}.yml").read_text(encoding="utf-8"))))
    return {"service": services - {""}, "workflow": workflows - {""}, "model": models - {""}}


def _keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def references(value: Any, path: str = "") -> Iterator[tuple[str, str, str]]:
    """(kind, id, where) for every reference to a service, workflow or model in a parsed document."""
    if isinstance(value, dict):
        for key, item in value.items():
            where = f"{path}/{key}"
            if isinstance(item, str) and item.strip():
                for kind, pattern in (("service", SERVICE_KEY), ("workflow", WORKFLOW_KEY), ("model", MODEL_KEY)):
                    if pattern.match(str(key)):
                        yield kind, item.strip(), where
            if key == "workflows" and isinstance(item, dict):
                for workflow_id in item:
                    yield "workflow", str(workflow_id), f"{where}/{workflow_id} (key)"
                if isinstance(value.get("aliases"), dict):
                    for alias, target in value["aliases"].items():
                        if isinstance(target, str):
                            yield "workflow", target, f"{path}/aliases/{alias}"
            yield from references(item, where)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from references(item, f"{path}/{index}")


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


def document_problems(name: str, document: Any, shipped: dict[str, set[str]]) -> list[str]:
    """Address and path problems of any document; reference problems of a data document (a schema describes keys)."""
    problems: list[str] = []
    for text in strings(document):
        for candidate in addresses_in(text):
            why = address_problem(candidate)
            if why:
                problems.append(f"{name}: {why}")
        if HOST_PATH.search(text):
            problems.append(f"{name}: host path in {text[:80]!r}")
    for kind, ident, where in ([] if name.endswith(".schema.json") else references(document)):
        if ident not in shipped[kind]:
            problems.append(f"{name}: {where} names the {kind} {ident!r}, which this release does not ship")
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
            found += [f"{path.name}:{number}: {why}" for why in map(address_problem, addresses_in(line)) if why]
    assert not found, "\n".join(found)


# --- the checks catch what they exist to catch (negative fixtures) ---------------------------------------------------

SHIPPED = {"service": {"litellm"}, "workflow": {"video-a"}, "model": {"video-a"}}


def _problems(document: Any) -> list[str]:
    return document_problems("fixture", document, SHIPPED)


LAN_HOST = ".".join(("192", "168", "1", "23"))  # built, so that this file holds no literal LAN address


def test_a_second_address_on_a_line_with_an_allowed_one_is_found():
    assert _problems({"hosts": f"[127.0.0.1, {LAN_HOST}]"})
    assert _problems({"hosts": ["127.0.0.1", LAN_HOST]})


def test_ipv6_is_checked():
    assert _problems({"host": "fd12:3456:789a::7"})
    assert _problems({"host": "http://[2a01:4f8::1]:8080"})
    assert _problems({"host": "fe80::1/128"})


def test_a_public_address_and_a_private_cidr_that_is_not_generic_are_found():
    assert _problems({"host": "8.8.8.8"})
    assert _problems({"net": ".".join(("192", "168", "1", "0")) + "/24"})
    assert _problems({"net": "10.4.0.0/16"})


def test_loopback_unspecified_documentation_and_generic_private_cidrs_pass():
    for allowed in ("127.0.0.1", "127.0.0.0/8", "0.0.0.0", "::1", "::", "192.0.2.7", "198.51.100.0/24", "2001:db8::5",
                    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7", "http://127.0.0.1:4011/health"):
        assert _problems({"value": allowed}) == [], allowed


def test_a_generic_private_range_as_a_single_host_is_found():
    assert _problems({"value": "10.0.0.0/32"}) and _problems({"value": ".".join(("192", "168", "0", "0"))})


def test_a_service_workflow_or_model_this_release_does_not_ship_is_found():
    assert _problems({"image": {"backend": "my-box-comfy"}})
    assert _problems({"default_workflow": "video-b"})
    assert _problems({"workflows": {"video-b": {"file": "x"}}})
    assert _problems({"workflows": {"video-a": {}}, "aliases": {"old": "video-b"}})
    assert _problems({"voices": {"design_engine": "tts-on-my-box"}})
    assert _problems({"default_model": "my-private-gguf"})


def test_a_shipped_service_workflow_or_model_passes():
    assert _problems({"backend": "litellm", "default_workflow": "video-a", "default_model": "video-a",
                      "workflows": {"video-a": {"i2v_workflow": "video-a"}}, "aliases": {"old": "video-a"}}) == []


def test_a_host_path_is_found():
    assert _problems({"path": "/" + "home/someone/models"})
