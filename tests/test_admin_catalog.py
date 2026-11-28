from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.registry_test_helpers import seed_registry_document
from gateway import service_registry, model_presentation
from gateway.admin_catalog import (
    admin_api_surfaces,
    load_admin_surfaces_catalog,
)


def test_admin_api_surfaces_derive_from_protocol_frontend_seed(tmp_path: Path) -> None:
    (tmp_path / "protocol_frontends.yml").write_text(
        """
records:
  - id: custom
    name: Custom API
    protocol: custom
    enabled: true
    routes:
      - /custom
      - /custom/v1
    intents:
      - custom.run
""",
        encoding="utf-8",
    )
    (tmp_path / "admin_surfaces.yml").write_text(
        """
records:
  - id: owner-api
    name: Owner API
    status: live
    route_base: /owner/api
    profile_gates:
      - owner session
""",
        encoding="utf-8",
    )

    surfaces = admin_api_surfaces(proxy_config={"registry": {"catalog_dir": str(tmp_path)}})

    custom = next(surface for surface in surfaces if surface["profile_gates"] == ["custom"])
    assert custom["name"] == "Custom API"
    assert custom["route_base"] == "/custom/v1"
    assert custom["status"] == "live"
    assert custom["description"] == "Supports custom run."
    owner = next(surface for surface in surfaces if surface["name"] == "Owner API")
    assert owner["route_base"] == "/owner/api"
    assert owner["profile_gates"] == ["owner session"]


def test_admin_api_surfaces_include_bundled_provider_native_frontend() -> None:
    surfaces = admin_api_surfaces()

    provider_native = next(
        surface for surface in surfaces if surface["profile_gates"] == ["provider-native"]
    )
    assert provider_native["name"] == "Provider-native passthrough"
    assert provider_native["route_base"] == "/gateway/v1/providers/{provider}"


def test_admin_api_surfaces_accept_service_registry_document() -> None:
    registry = seed_registry_document(
        {
            "protocol_frontends": {
                "internal": {
                    "name": "Internal API",
                    "protocol": "internal",
                    "enabled": False,
                    "routes": [{"method": "POST", "path": "/internal/run"}],
                }
            }
        }
    )

    surfaces = admin_api_surfaces(registry=registry)

    internal = next(surface for surface in surfaces if surface["profile_gates"] == ["internal"])
    assert internal["name"] == "Internal API"
    assert internal["route_base"] == "/internal/run"
    assert internal["status"] == "disabled"


def test_admin_control_surface_is_catalog_data() -> None:
    surfaces = admin_api_surfaces()

    admin = next(surface for surface in surfaces if surface["name"] == "Admin API")
    assert admin["route_base"] == "/admin/api"
    assert admin["profile_gates"] == ["admin session", "oak_"]
    assert admin["clients"] == ["operators", "automation"]


def test_admin_surfaces_catalog_ignores_missing_file(tmp_path: Path) -> None:
    assert load_admin_surfaces_catalog(tmp_path / "missing.yml") == []


@pytest.mark.parametrize("field", ["name", "status"])
@pytest.mark.parametrize("invalid", ["missing", "empty"])
def test_admin_surface_requires_presentation_fields(tmp_path: Path, field: str, invalid: str) -> None:
    shipped = Path(__file__).resolve().parents[1] / "registry" / "admin_surfaces.yml"
    document = yaml.safe_load(shipped.read_text(encoding="utf-8"))
    row = document["records"][0]
    if invalid == "missing":
        del row[field]
    else:
        row[field] = ""
    path = tmp_path / "admin_surfaces.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(ValueError) as error:
        load_admin_surfaces_catalog(path)
    assert row["id"] in str(error.value)
    assert field in str(error.value)


def test_shipped_admin_surface_projection_is_unchanged() -> None:
    shipped = Path(__file__).resolve().parents[1] / "registry" / "admin_surfaces.yml"
    expected = [{"name": "Admin API", "route_base": "/admin/api", "status": "live",
                 "profile_gates": ["admin session", "oak_"],
                 "clients": ["operators", "automation"],
                 "description": "Control-plane API for Apps, Profiles, keys, audit, and policy simulation."}]
    actual = load_admin_surfaces_catalog(shipped)
    assert actual == expected
    assert json.dumps(actual).encode("utf-8") == json.dumps(expected).encode("utf-8")


@pytest.mark.parametrize("field", ["name", "status"])
@pytest.mark.parametrize("bad", ["   ", None, 0, 1, True, [], ["x"], {}, {"a": 1}])
def test_admin_surface_rejects_non_text_fields(tmp_path, field, bad):
    row = {"id": "test-row", "name": "A surface", "status": "live", field: bad}
    path = tmp_path / "admin_surfaces.yml"
    path.write_text(yaml.safe_dump({"records": [row]}), encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_admin_surfaces_catalog(path)
    assert "test-row" in str(exc.value)
    assert field in str(exc.value)


def test_admin_surface_idless_second_row_names_index(tmp_path):
    path = tmp_path / "admin_surfaces.yml"
    path.write_text(yaml.safe_dump({"records": [
        {"id": "good", "name": "Good", "status": "live"},
        {"name": "Second", "status": True},
    ]}), encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        load_admin_surfaces_catalog(path)
    assert "records[1]" in str(exc.value)
    assert "status" in str(exc.value)


def test_admin_surface_uses_patched_presentation_words(tmp_path, monkeypatch):
    words = dict(model_presentation.PRESENTATION_WORDS)
    words["admin_surface"] = {
        "fallback_name": "Unnamed interface",
        "intents_description": "Offers {intents}!",
        "protocol_description": "Interface for {protocol}!",
        "default_protocol": "generic",
        "empty_description": "Interface without a protocol!",
    }
    monkeypatch.setattr(model_presentation, "PRESENTATION_WORDS", words)
    rows = admin_api_surfaces(
        registry={"protocol_frontends": {
            "empty": {}, "intents": {"intents": [" custom.do_work ", ""]},
            "protocol": {"protocol": "custom"}, "blank": {"protocol": " "},
        }},
        proxy_config={"registry": {"catalog_dir": str(tmp_path)}},
    )
    assert rows[0]["name"] == "Unnamed interface"
    assert [row["description"] for row in rows] == [
        "Interface for Generic!", "Offers custom do work!",
        "Interface for Custom!", "Interface without a protocol!",
    ]


def test_shipped_protocol_surface_words_are_byte_identical(tmp_path):
    rows = admin_api_surfaces(
        registry={"protocol_frontends": {
            "empty": {}, "intents": {"intents": ["custom.do_work", "other.run"]},
            "protocol": {"protocol": "custom"}, "blank": {"protocol": " "},
        }},
        proxy_config={"registry": {"catalog_dir": str(tmp_path)}},
    )
    assert rows[0]["name"] == "Protocol frontend"
    assert [row["description"].encode("utf-8") for row in rows] == [
        b"Protocol protocol frontend.", b"Supports custom do work, other run.",
        b"Custom protocol frontend.", b"Protocol frontend.",
    ]
