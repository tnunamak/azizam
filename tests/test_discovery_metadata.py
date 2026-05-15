"""Tests for public agent discovery metadata."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway.discovery_metadata import (  # noqa: E402
    build_gateway_discovery_metadata,
    build_llms_txt,
    build_skill_catalog,
)


class TestPureDiscoveryMetadata(unittest.TestCase):
    def test_metadata_has_protocols_auth_and_links(self) -> None:
        metadata = build_gateway_discovery_metadata(base_url="http://gw")
        self.assertEqual(metadata["object"], "ai_gateway_discovery")
        self.assertEqual(metadata["auth"]["token_prefix"], "opk_")
        self.assertIn("openai", {p["id"] for p in metadata["protocols"]})
        self.assertIn("anthropic", {p["id"] for p in metadata["protocols"]})
        self.assertIn("ollama", {p["id"] for p in metadata["protocols"]})
        self.assertEqual(
            metadata["absolute_links"]["capabilities"],
            "http://gw/gateway/v1/capabilities",
        )

    def test_metadata_does_not_contain_secret_values(self) -> None:
        rendered = repr(build_gateway_discovery_metadata(base_url="http://gw"))
        self.assertNotIn("proxy_api_keys.yml:", rendered)
        self.assertNotIn("sha256:", rendered)
        self.assertNotIn("Bearer opk_", rendered)

    def test_skill_catalog_points_at_skill(self) -> None:
        catalog = build_skill_catalog(base_url="http://gw")
        self.assertEqual(catalog["object"], "skill_catalog")
        self.assertEqual(
            catalog["skills"][0]["url"],
            "http://gw/.well-known/skills/openai-proxy-gateway/SKILL.md",
        )

    def test_llms_txt_mentions_discovery_flow(self) -> None:
        text = build_llms_txt(base_url="http://gw")
        self.assertIn("/.well-known/ai-gateway", text)
        self.assertIn("/gateway/v1/capabilities", text)
        self.assertNotIn("opk_", text)


class TestLiveDiscoveryRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(proxy.app)

    def test_well_known_ai_gateway_is_public(self) -> None:
        response = self.client.get("/.well-known/ai-gateway")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["object"], "ai_gateway_discovery")
        self.assertIn("/gateway/v1/capabilities", body["links"].values())

    def test_openai_proxy_alias_matches_discovery_shape(self) -> None:
        response = self.client.get("/.well-known/openai-proxy")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["object"], "ai_gateway_discovery")

    def test_skill_catalog_and_skill_are_public(self) -> None:
        catalog = self.client.get("/.well-known/skills")
        self.assertEqual(catalog.status_code, 200, catalog.text)
        href = catalog.json()["skills"][0]["href"]
        self.assertEqual(
            href, "/.well-known/skills/openai-proxy-gateway/SKILL.md"
        )

        skill = self.client.get(href)
        self.assertEqual(skill.status_code, 200, skill.text)
        self.assertIn("openai-proxy gateway", skill.text)
        self.assertIn("/gateway/v1/capabilities", skill.text)

    def test_llms_txt_is_public(self) -> None:
        response = self.client.get("/llms.txt")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/plain")
        self.assertIn("/.well-known/ai-gateway", response.text)

    def test_openapi_advertises_core_openai_compat_paths(self) -> None:
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200, response.text)
        paths = response.json()["paths"]
        self.assertIn("/v1/chat/completions", paths)
        self.assertIn("/v1/completions", paths)
        self.assertIn("/v1/embeddings", paths)
        self.assertIn("/gateway/v1/capabilities", paths)
        self.assertNotIn("/{path}", paths)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
