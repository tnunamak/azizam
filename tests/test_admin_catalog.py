from __future__ import annotations

from pathlib import Path

from gateway import service_registry
from gateway.admin_catalog import (
    admin_api_surfaces,
    load_admin_surfaces_catalog,
    load_profile_policy_catalog,
    profile_policy_catalog,
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
    registry = service_registry.build_service_registry_seed(
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


def test_profile_policy_catalog_loads_from_registry_yaml() -> None:
    catalog = profile_policy_catalog()

    assert catalog["protocols"][0] == {
        "id": "openai",
        "label": "OpenAI-compatible",
        "summary_label": "OpenAI-compatible",
        "description": "/openai/v1 and /v1 SDKs",
        "default": True,
    }
    assert [option["id"] for option in catalog["target_visibility"]] == [
        "catalog",
        "available",
    ]
    provider_native = next(
        option for option in catalog["protocols"] if option["id"] == "provider-native"
    )
    assert provider_native["profile_form"] is False
    assert provider_native["simulator"] is False
    assert [option["id"] for option in catalog["status_visibility"]] == [
        "none",
        "basic",
        "standard",
        "full",
    ]


def test_profile_policy_catalog_returns_deep_copy() -> None:
    catalog = profile_policy_catalog()
    catalog["protocols"][0]["label"] = "Mutated"

    assert profile_policy_catalog()["protocols"][0]["label"] == "OpenAI-compatible"


def test_profile_policy_catalog_accepts_override_directory(tmp_path: Path) -> None:
    (tmp_path / "profile_policy_catalog.yml").write_text(
        """
protocols:
  - id: acme
    label: Acme API
    summary_label: Acme API
    description: /acme/v1 clients
target_visibility:
  - id: catalog
    label: Allowed catalog
""",
        encoding="utf-8",
    )

    catalog = profile_policy_catalog(catalog_dir=tmp_path)

    assert catalog["protocols"] == [
        {
            "id": "acme",
            "label": "Acme API",
            "summary_label": "Acme API",
            "description": "/acme/v1 clients",
        }
    ]
    assert catalog["target_visibility"] == [
        {"id": "catalog", "label": "Allowed catalog"}
    ]


def test_profile_policy_catalog_override_directory_falls_back_to_bundled(
    tmp_path: Path,
) -> None:
    catalog = profile_policy_catalog(catalog_dir=tmp_path)

    assert catalog["protocols"][0]["id"] == "openai"


def test_profile_policy_catalog_rejects_unknown_code_owned_enum(tmp_path: Path) -> None:
    path = tmp_path / "profile_policy_catalog.yml"
    path.write_text(
        """
target_activation:
  - id: surprise
    label: Surprise
""",
        encoding="utf-8",
    )

    try:
        load_profile_policy_catalog(path)
    except ValueError as exc:
        assert "unsupported id 'surprise'" in str(exc)
    else:
        raise AssertionError("expected invalid target_activation id to fail")
