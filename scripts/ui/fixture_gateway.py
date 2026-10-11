"""Launch the browser fixture gateway with an enforced connection boundary."""

import os
import sys
from scripts.ui.fixture_guard import install, parse_allowed_ports

def _freeze_household_clock(now):
    """Freeze Python wall clocks process-wide; leave monotonic timers running."""
    import datetime
    import time

    real_datetime, real_date = datetime.datetime, datetime.date
    real_gmtime, real_localtime = time.gmtime, time.localtime
    real_ctime, real_strftime = time.ctime, time.strftime

    class FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(now, tz)

        @classmethod
        def utcnow(cls):
            return cls.fromtimestamp(now, datetime.timezone.utc).replace(tzinfo=None)

        @classmethod
        def today(cls):
            return cls.now()

    class FrozenDate(real_date):
        @classmethod
        def today(cls):
            return cls.fromtimestamp(now)

    replacements = [
        (time, "time", lambda: float(now)),
        (time, "time_ns", lambda: int(now * 1_000_000_000)),
        (time, "gmtime", lambda seconds=None: real_gmtime(now if seconds is None else seconds)),
        (time, "localtime", lambda seconds=None: real_localtime(now if seconds is None else seconds)),
        (time, "ctime", lambda seconds=None: real_ctime(now if seconds is None else seconds)),
        (time, "strftime", lambda fmt, value=None: real_strftime(fmt, real_localtime(now) if value is None else value)),
        (datetime, "datetime", FrozenDatetime),
        (datetime, "date", FrozenDate),
    ]
    aliases = {id(getattr(module, name)): replacement for module, name, replacement in replacements}
    # Rewrite existing module-level aliases regardless of their spelling or origin.
    for module in list(sys.modules.values()):
        for name, value in list(vars(module).items()) if module is not None else ():
            replacement = aliases.get(id(value))
            if replacement is not None:
                setattr(module, name, replacement)
    # Updating the canonical modules also covers every subsequent Python import.
    for module, name, replacement in replacements:
        setattr(module, name, replacement)


port, engine_port = map(int, sys.argv[1:])
install(
    {
        port,
        engine_port,
        *parse_allowed_ports(os.environ.get("UI_E2E_ALLOWED_PORTS", "")),
    },
    # Fixtures that serve seeded media (household, live data) allow the
    # metadata ffprobe/ffmpeg calls on files in their own images directory.
    media_root=os.environ.get("UI_E2E_MEDIA_ROOT")
    or (os.path.join(os.getcwd(), "images")
        if os.environ.get("UI_SEED_PROFILE") == "household" else None),
)

if os.environ.get("UI_SEED_PROFILE") == "household":
    from scripts.ui.household_seed import FIXED_NOW

    _freeze_household_clock(int(os.environ.get("UI_HOUSEHOLD_NOW") or FIXED_NOW))
    import proxy  # noqa: F401
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
