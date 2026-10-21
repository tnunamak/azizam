"""No-content precondition for the pinned, gateway-managed execution substrate.

Only structural results leave the check. No database rows, URLs or exceptions
are logged. Management calls remain available while execution is refused.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml


@lru_cache(maxsize=1)
def profile() -> dict[str, Any]:
    return yaml.safe_load((Path(__file__).resolve().parent.parent / "registry/litellm_no_content.yml").read_text())


def config_problem(config: Mapping[str, Any]) -> str | None:
    for section in ("general_settings", "litellm_settings", "router_settings"):
        actual = config.get(section)
        if not isinstance(actual, Mapping):
            return section
        for key, expected in profile()[section].items():
            value = actual.get(key)
            if type(value) is not type(expected) or value != expected:
                return f"{section}.{key}"
    if "cache_params" in config.get("litellm_settings", {}):
        return "litellm_settings.cache_params"
    allowed = {
        "general_settings": set(profile()["general_settings"]) | {"litellm_key_header_name", "forward_client_headers_to_llm_api"},
        "litellm_settings": set(profile()["litellm_settings"]) | {"drop_params"},
        "router_settings": set(profile()["router_settings"]) | {"num_retries"},
    }
    for section, keys in allowed.items():
        if set(config[section]) - keys:
            return section + ".unexpected_setting"
    if set(config) - {"model_list", *allowed}:
        return "unexpected_section"
    return None


def child_environment(base: Mapping[str, str], *, declared: set[str]) -> dict[str, str]:
    basics = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP", "DATABASE_URL",
              "LITELLM_MASTER_KEY", "AZIZAM_INTERNAL_LITELLM_KEY", "SSL_CERT_FILE", "SSL_CERT_DIR",
              "LITELLM_LOCAL_MODEL_COST_MAP", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "REQUESTS_CA_BUNDLE"}
    # Never let a record turn a runtime control back on through an env reference.
    blocked = {"STORE_MODEL_IN_DB", "STORE_PROMPTS_IN_SPEND_LOGS", "DEBUG", "DETAILED_DEBUG"}
    allowed = basics | {name for name in declared if name not in blocked and not name.startswith(("LITELLM_", "OTEL_", "LANGFUSE_"))}
    result = {name: base[name] for name in allowed if name in base}
    result.update(profile()["environment"])
    return result


def process_generation(pid: int) -> tuple[int, str, int]:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    if fields[0] == "Z":
        raise ValueError("process exited")
    return pid, fields[19], int(fields[2])


def write_launch_proof(path: Path, config_path: Path, env: Mapping[str, str]) -> None:
    if config_problem(yaml.safe_load(config_path.read_text())):
        raise ValueError("LiteLLM no-content configuration is incomplete")
    if any(env.get(key) != value for key, value in profile()["environment"].items()):
        raise ValueError("LiteLLM no-content environment is incomplete")
    payload = {"generation": process_generation(os.getpid()), "config": str(config_path.resolve()),
               "sha256": hashlib.sha256(config_path.read_bytes()).hexdigest()}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle)


# Select only booleans. Identity metadata, credentials and request text never
# leave PostgreSQL. Missing tables/columns or unreadable rows fail closed.
DB_CHECK_SQL = '''
SELECT
 EXISTS (SELECT 1 FROM "LiteLLM_Config" WHERE param_name IN ('general_settings', 'litellm_settings') AND (
   coalesce(jsonb_typeof(param_value), 'null') <> 'object' OR
   lower(coalesce(param_value->>'store_model_in_db', 'false')) <> 'false' OR
   lower(coalesce(param_value->>'store_prompts_in_spend_logs', 'false')) <> 'false' OR
   param_value ?| ARRAY['callbacks','success_callback','failure_callback','cache','cache_params'] OR
   (param_value ? 'disable_spend_logs' AND param_value->>'disable_spend_logs' <> 'true') OR
   (param_value ? 'disable_error_logs' AND param_value->>'disable_error_logs' <> 'true')
 )) AS config_unsafe,
 EXISTS (SELECT 1 FROM "LiteLLM_VerificationToken" WHERE
   coalesce(metadata->'logging', 'null'::jsonb) NOT IN ('null'::jsonb, '[]'::jsonb, '{}'::jsonb, 'false'::jsonb, '""'::jsonb)) AS key_unsafe,
 EXISTS (SELECT 1 FROM "LiteLLM_TeamTable" WHERE
   coalesce(metadata->'logging', 'null'::jsonb) NOT IN ('null'::jsonb, '[]'::jsonb, '{}'::jsonb, 'false'::jsonb, '""'::jsonb)
   OR coalesce(metadata->'callback_settings', 'null'::jsonb) NOT IN ('null'::jsonb, '[]'::jsonb, '{}'::jsonb, 'false'::jsonb, '""'::jsonb)) AS team_unsafe
'''


async def database_problem(database_url: str) -> str | None:
    if not database_url:
        return None
    from prisma import Prisma

    client = Prisma(datasource={"url": database_url})
    try:
        await client.connect()
        async with client.tx() as transaction:
            await transaction.execute_raw("SET TRANSACTION READ ONLY")
            await transaction.execute_raw("SET LOCAL statement_timeout = '5000ms'")
            rows = await transaction.query_raw(DB_CHECK_SQL)
        if len(rows) != 1 or set(rows[0]) != {"config_unsafe", "key_unsafe", "team_unsafe"}:
            return "database_check_unavailable"
        for key, value in rows[0].items():
            if value is not False:
                return f"database_{key}"
        return None
    finally:
        await client.disconnect()


async def compose_database_unseen(base_url: str) -> bool:
    import httpx
    from gateway.backends.llm_execution import litellm_headers
    from gateway.log_scrub import env_secret

    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        response = await client.get(base_url.rstrip("/") + "/health/readiness/details",
            headers=litellm_headers({}, api_key=env_secret("AZIZAM_LITELLM_API_KEY")))
        response.raise_for_status()
        details = response.json()
    # The pinned readiness endpoint distinguishes absent Prisma from an
    # unreachable configured DB. Missing diagnostics are not proof of absence.
    if details.get("db") != "Not connected":
        return True
    if details.get("log_level") != "CRITICAL" or details.get("cache") is not None or details.get("success_callbacks") not in ([], ["sync_deployment_callback_on_success"]):
        raise ValueError("Compose effective runtime differs from profile")
    return False


# Fixed vocabulary: never interpolate exception text, URLs or database values.
PROBLEM_FIXES = {
    "not_checked": "Run the pinned no-content launcher and wait for the profile check.",
    "profile_check_unavailable": "Check the pinned no-content launcher, generated config and DATABASE_URL connectivity, then retry.",
    "database_check_unavailable": "Check DATABASE_URL permissions and the pinned database schema, then retry.",
    "database_config_unsafe": "In LiteLLM_Config, disable store_model_in_db and store_prompts_in_spend_logs, enable disable_spend_logs and disable_error_logs, and remove callback and cache overrides.",
    "database_key_unsafe": "Remove metadata.logging overrides from LiteLLM_VerificationToken rows, then retry.",
    "database_team_unsafe": "Remove metadata.logging and metadata.callback_settings overrides from LiteLLM_TeamTable rows, then retry.",
}
logger = logging.getLogger(__name__)


class ContentGate:
    def __init__(self) -> None:
        self.checked: tuple[Any, ...] | None = None
        self.checked_at = 0.0
        self.problem: str | None = "not_checked"
        self.lock = asyncio.Lock()
        self.unseen_database = False
        self.retry_seconds = 30
        self.next_check_at = 0.0

    def mode(self, base_url: str) -> str:
        endpoint = urlsplit(base_url)
        if os.environ.get("AZIZAM_MANAGE_LITELLM") == "true" and endpoint.hostname in {"localhost", "127.0.0.1", "::1"} and endpoint.port == 4011:
            return "managed"
        if os.environ.get("AZIZAM_LITELLM_COMPOSE_PROFILE") == "true" and endpoint.hostname == "litellm" and endpoint.port == 4000:
            return "compose"
        return "unmanaged"

    def generation(self, base_url: str) -> tuple[Any, ...]:
        if self.mode(base_url) == "compose":
            return ("compose", base_url)
        pid_path = Path(os.environ.get("AZIZAM_LITELLM_PID_FILE", "data/litellm.pid"))
        generation = process_generation(int(pid_path.read_text().strip()))
        proof = json.loads(Path(str(pid_path) + ".retention.json").read_text())
        if tuple(proof["generation"]) != generation:
            raise ValueError("stale launch proof")
        argv = Path(f"/proc/{generation[0]}/cmdline").read_bytes().decode().split("\0")
        config_path = Path(proof["config"])
        if "--config" not in argv or Path(argv[argv.index("--config") + 1]).resolve() != config_path:
            raise ValueError("different process command")
        if not any(Path(arg).resolve() == Path(__file__).resolve().with_name("litellm_launcher.py") for arg in argv if arg):
            raise ValueError("different process command")
        if generation == self.checked:
            return generation
        data = config_path.read_bytes()
        if hashlib.sha256(data).hexdigest() != proof["sha256"] or config_problem(yaml.safe_load(data)):
            raise ValueError("changed launch configuration")
        return generation

    async def check(self, base_url: str, *, force: bool = False) -> str | None:
        if self.mode(base_url) == "unmanaged":
            return None
        async with self.lock:
            previous = self.problem
            try:
                # Unavailable process evidence also backs off; no content is admitted.
                if not force and self.checked is None and time.monotonic() < self.next_check_at:
                    return self.problem
                generation = self.generation(base_url)
                if not force and generation == self.checked and time.monotonic() < self.next_check_at:
                    return self.problem
                self.unseen_database = False
                database_url = os.environ.get("DATABASE_URL", "")
                if self.mode(base_url) == "compose" and not database_url:
                    self.unseen_database = await compose_database_unseen(base_url)
                problem = await asyncio.wait_for(database_problem(database_url), timeout=10)
                if generation != self.generation(base_url):
                    raise ValueError("process restarted during check")
                self.checked, self.checked_at, self.problem = generation, time.monotonic(), problem
            except Exception:
                self.checked = None
                self.problem = "profile_check_unavailable"
                self.checked_at = time.monotonic()
            if self.problem not in PROBLEM_FIXES and self.problem is not None:
                self.problem = "profile_check_unavailable"
            if self.problem != previous:
                logger.warning("litellm_content_state=%s", self.problem or "verified")
                self.retry_seconds = 30
            self.next_check_at = self.checked_at + (3600 if self.problem is None else self.retry_seconds)
            self.retry_seconds = min(60, self.retry_seconds * 2) if self.problem else 30
            return self.problem

    def status(self, base_url: str) -> dict[str, Any]:
        mode = self.mode(base_url)
        if mode == "compose" and self.unseen_database:
            return {"status": "unmanaged", "detail": "Azizam cannot check the database settings of the LiteLLM that runs chat for it."}
        if mode == "unmanaged":
            return {"status": mode, "detail": "LiteLLM, which runs chat for Azizam, is run outside Azizam. Azizam cannot confirm that it keeps nothing."}
        problem = self.problem if self.problem in PROBLEM_FIXES else "profile_check_unavailable"
        fix = PROBLEM_FIXES[problem] if self.problem else ""
        return {"status": "verified" if self.problem is None else "litellm_content_unverified",
                "problem": problem if self.problem else None, "fix": fix,
                "detail": "LiteLLM content capture is disabled." if self.problem is None else
                    f"LiteLLM content capture could not be ruled out ({problem}). {fix}"}


gate = ContentGate()


async def execution_refusal(base_url: str):
    if await gate.check(base_url) is None:
        return None
    from gateway.egress import safe_error_response
    return safe_error_response(503, "litellm_content_unverified", gate.status(base_url)["detail"])


def deployment_status(registry: Mapping[str, Any] | None = None) -> dict[str, Any]:
    from gateway.backends.llm_execution import litellm_runtime_from_registry

    return gate.status(litellm_runtime_from_registry(registry, env=os.environ).base_url)
