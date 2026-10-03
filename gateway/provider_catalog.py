"""Provider preset catalog loader.

Vendor-specific provider setup facts live in YAML under ``providers/catalog``.
Runtime code consumes the normalized protocol/auth/listing fields without
branching on vendor names.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

CATALOG_DIR = Path(__file__).resolve().parent.parent / "providers" / "catalog"
PROTOCOLS = frozenset({"openai-chat", "anthropic-messages", "typesafe-systemone"})


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    display_name: str
    logo_key: str
    base_url: str
    protocol: str
    litellm_provider: str
    auth: Mapping[str, str]
    model_listing: Mapping[str, Any]
    docs_url: str
    key_url: str
    billing_dimensions: tuple[str, ...]
    auth_test: Mapping[str, str]
    recommendations: tuple[Mapping[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "logo_key": self.logo_key,
            "base_url": self.base_url,
            "protocol": self.protocol,
            "litellm_provider": self.litellm_provider,
            "auth": dict(self.auth),
            "model_listing": dict(self.model_listing),
            "docs_url": self.docs_url,
            "key_url": self.key_url,
            "billing_dimensions": list(self.billing_dimensions),
            "auth_test": dict(self.auth_test),
            "recommendations": [dict(row) for row in self.recommendations],
        }


def load_provider_catalog(path: Path = CATALOG_DIR) -> dict[str, ProviderPreset]:
    presets: dict[str, ProviderPreset] = {}
    for file_path in sorted(path.glob("*.yml")):
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
        preset = normalize_provider_preset(raw, source=file_path)
        if preset.id in presets:
            raise ValueError(f"duplicate provider preset {preset.id!r}")
        presets[preset.id] = preset
    return presets


def normalize_provider_preset(raw: Mapping[str, Any], *, source: Path | str = "<memory>") -> ProviderPreset:
    preset_id = _required_id(raw, "id", source)
    protocol = str(raw.get("protocol") or "").strip()
    if protocol not in PROTOCOLS:
        raise ValueError(f"{source}: provider {preset_id!r} has unsupported protocol {protocol!r}")
    auth = raw.get("auth")
    if not isinstance(auth, Mapping) or not str(auth.get("header") or "").strip():
        raise ValueError(f"{source}: provider {preset_id!r} requires auth.header")
    listing = raw.get("model_listing")
    if not isinstance(listing, Mapping) or not str(listing.get("path") or "").startswith("/"):
        raise ValueError(f"{source}: provider {preset_id!r} requires model_listing.path")
    return ProviderPreset(
        id=preset_id,
        display_name=str(raw.get("display_name") or preset_id).strip(),
        logo_key=str(raw.get("logo_key") or preset_id).strip(),
        base_url=str(raw.get("base_url") or "").strip().rstrip("/"),
        protocol=protocol,
        litellm_provider=str(raw.get("litellm_provider") or "").strip(),
        auth={
            "header": str(auth.get("header") or "").strip(),
            "prefix": str(auth.get("prefix") or "").strip(),
        },
        model_listing={
            "method": str(listing.get("method") or "GET").strip().upper(),
            "path": str(listing.get("path") or "").strip(),
            "items_path": str(listing.get("items_path") if listing.get("items_path") is not None else "data").strip(),
            "id_path": str(listing.get("id_path") or "id").strip(),
            "name_path": str(listing.get("name_path") or listing.get("id_path") or "id").strip(),
            "prices": dict(listing.get("prices") if isinstance(listing.get("prices"), Mapping) else {}),
            **({"pricing_path": str(listing["pricing_path"]).strip()} if listing.get("pricing_path") else {}),
            **({"headers": dict(listing["headers"])} if isinstance(listing.get("headers"), Mapping) else {}),
        },
        docs_url=str(raw.get("docs_url") or "").strip(),
        key_url=str(raw.get("key_url") or "").strip(),
        billing_dimensions=tuple(str(item) for item in raw.get("billing_dimensions") or ()),
        auth_test=dict(raw.get("auth_test") if isinstance(raw.get("auth_test"), Mapping) else {}),
        recommendations=tuple(
            dict(item) for item in raw.get("recommendations") or () if isinstance(item, Mapping)
        ),
    )


def _required_id(raw: Mapping[str, Any], key: str, source: Path | str) -> str:
    value = str(raw.get(key) or "").strip()
    if not value:
        raise ValueError(f"{source}: provider preset requires {key}")
    if not value.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"{source}: provider preset id {value!r} is invalid")
    return value
