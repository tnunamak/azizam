"""Keep pytest's gateway imports out of an operator's persistent state."""

from __future__ import annotations

from collections.abc import Iterator
import os
import random
import socket
import tempfile
from pathlib import Path

# anyio binds ``asyncio.sleep`` with ``from asyncio import sleep`` when its
# asyncio backend is first imported, which is lazy: the first TestClient call or
# anyio.run in the worker. Many tests patch ``proxy.asyncio.sleep`` (the global
# asyncio.sleep) with an AsyncMock; if the first import falls inside such a
# patch, anyio keeps the mock for the rest of the worker. Then sleep_forever()
# returns at once, so a TestClient websocket closes its streams before the
# handshake is read (anyio.ClosedResourceError), and anyio checkpoints stop
# yielding to the loop. Import it now, while asyncio.sleep is the real one.
import anyio._backends._asyncio  # noqa: F401
import pytest

from _hermetic import HermeticGuard, assert_hermetic_environment


# The runner creates exactly one unique, disk-backed state directory before
# pytest collection.  Refusing a direct pytest invocation prevents import-time
# gateway stores from falling back to a real operator database.
if os.environ.get("HERMETIC_TEST_NAMESPACE") != "1":
    pytest.exit("pytest must run through scripts/run-hermetic-pytest.sh", returncode=2)
_STATE_DIR = Path(os.environ["HERMETIC_TEST_STATE_DIR"])

# These production overrides must be absent before we install test-owned paths.
# The runner supplies the admin path through its neutral, private contract.
# pytest-xdist workers inherit the controller's test-owned value, which is the
# runner's own admin path and so cannot name an operator database.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER")
for _name in ("AZIZAM_ADMIN_DB",):
    if _name in os.environ and not (_WORKER and os.environ[_name] == os.environ["HERMETIC_ADMIN_DB"]):
        pytest.exit(f"production environment variable leaked into pytest: {_name}", returncode=2)
# Workers share the run's namespace, so each owns a private subdirectory for
# its import-time stores.  Serial runs keep the run root.
if _WORKER:
    if Path(os.environ["HERMETIC_ADMIN_DB"]) != _STATE_DIR / "admin.db":
        pytest.exit("pytest admin database must belong to this run's state directory", returncode=2)
    _STATE_DIR = _STATE_DIR / f"xdist-{_WORKER}"
    _STATE_DIR.mkdir(exist_ok=True)
    os.environ["HERMETIC_TEST_STATE_DIR"] = str(_STATE_DIR)
    os.environ["HERMETIC_ADMIN_DB"] = str(_STATE_DIR / "admin.db")
_ADMIN_DB = Path(os.environ["HERMETIC_ADMIN_DB"])
if _ADMIN_DB != _STATE_DIR / "admin.db":
    pytest.exit("pytest admin database must belong to this run's state directory", returncode=2)
_STORE_PATHS = {
    "AZIZAM_ADMIN_DB": "admin.db",
    "AZIZAM_METRICS_DB": "metrics.db",
    "AZIZAM_RESPONSES_DB": "responses.db",
    "AZIZAM_RECEIPTS_DB": "receipts.db",
    "AZIZAM_MEDIA_JOBS_DB": "media_jobs.db",
    "AZIZAM_ARBITRATION_DB": "arbitration.db",
    "AZIZAM_KEY_ACTIVITY_LOG": "key_activity.jsonl",
}

# Test modules import proxy during collection.  These must be set while pytest
# imports this conftest, before that collection-time import can initialize or
# mutate the operator's default data/*.db files.
for _name, _filename in _STORE_PATHS.items():
    os.environ[_name] = str(_STATE_DIR / _filename)

_GUARD = HermeticGuard()


def _seed_hermetic_registry() -> None:
    """Give the run's own admin.db the deployment of ``tests/fixtures/config_homes/deployment_config.yml``: its engines as
    an owner's registry records, and each section block as the owner overrides an import of it writes.

    The gateway reads neither the file's ``backends`` nor any section block from the startup file (docs/design/CONFIG_HOMES.md,
    8.5 and C10), and most of the suite boots ``proxy`` against a deployment's engines and sections. A run that wants
    them says so here, once, before any test module imports ``proxy``.
    """
    from registry_test_helpers import deployment_config, import_deployment_sections, seed_registry_from_backends

    deployment = deployment_config()
    seed_registry_from_backends(_ADMIN_DB, deployment)
    import_deployment_sections(_ADMIN_DB, deployment)


