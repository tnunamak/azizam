"""Opt-in household data for the isolated browser harness; never imports live state."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import math
import os
import random
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

STABLE_TARGETS = {
    "fast": "qwen-8b",
    "default": "qwen-27b",
    "coder": "qwen-coder",
    "image": "krea2-turbo",
    "voice": "tts-1",
}
PASSWORD = "Household browser password 2026"
# Every timestamp derives from one fixed instant so reruns are byte-comparable.
# A late-October afternoon gives the monthly budget window enough days to fill.
FIXED_NOW = int(datetime(2026, 10, 28, 20, 30, tzinfo=timezone.utc).timestamp())
RNG_SEED = 20261028
SCENARIOS = (
    "default",
    "running",
    "budget-exhausted",
    "provider-down",
    "stale-health",
    "new-member",
)
REMOTE_SERVICE = "household-remote"
REMOTE_DOWN_HOURS = 4
STALE_VOICE_HOURS = 7
# Hours ago (within each day) when the video studio's checks were down, and
# how many recent hours its checks answered "degraded".
VIDEO_DOWN_HOURS = {9, 10, 16, 17}
VIDEO_DEGRADED_HOURS = 2
# Median latency (ms) per capability: a TTS clip and a video render differ by 100x.
LATENCY_MEDIAN_MS = {
    "chat": 2600, "image": 14000, "image-edit": 18000, "tts": 1400, "stt": 3200,
    "video": 95000, "music": 38000, "sfx": 7500, "embeddings": 120,
}
# Local hour of day (mean, spread) in which each capability is typically used.
HOUR_PROFILE = {
    "chat": (15, 5), "image": (20, 2.5), "image-edit": (20, 2.5), "tts": (8, 3),
    "stt": (18, 4), "video": (21, 1.5), "music": (19, 3), "sfx": (19, 3),
    "embeddings": (11, 2),
}
APP_WEIGHTS = {
    "sam": 3, "robin": 3, "alex": 2, "zoe": 1.5, "webui": 2, "home": 3, "codex": 2.5,
}
FIXTURES = Path(__file__).parent / "fixtures" / "household"
# Workflows the household's plans allow by their short id (the workflow
# capability's model). A picture-edit workflow keeps a form that takes a
# picture in front of the sweep and the browser tests.
PLAN_WORKFLOWS = ("qwen-image-2.1-edit",)
# Engine and model knowledge is fixture DATA, authored through registry contracts.
MODEL_GROUPS = {
    "household-chat": [
        ("fast", "Qwen 3.5 8B", "chat"),
        ("default", "Qwen 3.5 27B", "chat"),
        ("coder", "Qwen Coder 30B", "chat"),
        ("qwen-8b", "Qwen 3.5 8B", "chat"),
        ("qwen-27b", "Qwen 3.5 27B", "chat"),
        ("qwen-coder", "Qwen Coder 30B", "chat"),
        ("gemma-12b", "Gemma 3 12B", "chat"),
        ("mistral-small", "Mistral Small", "chat"),
    ],
    # The image engine's id is "comfyui": the gateway's image route always
    # runs on the backend of that name (proxy_comfy_request), and the fake
    # engine answers its API, so Try's "Make a picture" finishes (D1.5b).
    "comfyui": [
        ("image", "Krea 2 Turbo", "image"),
        ("krea2-turbo", "Krea 2 Turbo", "image"),
        ("qwen-image-edit", "Qwen Image Edit", "image-edit"),
    ],
    "household-voice": [
        ("voice", "TTS 1", "tts"),
        ("tts-1", "TTS 1", "tts"),
        ("musicgen", "MusicGen", "music"),
        ("audio-sfx", "Audio SFX", "sfx"),
        ("whisper-large", "Whisper Large v3", "stt"),
    ],
    "household-video": [("ltx-2.5", "LTX Video", "video")],
    "household-embedding": [("bge-m3", "BGE M3", "embeddings")],
    "household-remote": [
        ("claude-sonnet", "Claude Sonnet 4.5", "chat"),
        ("gpt-mini", "GPT 4.1 mini", "chat"),
    ],
}
APP_CAPABILITY_CHOICES = {
    "sam": (
        "chat",
        "chat",
        "image",
        "image-edit",
        "tts",
        "stt",
        "video",
        "music",
        "sfx",
    ),
    "robin": ("chat", "image", "chat", "tts", "stt", "video", "music", "sfx"),
    "zoe": ("chat", "chat", "image", "tts"),
    "alex": ("chat", "chat", "image", "image-edit", "stt", "music", "sfx"),
    "webui": ("chat", "chat", "image", "chat", "video", "image-edit"),
    "home": ("tts", "tts", "stt", "tts", "stt", "chat"),
    "codex": ("chat",),
}
SERVICE_NAMES = {
    "household-chat": "Study and coding",
    "comfyui": "Image studio",
    # An engine name of its own: the household's Home Assistant is an app
    # (the "home" app below); naming the engine after it sent the owner to the
    # wrong system (red team round 2).
    "household-voice": "Voice and sound",
    "household-video": "Video studio",
    "household-embedding": "Document search",
    "household-remote": "Remote backup",
}

MODEL_METADATA = {
    "chat": {
        "type": "language",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
    },
    "image": {
        "type": "image",
        "input_modalities": ["text"],
        "output_modalities": ["image"],
    },
    "image-edit": {
        "type": "image",
        "input_modalities": ["text", "image"],
        "output_modalities": ["image"],
    },
    "tts": {
        "type": "audio",
        "input_modalities": ["text"],
        "output_modalities": ["audio"],
    },
    "stt": {
        "type": "audio",
        "input_modalities": ["audio"],
        "output_modalities": ["text"],
    },
    "music": {
        "type": "audio",
        "input_modalities": ["text"],
        "output_modalities": ["audio"],
    },
    "sfx": {
        "type": "audio",
        "input_modalities": ["text"],
        "output_modalities": ["audio"],
    },
    "video": {
        "type": "video",
        "input_modalities": ["text"],
        "output_modalities": ["video"],
    },
    "embeddings": {
        "type": "embedding",
        "input_modalities": ["text"],
        "output_modalities": ["embedding"],
    },
}


def registry(engine_port: int, scenario: str = "default") -> dict:
    from gateway.service_registry import normalize_service_registry

    services = {}
    for sid, models in MODEL_GROUPS.items():
        remote = sid == "household-remote"
        config = {
            "base_url": f"http://127.0.0.1:{engine_port}",
            "host": "127.0.0.1",
            "port": engine_port,
            "protocol": "openai-compatible",
            "health_endpoint": "/fixture/degraded"
            if sid == "household-video"
            else "/fixture/down"
            if remote and scenario == "provider-down"
            # The fake engine serves no such route, so the probe finds nothing to check.
            else "/fixture/no-health"
            if sid == "household-voice" and scenario == "stale-health"
            else "/health",
            "models": [
                {
                    "id": mid,
                    "model_group": mid,
                    "upstream_model": STABLE_TARGETS.get(mid, mid),
                    **(
                        {"base_model_id": STABLE_TARGETS[mid]}
                        if mid in STABLE_TARGETS
                        else {}
                    ),
                    "name": name,
                    "capabilities": [cap],
                    "mode": cap,
                    "model_metadata": MODEL_METADATA[cap],
                    "capability": cap,
                    "context_length": 131072 if cap == "chat" else None,
                    "input_cost_per_token": (
                        0.0000004 if mid == "gpt-mini" else 0.000003
                    )
                    if remote
                    else 0,
                    "output_cost_per_token": (
                        0.0000016 if mid == "gpt-mini" else 0.000015
                    )
                    if remote
                    else 0,
                }
                for mid, name, cap in models
            ],
            "model_policy_schema_version": 1,
        }
        if remote:
            config.update(
                credential_origin=f"http://127.0.0.1:{engine_port}",
                provider_type="paid-remote",
                provider_id="household-remote",
                api_key_env="UI_HOUSEHOLD_PROVIDER_KEY",
                spend_caps_usd={"daily": 5, "monthly": 40}
                if scenario == "budget-exhausted" else {"daily": 1, "monthly": 8},
            )
        services[sid] = {
            "id": sid,
            "name": SERVICE_NAMES[sid],
            "enabled": sid != "household-embedding",
            "kind": "provider" if remote else "engine",
            "source": "ui",
            "role": "optional"
            if sid in {"household-video", "household-embedding"}
            else "required",
            "capabilities": ["audio", "music", "sfx"]
            if sid == "household-voice"
            else sorted({cap for _, _, cap in models}),
            "config": config,
            "lifecycle": {"type": "external", "managed": False},
        }
    services["litellm"] = {
        "id": "litellm",
        "name": "Execution gateway",
        "enabled": True,
        "source": "builtin",
        "kind": "execution-substrate",
        "role": "required",
        "capabilities": ["llm.execution"],
        "config": {
            "execution_substrate": "litellm",
            "base_url": f"http://127.0.0.1:{engine_port}",
            "host": "127.0.0.1",
            "port": engine_port,
            "health_endpoint": "/health",
        },
    }
    capability_specs = {
        "chat": ("chat.completions", ["text"], ["text"]),
        "image": ("image.generate", ["text"], ["image"]),
        "image-edit": ("image.edit", ["text", "image"], ["image"]),
        "tts": ("speech.synthesis", ["text"], ["audio"]),
        "stt": ("speech.transcription", ["audio"], ["text"]),
        "video": ("video.generate", ["text"], ["video"]),
        "music": ("audio.generate", ["text"], ["audio"]),
        "sfx": ("audio.generate", ["text"], ["audio"]),
        "embeddings": ("text.embeddings", ["text"], ["embedding"]),
    }
    capabilities = {}
    for sid, models in MODEL_GROUPS.items():
        for mid, name, cap in models:
            intent, inputs, outputs = capability_specs[cap]
            capabilities[mid] = {
                "id": mid,
                "intent": intent,
                "backend": sid,
                "model": mid,
                "input_modalities": inputs,
                "output_modalities": outputs,
                "execution": "job"
                if cap in {"image", "image-edit", "video", "music", "sfx"}
                else "sync",
                "health": "degraded"
                if cap == "video"
                else "unhealthy"
                if cap == "embeddings"
                or (sid == REMOTE_SERVICE and scenario == "provider-down")
                else "healthy",
                "enabled": True,
                "source": "ui",
                "tags": [cap],
            }
    document = {"schema_version": 1, "services": services, "capabilities": capabilities}
    # Off by default: the household screens and browser tests were written against a
    # registry without the built-in protocol frontends. The oracle server
    # (web/e2e/fixtures/oracle.ts) calls routes that read their policy from them.
    if os.environ.get("UI_HOUSEHOLD_PROTOCOL_FRONTENDS") == "builtin":
        from gateway.service_registry import build_service_registry_seed

        document["protocol_frontends"] = build_service_registry_seed({})["protocol_frontends"]
    return normalize_service_registry(document)


def seed(state: Path, port: int, scenario: str | None = None, now: int | None = None) -> dict:
    """Seed fresh isolated databases, using the same schemas and stores as the gateway.

    `scenario` layers one named state on the default household (see SCENARIOS).
    """
    scenario = scenario or os.environ.get("UI_HOUSEHOLD_SCENARIO") or "default"
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown household scenario {scenario!r}; use one of {SCENARIOS}")
    import metrics_store
    from gateway import accounts, admin_store, media_jobs, providers, receipt_store
    from gateway.local_owner import hash_password
    from gateway.receipts import RouteReceipt
    from gateway.runtime_auth import hash_api_key

    state = state.resolve()
    paths = {
        name: state / f"{name}.db"
        for name in ("admin", "metrics", "receipts", "media_jobs", "provider_spend")
    }
    # Fail closed before any write if called outside the disposable harness state.
    for env, name in [
        ("AZIZAM_ADMIN_DB", "admin"),
        ("AZIZAM_METRICS_DB", "metrics"),
        ("AZIZAM_RECEIPTS_DB", "receipts"),
        ("AZIZAM_MEDIA_JOBS_DB", "media_jobs"),
    ]:
        if Path(os.environ.get(env, "")).resolve() != paths[name]:
            raise ValueError(f"household seed requires isolated {env}")
    if paths["admin"].exists():
        raise ValueError("household seed requires fresh state")
    admin_store.init_db(paths["admin"])
    accounts.initialize(paths["admin"])
    receipt_store.init_db(paths["receipts"])
    media_jobs.init_db(paths["media_jobs"])
    metrics_store.init_db(paths["metrics"])
    providers.ensure_ledger(paths["provider_spend"])
    now = int(now or os.environ.get("UI_HOUSEHOLD_NOW") or FIXED_NOW)
    rng = random.Random(RNG_SEED)
    config_path = state / "config.yml"
    if config_path.is_file():
        import yaml

        document = yaml.safe_load(config_path.read_text())
        document["model_aliases"] = {
            "chat": {
                "stable": {
                    name: STABLE_TARGETS[name] for name in ("fast", "default", "coder")
                }
            },
            "images": {"stable": {"image": "krea2-turbo"}},
            "speech": {
                "stable": {"voice": ["tts-1"]},
                "engines": {"tts-1": {"service": "household-voice", "model": "tts-1"}},
            },
        }
        config_path.write_text(yaml.safe_dump(document))
    reg = registry(port + 1, scenario)
    admin_store.write_service_registry(
        reg, expected_generation=0, updated_by="household-fixture", path=paths["admin"]
    )
    plans = {}
    all_models = [mid for group in MODEL_GROUPS.values() for mid, _, _ in group] + list(PLAN_WORKFLOWS)
    for name, paid in [("Everyone", True), ("Teen", False), ("Builder", True)]:
        doc = {
            "id": name.lower(),
            "protocols": ["openai", "anthropic"],
            "models": all_models
            if paid
            else [
                mid for mid in all_models if mid not in {"claude-sonnet", "gpt-mini"}
            ],
            "backends": list(MODEL_GROUPS)
            if paid
            else [sid for sid in MODEL_GROUPS if sid != "household-remote"],
            "status_visibility": {"level": "full"},
            "remote": {
                "providers": ["household-remote"] if paid else [],
                "max_usd_per_request": "0.10",
                "budget_usd_per_day": "1",
                "budget_usd_per_month": "8",
                "allow_paid_fallback": False,
            },
        }
        plans[name] = admin_store.create_profile(
            name,
            doc,
            description="Local models only; paid models blocked"
            if not paid
            else "Local tools with a small remote backup allowance",
            now=now, path=paths["admin"],
        )
    admin_store.create_runtime_profile_mapping(
        source_kind="all_users",
        source_value="*",
        profile_id=plans["Everyone"]["id"],
        now=now, path=paths["admin"],
    )
    people, apps, hashes = {}, {}, {}
    for index, (slug, name, plan) in enumerate(
        [
            ("sam", "Sam", "Builder"),
            ("robin", "Robin", "Everyone"),
            ("alex", "Alexandria Montgomery-Featherstonehaugh", "Teen"),
            ("zoe", "Zoë Ångström", "Everyone"),
            ("casey", "Casey · former guest", "Everyone"),
        ]
        + (
            [("noor", "نیلوفر احمدی", "Everyone")] if scenario == "new-member" else []
        )
    ):
        user = admin_store.upsert_oidc_user(
            "local",
            slug,
            email=f"{slug}@example.test",
            display_name=name,
            now=now, path=paths["admin"],
        )
        people[slug] = user
        admin_store.set_local_password_verifier(
            user["id"], hash_password(PASSWORD), path=paths["admin"]
        )
        accounts.record_oidc_verification(user["id"], True, path=paths["admin"])
        with sqlite3.connect(paths["admin"]) as conn:
            conn.execute(
                "INSERT INTO account_access(user_id,profile_id) VALUES(?,?)",
                (user["id"], plans[plan]["id"]),
            )
            conn.execute(
                "UPDATE users SET created_at=?,last_login_at=? WHERE id=?",
                (now - (90 - index * 9) * 86400, now - index * 7200, user["id"]),
            )
        if slug == "sam":
            admin_store.assign_admin_role(
                role="owner",
                subject_kind="user",
                subject_id=user["id"],
                path=paths["admin"],
            )
            admin_store.replace_user_identity_claims(
                user["id"], {"oidc_group": ["/household/owners"]}, path=paths["admin"]
            )
            admin_store.assign_admin_role(
                role="admin",
                subject_kind="user",
                subject_id=user["id"],
                provenance="oidc",
                path=paths["admin"],
            )
        if slug == "noor":
            # A brand-new member has signed in but owns no app or key yet.
            with sqlite3.connect(paths["admin"]) as conn:
                conn.execute(
                    "UPDATE users SET created_at=?,last_login_at=? WHERE id=?",
                    (now - 86400, now - 1800, user["id"]),
                )
            continue
        app = admin_store.create_app(
            f"{name.split(' ·')[0]}'s personal app",
            kind="personal",
            owner_user_id=user["id"],
            default_profile_id=plans[plan]["id"],
            now=now, path=paths["admin"],
        )
        apps[slug] = app
    for slug, owner, name in [
        ("webui", "robin", "Open WebUI"),
        ("home", "sam", "Home Assistant voice"),
        ("codex", "sam", "Codex"),
        ("telegram", "robin", "Telegram homework and grocery reminder helper for the family group chat"),
        ("librechat", "sam", "LibreChat · retired"),
    ]:
        apps[slug] = admin_store.create_app(
            name,
            kind="personal",
            owner_user_id=people[owner]["id"],
            default_profile_id=plans["Builder" if owner == "sam" else "Everyone"]["id"],
            now=now, path=paths["admin"],
        )
    for index, (slug, app) in enumerate(apps.items()):
        digest = hash_api_key(f"household-fixture-key-{slug}-not-production")
        hashes[slug] = digest
        cred = admin_store.create_credential_hash(
            subject_kind="app",
            subject_id=app["id"],
            app_id=app["id"],
            hash=digest,
            prefix=f"fixture_{slug}",
            display_name="Desktop" if slug == "codex" else "Household app key",
            profile_id=app["default_profile_id"],
            now=now - (45 - index) * 86400,
            path=paths["admin"],
        )
        if slug != "telegram":
            admin_store.touch_credential_last_used(
                cred["id"], now=now - index * 3600, path=paths["admin"]
            )
    admin_store.disable_app(apps["librechat"]["id"], path=paths["admin"])
    accounts.change_person(
        people["casey"]["id"], people["sam"]["id"], disabled=True, path=paths["admin"]
    )
    with sqlite3.connect(paths["admin"]) as conn:
        conn.execute(
            "UPDATE account_access SET disabled_at=? WHERE user_id=?",
            (now - 3 * 86400, people["casey"]["id"]),
        )
        conn.execute(
            "UPDATE users SET last_login_at=? WHERE id=?",
            (now - 4 * 86400, people["casey"]["id"]),
        )
        conn.execute(
            "UPDATE credentials SET last_used_at=?,revoked_at=? WHERE app_id=?",
            (now - 4 * 86400, now - 3 * 86400, apps["casey"]["id"]),
        )
        conn.execute(
            "UPDATE apps SET disabled_at=? WHERE id=?",
            (now - 8 * 86400, apps["librechat"]["id"]),
        )
        conn.execute(
            "UPDATE credentials SET last_used_at=? WHERE app_id=?",
            (now - 9 * 86400, apps["librechat"]["id"]),
        )
    from unittest.mock import patch

    for email in ["june@example.test", "morgan@example.test"]:
        with patch.object(accounts.time, "time", return_value=now):
            _link, _token = accounts.create_link(
                "invite",
                people["sam"]["id"],
                email=email,
                profile_id=plans["Everyone"]["id"],
                token_factory=lambda _link_id: f"household-fixture-invite-{email}",
                path=paths["admin"],
            )
    active_apps = [
        slug for slug in apps if slug not in {"casey", "librechat", "telegram"}
    ]
    capabilities = [
        (
            "chat",
            "chat.completions",
            "/v1/chat/completions",
            "default",
            "household-chat",
        ),
        (
            "image",
            "image.generate",
            "/v1/images/generations",
            "image",
            "comfyui",
        ),
        ("tts", "audio.speech", "/v1/audio/speech", "voice", "household-voice"),
        (
            "stt",
            "audio.transcribe",
            "/v1/audio/transcriptions",
            "whisper-large",
            "household-voice",
        ),
        ("video", "video.generate", "/v1/videos", "ltx-2.5", "household-video"),
        (
            "image-edit",
            "image.edit",
            "/v1/images/edits",
            "qwen-image-edit",
            "comfyui",
        ),
        ("music", "audio.music", "/v1/audio/music", "musicgen", "household-voice"),
        ("sfx", "audio.sfx", "/v1/audio/sfx", "audio-sfx", "household-voice"),
    ]
    capability_by_name = {row[0]: row for row in capabilities}
    samples, spend, receipt_rows = [], [], []
    chicago = ZoneInfo("America/Chicago")
    today = datetime.fromtimestamp(now, chicago).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    failure_kinds = [
        "model_access_denied",
        "upstream_timeout",
        "upstream_unavailable",
        "budget_exceeded",
        "invalid_request",
    ]
    slugs = list(active_apps)
    weights = [APP_WEIGHTS[slug] for slug in slugs]
    for day in range(30):
        # Weekends run busier; the per-day count is irregular, not a repeating cycle.
        local_day = today - timedelta(days=day)
        total = rng.randint(24, 40) + (8 if local_day.weekday() >= 5 else 0)
        failure_at = rng.randrange(total) if day < 5 else None
        midnight = local_day
        for index in range(total):
            slug = rng.choices(slugs, weights)[0]
            choice = rng.choice(APP_CAPABILITY_CHOICES[slug])
            _cap, intent, endpoint, model, backend = capability_by_name[choice]
            if slug == "codex":
                model = "coder"
            elif slug == "home" and choice == "chat":
                model = "fast"
            mean_hour, spread = HOUR_PROFILE[choice]
            hour = min(23.9, max(6.0, rng.gauss(mean_hour, spread)))
            ts = int(midnight.timestamp() + hour * 3600 + rng.randint(0, 59))
            if ts > now - 120:
                # Today is only partly over: keep activity before the present.
                ts = int(midnight.timestamp() + rng.uniform(6 * 3600, now - 120 - midnight.timestamp()))
            if slug == "codex" and day > 4 and rng.random() < 0.2:
                _cap, intent, endpoint, model, backend = (
                    "embeddings",
                    "embeddings",
                    "/v1/embeddings",
                    "bge-m3",
                    "household-embedding",
                )
                choice = "embeddings"
            remote = (
                intent == "chat.completions"
                and rng.random() < 0.25
                and slug not in {"alex", "home"}
            )
            if remote:
                _cap, intent, endpoint, model, backend = (
                    "chat",
                    "chat.completions",
                    "/v1/chat/completions",
                    "claude-sonnet",
                    "household-remote",
                )
            failure = failure_kinds[(day + 2) % 5] if index == failure_at else None
            if failure in {"model_access_denied", "budget_exceeded"}:
                remote = True
                _cap, intent, endpoint, model, backend = (
                    "chat",
                    "chat.completions",
                    "/v1/chat/completions",
                    "claude-sonnet",
                    "household-remote",
                )
                choice = "chat"
                if failure == "model_access_denied":
                    slug = "alex"
                elif slug in {"alex", "home"}:
                    slug = "sam"
            if failure == "upstream_unavailable":
                if slug in {"codex", "home"}:
                    slug = "webui"
                _cap, intent, endpoint, model, backend = (
                    "video",
                    "video.generate",
                    "/v1/videos",
                    "ltx-2.5",
                    "household-video",
                )
                choice = "video"
                remote = False
            # A request refused before execution generates nothing.
            prompt_tokens = _lognormal(rng, 3500 if remote else 600, 0.6, 12, 9000)
            completion_tokens = _lognormal(rng, 1300 if remote else 260, 0.6, 12, 3500)
            if failure == "budget_exceeded":
                # The estimate alone exceeds the $0.10 per-request cap.
                prompt_tokens, completion_tokens = 38000 + rng.randint(0, 2500), 0
            elif failure == "invalid_request":
                prompt_tokens = completion_tokens = 0
            elif failure:
                completion_tokens = 0
            cost = (
                _usd(prompt_tokens, completion_tokens)
                if remote and not failure
                else "0.000000000"
            )
            latency_ms = _lognormal(rng, LATENCY_MEDIAN_MS[choice], 0.4, 20, 600000)
            if failure == "upstream_timeout":
                latency_ms = 60000
            elif failure in {"model_access_denied", "budget_exceeded", "invalid_request"}:
                latency_ms = rng.randint(8, 45)
            elif failure == "upstream_unavailable":
                latency_ms = rng.randint(1200, 2400)
            request_id = f"req_household_{day:02}_{index:02}"
            app = apps[slug]
            receipt = RouteReceipt(
                protocol="openai",
                intent=intent,
                path=endpoint,
                request_id=request_id,
                app_id=app["id"],
                app_profile=app["default_profile_id"],
                api_key_name=app["name"],
                client_hint=app["name"],
                requested_model=model,
                resolved_model=model,
                backend=backend,
                status="timed_out"
                if failure == "upstream_timeout"
                else "rejected"
                if failure
                in {"model_access_denied", "budget_exceeded", "invalid_request"}
                else "failed"
                if failure
                else "succeeded",
                remote=remote,
                owner_key_hash=hashes[slug],
                latency_ms=latency_ms,
                cost={
                    "usd": cost,
                    "source": "provider-reported" if remote else "local",
                    "cost_class": "paid-remote" if remote else "local",
                },
                usage={
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                },
                accepted_at_ms=ts * 1000,
                updated_at_ms=ts * 1000 + latency_ms,
                policy_decision="denied"
                if failure in {"model_access_denied", "budget_exceeded"}
                else "allowed",
                policy_reasons=[failure] if failure else [],
                warnings=[
                    {
                        "model_access_denied": "Teen plan blocks paid remote models.",
                        "upstream_timeout": "The model did not respond before the request deadline.",
                        "upstream_unavailable": "Video studio is recovering after a queue timeout.",
                        "budget_exceeded": "The per-request remote spending limit stopped this request.",
                        "invalid_request": "The request body is missing a required field.",
                    }[failure]
                ]
                if failure
                else [],
                metadata={
                    "status_code": {
                        "model_access_denied": 403,
                        "upstream_timeout": 504,
                        "upstream_unavailable": 503,
                        "budget_exceeded": 429,
                        "invalid_request": 400,
                    }[failure],
                    "litellm": {"response_cost": cost},
                }
                if failure
                else {"litellm": {"response_cost": cost}},
            )
            data = receipt_store._coerce_to_internal_dict(receipt)
            receipt_store._assert_safe(data)
            receipt_rows.append(receipt_store._row_values(data, created_at=ts))
            tags = json.dumps(
                {
                    "key": app["name"],
                    "app_id": app["id"],
                    "model": model,
                    "backend": backend,
                    "intent": intent,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            samples.append((ts, "request", tags, 1))
            if failure:
                samples.append((ts, "error", tags, 1))
            if remote and not failure:
                spend.append(
                    (
                        backend,
                        app["default_profile_id"],
                        "settled",
                        cost,
                        cost,
                        "provider-reported",
                        ts,
                        ts + 2,
                        request_id,
                    )
                )
    ctx = {
        "rng": rng, "now": now, "apps": apps, "hashes": hashes, "plans": plans,
        "people": people, "paths": paths, "samples": samples, "spend": spend,
        "receipt_rows": receipt_rows, "details": {},
    }
    if scenario == "budget-exhausted":
        _scenario_budget_exhausted(ctx)
    elif scenario == "provider-down":
        _scenario_provider_down(ctx)
    with sqlite3.connect(paths["receipts"]) as conn:
        conn.executemany(
            "INSERT INTO receipts(request_id,created_at,protocol,intent,path,app_profile,app_id,api_key_name,status,job_id,parent_request_id,receipt_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            receipt_rows,
        )
    for hour in range(30 * 24):
        ts = now - hour * 3600
        for sid in MODEL_GROUPS:
            if scenario == "stale-health" and (
                sid == "household-embedding"
                or (sid == "household-voice" and hour < STALE_VOICE_HOURS)
            ):
                continue  # no recent (or any) observation for these services
            tags = json.dumps(
                {
                    "backend": sid,
                    "name": SERVICE_NAMES[sid],
                    "role": reg["services"][sid]["role"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            # The video studio flaps: it was down in two spells earlier in
            # the day, and for the last two hours it has answered its check
            # "degraded" (503, recovering after a queue timeout), as its live
            # probe still does. The status sampler records degraded as 0.5.
            # (Until D1.3 the seed wrote "down" for the last two hours while
            # the live probe said "degraded", so the strip and the verdict
            # disagreed.)
            video_down = sid == "household-video" and hour % 24 in VIDEO_DOWN_HOURS
            video_degraded = sid == "household-video" and hour < VIDEO_DEGRADED_HOURS
            up = (
                (sid != "household-embedding" or hour > 4 * 24)
                and not video_down
                and not video_degraded
            )
            if (
                scenario == "provider-down"
                and sid == REMOTE_SERVICE
                and hour < REMOTE_DOWN_HOURS
            ):
                up = False
            samples.extend(
                [
                    (ts, "service_up", tags, int(up)),
                    (ts, "service_status_code", tags,
                     0.5 if video_degraded else 1 if up else 0),
                    (ts, "service_latency_ms", tags,
                     _lognormal(rng, 28 if up else 1400, 0.4, 5, 10000)),
                    (ts, "service_error_kind", tags, 0 if up else 3),
                ]
            )
    with sqlite3.connect(paths["metrics"]) as conn:
        metrics_store.record_samples(conn, samples)
    with sqlite3.connect(paths["provider_spend"]) as conn:
        conn.executemany(
            "INSERT INTO provider_spend(provider,profile_id,status,estimated_usd,actual_usd,cost_source,created_at,settled_at,request_id) VALUES(?,?,?,?,?,?,?,?,?)",
            spend,
        )
    admin_store.put_operator_setting(
        "provider_admin",
        {
            "providers": {
                "household-remote": {
                    "caps": {"daily_usd": 5, "monthly_usd": 40}
                    if scenario == "budget-exhausted" else {"daily_usd": 1, "monthly_usd": 8},
                    # The last connection test failed; only this scenario stores one.
                    **(
                        {"connection": {"reachable": False, "checked_at": now - 120}}
                        if scenario == "provider-down"
                        else {}
                    ),
                }
            },
            "global_caps": {"daily_usd": 5, "monthly_usd": 40}
            if scenario == "budget-exhausted" else {"daily_usd": 1, "monthly_usd": 8},
        },
        path=paths["admin"],
    )
    for day, action, kind, target in [
        (24, "people.update", "user", people["alex"]["id"]),
        (16, "app.key.create", "app", apps["codex"]["id"]),
        (12, "operator.provider_admin.update", "provider", "household-remote"),
        (8, "app.disable", "app", apps["librechat"]["id"]),
        (3, "people.update", "user", people["casey"]["id"]),
        (4, "operator.service.disable", "service", "household-embedding"),
    ]:
        admin_store.record_audit(
            action,
            actor_kind="user",
            actor_id=people["sam"]["id"],
            actor_label="Sam",
            target_kind=kind,
            target_id=target,
            now=now - day * 86400,
            path=paths["admin"],
        )
    assets = _media(state, paths, apps, hashes, now, rng, people)
    if scenario == "running":
        _scenario_running(ctx)
    manifest = {
        "base_url": f"http://127.0.0.1:{port}",
        "owner_email": "sam@example.test",
        "member_email": "noor@example.test"
        if scenario == "new-member"
        else "robin@example.test",
        "restricted_email": "alex@example.test",
        "password": PASSWORD,
        "people": {slug: row["id"] for slug, row in people.items()},
        "apps": {slug: row["id"] for slug, row in apps.items()},
        "scenario": scenario,
        "now": now,
        "scenario_details": ctx["details"],
        "assets": assets,
        "request_count": sum(metric == "request" for _, metric, _, _ in samples)
        + len(assets)
        + ctx.get("extra_requests", 0),
    }
    (state / "household-manifest.json").write_text(json.dumps(manifest))
    target = os.environ.get("UI_HOUSEHOLD_MANIFEST_PATH")
    if target:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text(json.dumps(manifest))
    return manifest


def _lognormal(rng, median: float, sigma: float, low: int, high: int) -> int:
    """Skewed positive integer (latency, token count) drawn from the seeded generator."""
    return int(min(high, max(low, rng.lognormvariate(math.log(median), sigma))))


def _usd(prompt_tokens: int, completion_tokens: int) -> str:
    return str(
        (
            Decimal(prompt_tokens) * Decimal("0.000003")
            + Decimal(completion_tokens) * Decimal("0.000015")
        ).quantize(Decimal("0.000000001"))
    )


def _remote_receipt(ctx, *, request_id, slug, ts, status, prompt_tokens, completion_tokens,
                    cost="0.000000000", status_code=None, reason=None, warning=None,
                    model="claude-sonnet", latency_ms=2400):
    """One paid-remote chat receipt plus its request sample (and ledger row if charged)."""
    from gateway import receipt_store
    from gateway.receipts import RouteReceipt

    app = ctx["apps"][slug]
    denied = status == "rejected"
    receipt = RouteReceipt(
        protocol="openai", intent="chat.completions", path="/v1/chat/completions",
        request_id=request_id, app_id=app["id"], app_profile=app["default_profile_id"],
        api_key_name=app["name"], client_hint=app["name"], requested_model=model,
        resolved_model=model, backend=REMOTE_SERVICE, status=status, remote=True,
        owner_key_hash=ctx["hashes"][slug], latency_ms=latency_ms,
        cost={"usd": cost, "source": "provider-reported", "cost_class": "paid-remote"},
        usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        accepted_at_ms=ts * 1000, updated_at_ms=ts * 1000 + latency_ms,
        policy_decision="denied" if denied else "allowed",
        policy_reasons=[reason] if reason else [],
        warnings=[warning] if warning else [],
        metadata={"status_code": status_code, "litellm": {"response_cost": cost}}
        if status_code
        else {"litellm": {"response_cost": cost}},
    )
    data = receipt_store._coerce_to_internal_dict(receipt)
    receipt_store._assert_safe(data)
    ctx["receipt_rows"].append(receipt_store._row_values(data, created_at=ts))
    tags = json.dumps(
        {"key": app["name"], "app_id": app["id"], "model": model,
         "backend": REMOTE_SERVICE, "intent": "chat.completions"},
        sort_keys=True, separators=(",", ":"),
    )
    ctx["samples"].append((ts, "request", tags, 1))
    if status != "succeeded":
        ctx["samples"].append((ts, "error", tags, 1))
    elif Decimal(cost) > 0:
        ctx["spend"].append((REMOTE_SERVICE, app["default_profile_id"], "settled", cost,
                             cost, "provider-reported", ts, ts + 2, request_id))


def _scenario_budget_exhausted(ctx):
    """Robin's plan (Everyone) has spent its daily and monthly allowance.

    Ledger rows and succeeded receipts share one cost, so the ledger total is the
    receipt total. Later 429 rejections follow the row that crossed the cap.
    """
    rng, now = ctx["rng"], ctx["now"]
    profile = ctx["plans"]["Everyone"]["id"]
    month_start = int(datetime.fromtimestamp(now, timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
    day_cap, month_cap = Decimal(1), Decimal(8)

    day_start = now - 86400

    def total(since, until=now):
        return sum((Decimal(row[4]) for row in ctx["spend"]
                    if row[1] == profile and since <= row[6] <= until), Decimal(0))

    # Reserve room for the existing history, then add dated candidates in order.
    # This amount is exactly representable at the model's $0.000003 quantum.
    # The last day's first $1 crossing will also cross $8 for the month.
    remaining = max(Decimal(0), Decimal("6.999999") - total(month_start, day_start - 1))
    count = 0

    def charge(ts, amount):
        nonlocal count
        count += 1
        units = int(amount / Decimal("0.000003"))
        prompt = min(units, 1000 + units % 5)
        completion = (units - prompt) // 5
        _remote_receipt(ctx, request_id=f"req_household_budget_{count:03}", slug="robin", ts=ts,
                        status="succeeded", prompt_tokens=prompt, completion_tokens=completion,
                        cost=_usd(prompt, completion), latency_ms=_lognormal(rng, 5200, 0.4, 900, 40000))

    for ts in range(month_start + 3600, day_start, 6 * 3600):
        if remaining <= 0:
            break
        amount = min(Decimal("0.06"), remaining)
        charge(ts, amount)
        remaining -= amount
    # The day's charges end at last_ts. Window sums at that time still include
    # the older tail, so the crossing charge must be the one that lifts the last
    # 24 h to the cap; earlier windows stay below it. Base rows after last_ts
    # are rejected by the replay, so only earlier ones count here.
    last_ts = now - 5400
    remaining = max(Decimal(0), Decimal("1.000002") - total(day_start, last_ts - 1))
    amounts = []
    while remaining > Decimal("0.06"):
        amounts.append(Decimal("0.06"))
        remaining -= Decimal("0.06")
    if remaining > 0:
        amounts.append(remaining)
    first_ts = day_start + 3600
    for number, amount in enumerate(amounts):
        charge(first_ts + (last_ts - first_ts) * number // max(1, len(amounts) - 1), amount)

    # Replay every paid request. The crossing charge settles; later requests
    # in an exhausted rolling-day/calendar-month scope are rejected.
    from gateway import receipt_store

    accepted = []
    charges = {row[-1]: row for row in ctx["spend"]}
    for index, row in sorted(enumerate(ctx["receipt_rows"]), key=lambda item: (item[1][1], item[1][0])):
        data = json.loads(row[-1])
        if not data.get("remote"):
            continue
        ts = row[1]
        month = int(datetime.fromtimestamp(ts, timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
        exhausted = False
        for scope, daily, monthly in (
            (lambda charge: charge[1] == data["app_profile"], day_cap, month_cap),
            (lambda charge: charge[0] == data["backend"], Decimal(5), Decimal(40)),
            (lambda charge: True, Decimal(5), Decimal(40)),
        ):
            relevant = [charge for charge in accepted if scope(charge)]
            exhausted |= any(
                sum((Decimal(charge[4]) for charge in relevant if charge[6] >= since), Decimal(0)) >= cap
                for since, cap in ((ts - 86400, daily), (month, monthly))
            )
        if exhausted:
            had_error = data["status"] != "succeeded"
            data.update(status="rejected", policy_decision="denied",
                        policy_reasons=["budget_exceeded"],
                        warnings=["The remote spending allowance is exhausted."])
            data["usage"]["completion_tokens"] = 0
            data["cost"]["usd"] = "0.000000000"
            data["metadata"].update(status_code=429, litellm={"response_cost": "0.000000000"})
            data["latency_ms"] = rng.randint(8, 40)
            data["updated_at_ms"] = ts * 1000 + data["latency_ms"]
            ctx["receipt_rows"][index] = receipt_store._row_values(data, created_at=ts)
            if not had_error:
                tags = json.dumps(
                    {"key": data["api_key_name"], "app_id": data["app_id"],
                     "model": data["requested_model"], "backend": data["backend"],
                     "intent": data["intent"]}, sort_keys=True, separators=(",", ":"))
                ctx["samples"].append((ts, "error", tags, 1))
        elif data["request_id"] in charges:
            accepted.append(charges[data["request_id"]])
    ctx["spend"][:] = accepted
    exhausted_at = max(row[6] for row in accepted if row[1] == profile)
    for number in range(1, 4):
        ts = exhausted_at + number * rng.randint(1200, 2400)
        prompt = _lognormal(rng, 1500, 0.4, 600, 3000)
        _remote_receipt(
            ctx, request_id=f"req_household_budget_rejected_{number}", slug="robin",
            ts=min(ts, now - 60), status="rejected", prompt_tokens=prompt, completion_tokens=0,
            status_code=429, reason="budget_exceeded", latency_ms=rng.randint(8, 40),
            warning="The Everyone plan has used its daily and monthly remote spending allowance.",
        )
    ctx["details"].update(
        member="robin", plan="Everyone", provider=REMOTE_SERVICE,
        daily_cap_usd="1", monthly_cap_usd="8",
        daily_spent_usd=str(total(day_start)), monthly_spent_usd=str(total(month_start)),
        exhausted_at=exhausted_at, rejections=3,
    )


def _scenario_provider_down(ctx):
    """The paid provider failed REMOTE_DOWN_HOURS ago; paid routes now return 503."""
    rng, now = ctx["rng"], ctx["now"]
    last_success = now - REMOTE_DOWN_HOURS * 3600
    error = "HTTP 503: provider unavailable"
    # Base requests share the same outage window as the explicit failures.
    from gateway import receipt_store

    failed_ids = set()
    for index, row in enumerate(ctx["receipt_rows"]):
        data = json.loads(row[-1])
        if (row[1] > last_success and data.get("remote")
                and data.get("backend") == REMOTE_SERVICE
                and data["status"] == "succeeded"):
            data.update(status="failed", policy_reasons=[],
                        warnings=["Remote backup is not answering. Local models still work."])
            data["usage"]["completion_tokens"] = 0
            data["cost"]["usd"] = "0.000000000"
            data["metadata"].update(status_code=503, litellm={"response_cost": "0.000000000"})
            data["latency_ms"] = rng.randint(1500, 3200)
            data["updated_at_ms"] = row[1] * 1000 + data["latency_ms"]
            ctx["receipt_rows"][index] = receipt_store._row_values(data, created_at=row[1])
            failed_ids.add(data["request_id"])
            tags = json.dumps(
                {"key": data["api_key_name"], "app_id": data["app_id"],
                 "model": data["requested_model"], "backend": REMOTE_SERVICE,
                 "intent": data["intent"]}, sort_keys=True, separators=(",", ":"))
            ctx["samples"].append((row[1], "error", tags, 1))
    ctx["spend"][:] = [row for row in ctx["spend"] if row[-1] not in failed_ids]
    for number, (slug, minutes) in enumerate(
        [("sam", 205), ("robin", 160), ("sam", 96), ("zoe", 50), ("robin", 12)], 1
    ):
        _remote_receipt(
            ctx, request_id=f"req_household_down_{number}", slug=slug, ts=now - minutes * 60,
            status="failed", prompt_tokens=_lognormal(rng, 700, 0.4, 200, 2000), completion_tokens=0,
            status_code=503, latency_ms=rng.randint(1500, 3200),
            warning="Remote backup is not answering. Local models still work.",
        )
    ctx["details"].update(
        provider=REMOTE_SERVICE, last_success_at=last_success, error=error,
        affected_models=["claude-sonnet", "gpt-mini"], failed_requests=5,
    )


def _scenario_running(ctx):
    """In-flight work: two accepted receipts, one running job, one failed job."""
    from gateway import media_jobs, receipt_store
    from gateway.receipts import RouteReceipt

    now, apps, hashes, paths = ctx["now"], ctx["apps"], ctx["hashes"], ctx["paths"]

    def receipt(request_id, slug, intent, path, model, backend, accepted_s, status, **extra):
        item = RouteReceipt(
            protocol="openai", intent=intent, path=path, request_id=request_id,
            app_id=apps[slug]["id"], app_profile=apps[slug]["default_profile_id"],
            api_key_name=apps[slug]["name"], requested_model=model, resolved_model=model,
            backend=backend, status=status, owner_key_hash=hashes[slug],
            accepted_at_ms=(now - accepted_s) * 1000, **extra,
        )
        if status == "in_progress":
            item.updated_at_ms = (now - 3) * 1000
        data = receipt_store._coerce_to_internal_dict(item)
        receipt_store._assert_safe(data)
        with sqlite3.connect(paths["receipts"]) as conn:
            conn.execute(
                "INSERT INTO receipts(request_id,created_at,protocol,intent,path,app_profile,app_id,api_key_name,status,job_id,parent_request_id,receipt_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                receipt_store._row_values(data, created_at=now - accepted_s),
            )
        with sqlite3.connect(paths["metrics"]) as conn:
            conn.execute(
                "INSERT INTO samples(ts,metric,tags,value) VALUES(?,?,?,?)",
                (now - accepted_s, "request", json.dumps(
                    {"key": apps[slug]["name"], "app_id": apps[slug]["id"], "model": model,
                     "backend": backend, "intent": intent}, sort_keys=True, separators=(",", ":")), 1),
            )
        ctx["extra_requests"] = ctx.get("extra_requests", 0) + 1

    def job(request_id, slug, intent, model, accepted_s, status, progress, error=None):
        media_jobs.create(
            media_jobs.MediaJob(
                request_id=request_id, intent=intent, status=status, progress=progress,
                error=error, app_id=apps[slug]["id"], owner_key_hash=hashes[slug],
                created_at=now - accepted_s, updated_at=now - 3 if status == "running" else now - accepted_s + 41,
                metadata={"model": model},
            ),
            path=paths["media_jobs"],
        )

    receipt("req_household_running_chat", "robin", "chat.completions", "/v1/chat/completions",
            "qwen-27b", "household-chat", 38, "in_progress", stream=True)
    receipt("req_household_running_video", "sam", "video.generate", "/v1/videos",
            "ltx-2.5", "household-video", 140, "in_progress")
    job("req_household_running_video", "sam", "video.generate", "ltx-2.5", 140, "running", 42)
    receipt("req_household_failed_image", "alex", "image.generate", "/v1/images/generations",
            "krea2-turbo", "comfyui", 1320, "failed", latency_ms=41000,
            metadata={"status_code": 503}, warnings=["The image engine ran out of memory before finishing."])
    job("req_household_failed_image", "alex", "image.generate", "krea2-turbo", 1320, "failed", 0,
        error="The image engine ran out of memory before finishing.")
    ctx["details"].update(
        in_progress_receipts=["req_household_running_chat", "req_household_running_video"],
        running_job="req_household_running_video", failed_job="req_household_failed_image",
    )


# Fixture indexes whose maker ran them with "Don't keep this prompt": the
# Library shows the maker "Prompt not saved" and everyone else no prompt.
PROMPT_NOT_KEPT = frozenset({4, 13})


def _media(state, paths, apps, hashes, now, rng, people):
    from gateway import media_jobs, prompt_memory, receipt_store
    from gateway.receipts import RouteReceipt

    entries = json.loads((FIXTURES / "manifest.json").read_text())
    cache = state / "images"
    cache.mkdir(exist_ok=True)
    assets = []
    ts = now - rng.randint(20 * 60, 5 * 3600)
    for index, entry in enumerate(entries):
        source = FIXTURES / entry["file"]
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        kind = entry["kind"]
        prefix = {"image": "img", "audio": "aud", "video": "vid"}[kind]
        asset_id = f"{prefix}_{digest}"
        shutil.copyfile(source, cache / f"{digest}{source.suffix}")
        owner = entry["owner"]
        if index:
            # Creations come in bursts and long gaps, not on a fixed grid.
            ts -= rng.randint(120, 900) if rng.random() < 0.3 else rng.randint(2 * 3600, 30 * 3600)
        latency_ms = _lognormal(
            rng, {"image": 14000, "audio": 1400, "video": 95000}[kind], 0.35, 300, 600000
        )
        request_id = f"req_household_asset_{index:02}"
        # The cache ages files by mtime; match it to the asset's own creation time.
        os.utime(cache / f"{digest}{source.suffix}", (ts, ts))
        maker = people[owner]["id"]
        (cache / f"{digest}.meta.json").write_text(
            json.dumps(
                {
                    "asset_id": asset_id,
                    "owner_key_hashes": [hashes[owner]],
                    "maker_person_ids": [maker],
                    "updated_at": ts,
                }
            )
        )
        # The prompt lives only in the maker's prompt record (PROMPT_MEMORY.md),
        # through the same write path, settings and lock as a real request.
        prompt_kept = index not in PROMPT_NOT_KEPT and prompt_memory.put(
            asset_id, maker, entry["prompt"], kept_until=None,
            path=paths["media_jobs"], admin_path=paths["admin"], created_at=ts,
        )
        poster_asset_id = None
        if entry.get("poster"):
            # A video's poster is its own cached image asset, owned like the video.
            poster_source = FIXTURES / entry["poster"]
            poster_digest = hashlib.sha256(poster_source.read_bytes()).hexdigest()
            poster_asset_id = f"img_{poster_digest}"
            shutil.copyfile(poster_source, cache / f"{poster_digest}{poster_source.suffix}")
            os.utime(cache / f"{poster_digest}{poster_source.suffix}", (ts, ts))
            (cache / f"{poster_digest}.meta.json").write_text(
                json.dumps(
                    {
                        "asset_id": poster_asset_id,
                        "owner_key_hashes": [hashes[owner]],
                        "maker_person_ids": [maker],
                        "updated_at": ts,
                    }
                )
            )
        intent = {
            "image": "image.generate",
            "audio": "audio.speech",
            "video": "video.generate",
        }[kind]
        receipt = RouteReceipt(
            protocol="openai",
            intent=intent,
            request_id=request_id,
            app_id=apps[owner]["id"],
            app_profile=apps[owner]["default_profile_id"],
            api_key_name=apps[owner]["name"],
            requested_model=entry["model"],
            resolved_model=entry["model"],
            status="succeeded",
            accepted_at_ms=ts * 1000,
            updated_at_ms=ts * 1000 + latency_ms,
            latency_ms=latency_ms,
            metadata={},
            owner_key_hash=hashes[owner],
        )
        receipt_store.record(receipt, created_at=ts, path=paths["receipts"])
        asset = {
            key: entry[key]
            for key in ("model", "duration_s", "width", "height")
            if key in entry
        }
        asset.update(asset_id=asset_id, media_type=mimetypes.guess_type(source.name)[0])
        if poster_asset_id:
            asset["poster_asset_id"] = poster_asset_id
        media_jobs.create(
            media_jobs.MediaJob(
                request_id=request_id,
                intent=intent,
                status="succeeded",
                progress=100,
                assets=[asset],
                app_id=apps[owner]["id"],
                owner_key_hash=hashes[owner],
                created_at=ts,
                updated_at=ts + max(1, latency_ms // 1000),
                metadata={"model": entry["model"]},
            ),
            path=paths["media_jobs"],
        )
        with sqlite3.connect(paths["metrics"]) as conn:
            metrics_store_tags = {
                "key": apps[owner]["name"],
                "app_id": apps[owner]["id"],
                "model": entry["model"],
                "intent": intent,
            }
            conn.execute(
                "INSERT INTO samples(ts,metric,tags,value) VALUES(?,?,?,?)",
                (
                    ts,
                    "request",
                    json.dumps(
                        metrics_store_tags, sort_keys=True, separators=(",", ":")
                    ),
                    1,
                ),
            )
        assets.append({"asset_id": asset_id, "owner": owner, "kind": kind,
                       "prompt_kept": bool(prompt_kept)})
    return assets


if __name__ == "__main__":
    import sys

    seed(Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else None)
