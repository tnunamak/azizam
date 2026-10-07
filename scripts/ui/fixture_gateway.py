"""Launch the browser fixture gateway with an enforced connection boundary."""

import os
import sys
from scripts.ui.fixture_guard import install, parse_allowed_ports

port, engine_port = map(int, sys.argv[1:])
install(
    {
        port,
        engine_port,
        *parse_allowed_ports(os.environ.get("UI_E2E_ALLOWED_PORTS", "")),
    },
    media_root=os.path.join(os.getcwd(), "images")
    if os.environ.get("UI_SEED_PROFILE") == "household"
    else None,
)

if os.environ.get("UI_SEED_PROFILE") == "household":
    # Keep the profile's thirty-day operational history across startup pruning.
    from gateway import receipt_store, media_jobs, media_delivery
    from pathlib import Path

    from gateway import default_profiles, admin_store

    original_seed_profiles = default_profiles.seed_default_profiles

    def household_seed_profiles(*, path=admin_store.DB_PATH, now=None):
        # The three household plans already exist. Remove only unused defaults
        # created at this startup, through the store's reference-checked API.
        for profile in original_seed_profiles(path=path, now=now):
            admin_store.delete_profile(profile["id"], path=path)
        return []

    default_profiles.seed_default_profiles = household_seed_profiles
    media_delivery.DEFAULT_WORKDIR = Path.cwd() / "media_metadata"
    import status_endpoint

    original_grid = status_endpoint._backend_grid

    async def household_backend_grid(config):
        rows = await original_grid(config)
        for row in rows:
            if config.get("backends", {}).get(row["key"], {}).get("enabled") is False:
                row.update(
                    status="stopped",
                    live_status="stopped",
                    last_error=None,
                    latency_ms=None,
                )
        return rows

    status_endpoint._backend_grid = household_backend_grid
    from functools import partial

    receipt_store.RETAIN_S = 31 * 86400
    media_jobs.RETAIN_S = 31 * 86400
    receipt_store.prune = partial(receipt_store.prune, retain_s=31 * 86400)
    media_jobs.prune = partial(media_jobs.prune, retain_s=31 * 86400)

import uvicorn

uvicorn.run(
    "proxy:app", host="127.0.0.1", port=port, log_level="warning", loop="asyncio"
)