_seed_hermetic_registry()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--order-seed", type=int, default=None,
                     help="shuffle test order from this seed (scripts/ci-local passes one on every run)")


def pytest_report_header(config: pytest.Config) -> str | None:
    seed = config.getoption("order_seed")
    return None if seed is None else f"order seed: {seed}"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Shuffle modules, classes in each module and tests in each class from
    --order-seed.

    pytest-xdist aborts unless every worker collects the same ordered list, so
    the order depends only on the seed and the node IDs, never on collection
    order or the worker.  Modules and classes stay contiguous so their scoped
    fixtures (and setUpClass) are not set up again for every interleaved test.
    """
    seed = config.getoption("order_seed")
    if seed is None:
        return
    rng = random.Random(seed)
    modules: dict[str, dict[str, list[pytest.Item]]] = {}
    for item in sorted(items, key=lambda item: item.nodeid):
        module = item.nodeid.split("::", 1)[0]
        modules.setdefault(module, {}).setdefault(item.parent.nodeid, []).append(item)

    def shuffled(values):
        values = list(values)
        rng.shuffle(values)
        return values

    items[:] = [item for parents in shuffled(modules.values())
                for tests in shuffled(parents.values())
                for item in shuffled(tests)]


def pytest_sessionstart(session: pytest.Session) -> None:
    assert_hermetic_environment()


def pytest_configure(config: pytest.Config) -> None:
    _GUARD.install()
    # xdist waits 10 s for a worker to leave after the last test, then kills it. The worker that ran the heaviest
    # tests can need longer than that to close its ~850 MB heap on a busy host, and the kill is refused here (the
    # guard blocks os.kill), which fails a session in which every test passed and leaves no JUnit file. A worker
    # that is slow but not hung gets time to finish.
    from xdist.workermanage import NodeManager

    NodeManager.EXIT_TIMEOUT = max(NodeManager.EXIT_TIMEOUT, 120)


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    # xdist may signal its own workers during this hook. All tests have ended.
    _GUARD.restore()


def pytest_unconfigure(config: pytest.Config) -> None:
    _GUARD.restore()


_ASYNCIO_GLOBALS = ("sleep", "wait_for", "to_thread", "create_task")


@pytest.fixture(autouse=True)
def _log_scrub_registry_is_restored() -> Iterator[None]:
    """A test leaves the log scrubber's registered values as it found them. The registry is process-wide and a
    monkeypatched environment variable that holds a credential (``env_secret``) outlives the monkeypatch; a short
    word such as ``test`` left behind then removes that word from every later failure message in the worker."""
    import copy

    from gateway import log_scrub

    with log_scrub._LOCK:
        before = copy.deepcopy(log_scrub._SOURCES)
    yield
    with log_scrub._LOCK:
        log_scrub._SOURCES.clear()
        log_scrub._SOURCES.update(before)
        log_scrub._SNAPSHOT = None


@pytest.fixture(autouse=True)
def _asyncio_globals_are_restored() -> Iterator[None]:
    """A test leaves the global asyncio functions as it found them. Defined before every other autouse fixture, so it
    is set up first and torn down last, after each MonkeyPatch has been undone. Two patch owners undone out of order
    (a test's monkeypatch over a rig's own patch of asyncio.sleep) leave a fake in place for every later test in the
    worker; this names the test that leaked it and puts the real function back."""
    import asyncio

    before = {name: getattr(asyncio, name) for name in _ASYNCIO_GLOBALS}
    yield
    leaked = [name for name, value in before.items() if getattr(asyncio, name) is not value]
    for name in leaked:
        setattr(asyncio, name, before[name])
    if leaked:
        pytest.fail(f"this test left asyncio.{', asyncio.'.join(leaked)} patched after teardown", pytrace=False)


@pytest.fixture(autouse=True)
def _no_comfy_object_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """Submitting a ComfyUI prompt derives the embedded UI workflow from
    ComfyUI's /object_info. Tests must not reach for a live ComfyUI, so the
    lookup reports "unavailable" unless a test patches it."""
    from gateway.engine_protocol import node_info

    monkeypatch.setattr(node_info, "lookup", lambda _host, _class_type: None)


@pytest.fixture
def isolated_receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Supply the store normally initialized by lifespan to direct route tests."""
    from gateway import receipt_store

    path = tmp_path / "receipts.db"
    receipt_store.init_db(path)
    monkeypatch.setattr(receipt_store, "DB_PATH", path)
    return path


