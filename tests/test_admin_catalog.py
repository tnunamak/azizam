from __future__ import annotations

from pathlib import Path

from gateway import service_registry
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
