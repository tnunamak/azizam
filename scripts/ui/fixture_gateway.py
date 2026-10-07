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
    # Serve the seeded instant, not the wall clock, so age and window logic matches
    # the fixed data. Only repo modules are frozen; the event loop keeps real time.
    import time as _time

    from scripts.ui.household_seed import FIXED_NOW

    class _FrozenTime:
        def __getattr__(self, name):
            return getattr(_time, name)

        @staticmethod
        def time():
            return int(os.environ.get("UI_HOUSEHOLD_NOW") or FIXED_NOW) + 0.0

    import datetime as _datetime

    class _FrozenDatetime(_datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(_FrozenTime.time(), tz)

        @classmethod
        def utcnow(cls):
            return cls.fromtimestamp(_FrozenTime.time(), _datetime.timezone.utc).replace(tzinfo=None)

    import proxy  # noqa: F401  (load repo modules before freezing them)

    for _module in list(sys.modules.values()):
        _file = getattr(_module, "__file__", None) or ""
        if getattr(_module, "time", None) is _time and "/venv/" not in _file and "scripts/ui" not in _file and _file.startswith(os.environ["PYTHONPATH"]):
            _module.time = _FrozenTime()
        if getattr(_module, "datetime", None) is _datetime.datetime and _file.startswith(os.environ["PYTHONPATH"]) and "/venv/" not in _file and "scripts/ui" not in _file:
            _module.datetime = _FrozenDatetime
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

    scenario = os.environ.get("UI_HOUSEHOLD_SCENARIO", "default")
    if scenario == "stale-health":
        # These services were last observed hours ago (voice) or never (embedding):
        # the fixture gateway reports them as unobserved and records no new samples.
        from scripts.ui.household_seed import STALE_VOICE_HOURS

        unobserved = {"household-voice", "household-embedding"}
        seeded_now = int(os.environ.get("UI_HOUSEHOLD_NOW") or FIXED_NOW)
        observing_grid = status_endpoint._backend_grid

        async def stale_backend_grid(config):
            rows = await observing_grid(config)
            for row in rows:
                if row["key"] in unobserved:
                    row.update(status="unknown", live_status="unknown", latency_ms=None, last_error=None)
                    row["observed_at_s"] = (
                        seeded_now - STALE_VOICE_HOURS * 3600 if row["key"] == "household-voice" else None
                    )
            return rows

        record_samples = status_endpoint._record_backend_health_samples
        status_endpoint._backend_grid = stale_backend_grid
        status_endpoint._record_backend_health_samples = lambda backends: record_samples(
            [row for row in backends or [] if row.get("key") not in unobserved]
        )
    from functools import partial

    receipt_store.RETAIN_S = 31 * 86400
    media_jobs.RETAIN_S = 31 * 86400
    receipt_store.prune = partial(receipt_store.prune, retain_s=31 * 86400)
    media_jobs.prune = partial(media_jobs.prune, retain_s=31 * 86400)

import uvicorn

uvicorn.run(
    "proxy:app", host="127.0.0.1", port=port, log_level="warning", loop="asyncio"
)