@pytest.fixture(autouse=True)
def _no_host_gpu_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to absent GPU data; a GPU-specific test can replace these probes."""
    import proxy

    monkeypatch.setattr(proxy, "_resolve_gpu0_uuid", lambda: None)
    monkeypatch.setattr(proxy, "_resolve_comfy_pids", lambda: set())
    monkeypatch.setattr(proxy, "_sample_comfy_vram_bytes", lambda _pids, _uuid: None)


@pytest.fixture(autouse=True)
def _no_live_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Listings probe a live service only through a probe a test registers; none is registered by default."""
    import proxy

    monkeypatch.setattr(proxy, "LIVE_PROBES", {})


@pytest.fixture(autouse=True)
def _no_model_list_selector_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model listing makes no health probe by default; a test that wants the real observation
    restores `proxy._model_list_served_declarations.real`."""
    import proxy

    async def no_target():
        return set()

    no_target.real = proxy._model_list_served_declarations  # type: ignore[attr-defined]
    monkeypatch.setattr(proxy, "_model_list_served_declarations", no_target)


@pytest.fixture(autouse=True)
def _isolated_media_delivery_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """`media_delivery.begin` sets a ContextVar on the calling thread. A test that
    begins a delivery without a request scope would otherwise leak that context
    into every later test run on the same thread."""
    import contextvars

    from gateway import media_delivery

    monkeypatch.setattr(
        media_delivery, "_CONTEXT", contextvars.ContextVar("test_media_delivery", default=None)
    )


@pytest.fixture(autouse=True)
def _fresh_read_caches() -> None:
    """The gateway keeps health answers and composed-service rows for a few seconds
    (proxy._HEALTH_EVIDENCE_CACHE, _SERVICE_ROW_CACHE, _RESOURCE_FACT_CACHE). A test starts with neither."""
    import sys

    module = sys.modules.get("proxy")
    if module is not None:
        module._HEALTH_EVIDENCE_CACHE.clear()
        module._SERVICE_ROW_CACHE.clear()
        module._RESOURCE_FACT_CACHE.clear()


@pytest.fixture(autouse=True)
def _no_registry_apply_report_from_an_earlier_test() -> None:
    """A reload that fails reports itself on the Overview until one succeeds (proxy.REGISTRY_APPLY_PROBLEM).
    That is process state, so a test that fails a reload on purpose must not leave it for the next."""
    import sys

    module = sys.modules.get("proxy")
    if module is not None:
        module.REGISTRY_APPLY_PROBLEM = None


@pytest.fixture(autouse=True)
def _config_snapshot_of_one_test() -> Iterator[None]:
    """``proxy.runtime()`` reads only the published snapshot (gateway/config_runtime.py ``runtime_for``). A test that
    reloads publishes a snapshot, and one that swaps ``proxy.config`` publishes none; neither may leave its config in the
    holder for the next test. A test that needs ``runtime()`` to see a config it swapped publishes it through
    ``tests/support/config_snapshot.py``.

    The module's ``config`` and the send-time egress map are put back with the holder: a reload swaps all three, and
    a test that restores the old config's contents into the new mapping (tests/test_hot_reload.py) leaves ``config``
    a different object from the restored snapshot's, which a later test on the same worker sees."""
    import sys

    module = sys.modules.get("proxy")
    holder = module.CONFIG_HOME.holder if module is not None else None
    saved = holder.current() if holder is not None else None
    saved_config = module.config if module is not None else None
    # A reload republishes the workflow alias tables as module names too (proxy.py `_publish_runtime_config`).
    saved_aliases = {name: getattr(module, name) for name in ("VIDEO_ALIASES", "AUDIO_ALIASES")} if module is not None else {}
    egress = sys.modules.get("gateway.engine_egress")
    saved_egress = egress._live if egress is not None else None
    # A reload also swaps the registry snapshot (proxy.py `_publish_runtime_config`); the next test on this worker starts
    # from the snapshot it found.
    provider = getattr(module, "GATEWAY_REGISTRY_PROVIDER", None) if module is not None else None
    saved_snapshot = provider.current() if provider is not None else None
    # ...and adopts the next workflow registry in place; an owner workflow a test saved must not serve on the next one.
    workflows = getattr(module, "GATEWAY_WORKFLOW_REGISTRY", None) if module is not None else None
    saved_workflows = workflows.contents() if workflows is not None else None
    yield
    if workflows is not None and saved_workflows is not None and workflows.contents()[0] is not saved_workflows[0]:
        workflows.restore(saved_workflows)
    if provider is not None and saved_snapshot is not None and provider.current() is not saved_snapshot:
        provider.swap(saved_snapshot)
    if holder is not None and saved is not None:
        holder.publish(saved)
    if module is not None:
        module.config = saved_config
        for name, table in saved_aliases.items():
            setattr(module, name, table)
    if egress is not None and saved_egress is not None:
        egress._live = saved_egress


