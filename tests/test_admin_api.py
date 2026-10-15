"""Tests for the admin-plane router."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import admin_store, extension_packages, service_registry  # noqa: E402
from tests.support.access import plan_for  # noqa: E402
from gateway.admin_api import (  # noqa: E402
    AdminAuthError,
    AdminContext,
    create_admin_router,
    derive_admin_health_projection,
    derive_admin_attention,
)
from gateway.admin_runtime import AdminSQLiteStore  # noqa: E402


class DeriveAdminAttentionTests(unittest.TestCase):
    def test_healthy_snapshot_yields_no_attention(self) -> None:
        snap = {
            "apps": [{"id": "a1", "name": "Good", "effective_access_plan_id": "p1"}],
            "credentials": [{"app_id": "a1", "revoked_at": None}],
        }
        self.assertEqual(derive_admin_attention(snap), [])

    def test_flags_app_and_service_problems_sorted_by_severity(self) -> None:
        snap = {
            "apps": [
                {"id": "a1", "name": "Disabled", "disabled_at": 1, "effective_access_plan_id": "p1"},
                {"id": "a2", "name": "MissingProf", "default_profile_id": "p_gone"},
                {"id": "a3", "name": "NoKey", "effective_access_plan_id": "p1"},
                {"id": "a4", "name": "Healthy", "effective_access_plan_id": "p1"},
                {"id": "a5", "name": "paperless", "effective_access_plan_id": "p1"},
            ],
            "credentials": [
                {"app_id": "a4", "revoked_at": None},
                {"app_id": "a5", "revoked_at": None},
            ],
            "services": [
                {"id": "comfy", "key": "comfy", "name": "ComfyUI", "status": "down", "tone": "bad"},
                {"id": "tabby", "key": "tabby", "name": "TabbyAPI", "status": "degraded", "tone": "warn"},
                {"id": "ok", "key": "ok", "name": "Fine", "status": "up", "tone": "ok"},
            ],
            "status": {
                "banner": "bad",
                "banner_reasons": ["required backend down"],
                "system_health": "warn",
                "system_health_reasons": ["disk / 91% full"],
                "auth_keys": [
                    {
                        "name": "paperless",
                        "app_id": "a5",
                        "requests_24h": 40,
                        "errors_4xx_24h": 3,
                        "errors_5xx_24h": 30,
                        "latency_ms_p50": 600_200,
                    },
                    {
                        "name": "brother-chat",
                        "requests_24h": 10,
                        "errors_4xx_24h": 5,
                        "errors_5xx_24h": 0,
                        "latency_ms_p50": 100,
                    }
                ],
            },
        }
        items = derive_admin_attention(snap)
        # Problems sort before attention; healthy app + up service produce nothing.
        severities = [i["severity"] for i in items]
        self.assertEqual(severities, sorted(severities, key=lambda s: 0 if s == "problem" else 1))
        titles = " | ".join(i["title"] for i in items)
        self.assertNotIn("Disabled is disabled", titles)
        self.assertIn("MissingProf has no access plan", titles)
        self.assertIn("NoKey has no active key", titles)
        self.assertIn("ComfyUI is down", titles)
        self.assertIn("TabbyAPI is degraded", titles)
        self.assertNotIn("API health is degraded", titles)
        self.assertNotIn("Host health needs attention", titles)
        self.assertIn("paperless has a high error rate", titles)
        self.assertIn("brother-chat has a high error rate", titles)
        self.assertIn("paperless is slow", titles)
        self.assertNotIn("Healthy", titles)
        self.assertNotIn("Fine", titles)
        # Target identity is canonical; names are display-only.
        by_title = {i["title"]: i for i in items}
        self.assertEqual(by_title["MissingProf has no access plan"]["target_kind"], "app")
        self.assertEqual(by_title["MissingProf has no access plan"]["target_id"], "a2")
        self.assertEqual(by_title["MissingProf has no access plan"]["target_name"], "MissingProf")
        self.assertEqual(
            by_title["MissingProf has no access plan"]["href"],
            "/admin/apps?app=a2",
        )
        self.assertEqual(by_title["ComfyUI is down"]["target_id"], "comfy")
        self.assertEqual(
            by_title["ComfyUI is down"]["href"],
            "/admin/services?service=comfy",
        )
        self.assertEqual(
            by_title["TabbyAPI is degraded"]["href"],
            "/admin/services?service=tabby",
        )
        self.assertEqual(
            by_title["paperless has a high error rate"]["href"],
            "/admin/apps?app=a5",
        )
        self.assertEqual(by_title["paperless has a high error rate"]["target_id"], "a5")
        self.assertEqual(
            by_title["paperless is slow"]["href"],
            "/admin/apps?app=a5",
        )
        self.assertNotIn("href", by_title["brother-chat has a high error rate"])
        self.assertNotIn("target_id", by_title["brother-chat has a high error rate"])
        for item in items:
            self.assertNotIn("entity_id", item)
            self.assertNotIn("entity_name", item)

    def test_auth_key_attention_does_not_guess_app_by_name(self) -> None:
        snap = {
            "apps": [{"id": "a1", "name": "paperless", "effective_access_plan_id": "p1"}],
            "credentials": [{"app_id": "a1", "revoked_at": None}],
            "status": {
                "auth_keys": [
                    {
                        "name": "paperless",
                        "requests_24h": 30,
                        "errors_4xx_24h": 0,
                        "errors_5xx_24h": 30,
                    }
                ],
            },
        }

        item = next(
            i for i in derive_admin_attention(snap)
            if i["title"] == "paperless has a high error rate"
        )
        self.assertNotIn("href", item)
        self.assertNotIn("target_id", item)

    def test_service_attention_does_not_link_without_resolved_service_id(self) -> None:
        snap = {
            "services": [
                {"key": "probe-only", "name": "Probe-only backend", "status": "down", "tone": "bad"},
            ],
        }

        item = derive_admin_attention(snap)[0]
        self.assertEqual(item["title"], "Probe-only backend is down")
        self.assertNotIn("href", item)
        self.assertNotIn("target_id", item)

    def test_current_health_distinguishes_unavailable_and_standby(self) -> None:
        registry = {
            "services": {
                "required": {
                    "id": "required",
                    "name": "Required",
                    "enabled": True,
                    "config": {
                        "models": [{"model_group": "public-model", "upstream_model": "upstream-model"}],
                    },
                },
                "standby": {"id": "standby", "name": "Standby", "enabled": True, "role": "standby", "lifecycle": {"driver": "systemd"}},
                "disabled": {"id": "disabled", "name": "Disabled", "enabled": False},
            }
        }
        unavailable = derive_admin_health_projection(registry, {}, observation_available=False)
        self.assertEqual(unavailable["state"], "unavailable")
        self.assertFalse(unavailable["observation_available"])
        self.assertEqual(unavailable["services"][0]["observation"], "unavailable")

        live = derive_admin_health_projection(
            registry,
            {"backends": [
                {"service_id": "required", "live_status": "up"},
                {"service_id": "standby", "live_status": "stopped"},
            ]},
            observation_available=True,
        )
        by_id = {service["service_id"]: service for service in live["services"]}
        self.assertEqual(live["state"], "ok")
        self.assertEqual(by_id["required"]["observation"], "up")
        self.assertEqual(by_id["standby"]["observation"], "standby_stopped")
        self.assertEqual(by_id["disabled"]["observation"], "disabled")

        incomplete = derive_admin_health_projection(
            registry,
            {"backends": [{"service_id": "required", "live_status": "up"}]},
            observation_available=True,
        )
        self.assertEqual(incomplete["state"], "unavailable")
        self.assertEqual(incomplete["label"], "Live status incomplete")

    def test_current_health_reduces_all_configuration_and_live_states(self) -> None:
        cases = (
            {
                "name": "empty configured registry",
                "registry": {"services": {}},
                "payload": {"banner": "bad", "backends": []},
                "available": True,
                "state": "empty",
                "tone": "neutral",
                "label": "No services configured",
            },
            {
                "name": "optional service down",
                "registry": {"services": {"optional": {"role": "optional"}}},
                "payload": {"backends": [{"key": "optional", "status": "down"}]},
                "available": True,
                "state": "warn",
                "tone": "warn",
                "observation": "down",
            },
            {
                "name": "disabled service ignores raw banner",
                "registry": {"services": {"disabled": {"enabled": False}}},
                "payload": {
                    "banner": "bad",
                    "banner_reasons": ["required backend down"],
                    "backends": [{"key": "disabled", "status": "down"}],
                },
                "available": True,
                "state": "inactive",
                "tone": "neutral",
                "observation": "disabled",
            },
            {
                "name": "required service down",
                "registry": {"services": {"required": {"role": "required"}}},
                "payload": {"backends": [{"key": "required", "status": "down"}]},
                "available": True,
                "state": "bad",
                "tone": "bad",
                "observation": "down",
            },
            {
                "name": "degraded service",
                "registry": {"services": {"degraded": {}}},
                "payload": {"backends": [{"key": "degraded", "status": "degraded"}]},
                "available": True,
                "state": "warn",
                "tone": "warn",
                "observation": "degraded",
            },
            {
                "name": "standby failure is observed down",
                "registry": {"services": {"standby": {"role": "standby"}}},
                "payload": {
                    "system_health": "bad",
                    "backends": [{"key": "standby", "status": "down"}],
                },
                "available": True,
                "state": "warn",
                "tone": "warn",
                "observation": "down",
            },
            {
                "name": "lifecycle-backed stopped service is idle",
                "registry": {
                    "services": {
                        "ondemand": {
                            "config": {"lifecycle": {"driver": "systemd"}},
                        },
                    },
                },
                "payload": {"backends": [{"key": "ondemand", "status": "stopped"}]},
                "available": True,
                "state": "inactive",
                "tone": "neutral",
                "observation": "idle",
            },
            {
                "name": "starting stays starting",
                "registry": {"services": {"starting": {}}},
                "payload": {"backends": [{"key": "starting", "status": "starting"}]},
                "available": True,
                "state": "warn",
                "tone": "warn",
                "observation": "starting",
            },
            {
                "name": "status source unavailable",
                "registry": {"services": {"required": {}}},
                "payload": {},
                "available": False,
                "state": "unavailable",
                "tone": "neutral",
                "observation": "unavailable",
            },
            {
                "name": "incomplete status source",
                "registry": {"services": {"required": {}}},
                "payload": {"backends": []},
                "available": True,
                "state": "unavailable",
                "tone": "neutral",
                "observation": "unavailable",
            },
            {
                "name": "empty model mappings leave global routing unknown",
                "registry": {"services": {"required": {"config": {"models": []}}}},
                "payload": {"backends": [{"key": "required", "status": "up"}]},
                "available": True,
                "state": "unavailable",
                "tone": "neutral",
                "label": "Live status incomplete",
                "observation": "up",
                "routable": None,
            },
            {
                "name": "live service without a routing declaration is incomplete",
                "registry": {"services": {"required": {"config": {"protocol": "openai-compatible"}}}},
                "payload": {"backends": [{"key": "required", "status": "up"}]},
                "available": True,
                "state": "unavailable",
                "tone": "neutral",
                "label": "Live status incomplete",
                "observation": "up",
                "routable": None,
            },
            {
                "name": "model mapping supplies a positive route declaration",
                "registry": {
                    "services": {
                        "required": {
                            "config": {
                                "models": [{"model_group": "public-model", "upstream_model": "upstream-model"}],
                            },
                        },
                    },
                },
                "payload": {"backends": [{"key": "required", "status": "up"}]},
                "available": True,
                "state": "ok",
                "tone": "ok",
                "observation": "up",
                "routable": True,
            },
            {
                "name": "empty model audio capability remains globally unknown",
                "registry": {
                    "services": {
                        "audio": {
                            "capabilities": ["audio.transcriptions"],
                            "config": {"models": []},
                        },
                    },
                },
                "payload": {"backends": [{"key": "audio", "status": "up"}]},
                "available": True,
                "state": "unavailable",
                "tone": "neutral",
                "label": "Live status incomplete",
                "observation": "up",
                "routable": None,
            },
            {
                "name": "empty model native pass-through remains globally unknown",
                "registry": {
                    "services": {
                        "provider": {
                            "config": {
                                "models": [],
                                "native_passthrough": True,
                            },
                        },
                    },
                },
                "payload": {"backends": [{"key": "provider", "status": "up"}]},
                "available": True,
                "state": "unavailable",
                "tone": "neutral",
                "label": "Live status incomplete",
                "observation": "up",
                "routable": None,
            },
        )

        for case in cases:
            with self.subTest(case["name"]):
                projection = derive_admin_health_projection(
                    case["registry"],
                    case["payload"],
                    observation_available=case["available"],
                )
                self.assertEqual(projection["state"], case["state"])
                self.assertEqual(projection["tone"], case["tone"])
                if "label" in case:
                    self.assertEqual(projection["label"], case["label"])
                if "observation" in case:
                    self.assertEqual(
                        projection["services"][0]["observation"], case["observation"]
                    )
                if "routable" in case:
                    self.assertEqual(
                        projection["services"][0]["routable"], case["routable"]
                    )

    def test_current_health_keeps_only_structured_independent_signals(self) -> None:
        registry = {
            "services": {
                "required": {
                    "config": {
                        "models": [{"model_group": "public-model", "upstream_model": "upstream-model"}],
                    },
                },
            },
        }
        live = {"backends": [{"key": "required", "status": "up"}]}

        host_bad = derive_admin_health_projection(
            registry,
            {
                **live,
                "banner": "ok",
                "system_health": "bad",
                "health_sources": {
                    "host": {"tone": "bad", "reasons": ["disk / 99% full"]},
                },
            },
            observation_available=True,
        )
        self.assertEqual((host_bad["state"], host_bad["tone"]), ("bad", "bad"))
        self.assertEqual(host_bad["signals"], [{"source": "host", "tone": "bad", "reasons": ["disk / 99% full"]}])

        api_warn = derive_admin_health_projection(
            registry,
            {
                **live,
                "banner": "warn",
                "health_sources": {
                    "api": {"tone": "warn", "reasons": ["model swap in progress"]},
                },
            },
            observation_available=True,
        )
        self.assertEqual((api_warn["state"], api_warn["tone"]), ("warn", "warn"))
        self.assertEqual(api_warn["signals"][0]["source"], "api")

        legacy_aggregate_only = derive_admin_health_projection(
            registry,
            {**live, "banner": "bad", "banner_reasons": ["required backend down"]},
            observation_available=True,
        )
        self.assertEqual((legacy_aggregate_only["state"], legacy_aggregate_only["tone"]), ("ok", "ok"))

    def test_current_health_keeps_known_severity_when_coverage_is_incomplete(self) -> None:
        required_down = derive_admin_health_projection(
            {
                "services": {
                    "unknown": {"config": {"models": []}},
                    "broken": {"config": {"models": [{"model_group": "mapped"}]}},
                },
            },
            {
                "backends": [
                    {"key": "unknown", "status": "up"},
                    {"key": "broken", "status": "down"},
                ],
            },
            observation_available=True,
        )
        self.assertEqual((required_down["state"], required_down["tone"]), ("bad", "bad"))
        self.assertEqual(required_down["incomplete"], {"routing": ["Unknown"]})
        self.assertIn("No route declaration for Unknown.", required_down["detail"])

        degraded_with_unobserved = derive_admin_health_projection(
            {
                "services": {
                    "degraded": {"config": {"models": [{"model_group": "mapped"}]}},
                    "missing": {"config": {"models": [{"model_group": "mapped"}]}},
                },
            },
            {"backends": [{"key": "degraded", "status": "degraded"}]},
            observation_available=True,
        )
        self.assertEqual(
            (degraded_with_unobserved["state"], degraded_with_unobserved["tone"]),
            ("warn", "warn"),
        )
        self.assertEqual(degraded_with_unobserved["incomplete"], {"observations": ["Missing"]})

        signal_with_unknown = derive_admin_health_projection(
            {"services": {"unknown": {"config": {"models": []}}}},
            {
                "backends": [{"key": "unknown", "status": "up"}],
                "health_sources": {
                    "api": {"tone": "warn", "reasons": ["maintenance in progress"]},
                },
            },
            observation_available=True,
        )
        self.assertEqual((signal_with_unknown["state"], signal_with_unknown["tone"]), ("warn", "warn"))
        self.assertEqual(signal_with_unknown["incomplete"], {"routing": ["Unknown"]})
        self.assertEqual(signal_with_unknown["signals"][0]["source"], "api")

    def test_current_health_routing_unknown_fixture_matches_producer_by_service_id(self) -> None:
        """Keep the shared frontend fixture aligned with the server projection."""
        fixture_path = ROOT / "web/tests/fixtures/gateway/current-health-routing-unknown.json"
        expected = json.loads(fixture_path.read_text(encoding="utf-8"))
        cases = {
            "routingUnknown": (
                {"services": {
                    "route-unknown": {
                        "id": "route-unknown",
                        "name": "Route declaration pending",
                        "config": {"models": []},
                    },
                }},
                {"backends": [{"service_id": "route-unknown", "live_status": "up"}]},
            ),
            "badWithRoutingUnknown": (
                {"services": {
                    "route-unknown": {
                        "id": "route-unknown",
                        "name": "Route declaration pending",
                        "config": {"models": []},
                    },
                    "required-down": {
                        "id": "required-down",
                        "name": "Required service down",
                        "config": {"models": [{"model_group": "mapped"}]},
                    },
                }},
                {"backends": [
                    {"service_id": "route-unknown", "live_status": "up"},
                    {"service_id": "required-down", "live_status": "down"},
                ]},
            ),
        }

        for case_id, (registry, payload) in cases.items():
            with self.subTest(case_id=case_id):
                projection = derive_admin_health_projection(
                    registry,
                    payload,
                    observation_available=True,
                )
                for row in projection["services"]:
                    self.assertIn("availability", row)
                    self.assertIn(row["availability"]["status"], {"ready", "unavailable"})
                comparable = {
                    **projection,
                    "services": [
                        {key: value for key, value in row.items() if key != "availability"}
                        for row in projection["services"]
                    ],
                }
                self.assertEqual(comparable, expected[case_id])
                self.assertEqual(
                    {row["service_id"] for row in projection["services"]},
                    {row["service_id"] for row in expected[case_id]["services"]},
                )

    def test_independent_health_attention_has_no_service_target(self) -> None:
        items = derive_admin_attention({
            "current_health": {
                "signals": [{
                    "source": "host",
                    "tone": "bad",
                    "reasons": ["disk / 99% full"],
                }],
            },
        })
        self.assertEqual(items[0]["title"], "host health needs attention")
        self.assertEqual(items[0]["detail"], "disk / 99% full")
        self.assertNotIn("target_id", items[0])
        self.assertNotIn("href", items[0])


class FakeAdminStore:
    """In-memory admin store used to exercise the router."""

    def __init__(self) -> None:
        self.users: list[dict[str, Any]] = [
            {
                "id": "u_owner",
                "email": "owner@example.com",
                "name": "Owner",
                "roles": ["admin"],
            }
        ]
        self.apps: dict[str, dict[str, Any]] = {}
        self.access_plans: list[dict[str, Any]] = [
            {"id": "plan_local", "name": "Local only"}
        ]
        self.credentials: dict[str, dict[str, Any]] = {}
        self.service_availability: dict[str, Any] = {}
        self.media_job_admission: dict[str, Any] = {}
        self.service_registry: dict[str, Any] = {
            "schema_version": 1,
            "services": {},
            "protocol_frontends": {},
            "workflows": {},
            "capabilities": {},
        }
        self.extension_package_index: dict[str, Any] = (
            extension_packages.empty_extension_package_index()
        )
        self.audit: list[dict[str, Any]] = []
        self._next_id = 1

    def _id(self, prefix: str) -> str:
        ident = f"{prefix}_{self._next_id}"
        self._next_id += 1
        return ident

    def list_users(self) -> list[dict[str, Any]]:
        return list(self.users)

    def list_apps(self) -> list[dict[str, Any]]:
        return list(self.apps.values())

    def get_app(self, app_id: str) -> dict[str, Any] | None:
        return self.apps.get(app_id)

    def create_app(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        app_id = self._id("app")
        record = {
            "id": app_id,
            "name": data["name"],
            "owner": data.get("owner") or actor.subject,
            "access_plan": data.get("access_plan"),
            "created_at": "2026-05-17T00:00:00Z",
        }
        self.apps[app_id] = record
        self.audit.append(
            {
                "at": "2026-05-17T00:00:00Z",
                "actor": actor.subject,
                "action": "app.create",
                "target": app_id,
            }
        )
        return dict(record)

    def update_app(
        self, *, app_id: str, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any] | None:
        record = self.apps.get(app_id)
        if record is None:
            return None
        if "name" in data:
            record["name"] = data["name"]
        if "access_plan" in data:
            record["access_plan"] = data["access_plan"]
        self.audit.append(
            {
                "at": "2026-05-17T01:00:00Z",
                "actor": actor.subject,
                "action": "app.update",
                "target": app_id,
            }
        )
        return dict(record)

    def disable_app(
        self, *, app_id: str, actor: AdminContext
    ) -> dict[str, Any] | None:
        record = self.apps.get(app_id)
        if record is None:
            return None
        record["disabled_at"] = "2026-05-17T01:00:00Z"
        self.audit.append(
            {
                "at": "2026-05-17T01:00:00Z",
                "actor": actor.subject,
                "action": "app.disable",
                "target": app_id,
            }
        )
        return dict(record)

    def enable_app(
        self, *, app_id: str, actor: AdminContext
    ) -> dict[str, Any] | None:
        record = self.apps.get(app_id)
        if record is None:
            return None
        record["disabled_at"] = None
        self.audit.append(
            {
                "at": "2026-05-17T02:00:00Z",
                "actor": actor.subject,
                "action": "app.enable",
                "target": app_id,
            }
        )
        return dict(record)

    def list_access_plans(self) -> list[dict[str, Any]]:
        return [dict(plan) for plan in self.access_plans]

    def get_access_plan(self, plan_id: str) -> dict[str, Any] | None:
        return next((dict(plan) for plan in self.access_plans if plan["id"] == plan_id), None)

    def create_access_plan(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        name = str(data.get("name") or "").strip()
        if any(plan["name"] == name for plan in self.access_plans):
            raise ValueError(f"access plan name {name!r} is already in use")
        record = {"id": self._id("plan"), "name": name}
        self.access_plans.append(record)
        self.audit.append({"at": "2026-05-17T00:00:00Z", "actor": actor.subject,
                           "action": "access_plan.create", "target": record["id"]})
        return dict(record)

    def update_access_plan(
        self, *, plan_id: str, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any] | None:
        record = next((plan for plan in self.access_plans if plan["id"] == plan_id), None)
        if record is None:
            return None
        record["name"] = str(data.get("name") or record["name"])
        self.audit.append({"at": "2026-05-17T01:00:00Z", "actor": actor.subject,
                           "action": "access_plan.update", "target": plan_id})
        return dict(record)

    def delete_access_plan(
        self, *, plan_id: str, actor: AdminContext
    ) -> dict[str, Any] | None:
        record = next((plan for plan in self.access_plans if plan["id"] == plan_id), None)
        if record is None:
            return None
        if any(app.get("access_plan") == plan_id for app in self.apps.values()):
            raise ValueError(f"{record['name']} is used by 1 app. Move it to another plan first.")
        self.access_plans.remove(record)
        self.audit.append({"at": "2026-05-17T01:00:00Z", "actor": actor.subject,
                           "action": "access_plan.delete", "target": plan_id})
        return dict(record)

    def list_credentials(self) -> list[dict[str, Any]]:
        # Records intentionally never carry raw key material.
        return [
            {k: v for k, v in c.items() if k != "api_key"}
            for c in self.credentials.values()
        ]

    def create_credential(
        self,
        *,
        app_id: str,
        data: Mapping[str, Any],
        actor: AdminContext,
    ) -> dict[str, Any]:
        cred_id = self._id("cred")
        raw_key = f"sk-test-{cred_id}-secret"
        stored = {
            "id": cred_id,
            "app_id": app_id,
            "label": data.get("label") or "api key",
            "display_name": data.get("label") or "api key",
            "prefix": raw_key[:11],
            "created_at": "2026-05-17T00:00:00Z",
            "revoked_at": None,
            "last_used_at": None,
        }
        self.credentials[cred_id] = stored
        self.audit.append(
            {
                "at": "2026-05-17T00:00:00Z",
                "actor": actor.subject,
                "action": "credential.create",
                "target": cred_id,
            }
        )
        # Caller of create_credential gets the one-time raw key.
        return {**stored, "api_key": raw_key}

    def create_admin_automation_key(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        cred_id = self._id("cred")
        raw_key = f"oak_test_{cred_id}_secret"
        stored = {
            "id": cred_id,
            "subject_kind": "service_account",
            "subject_id": self._id("svc"),
            "label": f"{data['name']} admin automation key",
            "prefix": raw_key[:18],
            "role": data.get("role") or "admin",
            "created_at": "2026-05-17T00:00:00Z",
            "revoked_at": None,
        }
        self.credentials[cred_id] = stored
        self.audit.append(
            {
                "at": "2026-05-17T00:00:00Z",
                "actor": actor.subject,
                "action": "admin_key.create",
                "target": cred_id,
            }
        )
        return {**stored, "api_key": raw_key}

    def revoke_credential(
        self, *, credential_id: str, actor: AdminContext
    ) -> dict[str, Any] | None:
        record = self.credentials.get(credential_id)
        if record is None:
            return None
        record["revoked_at"] = "2026-05-17T01:00:00Z"
        self.audit.append(
            {
                "at": "2026-05-17T01:00:00Z",
                "actor": actor.subject,
                "action": "credential.revoke",
                "target": credential_id,
            }
        )
        return dict(record)

    def operator_overrides(self) -> dict[str, Any]:
        return {
            "service_availability": dict(self.service_availability),
            "media_job_admission": dict(self.media_job_admission),
        }

    def update_service_availability_overrides(
        self, *, data: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        self.service_availability = dict(data)
        self.audit.append(
            {
                "at": "2026-05-17T01:00:00Z",
                "actor": actor.subject,
                "action": "operator.service_availability.update",
                "target": "service_availability_overrides",
            }
        )
        return dict(self.service_availability)

    def update_media_job_admission_overrides(
        self, *, data: Mapping[str, Any], actor: AdminContext, expected_revision: str | None = None
    ) -> dict[str, Any]:
        self.media_job_admission = dict(data)
        self.audit.append(
            {
                "at": "2026-05-17T01:00:00Z",
                "actor": actor.subject,
                "action": "operator.media_job_admission.update",
                "target": "media_job_admission_overrides",
            }
        )
        return {**self.media_job_admission, "revision": "os-1"}

    def get_service_registry(self) -> dict[str, Any]:
        return dict(self.service_registry)

    def _registry_write_result(self) -> dict[str, Any]:
        return {
            "value": dict(self.service_registry),
            "operator_settings_generation": 1,
        }

    def update_service_registry(
        self, *, data: Mapping[str, Any], actor: AdminContext,
        expected_generation: int,
    ) -> dict[str, Any]:
        del expected_generation
        self.service_registry = dict(data)
        self.audit.append(
            {
                "at": "2026-05-17T01:00:00Z",
                "actor": actor.subject,
                "action": "operator.service_registry.update",
                "target": "service_registry",
            }
        )
        return self._registry_write_result()

    # The single-service CRUD methods delegate to the real registry ops so the
    # route tests exercise actual validation, guards, and error mapping.
    def add_service(
        self, *, data: Mapping[str, Any], actor: AdminContext,
        expected_generation: int,
    ) -> dict[str, Any]:
        del expected_generation
        service_id = service_registry.normalize_id(data.get("id") or data.get("name"))
        raw = service_registry.service_form_to_raw(data)
        registry, _ = service_registry.upsert_service_in_registry(
            self.service_registry, service_id=service_id, raw=raw, source="ui"
        )
        self.service_registry = registry
        return self._registry_write_result()

    def update_service(
        self, *, service_id: str, data: Mapping[str, Any], actor: AdminContext,
        expected_generation: int,
    ) -> dict[str, Any]:
        del expected_generation
        raw = service_registry.service_form_to_raw(data)
        registry, _ = service_registry.upsert_service_in_registry(
            self.service_registry,
            service_id=service_id,
            raw=raw,
            source="ui",
            replace=True,
        )
        self.service_registry = registry
        return self._registry_write_result()

    def remove_service(
        self, *, service_id: str, actor: AdminContext,
        expected_generation: int,
    ) -> dict[str, Any]:
        del expected_generation
        self.service_registry = service_registry.remove_service_from_registry(
            self.service_registry, service_id
        )
        return self._registry_write_result()

    def set_service_enabled(
        self, *, service_id: str, enabled: bool, actor: AdminContext,
        expected_generation: int,
    ) -> dict[str, Any]:
        del expected_generation
        self.service_registry = service_registry.set_service_enabled(
            self.service_registry, service_id, enabled
        )
        return self._registry_write_result()

    def get_extension_package_index(self) -> dict[str, Any]:
        return dict(self.extension_package_index)

    def validate_extension_package_source(
        self, *, source: Mapping[str, Any], actor: AdminContext
    ) -> dict[str, Any]:
        del actor
        return extension_packages.validate_extension_package_source(source)

    def enable_extension_package(
        self, *, source: Mapping[str, Any], actor: AdminContext,
        expected_generation: int,
    ) -> dict[str, Any]:
        del expected_generation
        review = extension_packages.validate_extension_package_source(source)
        if not review.get("ok"):
            return {
                "extension_package": None,
                "review": review,
                "service_registry": self.service_registry,
                "extension_packages": self.extension_package_index,
            }
        imported = extension_packages.imported_registry_records(review)
        self.service_registry = service_registry.merge_imported_records(
            self.service_registry,
            services=imported["services"],
            protocol_frontends=imported["protocol_frontends"],
            workflows=imported["workflows"],
            capabilities=imported["capabilities"],
        )
        self.extension_package_index = extension_packages.merge_package_index(
            self.extension_package_index,
            review,
            source=source,
        )
        package_id = review["package"]["id"]
        self.audit.append(
            {
                "at": "2026-05-17T01:00:00Z",
                "actor": actor.subject,
                "action": "operator.extension_package.enable",
                "target": package_id,
            }
        )
        return {
            "extension_package": self.extension_package_index["packages"][package_id],
            "review": review,
            "service_registry": self.service_registry,
            "operator_settings_generation": 1,
            "extension_packages": self.extension_package_index,
        }

    def remove_extension_package(
        self, *, package_id: str, actor: AdminContext
    ) -> dict[str, Any]:
        try:
            self.extension_package_index = extension_packages.remove_extension_package(
                self.extension_package_index,
                package_id,
            )
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        self.audit.append(
            {
                "at": "2026-05-17T01:00:00Z",
                "actor": actor.subject,
                "action": "operator.extension_package.remove",
                "target": package_id,
            }
        )
        return {"extension_packages": self.extension_package_index}

    def recent_audit(self, *, limit: int) -> list[dict[str, Any]]:
        return list(self.audit[-limit:])

    def get_audit_event(self, event_id: str) -> dict[str, Any] | None:
        for event in self.audit:
            if event.get("id") == event_id:
                return event
        return None

    def list_audit_page(
        self,
        *,
        limit: int = 50,
        actor_id: str | None = None,
        action_prefix: str | None = None,
        exclude_access_audit: bool = False,
        include_internal: bool = False,
        include_automated: bool = False,
        before_created_at: int | None = None,
        before_id: str | None = None,
        target_kind: str | None = None,
        target_id: str | None = None,
    ) -> dict[str, Any]:
        """Minimal in-memory implementation of cursor-paged audit for route tests."""
        rows = list(self.audit)
        automated_count = sum(
            row.get("actor_kind") == "service_account" for row in rows
        )
        # Apply keyset cursor (audit rows in this fake store have a 'created_at'
        # and 'id' field when the real store is used; the fake rows may only
        # have partial fields — fall back gracefully).
        if before_created_at is not None and before_id is not None:
            rows = [
                r for r in rows
                if (
                    int(r.get("created_at", 0)) < before_created_at
                    or (
                        int(r.get("created_at", 0)) == before_created_at
                        and str(r.get("id", "")) < before_id
                    )
                )
            ]
        if actor_id is not None:
            rows = [r for r in rows if r.get("actor_id") == actor_id or r.get("actor") == actor_id]
        if action_prefix is not None:
            rows = [r for r in rows if str(r.get("action", "")).startswith(action_prefix)]
        if exclude_access_audit:
            rows = [r for r in rows if r.get("action") not in {"media.library.everyone", "usage.summary.everyone"}]
        if not include_internal:
            rows = [
                r for r in rows
                if r.get("action") not in {
                    "operator.safe_restart",
                    "operator.safe-restart",
                    "safe_restart",
                    "safe-restart",
                    "deploy.smoke",
                    "deploy_smoke",
                    "service_account.deploy_smoke",
                    "service-account.deploy-smoke",
                }
            ]
        if not include_automated:
            rows = [row for row in rows if row.get("actor_kind") != "service_account"]
        if target_kind is not None:
            rows = [r for r in rows if r.get("target_kind") == target_kind]
        if target_id is not None:
            rows = [r for r in rows if r.get("target_id") == target_id or r.get("target") == target_id]
        # Sort newest-first by (created_at DESC, id DESC); fall back to list order.
        rows_sorted = sorted(
            rows,
            key=lambda r: (int(r.get("created_at", 0)), str(r.get("id", ""))),
            reverse=True,
        )
        fetch = rows_sorted[: limit + 1]
        has_more = len(fetch) == limit + 1
        page = fetch[:limit]
        next_cursor: str | None = None
        if has_more and page:
            last = page[-1]
            next_cursor = f"{last.get('created_at', 0)}:{last.get('id', '')}"
        return {
            "records": page,
            "next_cursor": next_cursor,
            "automated_count": automated_count,
        }

    def setting_revision(self, key: str) -> str:
        return "os-1"


def _admin_auth(request: Request) -> AdminContext:
    header = request.headers.get("authorization", "")
    if header == "Bearer owner-token":
        return AdminContext(subject="u_owner", roles=frozenset({"owner"}))
    if header == "Bearer admin-token":
        return AdminContext(
            subject="u_owner",
            email="owner@example.com",
            roles=frozenset({"admin"}),
        )
    if header == "Bearer member-token":
        return AdminContext(subject="u_member", roles=frozenset({"member"}))
    if header == "Bearer viewer-token":
        return AdminContext(
            subject="u_viewer",
            email="viewer@example.com",
            roles=frozenset({"viewer"}),
        )
    raise AdminAuthError()


async def _fake_probe(host, port, path):
    # Deterministic stand-in for proxy.probe_backend in route tests: a host of
    # "reachable.test" passes, anything else reports a connection error.
    if host == "reachable.test":
        body = {
            "data": [
                {"id": "gpt-test-1", "object": "model"},
                {"id": "gpt-test-2", "object": "model"},
            ]
        }
        return {"ok": True, "status": 200, "error": "", "url": f"http://{host}:{port}{path}", "body": body}
    return {"ok": False, "status": None, "error": "Connection refused", "url": f"http://{host}:{port}{path}", "body": None}


def _make_client(
    store: FakeAdminStore | None = None,
    *,
    access_resolver=None,
    service_probe=_fake_probe,
    status_provider=None,
) -> tuple[TestClient, FakeAdminStore]:
    store = store or FakeAdminStore()
    app = FastAPI()
    app.include_router(
        create_admin_router(
            store,
            auth=_admin_auth,
            access_resolver=access_resolver,
            service_probe=service_probe,
            status_provider=status_provider,
        )
    )
    return TestClient(app), store


def _auth(role: str = "admin") -> dict[str, str]:
    # Registry writers now require this global snapshot precondition.  The
    # fake store deliberately holds generation 1 for ordinary route tests;
    # real-store CAS races have focused tests below.
    return {"Authorization": f"Bearer {role}-token", "If-Match": '"os-1"'}



class AdminHighRiskAuthorizationTests(unittest.TestCase):
    """Authorization must precede validation and every privileged side effect."""

    def _assert_denied_then_allowed(
        self, method, path, body, store_method, *, role="admin",
        code="owner_required", allowed_status=200, result=None,
    ) -> None:
        client, store = _make_client()
        # Keep the existing fake store but spy at the side-effect boundary.
        # A successful owner control rules out an invalid body or missing route.
        with patch.object(store, store_method, return_value=result) as operation:
            denied = client.request(method, path, json=body, headers=_auth(role))
            self.assertEqual(denied.status_code, 403, denied.text)
            self.assertEqual(denied.json()["error"]["code"], code)
            operation.assert_not_called()
            self.assertEqual(store.audit, [])
            allowed = client.request(method, path, json=body, headers=_auth("owner"))
            self.assertEqual(allowed.status_code, allowed_status, allowed.text)
            operation.assert_called_once()
            self.assertIn("owner", operation.call_args.kwargs["actor"].roles)

    def test_non_owner_cannot_create_access_plan(self) -> None:
        self._assert_denied_then_allowed(
            "POST", "/admin/api/access-plans", {"name": "Restricted plan"},
            "create_access_plan", allowed_status=201,
            result={"id": "plan_new", "name": "Restricted plan"},
        )

    def test_non_owner_cannot_update_access_plan(self) -> None:
        self._assert_denied_then_allowed(
            "PUT", "/admin/api/access-plans/plan_local", {"name": "Changed plan"},
            "update_access_plan", result={"id": "plan_local", "name": "Changed plan"},
        )

    def test_non_owner_cannot_delete_access_plan(self) -> None:
        self._assert_denied_then_allowed(
            "DELETE", "/admin/api/access-plans/plan_local", None,
            "delete_access_plan", result={"id": "plan_local", "name": "Local only"},
        )

    def _assert_provider_key_owner_only(self, path, body, operation_name, expected_status):
        # This client never contacts a sidecar or mints a real credential.
        from unittest.mock import AsyncMock, Mock

        key_client = Mock()
        operation = AsyncMock(return_value={"key_alias": "test-alias"})
        setattr(key_client, operation_name, operation)
        store = FakeAdminStore()
        app = FastAPI()
        app.include_router(create_admin_router(
            store, auth=_admin_auth, litellm_key_client=key_client,
        ))
        client = TestClient(app)
        denied = client.post(path, json=body, headers=_auth())
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"]["code"], "owner_required")
        operation.assert_not_called()
        self.assertEqual(store.audit, [])
        allowed = client.post(path, json=body, headers=_auth("owner"))
        self.assertEqual(allowed.status_code, expected_status, allowed.text)
        operation.assert_awaited_once()

    def test_non_owner_cannot_create_provider_key(self) -> None:
        self._assert_provider_key_owner_only(
            "/admin/api/operator/litellm/keys",
            {"key_alias": "test-alias", "models": ["test-model"], "max_budget": 1},
            "generate_key", 201,
        )

    def test_non_owner_cannot_delete_provider_keys(self) -> None:
        self._assert_provider_key_owner_only(
            "/admin/api/operator/litellm/keys/delete",
            {"key_aliases": ["test-alias"]}, "delete_keys", 200,
        )

    def test_member_cannot_create_admin_key(self) -> None:
        self._assert_denied_then_allowed(
            "POST", "/admin/api/admin-keys", {"name": "Automation", "role": "admin"},
            "create_admin_automation_key", role="member", code="admin_forbidden",
            allowed_status=201, result={"id": "cred_test", "name": "Automation"},
        )

    def test_member_cannot_replace_service_registry(self) -> None:
        self._assert_denied_then_allowed(
            "PUT", "/admin/api/operator/service-registry",
            {"services": {}, "protocol_frontends": {}, "workflows": {}, "capabilities": {}},
            "update_service_registry", role="member", code="admin_forbidden",
            result={"value": {}, "operator_settings_generation": 2},
        )

    def test_member_cannot_enable_extension_package(self) -> None:
        self._assert_denied_then_allowed(
            "POST", "/admin/api/extensions/enable",
            {"source": {"type": "local_directory", "path": "/unused-test-package"}},
            "enable_extension_package", role="member", code="admin_forbidden",
            result={"review": {"ok": True}, "operator_settings_generation": 2},
        )
class AdminAuthTests(unittest.TestCase):
    def test_missing_token_returns_401_json(self) -> None:
        client, _ = _make_client()
        resp = client.get("/admin/api/overview")
        self.assertEqual(resp.status_code, 401)
        body = resp.json()
        self.assertEqual(body["error"]["code"], "admin_unauthenticated")

    def test_non_admin_token_returns_403_json(self) -> None:
        client, _ = _make_client()
        resp = client.get(
            "/admin/api/overview",
            headers={"Authorization": "Bearer viewer-token"},
        )
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertEqual(body["error"]["code"], "admin_forbidden")

    def test_authenticated_admin_can_read_overview(self) -> None:
        client, _ = _make_client()
        resp = client.get("/admin/api/overview", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["actor"]["subject"], "u_owner")
        self.assertEqual(body["actor"]["roles"], ["admin"])
        self.assertIn("apps", body["counts"])
        self.assertEqual(body["counts"]["access_plans"], 1)
        self.assertEqual(
            body["access_model"]["nouns"],
            ["user", "app", "access_plan", "key", "admin_role"],
        )
        self.assertNotIn("runtime_mappings", body["access_model"])
        self.assertIn(
            "separately",
            body["access_model"]["admin_roles"]["relationship"],
        )
        # The seam exposes the "attention" list (things that want a look) so a
        # future Next.js admin overview can consume it. Empty here = all healthy.
        self.assertIn("attention", body)
        self.assertIsInstance(body["attention"], list)

    def test_overview_includes_service_counts(self) -> None:
        client, store = _make_client()
        store.service_registry = {
            "schema_version": 1,
            "services": {
                "openai": {"id": "openai", "name": "OpenAI", "enabled": True},
                "local-llm": {"id": "local-llm", "name": "Local LLM", "enabled": False},
                "comfy": {"id": "comfy", "name": "ComfyUI", "enabled": True},
            },
            "protocol_frontends": {},
            "workflows": {},
            "capabilities": {},
        }
        resp = client.get("/admin/api/overview", headers=_auth())
        self.assertEqual(resp.status_code, 200, resp.text)
        counts = resp.json()["counts"]
        self.assertEqual(counts["services_total"], 3)
        self.assertEqual(counts["services_enabled"], 2)
        self.assertEqual(counts["service_principals"], 0)
        self.assertEqual(resp.json()["current_health"]["state"], "unavailable")

    def test_overview_counts_service_principals_separately_from_people(self) -> None:
        client, store = _make_client()
        store.credentials["cred_service"] = {
            "id": "cred_service",
            "subject_kind": "service_account",
            "subject_id": "svc_automation",
            "revoked_at": None,
        }

        resp = client.get("/admin/api/overview", headers=_auth())
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["counts"]["users"], 1)
        self.assertEqual(resp.json()["counts"]["service_principals"], 1)

    def test_overview_links_live_service_attention_to_registry_id(self) -> None:
        async def status_provider() -> Mapping[str, Any]:
            return {
                "backends": [
                    {
                        "key": "svc_comfy",
                        "service_id": "svc_comfy",
                        "name": "ComfyUI",
                        "live_status": "down",
                        "last_error": "connection refused on port 8188",
                    },
                ],
            }

        client, store = _make_client(status_provider=status_provider)
        store.service_registry = {
            "schema_version": 1,
            "services": {
                "svc_comfy": {"id": "svc_comfy", "name": "ComfyUI", "enabled": True},
            },
            "protocol_frontends": {},
            "workflows": {},
            "capabilities": {},
        }

        resp = client.get("/admin/api/overview", headers=_auth())
        self.assertEqual(resp.status_code, 200, resp.text)
        by_title = {i["title"]: i for i in resp.json()["attention"]}
        self.assertEqual(
            by_title["ComfyUI is down"]["href"],
            "/admin/services?service=svc_comfy",
        )
        self.assertEqual(by_title["ComfyUI is down"]["target_id"], "svc_comfy")
        self.assertEqual(
            by_title["ComfyUI is down"]["detail"],
            "connection refused on port 8188",
        )

    def test_overview_does_not_link_unresolved_live_service_attention(self) -> None:
        async def status_provider() -> Mapping[str, Any]:
            return {
                "backends": [
                    {
                        "key": "probe-only",
                        "service_id": "probe-only",
                        "name": "Probe-only backend",
                        "live_status": "down",
                    },
                ],
            }

        client, _store = _make_client(status_provider=status_provider)

        resp = client.get("/admin/api/overview", headers=_auth())
        self.assertEqual(resp.status_code, 200, resp.text)
        by_title = {i["title"]: i for i in resp.json()["attention"]}
        item = by_title["Probe-only backend is down"]
        self.assertNotIn("href", item)
        self.assertNotIn("target_id", item)

    def test_admin_status_returns_cached_payload(self) -> None:
        async def status_provider() -> Mapping[str, Any]:
            return {"banner": "ok", "backends": [{"name": "ComfyUI"}]}

        client, _store = _make_client(status_provider=status_provider)

        resp = client.get("/admin/api/status", headers=_auth())
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.headers["cache-control"], "no-store")
        body = resp.json()["status"]
        self.assertEqual(body["banner"], "ok")
        self.assertEqual(body["backends"], [{"name": "ComfyUI"}])
        self.assertEqual(body["current_health"]["state"], "empty")
        self.assertTrue(body["current_health"]["observation_available"])

    def test_admin_status_normalizes_key_to_service_id_for_the_frontend_contract(self) -> None:
        """The React Status page keys its backend lookup by service_id
        (web/src/app/admin/status/page.tsx), but the real status provider's
        live probes (status_endpoint._probe) only guarantee ``key``. Without
        server-side normalization the frontend Map is built empty and no
        chart ever renders, even when the provider supplies real history."""
        history = {
            "uptime_pct_24h": 83.333,
            "avg_latency_ms_24h": 149,
            "status_series": [[1, 1.0], [2, 1.0], [3, 0.5]],
            "latency_series": [[1, 80], [2, 95], [3, 260]],
        }

        async def status_provider() -> Mapping[str, Any]:
            return {
                "banner": "ok",
                "backends": [
                    {"key": "fixture-required", "status": "up", "history": history},
                    {"name": "unidentifiable-probe", "status": "down"},
                ],
            }

        client, _store = _make_client(status_provider=status_provider)

        resp = client.get("/admin/api/status", headers=_auth())
        self.assertEqual(resp.status_code, 200, resp.text)
        backends = resp.json()["status"]["backends"]
        self.assertEqual(len(backends), 2)
        required = next(row for row in backends if row.get("key") == "fixture-required")
        self.assertEqual(required["service_id"], "fixture-required")
        self.assertEqual(required["history"], history)
        unidentifiable = next(row for row in backends if row.get("name") == "unidentifiable-probe")
        self.assertNotIn("service_id", unidentifiable)

    def test_admin_status_projects_unavailable_when_provider_missing(self) -> None:
        client, _store = _make_client()

        resp = client.get("/admin/api/status", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        health = resp.json()["status"]["current_health"]
        self.assertEqual(health["state"], "unavailable")
        self.assertFalse(health["observation_available"])

    def test_admin_status_projects_unavailable_when_provider_raises(self) -> None:
        async def status_provider() -> Mapping[str, Any]:
            raise RuntimeError("boom")

        client, _store = _make_client(status_provider=status_provider)

        resp = client.get("/admin/api/status", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"]["current_health"]["state"], "unavailable")

    def test_owner_role_is_admin_capable(self) -> None:
        def owner_auth(_request: Request) -> AdminContext:
            return AdminContext(
                subject="u_owner",
                email="owner@example.com",
                roles=frozenset({"owner"}),
            )

        app = FastAPI()
        app.include_router(create_admin_router(FakeAdminStore(), auth=owner_auth))
        resp = TestClient(app).get("/admin/api/overview")
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_cookie_session_mutations_require_admin_action_header(self) -> None:
        def session_auth(_request: Request) -> AdminContext:
            return AdminContext(
                subject="u_owner",
                email="owner@example.com",
                roles=frozenset({"owner"}),
                auth_method="session",
            )

        app = FastAPI()
        app.include_router(create_admin_router(FakeAdminStore(), auth=session_auth))
        client = TestClient(app)

        blocked = client.post("/admin/api/apps", json={"name": "brother-laptop"})
        self.assertEqual(blocked.status_code, 403, blocked.text)
        self.assertEqual(
            blocked.json()["error"]["code"],
            "admin_action_header_required",
        )

        allowed = client.post(
            "/admin/api/apps",
            json={"name": "brother-laptop"},
            headers={"x-azizam-admin-action": "1"},
        )
        self.assertEqual(allowed.status_code, 201, allowed.text)

    def test_cookie_session_mutations_require_csrf_when_available(self) -> None:
        def session_auth(_request: Request) -> AdminContext:
            return AdminContext(
                subject="u_owner",
                email="owner@example.com",
                roles=frozenset({"owner"}),
                auth_method="session",
                csrf_token="csrf-token",
            )

        app = FastAPI()
        app.include_router(create_admin_router(FakeAdminStore(), auth=session_auth))
        client = TestClient(app)

        action_only = client.post(
            "/admin/api/apps",
            json={"name": "brother-laptop"},
            headers={"x-azizam-admin-action": "1"},
        )
        self.assertEqual(action_only.status_code, 403, action_only.text)
        self.assertEqual(action_only.json()["error"]["code"], "admin_csrf_required")

        allowed = client.post(
            "/admin/api/apps",
            json={"name": "brother-laptop"},
            headers={
                "x-azizam-admin-action": "1",
                "x-azizam-csrf": "csrf-token",
            },
        )
        self.assertEqual(allowed.status_code, 201, allowed.text)


class AdminSessionTests(unittest.TestCase):
    """GET /admin/api/session — the JSON bootstrap for browser clients."""

    def test_session_requires_auth(self) -> None:
        client, _ = _make_client()
        resp = client.get("/admin/api/session")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "admin_unauthenticated")

    def test_session_returns_actor_identity_for_key_auth(self) -> None:
        client, _ = _make_client()
        resp = client.get("/admin/api/session", headers=_auth())
        self.assertEqual(resp.status_code, 200, resp.text)
        actor = resp.json()["actor"]
        self.assertEqual(actor["subject"], "u_owner")
        self.assertEqual(actor["email"], "owner@example.com")
        self.assertEqual(actor["roles"], ["admin"])
        # Bearer-key callers don't need CSRF; the field is present but empty.
        self.assertEqual(actor["csrf_token"], "")

    def test_session_names_service_admin_without_a_person_identity(self) -> None:
        app = FastAPI()
        app.include_router(create_admin_router(FakeAdminStore(), auth=lambda request: AdminContext(
            subject="automation", actor_kind="service_account", actor_label="CI admin",
            roles=frozenset({"admin"}),
        )))
        response = TestClient(app).get("/admin/api/session")
        assert response.status_code == 200
        assert response.json()["actor"]["kind"] == "service_account"
        assert response.json()["actor"]["label"] == "CI admin"

    def test_session_exposes_csrf_token_for_cookie_sessions(self) -> None:
        def session_auth(_request: Request) -> AdminContext:
            return AdminContext(
                subject="u_owner",
                email="owner@example.com",
                roles=frozenset({"owner"}),
                auth_method="session",
                csrf_token="csrf-token",
            )

        app = FastAPI()
        app.include_router(create_admin_router(FakeAdminStore(), auth=session_auth))
        resp = TestClient(app).get("/admin/api/session")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["actor"]["csrf_token"], "csrf-token")


class AdminCredentialListingTests(unittest.TestCase):
    """GET /admin/api/credentials — flat scrubbed list for the keys surface."""

    def test_list_credentials_requires_auth(self) -> None:
        client, _ = _make_client()
        resp = client.get("/admin/api/credentials")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "admin_unauthenticated")

    def test_list_credentials_returns_scrubbed_flat_list(self) -> None:
        client, _ = _make_client()
        app_id = client.post(
            "/admin/api/apps", json={"name": "brother-laptop"}, headers=_auth()
        ).json()["app"]["id"]
        minted = client.post(
            f"/admin/api/apps/{app_id}/keys",
            json={"label": "first key"},
            headers=_auth(),
        ).json()["credential"]

        resp = client.get("/admin/api/credentials", headers=_auth())
        self.assertEqual(resp.status_code, 200, resp.text)
        creds = resp.json()["credentials"]
        self.assertEqual(len(creds), 1)
        record = creds[0]
        self.assertEqual(record["id"], minted["id"])
        self.assertEqual(record["app_id"], app_id)
        # The Apps & Keys surface renders these; the store always has them.
        self.assertIn("prefix", record)
        self.assertIn("created_at", record)
        self.assertIn("last_used_at", record)
        # List responses never carry raw key material (the display prefix is
        # fine — it's what SecretMask renders — but the full key must not be).
        self.assertNotIn("api_key", record)
        self.assertNotIn(minted["api_key"], resp.text)


class AdminAppKeyFlowTests(unittest.TestCase):
    def test_create_app_then_mint_key_returns_one_time_key(self) -> None:
        client, store = _make_client()

        app_resp = client.post(
            "/admin/api/apps",
            json={"name": "brother-laptop"},
            headers=_auth(),
        )
        self.assertEqual(app_resp.status_code, 201, app_resp.text)
        app_id = app_resp.json()["app"]["id"]

        key_resp = client.post(
            f"/admin/api/apps/{app_id}/keys",
            json={"label": "first key"},
            headers=_auth(),
        )
        self.assertEqual(key_resp.status_code, 201, key_resp.text)
        cred = key_resp.json()["credential"]
        self.assertIn("api_key", cred)
        self.assertTrue(cred["api_key"].startswith("sk-test-"))
        self.assertEqual(cred["app_id"], app_id)

        # List endpoints must never return raw key material.
        list_resp = client.get("/admin/api/users", headers=_auth())
        self.assertNotIn("sk-test-", list_resp.text)
        # The fake store's list_credentials strips api_key; verify the admin
        # overview/audit endpoints also don't leak it.
        audit_resp = client.get("/admin/api/audit", headers=_auth())
        self.assertEqual(audit_resp.status_code, 200)
        self.assertNotIn("sk-test-", audit_resp.text)

    def test_create_key_for_unknown_app_returns_404(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/apps/app_missing/keys",
            json={},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "app_not_found")

    def test_create_app_requires_name(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/apps", json={}, headers=_auth()
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_app")

    def test_update_app_name_and_access_plan(self) -> None:
        client, store = _make_client()
        app_id = client.post(
            "/admin/api/apps", json={"name": "old"}, headers=_auth()
        ).json()["app"]["id"]

        resp = client.patch(
            f"/admin/api/apps/{app_id}",
            json={"name": "new", "access_plan": "plan_local"},
            headers=_auth(),
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["app"]["name"], "new")
        self.assertIn("app.update", [event["action"] for event in store.audit])

    def test_delete_app_disables_it(self) -> None:
        client, store = _make_client()
        app_id = client.post(
            "/admin/api/apps", json={"name": "old"}, headers=_auth()
        ).json()["app"]["id"]

        resp = client.delete(f"/admin/api/apps/{app_id}", headers=_auth())

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNotNone(resp.json()["app"]["disabled_at"])
        self.assertIn("app.disable", [event["action"] for event in store.audit])

    def test_enable_app_clears_disabled_at(self) -> None:
        client, store = _make_client()
        app_id = client.post(
            "/admin/api/apps", json={"name": "toggled"}, headers=_auth()
        ).json()["app"]["id"]
        # disable first
        client.delete(f"/admin/api/apps/{app_id}", headers=_auth())
        self.assertIsNotNone(store.apps[app_id]["disabled_at"])

        resp = client.post(f"/admin/api/apps/{app_id}/enable", headers=_auth())

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()["app"]["disabled_at"])
        self.assertIn("app.enable", [event["action"] for event in store.audit])

    def test_enable_already_active_app_is_idempotent(self) -> None:
        client, _ = _make_client()
        app_id = client.post(
            "/admin/api/apps", json={"name": "active"}, headers=_auth()
        ).json()["app"]["id"]

        resp = client.post(f"/admin/api/apps/{app_id}/enable", headers=_auth())

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIsNone(resp.json()["app"]["disabled_at"])

    def test_enable_unknown_app_returns_404(self) -> None:
        client, _ = _make_client()
        resp = client.post("/admin/api/apps/app_missing/enable", headers=_auth())
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "app_not_found")

    def test_enable_requires_admin(self) -> None:
        client, _ = _make_client()
        resp = client.post("/admin/api/apps/app_x/enable")
        self.assertEqual(resp.status_code, 401)


class AdminAutomationKeyFlowTests(unittest.TestCase):
    def test_admin_can_create_admin_automation_key(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/admin-keys",
            json={"name": "key-rotation-script", "role": "admin"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        cred = resp.json()["credential"]
        self.assertTrue(cred["api_key"].startswith("oak_"))
        self.assertEqual(cred["subject_kind"], "service_account")
        self.assertEqual(cred["role"], "admin")

        audit_resp = client.get("/admin/api/audit", headers=_auth())
        self.assertNotIn("oak_test_", audit_resp.text)
        self.assertIn("admin_key.create", audit_resp.text)

    def test_admin_automation_key_requires_name(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/admin-keys",
            json={},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_admin_key")

    def test_admin_automation_key_rejects_superadmin_role(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/admin-keys",
            json={"name": "too-powerful", "role": "superadmin"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_admin_key")
        self.assertIn("admin, owner", resp.json()["error"]["message"])

    def test_admin_role_cannot_mint_owner_key(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/admin-keys",
            json={"name": "too-powerful", "role": "owner"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "admin_role_forbidden")


class AdminCredentialRevocationTests(unittest.TestCase):
    def test_revoke_marks_credential_revoked(self) -> None:
        client, store = _make_client()
        client.post(
            "/admin/api/apps",
            json={"name": "brother-laptop"},
            headers=_auth(),
        )
        app_id = next(iter(store.apps))
        key_resp = client.post(
            f"/admin/api/apps/{app_id}/keys",
            json={},
            headers=_auth(),
        )
        cred_id = key_resp.json()["credential"]["id"]

        revoke_resp = client.post(
            f"/admin/api/credentials/{cred_id}/revoke",
            headers=_auth(),
        )
        self.assertEqual(revoke_resp.status_code, 200, revoke_resp.text)
        body = revoke_resp.json()
        self.assertIsNotNone(body["credential"]["revoked_at"])
        self.assertNotIn("api_key", body["credential"])

    def test_revoke_unknown_credential_returns_404(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/credentials/cred_missing/revoke",
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "credential_not_found")


class AdminAuditTests(unittest.TestCase):
    def test_service_account_activity_is_grouped_and_hidden_by_default(self) -> None:
        client, store = _make_client()
        store.audit.extend(
            [
                {
                    "id": "human-change",
                    "created_at": 1,
                    "actor_kind": "user",
                    "action": "operator.service_registry.backfill",
                },
                {
                    "id": "automated-key",
                    "created_at": 2,
                    "actor_kind": "service_account",
                    "actor_id": "svc_deploy",
                    "actor_label": "safe-restart",
                    "action": "credential.create",
                },
                {
                    "id": "automated-app",
                    "created_at": 3,
                    "actor_kind": "service_account",
                    "actor_id": "svc_deploy",
                    "actor_label": "safe-restart",
                    "action": "app.create",
                },
            ]
        )

        hidden = client.get("/admin/api/audit", headers=_auth()).json()
        self.assertEqual([row["id"] for row in hidden["records"]], ["human-change"])
        self.assertEqual(hidden["automated_count"], 2)

        shown = client.get(
            "/admin/api/audit?show_automated=true", headers=_auth()
        ).json()
        self.assertEqual(len(shown["records"]), 3)
        self.assertEqual(shown["automated_count"], 2)

    def test_audit_prefix_fields_accept_only_derived_public_prefixes(self) -> None:
        client, store = _make_client()
        public_prefix = "opk_" + "A" * 14
        full_app_key = "opk_" + "A" * 43
        full_admin_key = "oak_" + "B" * 43
        store.audit.append({
            "id": "aud_prefix", "created_at": 1, "action": "credential.create",
            "metadata": {
                "prefix": public_prefix,
                "credential_prefix": "oak_" + "C" * 14,
            },
        })
        store.audit.append({
            "id": "aud_rejected", "created_at": 2, "action": "credential.create",
            "metadata": {"prefix": full_app_key, "credential_prefix": full_admin_key},
        })
        response = client.get("/admin/api/audit", headers=_auth())
        self.assertEqual(response.status_code, 200, response.text)
        rejected = response.json()["records"][0]["metadata"]
        metadata = response.json()["records"][1]["metadata"]
        self.assertIsNone(rejected)
        self.assertEqual(metadata, {
            "prefix": public_prefix,
            "credential_prefix": "oak_" + "C" * 14,
        })
        self.assertNotIn(full_app_key, response.text)
        self.assertNotIn(full_admin_key, response.text)

    def test_audit_detail_drops_nested_and_unknown_secret_values(self) -> None:
        client, store = _make_client()
        store.audit.append({
            "id": "aud_safe",
            "created_at": 1,
            "actor_kind": "service_account",
            "actor_id": "not-a-prefix-contract",
            "actor_label": "Release bot",
            "action": "app.create",
            "metadata": {
                "name": "Bearer secret",
                "context": {"authorization": "Bearer nested-secret"},
                "innocuous": "also-secret",
            },
        })
        response = client.get(
            "/admin/api/audit?show_automated=true", headers=_auth()
        )
        self.assertEqual(response.status_code, 200, response.text)
        record = response.json()["records"][0]
        self.assertEqual(record["actor_kind"], "service_account")
        self.assertEqual(record["actor_label"], "Release bot")
        self.assertIsNone(record["metadata"])
        self.assertNotIn("nested-secret", response.text)
        self.assertNotIn("also-secret", response.text)
        self.assertNotIn("Bearer secret", response.text)

    def test_audit_returns_recent_entries(self) -> None:
        client, store = _make_client()
        client.post(
            "/admin/api/apps",
            json={"name": "brother-laptop"},
            headers=_auth(),
        )
        resp = client.get("/admin/api/audit", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # New response shape: {records, next_cursor}
        self.assertIn("records", body)
        entries = body["records"]
        self.assertTrue(any(e.get("action") == "app.create" for e in entries))

    def test_audit_page_size_respected(self) -> None:
        client, store = _make_client()
        # Populate the audit log directly with created_at so the fake store
        # sorts correctly.
        for i in range(10):
            store.audit.append({"action": f"event_{i}", "created_at": i, "id": f"aud_{i}"})
        resp = client.get("/admin/api/audit?limit=3", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("records", body)
        self.assertEqual(len(body["records"]), 3)

    def test_audit_event_lookup_by_id(self) -> None:
        client, store = _make_client()
        store.audit.append({
            "action": "old_event",
            "created_at": 1,
            "id": "aud_old",
            "actor_id": "usr_old",
        })
        resp = client.get("/admin/api/audit/aud_old", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["event"]["id"], "aud_old")
        self.assertEqual(resp.json()["event"]["action"], "old_event")

    def test_audit_event_lookup_404_for_missing(self) -> None:
        client, _store = _make_client()
        resp = client.get("/admin/api/audit/aud_missing", headers=_auth())
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "audit_event_not_found")

    def test_audit_pagination_reaches_oldest_event(self) -> None:
        """Paging via next_cursor must reach events older than the first page."""
        client, store = _make_client()
        # Seed 260 audit events with distinct created_at + id values so all
        # pages can be distinguished.  The oldest event has action "oldest_event".
        for i in range(259):
            store.audit.append({
                "action": f"bulk_event_{i}",
                "created_at": 1000 + i,
                "id": f"aud_{i:04d}",
                "actor_id": "usr_bulk",
            })
        store.audit.append({
            "action": "oldest_event",
            "created_at": 999,
            "id": "aud_oldest",
            "actor_id": "usr_special",
        })
        # Page through with limit=50 until exhausted.
        seen_actions: list[str] = []
        cursor: str | None = None
        iterations = 0
        while True:
            url = "/admin/api/audit?limit=50"
            if cursor:
                url += f"&before={cursor}"
            resp = client.get(url, headers=_auth())
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            page = body["records"]
            seen_actions.extend(r.get("action", "") for r in page)
            cursor = body["next_cursor"]
            iterations += 1
            if cursor is None:
                break
            # Safety guard — should never need more than 10 pages for 260 rows.
            self.assertLess(iterations, 10, "cursor pagination did not terminate")
        self.assertIn("oldest_event", seen_actions)

    def test_audit_actor_id_filter_reaches_old_events(self) -> None:
        """actor_id filter must return matching events older than the first 200 rows.

        This is the regression test for the deception bug: filtering by actor
        client-side over a 200-row window silently omits older matching events.
        """
        client, store = _make_client()
        # Flood 210 events from a common actor so a naive 200-row window fills up.
        for i in range(210):
            store.audit.append({
                "action": f"noise_event_{i}",
                "created_at": 2000 + i,
                "id": f"aud_noise_{i:04d}",
                "actor_id": "usr_noise",
            })
        # Add a target event from a different actor at an older timestamp.
        store.audit.append({
            "action": "target_action",
            "created_at": 1999,
            "id": "aud_target",
            "actor_id": "usr_target",
        })
        # actor_id filter must find the target event even though it's older
        # than the 200 newest events.
        resp = client.get("/admin/api/audit?actor_id=usr_target", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        actions = [r.get("action") for r in body["records"]]
        self.assertIn("target_action", actions)
        # Must NOT include the noise events.
        for a in actions:
            self.assertEqual(a, "target_action")

    def test_audit_action_prefix_filter(self) -> None:
        client, store = _make_client()
        store.audit.append({"action": "app.create", "created_at": 100, "id": "a1", "actor_id": "u"})
        store.audit.append({"action": "service.update", "created_at": 101, "id": "a2", "actor_id": "u"})
        store.audit.append({"action": "app.disable", "created_at": 102, "id": "a3", "actor_id": "u"})
        resp = client.get("/admin/api/audit?action_prefix=app.", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        actions = [r.get("action") for r in resp.json()["records"]]
        self.assertIn("app.create", actions)
        self.assertIn("app.disable", actions)
        self.assertNotIn("service.update", actions)

    def test_audit_can_hide_owner_access_views_without_breaking_pagination(self) -> None:
        client, store = _make_client()
        store.audit.extend([
            {"action": "media.library.everyone", "created_at": 3, "id": "access-media"},
            {"action": "usage.summary.everyone", "created_at": 2, "id": "access-usage"},
            {"action": "app.create", "created_at": 1, "id": "app-create"},
        ])
        default = client.get("/admin/api/audit?limit=10&exclude_access_audit=true", headers=_auth())
        self.assertEqual(default.status_code, 200, default.text)
        self.assertEqual([row["action"] for row in default.json()["records"]], ["app.create"])
        self.assertIsNone(default.json()["next_cursor"])

        revealed = client.get("/admin/api/audit?limit=10", headers=_auth())
        self.assertEqual(
            [row["action"] for row in revealed.json()["records"][:2]],
            ["media.library.everyone", "usage.summary.everyone"],
        )

    def test_audit_next_cursor_null_at_exhaustion(self) -> None:
        client, store = _make_client()
        for i in range(5):
            store.audit.append({"action": f"ev_{i}", "created_at": i, "id": f"aud_{i}"})
        # With limit=10 and only 5 rows, next_cursor must be null.
        resp = client.get("/admin/api/audit?limit=10", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["next_cursor"])

    def test_audit_filter_and_cursor_compose(self) -> None:
        """action_prefix filter must still work when a before cursor is supplied."""
        client, store = _make_client()
        for i in range(10):
            store.audit.append({
                "action": "app.create" if i % 2 == 0 else "service.update",
                "created_at": 1000 + i,
                "id": f"aud_{i:03d}",
                "actor_id": "u",
            })
        # First page — limit 3 with filter.
        resp1 = client.get("/admin/api/audit?action_prefix=app.&limit=3", headers=_auth())
        self.assertEqual(resp1.status_code, 200)
        body1 = resp1.json()
        # 5 matching events total; page 1 returns 3, next_cursor non-null.
        self.assertEqual(len(body1["records"]), 3)
        cursor = body1["next_cursor"]
        self.assertIsNotNone(cursor)
        # Second page.
        resp2 = client.get(
            f"/admin/api/audit?action_prefix=app.&limit=3&before={cursor}",
            headers=_auth(),
        )
        self.assertEqual(resp2.status_code, 200)
        body2 = resp2.json()
        # Remaining 2 events on page 2, no further cursor.
        self.assertEqual(len(body2["records"]), 2)
        self.assertIsNone(body2["next_cursor"])
        # All records should match the filter.
        for r in body2["records"]:
            self.assertEqual(r["action"], "app.create")

    def test_audit_bad_cursor_returns_400(self) -> None:
        client, _ = _make_client()
        resp = client.get("/admin/api/audit?before=notavalidcursor", headers=_auth())
        self.assertEqual(resp.status_code, 400)


class AdminOperatorOverrideTests(unittest.TestCase):
    def test_admin_can_write_and_read_operator_overrides(self) -> None:
        client, store = _make_client()
        service_resp = client.put(
            "/admin/api/operator/service-availability",
            json={
                "overrides": {
                    "openai": {
                        "state": "degraded",
                        "reason": "provider incident",
                        "retry_after_seconds": 120,
                    }
                }
            },
            headers=_auth(),
        )
        media_resp = client.put(
            "/admin/api/operator/media-job-admission",
            json={
                "disabled_intents": ["video.generate"],
                "disabled_resource_classes": ["gpu"],
                "reserved_tokens": ["gpu"],
                "max_queue_depth": 5,
                "retry_after_seconds": 30,
            },
            headers=_auth(),
        )
        list_resp = client.get("/admin/api/operator/overrides", headers=_auth())

        self.assertEqual(service_resp.status_code, 200, service_resp.text)
        self.assertEqual(media_resp.status_code, 200, media_resp.text)
        self.assertEqual(list_resp.status_code, 200, list_resp.text)
        overrides = list_resp.json()["overrides"]
        self.assertNotIn("user_access_defaults", overrides)
        self.assertEqual(
            overrides["service_availability"]["openai"]["state"],
            "degraded",
        )
        self.assertEqual(
            overrides["media_job_admission"]["disabled_intents"],
            ["video.generate"],
        )
        self.assertEqual(
            [event["action"] for event in store.audit[-2:]],
            [
                "operator.service_availability.update",
                "operator.media_job_admission.update",
            ],
        )

    def test_service_availability_updates_merge_and_clear_defaults(self) -> None:
        client, store = _make_client()
        store.service_availability = {
            "openai": {
                "state": "maintenance",
                "reason": "planned",
                "retry_after_seconds": 60,
            },
            "comfyui": {
                "state": "disabled",
                "reason": None,
                "retry_after_seconds": None,
            },
        }

        update_resp = client.put(
            "/admin/api/operator/service-availability",
            json={
                "overrides": {
                    "openai": {
                        "state": "degraded",
                        "reason": "slow",
                    }
                }
            },
            headers=_auth(),
        )
        clear_resp = client.put(
            "/admin/api/operator/service-availability",
            json={"overrides": {"openai": {"state": "available"}}},
            headers=_auth(),
        )

        self.assertEqual(update_resp.status_code, 200, update_resp.text)
        self.assertEqual(clear_resp.status_code, 200, clear_resp.text)
        self.assertNotIn("openai", store.service_availability)
        self.assertEqual(
            store.service_availability["comfyui"]["state"],
            "disabled",
        )

    def test_admin_can_read_service_registry(self) -> None:
        client, store = _make_client()
        store.service_registry = {
            "schema_version": 1,
            "services": {
                "openai": {
                    "id": "openai",
                    "name": "OpenAI",
                    "kind": "provider",
                    "source": "provisioned",
                    "enabled": True,
                    "capabilities": ["chat.completions"],
                }
            },
            "protocol_frontends": {},
        }

        resp = client.get(
            "/admin/api/operator/service-registry",
            headers=_auth(),
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        registry = resp.json()["service_registry"]
        self.assertEqual(registry["services"]["openai"]["name"], "OpenAI")
        self.assertEqual(registry["protocol_frontends"], {})

    def test_admin_can_update_service_registry(self) -> None:
        client, store = _make_client()

        resp = client.put(
            "/admin/api/operator/service-registry",
            json={
                "services": {
                    "Open AI": {
                        "name": "OpenAI",
                        "source": "provisioned",
                        "capabilities": ["chat.completions"],
                        "config": {"base_url": "https://api.openai.com/v1"},
                    }
                },
                "protocol_frontends": {
                    "OpenAI API": {
                        "name": "OpenAI-compatible API",
                        "protocol": "openai",
                        "routes": ["/v1/chat/completions"],
                        "intents": ["chat.completions"],
                        "canonical": True,
                    }
                },
            },
            headers=_auth(),
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        registry = resp.json()["service_registry"]
        self.assertEqual(sorted(registry["services"]), ["open-ai"])
        self.assertEqual(registry["services"]["open-ai"]["id"], "open-ai")
        self.assertEqual(
            registry["services"]["open-ai"]["config"]["base_url"],
            "https://api.openai.com/v1",
        )
        self.assertEqual(
            registry["protocol_frontends"]["openai-api"]["routes"],
            ["/v1/chat/completions"],
        )
        self.assertEqual(store.service_registry, registry)
        self.assertIn(
            "operator.service_registry.update",
            [event["action"] for event in store.audit],
        )

    def test_admin_can_get_generated_litellm_config(self) -> None:
        client, store = _make_client()
        store.service_registry = {
            "schema_version": 1,
            "services": {
                "litellm": {
                    "id": "litellm",
                    "name": "LiteLLM",
                    "kind": "llm-substrate",
                    "enabled": True,
                    "capabilities": ["llm.execution"],
                    "config": {
                        "base_url": "http://127.0.0.1:4011",
                        "execution_substrate": "litellm",
                    },
                },
                "local-llama": {
                    "id": "local-llama",
                    "name": "Local llama.cpp",
                    "kind": "provider",
                    "enabled": True,
                    "capabilities": ["openai"],
                    "config": {
                        "protocol": "openai-compatible",
                        "base_url": "http://127.0.0.1:8081/v1",
                        "api_key_env": "LOCAL_LLM_KEY",
                        "credential_origin": "http://127.0.0.1:8081",
                        "models": [
                            {
                                "id": "assistant-default",
                                "upstream_model": "qwen-27b",
                            }
                        ],
                    },
                }
            },
            "protocol_frontends": {},
            "workflows": {},
            "capabilities": {},
        }
        resp = client.get(
            "/admin/api/operator/litellm-config",
            headers=_auth(),
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        config = resp.json()["litellm_config"]
        self.assertEqual(config["model_list"][0]["model_name"], "assistant-default")
        self.assertEqual(
            config["model_list"][0]["litellm_params"]["api_base"],
            "http://127.0.0.1:8081/v1",
        )
        self.assertEqual(
            config["model_list"][0]["litellm_params"]["api_key"],
            "os.environ/LOCAL_LLM_KEY",
        )
        self.assertNotIn("metadata", config)

    def test_admin_can_list_validate_and_enable_extension_package(self) -> None:
        client, store = _make_client()
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package_dir = root / "package"
            (package_dir / "objects").mkdir(parents=True)
            (package_dir / "extension.yml").write_text(
                """
