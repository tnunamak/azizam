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
        self.assertEqual(metadata["links"]["login"], "/login")
        self.assertEqual(metadata["links"]["user_portal"], "/")
        self.assertEqual(metadata["links"]["user_portal_alias"], "/me")
        self.assertEqual(metadata["links"]["user_session"], "/me/api/session")
        self.assertEqual(metadata["links"]["user_apps"], "/me/api/apps")
        self.assertEqual(metadata["links"]["user_app"], "/me/api/apps/{app_id}")
        self.assertEqual(metadata["links"]["media_jobs"], "/gateway/v1/media/jobs")
        self.assertEqual(
            metadata["links"]["user_api_key"],
            "/me/api/credentials/{credential_id}",
        )
        self.assertEqual(
            metadata["links"]["user_api_key_access"],
            "/me/api/credentials/{credential_id}/access",
        )
        self.assertEqual(
            metadata["links"]["user_access_request"],
            "/me/api/access-request",
        )
        self.assertEqual(
            metadata["absolute_links"]["capabilities"],
            "http://gw/gateway/v1/capabilities",
        )
        self.assertEqual(metadata["links"]["asset_content"], "/v1/assets/{asset_id}/content")

    def test_service_block_advertises_canonical_host_and_alias(self) -> None:
        metadata = build_gateway_discovery_metadata(base_url="http://gw")
        service = metadata["service"]
        self.assertEqual(service["product"], "Vivid Fish AI Gateway")
        self.assertEqual(service["canonical_host"], "https://ai.vivid.fish")
        self.assertIn("https://openai.vivid.fish", service["compatibility_aliases"])
        # Default service name no longer leaks the legacy product slug.
        self.assertNotEqual(service["name"], "openai-proxy")

    def test_metadata_does_not_contain_secret_values(self) -> None:
        rendered = repr(build_gateway_discovery_metadata(base_url="http://gw"))
        self.assertNotIn("proxy_api_keys.yml:", rendered)
        self.assertNotIn("sha256:", rendered)
        self.assertNotIn("Bearer opk_", rendered)

    def test_skill_catalog_points_at_skill(self) -> None:
        catalog = build_skill_catalog(base_url="http://gw")
        self.assertEqual(catalog["object"], "skill_catalog")
        # Skill ID remains `openai-proxy-gateway` for client/test compatibility.
        self.assertEqual(catalog["skills"][0]["id"], "openai-proxy-gateway")
        self.assertEqual(
            catalog["skills"][0]["url"],
            "http://gw/.well-known/skills/openai-proxy-gateway/SKILL.md",
        )

    def test_llms_txt_mentions_discovery_flow(self) -> None:
        text = build_llms_txt(base_url="http://gw")
        self.assertIn("/.well-known/ai-gateway", text)
        self.assertIn("/gateway/v1/capabilities", text)
        self.assertIn("/v1/assets/{asset_id}/content", text)
        self.assertIn("Vivid Fish AI Gateway", text)
        self.assertIn("https://ai.vivid.fish", text)
        self.assertIn("https://openai.vivid.fish", text)
        self.assertNotIn("opk_", text)


class TestLiveDiscoveryRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(proxy.app)

    def test_root_serves_user_portal(self) -> None:
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/html")
        self.assertIn("AI Gateway", response.text)

    def test_legacy_firehose_redirects_to_status_dashboard(self) -> None:
        response = self.client.get("/firehose", follow_redirects=False)
        self.assertEqual(response.status_code, 301, response.text)
        self.assertEqual(response.headers["location"], "/status")

    def test_well_known_ai_gateway_is_public(self) -> None:
        response = self.client.get("/.well-known/ai-gateway")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["object"], "ai_gateway_discovery")
        self.assertEqual(body["service"]["product"], "Vivid Fish AI Gateway")
        self.assertEqual(body["service"]["canonical_host"], "https://ai.vivid.fish")
        self.assertEqual(body["links"]["user_portal"], "/")
        self.assertEqual(body["links"]["user_portal_alias"], "/me")
        self.assertEqual(body["links"]["media_jobs"], "/gateway/v1/media/jobs")
        self.assertEqual(
            body["absolute_links"]["user_portal"],
            "http://testserver/",
        )
        self.assertEqual(
            body["absolute_links"]["user_portal_alias"],
            "http://testserver/me",
        )
        self.assertIn("/gateway/v1/capabilities", body["links"].values())

    def test_user_portal_alias_redirects_to_root(self) -> None:
        response = self.client.get("/me", follow_redirects=False)
        self.assertEqual(response.status_code, 302, response.text)
        self.assertEqual(response.headers["location"], "/")

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
        self.assertIn("Vivid Fish AI Gateway", skill.text)
        self.assertIn("/gateway/v1/capabilities", skill.text)

    def test_llms_txt_is_public(self) -> None:
        response = self.client.get("/llms.txt")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/plain")
        self.assertIn("/.well-known/ai-gateway", response.text)
        self.assertIn("Vivid Fish AI Gateway", response.text)

    def test_openapi_advertises_core_openai_compat_paths(self) -> None:
        response = self.client.get("/openapi.json")
        self.assertEqual(response.status_code, 200, response.text)
        paths = response.json()["paths"]
        self.assertIn("/v1/chat/completions", paths)
        self.assertIn("/v1/completions", paths)
        self.assertIn("/v1/embeddings", paths)
        self.assertIn("/gateway/v1/capabilities", paths)
        self.assertNotIn("/{path}", paths)

    def test_unmapped_v1_path_returns_404_not_backend_unavailable(self) -> None:
        response = self.client.get("/v1/capabilities")
        self.assertEqual(response.status_code, 404, response.text)
        body = response.json()
        self.assertEqual(body["error"]["code"], "not_found")
        self.assertNotEqual(body["error"]["type"], "service_unavailable")

    def test_backend_model_passthrough_is_not_classified_as_unmapped(self) -> None:
        self.assertFalse(proxy._should_return_not_found_before_backend("/v1/model"))
        self.assertFalse(proxy._should_return_not_found_before_backend("/v1/model/load"))

    def test_unmapped_discovery_probes_return_404(self) -> None:
        for path in ("/v1", "/manifest", "/.well-known/openapi"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(response.json()["error"]["code"], "not_found")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