@pytest.fixture(scope="session")
def gateway_test_state_dir() -> Path:
    return _STATE_DIR


@pytest.fixture
def host_disk_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A private directory on the host disk, for the few tests whose subject is
    a real-disk property (fsync, free space, a device boundary).  `tmp_path` is
    RAM under the hermetic runner (docs/design/TEST_STRATEGY.md 4.4)."""
    root = os.environ.get("HERMETIC_HOST_DISK_DIR")
    if not root:
        pytest.fail("host_disk_dir needs the hermetic runner: HERMETIC_HOST_DISK_DIR is unset")
    return Path(tempfile.mkdtemp(prefix="case-", dir=root))


@pytest.fixture
def test_owned_tcp_listener():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        yield listener
    finally:
        listener.close()


# Hypothesis profiles (docs/design/TEST_STRATEGY.md 4.3). The hermetic runner
# forwards HYPOTHESIS_PROFILE into the sandbox; the gate uses "gate". Both
# profiles derandomize, so a run at one commit always tries the same examples
# (generation adds no flake; a property over timing or shared state still can),
# and keep no example database:
# shrunk counterexamples live in the test file as explicit @example cases.
os.environ.setdefault("HYPOTHESIS_STORAGE_DIRECTORY", str(_STATE_DIR / "hypothesis"))
from hypothesis import HealthCheck, settings as _hypothesis_settings  # noqa: E402

_HYPOTHESIS_COMMON = dict(derandomize=True, database=None, deadline=None,
                          suppress_health_check=(HealthCheck.too_slow,))
_hypothesis_settings.register_profile("gate", max_examples=100, **_HYPOTHESIS_COMMON)
_hypothesis_settings.register_profile("nightly", max_examples=2000, **_HYPOTHESIS_COMMON)
_HYPOTHESIS_PROFILE = os.environ.get("HYPOTHESIS_PROFILE", "gate")
if _HYPOTHESIS_PROFILE not in {"gate", "nightly"}:
    pytest.exit(f"unknown HYPOTHESIS_PROFILE {_HYPOTHESIS_PROFILE!r}; use gate or nightly", returncode=2)
_hypothesis_settings.load_profile(_HYPOTHESIS_PROFILE)


@pytest.fixture(autouse=True)
def _restore_video_job_records() -> Iterator[None]:
    """`proxy._video_job_records` is process-global; a record one test writes must
    not reach the next test on the same worker (it changes which code paths a
    route takes, e.g. owner-key lookups in /v1/videos)."""
    import sys

    module = sys.modules.get("proxy")
    records = getattr(module, "_video_job_records", None)
    snapshot = dict(records) if records is not None else None
    yield
    module = sys.modules.get("proxy")
    records = getattr(module, "_video_job_records", None)
    if snapshot is not None and records is not None:
        records.clear()
        records.update(snapshot)
    elif records:
        records.clear()


# A browser sends Origin on every same-origin POST, PUT, PATCH and DELETE, and the gateway refuses a signed-in write
# that does not say it came from its own pages (gateway/browser_origin.py). A test client is the browser here, so it
# sends its own base URL as Origin unless the test names one, or sends a bearer key (a script, which has none). A
# test that needs the browser to send nothing adds the header below. Cookie-less MCP calls model protocol clients
# and send no browser Origin, so unauthenticated calls exercise the bearer challenge.
NO_ORIGIN = "x-test-browser-sends-no-origin"


@pytest.fixture(autouse=True)
def _test_browser_sends_origin(monkeypatch) -> None:
    import httpx
    from starlette.testclient import TestClient

    original = TestClient.request

    def request(self, method, url, **kwargs):
        headers = httpx.Headers(kwargs.get("headers") or {})
        opted_out = headers.pop(NO_ORIGIN, None) is not None
        browser = httpx.URL(url).path != "/mcp" or self.cookies or kwargs.get("cookies")
        if (not opted_out and browser and method.upper() not in {"GET", "HEAD", "OPTIONS"} and "authorization" not in headers
                and not {"origin", "referer", "sec-fetch-site"} & set(headers.keys())):
            headers["origin"] = str(self.base_url).rstrip("/")
        kwargs["headers"] = headers
        return original(self, method, url, **kwargs)

    monkeypatch.setattr(TestClient, "request", request)