schema_version: 1
kind: extension_package
id: demo-package
version: 0.1.0
objects:
  services:
    - objects/service.yml
  protocol_frontends:
    - objects/frontend.yml
  workflows:
    - objects/workflow.yml
  capabilities:
    - objects/capability.yml
""",
                encoding="utf-8",
            )
            (package_dir / "objects" / "service.yml").write_text(
                """
id: demo-service
name: Demo Service
config:
  base_url: http://127.0.0.1:9999
""",
                encoding="utf-8",
            )
            (package_dir / "objects" / "frontend.yml").write_text(
                """
id: demo-api
name: Demo API
protocol: demo
routes:
  - /demo/v1
""",
                encoding="utf-8",
            )
            (package_dir / "objects" / "workflow.yml").write_text(
                """
schema_version: 1
id: image.generate.demo
intent: image.generate
inputs:
  prompt:
    type: string
outputs:
  image:
    media_type: image/png
bindings:
  comfyui:
    workflow: workflows/demo.json
    map:
      prompt:
        - node: "6"
          input: text
""",
                encoding="utf-8",
            )
            (package_dir / "objects" / "capability.yml").write_text(
                """
kind: capability
id: demo-capability
intent: image.generate
backend: comfyui
workflow: image.generate.demo
input_modalities: [text]
output_modalities: [image]
execution: job
""",
                encoding="utf-8",
            )
            source = {"type": "local_directory", "path": str(package_dir)}

            list_resp = client.get("/admin/api/extensions", headers=_auth())
            validate_resp = client.post(
                "/admin/api/extensions/validate",
                json={"source": source},
                headers=_auth(),
            )
            enable_resp = client.post(
                "/admin/api/extensions/enable",
                json={"source": source},
                headers=_auth(),
            )

        self.assertEqual(list_resp.status_code, 200, list_resp.text)
        self.assertEqual(validate_resp.status_code, 200, validate_resp.text)
        self.assertTrue(validate_resp.json()["review"]["ok"])
        self.assertEqual(enable_resp.status_code, 200, enable_resp.text)
        body = enable_resp.json()
        self.assertEqual(body["extension_package"]["id"], "demo-package")
        self.assertIn("demo-service", body["service_registry"]["services"])
        self.assertIn("demo-api", body["service_registry"]["protocol_frontends"])
        self.assertIn("image.generate.demo", body["service_registry"]["workflows"])
        self.assertIn("demo-capability", body["service_registry"]["capabilities"])
        self.assertIn("demo-package", store.extension_package_index["packages"])
        self.assertIn(
            "operator.extension_package.enable",
            [event["action"] for event in store.audit],
        )

    def test_enable_extension_package_rejects_invalid_package(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/extensions/enable",
            json={"source": {"type": "local_directory", "path": "/no/such/package"}},
            headers=_auth(),
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_extension_package")

    def test_extension_enable_pins_reviewed_resolved_git_ref(self) -> None:
        client, store = _make_client()
        calls: list[dict[str, Any]] = []

        def fake_review(source: Mapping[str, Any], **_: Any) -> dict[str, Any]:
            calls.append(dict(source))
            return {
                "package": {
                    "schema_version": 1,
                    "kind": "extension_package",
                    "id": "git-reviewed-package",
                    "version": "0.1.0",
                },
                "package_hash": "a" * 64,
                "services": [],
                "protocol_frontends": [],
                "capabilities": [],
                "workflows": [],
                "adapters": [],
                "translators": [],
                "code_objects": [],
                "requires_code_trust": False,
                "warnings": [],
                "errors": [],
                "ok": True,
                "schema_version": 1,
                "kind": "extension_package",
                "id": "git-reviewed-package",
                "version": "0.1.0",
                "object_counts": {
                    "services": 0,
                    "protocol_frontends": 0,
                    "capabilities": 0,
                    "workflows": 0,
                },
                "resolved_source": {
                    "type": "git",
                    "url": "/tmp/repo",
                    "ref": "main",
                    "resolved_ref": source.get("resolved_ref") or "newer-commit",
                },
            }

        with patch.object(
            extension_packages,
            "validate_extension_package_source",
            side_effect=fake_review,
        ):
            resp = client.post(
                "/admin/api/extensions/enable",
                json={
                    "source": {"type": "git", "url": "/tmp/repo", "ref": "main"},
                    "review": {
                        "resolved_source": {
                            "type": "git",
                            "url": "/tmp/repo",
                            "ref": "main",
                            "resolved_ref": "reviewed-commit",
                        }
                    },
                },
                headers=_auth(),
            )

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(calls, [
            {
                "type": "git",
                "url": "/tmp/repo",
                "ref": "main",
                "resolved_ref": "reviewed-commit",
            }
        ])
        self.assertEqual(
            store.extension_package_index["packages"]["git-reviewed-package"]["source"][
                "resolved_ref"
            ],
            "reviewed-commit",
        )

    def test_admin_can_remove_extension_package_from_index(self) -> None:
        client, store = _make_client()
        store.extension_package_index = {
            "packages": {
                "demo-package": {
                    "id": "demo-package",
                    "version": "0.1.0",
                    "enabled": True,
                    "source": {"type": "local_directory", "path": "/tmp/demo"},
                }
            },
            "sources": [],
            "updated_at": 1,
        }

        resp = client.delete(
            "/admin/api/extensions/demo-package",
            headers=_auth(),
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertNotIn("demo-package", resp.json()["extension_packages"]["packages"])
        self.assertNotIn("demo-package", store.extension_package_index["packages"])
        self.assertIn(
            "operator.extension_package.remove",
            [event["action"] for event in store.audit],
        )

    def test_remove_extension_package_returns_404_for_unknown_id(self) -> None:
        client, _ = _make_client()

        resp = client.delete(
            "/admin/api/extensions/missing-package",
            headers=_auth(),
        )

        self.assertEqual(resp.status_code, 404, resp.text)
        self.assertEqual(
            resp.json()["error"]["code"],
            "extension_package_not_found",
        )

    def test_operator_override_write_requires_admin(self) -> None:
        client, _ = _make_client()
        resp = client.put(
            "/admin/api/operator/media-job-admission",
            json={"disabled_intents": []},
        )
        self.assertEqual(resp.status_code, 401)

    def test_operator_override_validation_rejects_bad_values(self) -> None:
        client, _ = _make_client()
        service_resp = client.put(
            "/admin/api/operator/service-availability",
            json={"openai": {"state": "paused"}},
            headers=_auth(),
        )
        media_resp = client.put(
            "/admin/api/operator/media-job-admission",
            json={"reserved_tokens": "gpu"},
            headers=_auth(),
        )

        self.assertEqual(service_resp.status_code, 400)
        self.assertEqual(
            service_resp.json()["error"]["code"],
            "invalid_operator_override",
        )
        self.assertEqual(media_resp.status_code, 400)
        self.assertEqual(
            media_resp.json()["error"]["code"],
            "invalid_operator_override",
        )


class AdminAccessPlanRouteTests(unittest.TestCase):
    """Plans and app assignments over HTTP, against the real SQLite store."""

    PLAN = {
        "name": "Family",
        "can_use": {"chat": {"items": "all"}},
        "direct_engine_access": {"engines": [], "operations": []},
        "paid_providers": [],
        "provider_fallback": False,
        "caps": {"usd_per_request": None, "usd_per_day": None, "usd_per_month": None},
        "can_load_models": False,
        "receipt_detail": "basic",
        "media_metadata": "none",
    }

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "admin.db"
        admin_store.init_db(self.db)
        self.store = AdminSQLiteStore(self.db)
        self.owner = admin_store.upsert_oidc_user(
            "https://idp.test", "owner", email="owner@example.test", path=self.db)
        app = FastAPI()
        app.include_router(create_admin_router(self.store, auth=_admin_auth))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _plan(self, **changes) -> dict:
        response = self.client.post("/admin/api/access-plans", json={**self.PLAN, **changes}, headers=_auth("owner"))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["access_plan"]

    def _app(self, access_plan, **body):
        return self.client.post(
            "/admin/api/apps",
            json={"name": "client", "owner_user_id": self.owner["id"], "access_plan": access_plan, **body},
            headers=_auth("owner"),
        )

    def test_plan_is_created_with_a_server_assigned_id_and_listed(self) -> None:
        plan = self._plan()
        self.assertTrue(plan["id"].startswith("plan_"))
        listed = self.client.get("/admin/api/access-plans", headers=_auth("owner")).json()["access_plans"]
        self.assertEqual([row["name"] for row in listed], ["Family"])

    def test_plan_with_a_missing_field_is_refused_and_names_it(self) -> None:
        body = {key: value for key, value in self.PLAN.items() if key != "receipt_detail"}
        response = self.client.post("/admin/api/access-plans", json=body, headers=_auth("owner"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_access_plan")
        self.assertIn("receipt_detail", response.json()["error"]["message"])

    def test_duplicate_plan_name_is_a_conflict(self) -> None:
        self._plan()
        response = self.client.post("/admin/api/access-plans", json=self.PLAN, headers=_auth("owner"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "access_plan_name_conflict")

    def test_app_without_an_access_plan_is_refused(self) -> None:
        response = self.client.post(
            "/admin/api/apps", json={"name": "client", "owner_user_id": self.owner["id"]}, headers=_auth("owner"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("access_plan", response.json()["error"]["message"])
        self.assertEqual(admin_store.list_apps(path=self.db), [])

    def test_app_with_an_unknown_plan_is_refused(self) -> None:
        response = self._app("plan_missing")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(admin_store.list_apps(path=self.db), [])

    def test_app_assignment_can_be_changed_but_never_cleared(self) -> None:
        plan = self._plan()
        app_id = self._app("person").json()["app"]["id"]
        moved = self.client.patch(f"/admin/api/apps/{app_id}", json={"access_plan": plan["id"]}, headers=_auth("owner"))
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(admin_store.get_app(app_id, path=self.db)["access_plan"], plan["id"])
        for cleared in ("", None):
            response = self.client.patch(
                f"/admin/api/apps/{app_id}", json={"access_plan": cleared}, headers=_auth("owner"))
            self.assertEqual(response.status_code, 400)
        self.assertEqual(admin_store.get_app(app_id, path=self.db)["access_plan"], plan["id"])

    def test_plan_in_use_cannot_be_deleted_until_its_apps_move(self) -> None:
        plan = self._plan()
        app_id = self._app(plan["id"]).json()["app"]["id"]
        refused = self.client.delete(f"/admin/api/access-plans/{plan['id']}", headers=_auth("owner"))
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.json()["error"]["code"], "access_plan_in_use")
        self.assertIn("client", refused.json()["error"]["message"])
        self.client.patch(f"/admin/api/apps/{app_id}", json={"access_plan": "person"}, headers=_auth("owner"))
        deleted = self.client.delete(f"/admin/api/access-plans/{plan['id']}", headers=_auth("owner"))
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(self.client.get("/admin/api/access-plans", headers=_auth("owner")).json()["access_plans"], [])

    def test_plan_routes_require_an_admin(self) -> None:
        for method, path in (("get", "/admin/api/access-plans"), ("post", "/admin/api/access-plans"),
                             ("delete", "/admin/api/access-plans/plan_x")):
            self.assertEqual(getattr(self.client, method)(path).status_code, 401, path)


class AdminRegistrySQLiteCASTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "admin.db"
        admin_store.init_db(self.db)
        self.store = AdminSQLiteStore(self.db)
        self.store.write_litellm_config = lambda _registry: self.db.parent / "litellm.config.yaml"  # type: ignore[method-assign]
        admin_store.write_service_registry(
            {
                "schema_version": 1,
                "services": {
                    "one": {
                        "id": "one",
                        "name": "One",
                        "source": "ui",
                        "enabled": True,
                        "capabilities": [],
                        "config": {"base_url": "http://127.0.0.1:9000/v1"},
                    }
                },
                "protocol_frontends": {},
                "workflows": {},
                "capabilities": {},
            },
            expected_generation=0,
            path=self.db,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _client(self) -> TestClient:
        app = FastAPI()
        app.include_router(
            create_admin_router(
                self.store, auth=_admin_auth, service_probe=_fake_probe
            )
        )
        return TestClient(app)

    def test_real_sqlite_registry_writers_require_and_advance_exact_etag(self) -> None:
        client = self._client()
        read = client.get("/admin/api/operator/service-registry", headers=_auth())
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(read.headers["etag"], '"os-1"')

        registry = read.json()["service_registry"]
        registry["services"]["one"]["name"] = "One updated"
        put = client.put(
            "/admin/api/operator/service-registry",
            json=registry,
            headers={**_auth(), "If-Match": '"os-1"'},
        )
        self.assertEqual(put.status_code, 200, put.text)
        self.assertEqual(put.headers["etag"], '"os-2"')

        stale = client.patch(
            "/admin/api/operator/services/one",
            json={"name": "stale write"},
            headers={**_auth(), "If-Match": '"os-1"'},
        )
        self.assertEqual(stale.status_code, 412, stale.text)
        self.assertEqual(stale.json()["error"]["code"], "generation_mismatch")

        add = client.post(
            "/admin/api/operator/services",
            json={"name": "two", "base_url": "http://127.0.0.1:9001/v1"},
            headers={**_auth(), "If-Match": '"os-2"'},
        )
        self.assertEqual(add.status_code, 201, add.text)
        self.assertEqual(add.headers["etag"], '"os-3"')

        edit = client.patch(
            "/admin/api/operator/services/one",
            json={"name": "One final"},
            headers={**_auth(), "If-Match": '"os-3"'},
        )
        self.assertEqual(edit.status_code, 200, edit.text)
        self.assertEqual(edit.headers["etag"], '"os-4"')

        toggle = client.post(
            "/admin/api/operator/services/one/enabled",
            json={"enabled": False},
            headers={**_auth(), "If-Match": '"os-4"'},
        )
        self.assertEqual(toggle.status_code, 200, toggle.text)
        self.assertEqual(toggle.headers["etag"], '"os-5"')

        remove = client.delete(
            "/admin/api/operator/services/two",
            headers={**_auth(), "If-Match": '"os-5"'},
        )
        self.assertEqual(remove.status_code, 200, remove.text)
        self.assertEqual(remove.headers["etag"], '"os-6"')

        current = admin_store.get_service_registry_with_generation(path=self.db)
        assert current is not None
        self.assertEqual(current["operator_settings_generation"], 6)
        self.assertEqual(current["value"]["services"]["one"]["name"], "One final")
        self.assertFalse(current["value"]["services"]["one"]["enabled"])
        self.assertEqual(len(admin_store.list_audit(path=self.db)), 5)

    def test_real_sqlite_registry_routes_reject_missing_if_match(self) -> None:
        client = self._client()
        registry = client.get(
            "/admin/api/operator/service-registry", headers=_auth()
        ).json()["service_registry"]
        requests = (
            ("put", "/admin/api/operator/service-registry", registry),
            ("post", "/admin/api/operator/services", {"name": "two", "base_url": "http://h:1/v1"}),
            ("patch", "/admin/api/operator/services/one", {"name": "two"}),
            ("delete", "/admin/api/operator/services/one", None),
            ("post", "/admin/api/operator/services/one/enabled", {"enabled": False}),
            ("post", "/admin/api/extensions/enable", {}),
        )
        for method, path, body in requests:
            response = client.request(
                method,
                path,
                json=body,
                headers={"Authorization": "Bearer admin-token"},
            )
            self.assertEqual(response.status_code, 412, response.text)
            self.assertEqual(response.json()["error"]["code"], "generation_mismatch")

    def test_extension_enable_uses_real_registry_cas_writer(self) -> None:
        client = self._client()
        review = {
            "ok": True,
            "package": {"id": "demo", "version": "1.0.0"},
            "package_hash": "a" * 64,
            "services": [{"id": "imported", "name": "Imported", "config": {"base_url": "http://h:2/v1"}}],
            "protocol_frontends": [],
            "workflows": [],
            "capabilities": [],
            "adapters": [],
            "translators": [],
            "code_objects": [],
            "requires_code_trust": False,
            "warnings": [],
            "errors": [],
            "object_counts": {"services": 1, "protocol_frontends": 0, "workflows": 0, "capabilities": 0},
        }
        source = {"type": "git", "url": "https://example.com/package.git"}
        with patch.object(
            extension_packages,
            "validate_extension_package_source",
            return_value=review,
        ):
            response = client.post(
                "/admin/api/extensions/enable",
                json={"source": source},
                headers={**_auth(), "If-Match": '"os-1"'},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["etag"], '"os-2"')
        self.assertIn("imported", response.json()["service_registry"]["services"])
        current = admin_store.get_service_registry_with_generation(path=self.db)
        assert current is not None
        self.assertEqual(current["operator_settings_generation"], 2)


class AdminServiceCrudTests(unittest.TestCase):
    def _store_with_provisioned(self) -> FakeAdminStore:
        store = FakeAdminStore()
        store.service_registry = service_registry.build_service_registry_seed(
            {
                "backends": {
                    "tabby": {
                        "name": "TabbyAPI",
                        "host": "127.0.0.1",
                        "port": 5050,
                        "capabilities": ["vision"],
                        "source": "provisioned",
                    }
                }
            }
        )
        return store

    def test_add_service_creates_ui_record(self) -> None:
        client, store = _make_client(self._store_with_provisioned())
        resp = client.post(
            "/admin/api/operator/services",
            json={"name": "My vLLM", "base_url": "http://10.0.0.5:8000/v1"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 201)
        services = resp.json()["service_registry"]["services"]
        self.assertIn("my-vllm", services)
        self.assertEqual(services["my-vllm"]["source"], "ui")
        self.assertEqual(services["my-vllm"]["config"]["port"], 8000)

    def test_add_service_requires_name_and_url(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/operator/services", json={"name": "x"}, headers=_auth()
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_service")

    def test_add_service_rejects_inline_api_key(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/operator/services",
            json={"name": "x", "base_url": "http://h:1/v1", "api_key": "sk-secret"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("api_key_env", resp.json()["error"]["message"])

    def test_add_duplicate_is_conflict(self) -> None:
        client, _ = _make_client()
        payload = {"name": "dup", "base_url": "http://h:1/v1"}
        self.assertEqual(
            client.post("/admin/api/operator/services", json=payload, headers=_auth()).status_code,
            201,
        )
        resp = client.post("/admin/api/operator/services", json=payload, headers=_auth())
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "service_conflict")

    def test_update_service_edits_ui_record(self) -> None:
        client, _ = _make_client()
        client.post(
            "/admin/api/operator/services",
            json={"name": "edit-me", "base_url": "http://h:1/v1", "capabilities": ["chat"]},
            headers=_auth(),
        )
        resp = client.patch(
            "/admin/api/operator/services/edit-me",
            json={"base_url": "http://h:1/v1", "capabilities": ["chat", "completions"], "role": "standby"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200)
        svc = resp.json()["service_registry"]["services"]["edit-me"]
        self.assertEqual(svc["capabilities"], ["chat", "completions"])
        self.assertEqual(svc["role"], "standby")

    def test_edit_provisioned_is_allowed(self) -> None:
        # EDITABILITY LAW: provisioned rows are editable — source is provenance,
        # not a permission gate. Only managed-substrate (litellm) is locked.
        client, _ = _make_client(self._store_with_provisioned())
        resp = client.patch(
            "/admin/api/operator/services/tabby",
            json={"base_url": "http://h:1/v1", "name": "TabbyAPI Updated"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200)
        svc = resp.json()["service_registry"]["services"]["tabby"]
        self.assertEqual(svc["name"], "TabbyAPI Updated")

    def test_delete_ui_service_then_404(self) -> None:
        client, _ = _make_client()
        client.post(
            "/admin/api/operator/services",
            json={"name": "gone", "base_url": "http://h:1/v1"},
            headers=_auth(),
        )
        self.assertEqual(
            client.delete("/admin/api/operator/services/gone", headers=_auth()).status_code,
            200,
        )
        resp = client.delete("/admin/api/operator/services/gone", headers=_auth())
        self.assertEqual(resp.status_code, 404)

    def test_delete_provisioned_is_allowed(self) -> None:
        # EDITABILITY LAW: provisioned rows are removable after the one-time seed.
        # The DB is the source of truth; the YAML is never re-merged on reboot.
        client, _ = _make_client(self._store_with_provisioned())
        resp = client.delete("/admin/api/operator/services/tabby", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("tabby", resp.json()["service_registry"]["services"])

    def test_toggle_enabled_on_any_source(self) -> None:
        client, _ = _make_client(self._store_with_provisioned())
        resp = client.post(
            "/admin/api/operator/services/tabby/enabled",
            json={"enabled": False},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(
            resp.json()["service_registry"]["services"]["tabby"]["enabled"]
        )

    def test_test_connection_reports_reachability(self) -> None:
        client, _ = _make_client()
        ok = client.post(
            "/admin/api/operator/services/test",
            json={"base_url": "http://reachable.test:8000/v1"},
            headers=_auth(),
        )
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()["probe"]["ok"])

        bad = client.post(
            "/admin/api/operator/services/test",
            json={"base_url": "http://nope.test:9999/v1"},
            headers=_auth(),
        )
        self.assertEqual(bad.status_code, 200)
        self.assertFalse(bad.json()["probe"]["ok"])
        self.assertIn("Connection refused", bad.json()["probe"]["error"])

    def test_detect_models_returns_model_ids_from_reachable_host(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/operator/services/detect-models",
            json={"base_url": "http://reachable.test:8000/v1"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200)
        detected = resp.json()["detected"]
        self.assertTrue(detected["ok"])
        self.assertEqual(detected["error"], "")
        self.assertEqual(detected["models"], ["gpt-test-1", "gpt-test-2"])

    def test_detect_models_returns_ok_false_for_unreachable_host(self) -> None:
        client, _ = _make_client()
        resp = client.post(
            "/admin/api/operator/services/detect-models",
            json={"base_url": "http://nope.test:9999/v1"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200)
        detected = resp.json()["detected"]
        self.assertFalse(detected["ok"])
        self.assertIn("Connection refused", detected["error"])
        self.assertEqual(detected["models"], [])

    def test_detect_models_503_when_probe_not_injected(self) -> None:
        client, _ = _make_client(service_probe=None)
        resp = client.post(
            "/admin/api/operator/services/detect-models",
            json={"base_url": "http://reachable.test:8000/v1"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"]["code"], "probe_unavailable")

    def test_service_routes_require_admin(self) -> None:
        client, _ = _make_client()
        for method, url in (
            ("POST", "/admin/api/operator/services"),
            ("PATCH", "/admin/api/operator/services/x"),
            ("DELETE", "/admin/api/operator/services/x"),
            ("POST", "/admin/api/operator/services/x/enabled"),
            ("POST", "/admin/api/operator/services/test"),
            ("POST", "/admin/api/operator/services/detect-models"),
        ):
            resp = client.request(method, url, json={})
            self.assertEqual(resp.status_code, 401, f"{method} {url}")

    def test_patch_service_accepts_models_list(self) -> None:
        """PATCH with a models list must be accepted and round-trip through the registry."""
        client, store = _make_client()
        # Create a service first.
        client.post(
            "/admin/api/operator/services",
            json={"name": "tgw", "protocol": "openai-compatible", "base_url": "http://h:7860/v1"},
            headers=_auth(),
        )
        # Patch with a models list.
        resp = client.patch(
            "/admin/api/operator/services/tgw",
            json={
                "base_url": "http://h:7860/v1",
                "models": [
                    {"public": "llama-3-8b", "upstream": "Meta-Llama-3-8B"},
                    {"public": "mistral-7b", "upstream": "Mistral-7B-Instruct"},
                ],
            },
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200)
        svc = resp.json()["service_registry"]["services"]["tgw"]
        stored_models = svc["config"].get("models", [])
        model_groups = [m["model_group"] for m in stored_models]
        self.assertIn("llama-3-8b", model_groups)
        self.assertIn("mistral-7b", model_groups)

    def test_patch_models_invalid_type_is_400(self) -> None:
        """A non-list models value must return 400."""
        client, _ = _make_client()
        client.post(
            "/admin/api/operator/services",
            json={"name": "tgw2", "base_url": "http://h:9/v1"},
            headers=_auth(),
        )
        resp = client.patch(
            "/admin/api/operator/services/tgw2",
            json={"base_url": "http://h:9/v1", "models": "not-a-list"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "invalid_service")

    def test_patch_full_fidelity_fields_accepted(self) -> None:
        """PATCH accepts all full-fidelity fields (health_endpoint, timeout,
        resources, note, native_passthrough, protocol, role, priority) and
        they appear in the returned service_registry config."""
        client, _ = _make_client()
        client.post(
            "/admin/api/operator/services",
            json={"name": "emb", "base_url": "http://127.0.0.1:5061/v1"},
            headers=_auth(),
        )
        resp = client.patch(
            "/admin/api/operator/services/emb",
            json={
                "base_url": "http://127.0.0.1:5061/v1",
                "health_endpoint": "/health",
                "timeout": 120,
                "protocol": "openai-compatible",
                "native_passthrough": True,
                "capabilities": ["openai", "embeddings"],
                "role": "required",
                "priority": 100,
                "note": "Local embedding backend",
                "resources": {
                    "duration_class": "interactive",
                    "gpu_required": False,
                    "max_parallel": 4,
                    "vram_gb_estimate": 0,
                },
            },
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        svc = resp.json()["service_registry"]["services"]["emb"]
        cfg = svc["config"]
        res = cfg.get("resources") or {}

        self.assertEqual(cfg.get("health_endpoint"), "/health")
        self.assertEqual(cfg.get("timeout"), 120)
        self.assertTrue(cfg.get("native_passthrough"))
        self.assertEqual(cfg.get("note"), "Local embedding backend")
        self.assertEqual(res.get("duration_class"), "interactive")
        self.assertEqual(res.get("max_parallel"), 4)
        self.assertEqual(res.get("gpu_required"), False)
        self.assertEqual(svc.get("role"), "required")
        self.assertEqual(svc.get("priority"), 100)


class AdminServicePresetsTests(unittest.TestCase):
    def test_engine_definitions_replace_the_preset_list(self) -> None:
        """GET /engine-definitions lists the definitions; Any OpenAI-compatible server is first."""
        client, _ = _make_client()
        resp = client.get("/admin/api/engine-definitions", headers=_auth())
        self.assertEqual(resp.status_code, 200)
        definitions = resp.json()["definitions"]
        self.assertEqual(definitions[0]["definition"], "openai-compatible")
        for row in definitions:
            for field in ("definition", "definition_version", "title", "summary", "fields", "capabilities", "steps"):
                self.assertIn(field, row, f"definition {row.get('definition')!r} missing {field!r}")
        self.assertEqual(client.get("/admin/api/operator/service-presets", headers=_auth()).status_code, 404)

    def test_engine_definitions_require_admin(self) -> None:
        client, _ = _make_client()
        resp = client.get("/admin/api/engine-definitions")
        self.assertEqual(resp.status_code, 401)

    def test_a_preset_body_is_refused_with_the_way_forward(self) -> None:
        client, _ = _make_client()
        resp = client.post("/admin/api/operator/services", headers=_auth(),
                           json={"name": "x", "preset": "openai-compatible", "base_url": "http://h:1/v1"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("engine-definitions", resp.json()["error"]["message"])

    def test_managed_substrate_remove_is_blocked(self) -> None:
        """DELETE on the litellm managed-substrate row returns 403."""
        store = FakeAdminStore()
        store.service_registry = service_registry.build_service_registry_seed({"backends": {}})
        client, _ = _make_client(store)
        resp = client.delete("/admin/api/operator/services/litellm", headers=_auth())
        self.assertEqual(resp.status_code, 403)
        self.assertIn("managed substrate", resp.json()["error"]["message"])

    def test_managed_substrate_host_edit_is_blocked(self) -> None:
        """PATCH with a base_url on the litellm managed-substrate row returns 403."""
        store = FakeAdminStore()
        store.service_registry = service_registry.build_service_registry_seed({"backends": {}})
        client, _ = _make_client(store)
        resp = client.patch(
            "/admin/api/operator/services/litellm",
            json={"base_url": "http://127.0.0.1:9999/v1"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 403)
        self.assertIn("managed substrate", resp.json()["error"]["message"])

    def test_managed_substrate_rename_is_allowed(self) -> None:
        """PATCH with only name on the litellm managed-substrate row returns 200."""
        store = FakeAdminStore()
        store.service_registry = service_registry.build_service_registry_seed({"backends": {}})
        client, _ = _make_client(store)
        resp = client.patch(
            "/admin/api/operator/services/litellm",
            json={"name": "LiteLLM Custom"},
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200)
        svc = resp.json()["service_registry"]["services"]["litellm"]
        self.assertEqual(svc["name"], "LiteLLM Custom")
        # Config must still be derived (execution_substrate preserved).
        self.assertEqual(svc["config"]["execution_substrate"], "litellm")

    def test_detect_models_comfyui_protocol_returns_not_ok(self) -> None:
        """POST /operator/services/detect-models for a comfyui-protocol service.

        ComfyUI does not expose an OpenAI-compatible /v1/models endpoint, so
        detect-models returns ok=False (cannot scan workflow dir via HTTP probe).
        The endpoint must still return 200 with a structured detected object.
        This documents the gap: detect-models does not support comfyui.
        """
        import asyncio

        async def _failing_probe(host: str, port: Any, path: str) -> dict:
            # Simulate the probe reaching an endpoint that isn't /models
            return {"ok": False, "status": 404, "error": "Not Found", "body": None}

        client, _ = _make_client(service_probe=_failing_probe)
        resp = client.post(
            "/admin/api/operator/services/detect-models",
            json={
                "name": "comfyui-local",
                "host": "127.0.0.1",
                "port": 8188,
                "protocol": "comfyui",
                "base_path": "",
            },
            headers=_auth(),
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        detected = resp.json()["detected"]
        self.assertIn("ok", detected)
        self.assertIn("models", detected)
        # A comfyui backend has no /models endpoint — probe returns not-ok.
        self.assertFalse(detected["ok"],
                         "detect-models for comfyui must return ok=False "
                         "(no OpenAI-compatible /models endpoint)")
        self.assertEqual(detected["models"], [])


if __name__ == "__main__":
    unittest.main()
