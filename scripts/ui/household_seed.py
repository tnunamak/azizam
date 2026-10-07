"""Opt-in household data for the isolated browser harness; never imports live state."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
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
FIXTURES = Path(__file__).parent / "fixtures" / "household"
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
    "household-image": [
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
SERVICE_NAMES = {
    "household-chat": "Study and coding",
    "household-image": "Image studio",
    "household-voice": "Home Assistant voice",
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


def registry(engine_port: int) -> dict:
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
                spend_caps_usd={"daily": 1, "monthly": 8},
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
                else "healthy",
                "enabled": True,
                "source": "ui",
                "tags": [cap],
            }
    return normalize_service_registry(
        {"schema_version": 1, "services": services, "capabilities": capabilities}
    )


def seed(state: Path, port: int) -> dict:
    """Seed fresh isolated databases, using the same schemas and stores as the gateway."""
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
    now = int(time.time())
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
                "engines": {
                    "tts-1": {"service": "household-voice", "model": "tts-1"}
                },
            },
        }
        config_path.write_text(yaml.safe_dump(document))
    reg = registry(port + 1)
    admin_store.write_service_registry(
        reg, expected_generation=0, updated_by="household-fixture", path=paths["admin"]
    )
    plans = {}
    all_models = [mid for group in MODEL_GROUPS.values() for mid, _, _ in group]
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
            path=paths["admin"],
        )
    admin_store.create_runtime_profile_mapping(
        source_kind="all_users",
        source_value="*",
        profile_id=plans["Everyone"]["id"],
        path=paths["admin"],
    )
    people, apps, hashes = {}, {}, {}
    for index, (slug, name, plan) in enumerate(
        [
            ("sam", "Sam", "Builder"),
            ("robin", "Robin", "Everyone"),
            ("alex", "Alex", "Teen"),
            ("casey", "Casey · former guest", "Everyone"),
        ]
    ):
        user = admin_store.upsert_oidc_user(
            "local",
            slug,
            email=f"{slug}@example.test",
            display_name=name,
            path=paths["admin"],
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
        app = admin_store.create_app(
            f"{name.split(' ·')[0]}'s personal app",
            kind="personal",
            owner_user_id=user["id"],
            default_profile_id=plans[plan]["id"],
            path=paths["admin"],
        )
        apps[slug] = app
    for slug, owner, name in [
        ("webui", "robin", "Open WebUI"),
        ("home", "sam", "Home Assistant voice"),
        ("codex", "sam", "Codex"),
        ("telegram", "robin", "Telegram helper"),
        ("librechat", "sam", "LibreChat · retired"),
    ]:
        apps[slug] = admin_store.create_app(
            name,
            kind="personal",
            owner_user_id=people[owner]["id"],
            default_profile_id=plans["Builder" if owner == "sam" else "Everyone"]["id"],
            path=paths["admin"],
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
    for email in ["june@example.test", "morgan@example.test"]:
        _link, _token = accounts.create_link(
            "invite",
            people["sam"]["id"],
            email=email,
            profile_id=plans["Everyone"]["id"],
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
            "household-image",
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
            "household-image",
        ),
        ("music", "audio.music", "/v1/audio/music", "musicgen", "household-voice"),
        ("sfx", "audio.sfx", "/v1/audio/sfx", "audio-sfx", "household-voice"),
    ]
    samples, spend, receipt_rows = [], [], []
    evening = datetime.fromtimestamp(now, ZoneInfo("America/Chicago")).replace(
        hour=21, minute=0, second=0, microsecond=0
    )
    if evening.timestamp() > now:
        evening -= timedelta(days=1)
    for day in range(30):
        for index in range(30 + (day % 4) * 5):
            slug = active_apps[(index + day) % len(active_apps)]
            _cap, intent, endpoint, model, backend = capabilities[
                index % len(capabilities)
            ]
            ts = (
                int((evening - timedelta(days=day)).timestamp())
                - (index % 8) * 900
                - (12 * 3600 if index % 7 == 0 else 0)
            )
            if day > 4 and index % 17 == 0:
                _cap, intent, endpoint, model, backend = (
                    "embeddings",
                    "embeddings",
                    "/v1/embeddings",
                    "bge-m3",
                    "household-embedding",
                )
            remote = index % 11 == 0 and slug != "alex"
            if remote:
                _cap, intent, endpoint, model, backend = (
                    "chat",
                    "chat.completions",
                    "/v1/chat/completions",
                    "claude-sonnet",
                    "household-remote",
                )
            failure = (
                [
                    "model_access_denied",
                    "upstream_timeout",
                    "upstream_unavailable",
                    "budget_exceeded",
                    "invalid_request",
                ][(index + day) % 5]
                if index % 13 == 0
                else None
            )
            if failure in {"model_access_denied", "budget_exceeded"}:
                remote = True
                _cap, intent, endpoint, model, backend = (
                    "chat",
                    "chat.completions",
                    "/v1/chat/completions",
                    "claude-sonnet",
                    "household-remote",
                )
                if failure == "model_access_denied":
                    slug = "alex"
                elif slug == "alex":
                    slug = "sam"
            if failure == "upstream_unavailable":
                _cap, intent, endpoint, model, backend = (
                    "video",
                    "video.generate",
                    "/v1/videos",
                    "ltx-2.5",
                    "household-video",
                )
                remote = False
            prompt_tokens, completion_tokens = (
                (4000 + index * 70, 1400 + index * 45)
                if remote
                else (350 + index * 31, 140 + index * 17)
            )
            cost = (
                str(
                    (
                        Decimal(prompt_tokens) * Decimal("0.000003")
                        + Decimal(completion_tokens) * Decimal("0.000015")
                    ).quantize(Decimal("0.000000001"))
                )
                if remote and not failure
                else "0.000000000"
            )
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
                latency_ms=1200 + index * 87,
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
                updated_at_ms=ts * 1000 + 1200 + index * 87,
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
    with sqlite3.connect(paths["receipts"]) as conn:
        conn.executemany(
            "INSERT INTO receipts(request_id,created_at,protocol,intent,path,app_profile,app_id,api_key_name,status,job_id,parent_request_id,receipt_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            receipt_rows,
        )
    for hour in range(30 * 24):
        ts = now - hour * 3600
        for sid in MODEL_GROUPS:
            tags = json.dumps(
                {
                    "backend": sid,
                    "name": SERVICE_NAMES[sid],
                    "role": reg["services"][sid]["role"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            up = (sid != "household-embedding" or hour > 4 * 24) and not (
                sid == "household-video" and hour % 7 in {0, 1}
            )
            samples.extend(
                [
                    (ts, "service_up", tags, int(up)),
                    (ts, "service_status_code", tags, 1 if up else 0),
                    (ts, "service_latency_ms", tags, 22 + hour % 14 if up else 1400),
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
                "household-remote": {"caps": {"daily_usd": 1, "monthly_usd": 8}}
            },
            "global_caps": {"daily_usd": 1, "monthly_usd": 8},
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
    assets = _media(state, paths, apps, hashes, now)
    manifest = {
        "base_url": f"http://127.0.0.1:{port}",
        "owner_email": "sam@example.test",
        "member_email": "robin@example.test",
        "restricted_email": "alex@example.test",
        "password": PASSWORD,
        "people": {slug: row["id"] for slug, row in people.items()},
        "apps": {slug: row["id"] for slug, row in apps.items()},
        "assets": assets,
        "request_count": sum(metric == "request" for _, metric, _, _ in samples)
        + len(assets),
    }
    (state / "household-manifest.json").write_text(json.dumps(manifest))
    target = os.environ.get("UI_HOUSEHOLD_MANIFEST_PATH")
    if target:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text(json.dumps(manifest))
    return manifest


def _media(state, paths, apps, hashes, now):
    from gateway import media_jobs, receipt_store
    from gateway.receipts import RouteReceipt

    entries = json.loads((FIXTURES / "manifest.json").read_text())
    cache = state / "images"
    cache.mkdir(exist_ok=True)
    assets = []
    for index, entry in enumerate(entries):
        source = FIXTURES / entry["file"]
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        kind = entry["kind"]
        prefix = {"image": "img", "audio": "aud", "video": "vid"}[kind]
        asset_id = f"{prefix}_{digest}"
        shutil.copyfile(source, cache / f"{digest}{source.suffix}")
        owner = entry["owner"]
        ts = now - index * 11 * 3600
        request_id = f"req_household_asset_{index:02}"
        (cache / f"{digest}.meta.json").write_text(
            json.dumps(
                {
                    "asset_id": asset_id,
                    "owner_key_hashes": [hashes[owner]],
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
            updated_at_ms=ts * 1000 + 4200,
            latency_ms=4200,
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
                updated_at=ts + 5,
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
        assets.append({"asset_id": asset_id, "owner": owner, "kind": kind})
    return assets


if __name__ == "__main__":
    import sys

    seed(Path(sys.argv[1]), int(sys.argv[2]))
