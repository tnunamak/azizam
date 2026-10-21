"""start.sh must let the proxy finish in-flight work before it stops LiteLLM."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from gateway.litellm_substrate import secret_env_name_for_provider
from gateway.secret_store import delete_provider_secret, put_provider_secret

ROOT = Path(__file__).resolve().parent.parent

FAKE_PROXY = textwrap.dedent(
    """
    import os, signal, sys, time
    log = os.environ["FAKE_LOG"]
    def note(text):
        with open(log, "a") as handle:
            handle.write(text + "\\n")
    def on_term(*_):
        note("proxy got SIGTERM")
        time.sleep(1.0)  # an in-flight response finishing
        note("proxy finished in-flight work")
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_term)
    note("proxy started")
    while True:
        time.sleep(0.05)
    """
)

FAKE_LITELLM = textwrap.dedent(
    """\
    #!/bin/bash
    # The handler must exist before the marker the harness waits for.
    trap 'echo "litellm terminated" >> "$FAKE_LOG"; exit 0' TERM
    if [ -n "${AZIZAM_PRIVATE_TMPDIR_LOCK_FD:-}" ] && [ -e "/proc/$$/fd/$AZIZAM_PRIVATE_TMPDIR_LOCK_FD" ]; then
      echo "litellm holds the private temporary directory lock" >> "$FAKE_LOG"
    fi
    echo "litellm started" >> "$FAKE_LOG"
    while true; do sleep 0.1; done
    """
)

FAKE_LITELLM_RECORD_ENV = textwrap.dedent(
    """\
    #!/bin/bash
    env_name="$EXPECTED_PROVIDER_ENV"
    value="${!env_name}"
    if [[ -n "$value" ]]; then
      digest="$(printf '%s' "$value" | sha256sum | cut -d' ' -f1)"
    else
      digest=missing
    fi
    trap 'echo "litellm terminated" >> "$FAKE_LOG"; exit 0' TERM
    printf 'litellm started %s argv=%s\\n' "$digest" "$0 $*" >> "$FAKE_LOG"
    while true; do sleep 0.1; done
    """
)

HARNESS = textwrap.dedent(
    """\
    cd "$1"
    ./start.sh 2>"$FAKE_LOG.err" &
    pid=$!
    for _ in $(seq 150); do
      grep -q "proxy started" "$FAKE_LOG" 2>/dev/null && grep -q "litellm started" "$FAKE_LOG" 2>/dev/null && break
      sleep 0.1
    done
    grep -q "litellm started" "$FAKE_LOG" 2>/dev/null || { echo "startup markers never appeared" >&2; kill -TERM "$pid"; exit 97; }
    kill -TERM "$pid"
    wait "$pid"
    echo "start.sh exit=$?" >> "$FAKE_LOG"
    """
)

WATCHER_HARNESS = textwrap.dedent(
    """\
    cd "$1"
    ./start.sh 2>"$FAKE_LOG.err" &
    pid=$!
    echo "$pid" > "$FAKE_LOG.pid"
    while [ ! -f "$FAKE_LOG.stop" ]; do sleep 0.1; done
    kill -TERM "$pid"
    wait "$pid"
    echo "watcher harness exit=$?" >> "$FAKE_LOG"
    """
)


class StartScriptGracefulStopTests(unittest.TestCase):
    def setUp(self) -> None:
        # start.sh empties the gateway's private temporary directory, which must be on disk; the hermetic temp dir is RAM.
        self.private = Path(tempfile.mkdtemp(prefix="start-private-", dir=os.environ["HERMETIC_HOST_DISK_DIR"])) / "private-tmp"
        self.private.mkdir(mode=0o700)
        patcher = patch.dict(os.environ, {"AZIZAM_PRIVATE_TMPDIR": str(self.private)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.private.parent, ignore_errors=True)

    def test_the_private_temporary_directory_is_emptied_before_the_proxy_starts_and_handed_to_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            _prepare_start_fixture(work)
            (self.private / "left-by-a-crash").write_text("old audio")
            (work / "proxy.py").write_text(
                "import os\n"
                "names = ('TMPDIR', 'TMP', 'TEMP', 'AZIZAM_PRIVATE_TMPDIR')\n"
                "print([os.environ[name] for name in names], os.listdir(os.environ['TMPDIR']))\n"
            )
            _write_python_wrapper(work / "bin" / "python3")
            _write_awk_wrapper(work / "bin" / "awk")
            env = {**os.environ, "AZIZAM_MANAGE_LITELLM": "false", "PATH": f"{work / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}"}
            result = subprocess.run(["/bin/bash", f"{work.name}/start.sh"], cwd=work.parent, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str([str(self.private)] * 4) + " ['.owner.lock']")

    def test_prisma_generation_is_idempotent_and_repairs_missing_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            _prepare_start_fixture(work)
            (work / "proxy.py").write_text("print('fixture proxy started')\n")
            log = work / "log.txt"
            _write_python_wrapper(work / "bin" / "python3")
            _write_awk_wrapper(work / "bin" / "awk")
            env = {**os.environ, "FAKE_LOG": str(log), "DATABASE_URL": "",
                   "PATH": f"{work / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}"}

            def start():
                result = subprocess.run(["/bin/bash", "start.sh"], cwd=work, env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)

            start()
            start()
            self.assertEqual(log.read_text().splitlines().count("prisma generate"), 1)
            (work / "venv" / ".fake-prisma-client").unlink()
            start()
            self.assertEqual(log.read_text().splitlines().count("prisma generate"), 2)
            (work / "schema.prisma").write_text("// changed schema\n")
            start()
            self.assertEqual(log.read_text().splitlines().count("prisma generate"), 3)

    def test_prisma_generation_failure_refuses_before_proxy_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            _prepare_start_fixture(work)
            _write_python_wrapper(work / "bin" / "python3")
            _write_awk_wrapper(work / "bin" / "awk")
            log = work / "log.txt"
            env = {**os.environ, "FAKE_LOG": str(log), "DATABASE_URL": "",
                   "FAKE_PRISMA_GENERATE_EXIT": "1",
                   "PATH": f"{work / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}"}
            result = subprocess.run(["/bin/bash", "start.sh"], cwd=work, env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Prisma client generation failed", result.stderr)
            self.assertNotIn("proxy started", log.read_text())
            self.assertFalse((work / "venv" / ".prisma-generated.sha256").exists())

    def test_sigterm_is_forwarded_and_litellm_outlives_the_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            _prepare_start_fixture(work)
            bindir = work / "bin"
            _write_python_wrapper(bindir / "python3")
            litellm = bindir / "litellm"
            litellm.write_text(FAKE_LITELLM)
            litellm.chmod(litellm.stat().st_mode | stat.S_IXUSR)
            # The hermetic sandbox ships no awk; start.sh only uses `awk '{print $1}'`.
            awk = bindir / "awk"
            awk.write_text("#!/bin/bash\nexec cut -d' ' -f1\n")
            awk.chmod(awk.stat().st_mode | stat.S_IXUSR)
            log = work / "log.txt"
            # Without a registry the sidecar never launches LiteLLM, so the
            # harness would wait out its whole startup loop on ambient state.
            db_path = work / "data" / "admin.db"
            _write_registry_json(db_path, {"services": {}})
            env = {
                **os.environ,
                "FAKE_LOG": str(log),
                "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "AZIZAM_ADMIN_DB": str(db_path),
                "AZIZAM_LITELLM_RELOAD_INTERVAL": "1",
                "DATABASE_URL": "",
            }
            result = subprocess.run(
                ["/bin/bash", "-c", HARNESS, "harness", str(work)],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = log.read_text().splitlines()
            err = Path(str(log) + ".err").read_text()
            self.assertIn("proxy finished in-flight work", lines, lines)
            self.assertIn("litellm terminated", lines, (lines, err))
            self.assertLess(
                lines.index("proxy finished in-flight work"),
                lines.index("litellm terminated"),
                f"LiteLLM stopped before the proxy finished: {lines}",
            )
            self.assertIn("start.sh exit=0", lines)
            # A LiteLLM that outlives the gateway must not keep the next launch from claiming the directory.
            self.assertNotIn("litellm holds the private temporary directory lock", lines)

    def test_litellm_restarts_when_provider_secret_rotates_and_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            _prepare_start_fixture(work)
            log = work / "log.txt"
            db_path = work / "data" / "admin.db"
            secret_id = "provider:openrouter:api_key"
            env_name = secret_env_name_for_provider("openrouter", secret_id)
            (work / "expected-env-name").write_text(env_name)
            _write_registry_json(
                db_path,
                {
                    "services": {
                        "openrouter": {
                            "id": "openrouter",
                            "kind": "provider",
                            "config": {
                                "provider_id": "openrouter",
                                "base_url": "https://openrouter.ai/api/v1",
                                "api_key_secret_id": secret_id,
                            },
                            "secret_refs": {"api_key_secret_id": secret_id},
                        }
                    }
                },
            )
            put_provider_secret(secret_id, "sk-one", db_path=db_path, now=1000, approved_origin="https://openrouter.ai/api/v1")

            bindir = work / "bin"
            litellm = bindir / "litellm"
            litellm.write_text(FAKE_LITELLM_RECORD_ENV)
            litellm.chmod(litellm.stat().st_mode | stat.S_IXUSR)
            _write_python_wrapper(bindir / "python3")
            _write_awk_wrapper(bindir / "awk")

            env = {
                **os.environ,
                "FAKE_LOG": str(log),
                "EXPECTED_PROVIDER_ENV": env_name,
                "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "AZIZAM_ADMIN_DB": str(db_path),
                "AZIZAM_LITELLM_RELOAD_INTERVAL": "0.2",
                "DATABASE_URL": "",
            }
            proc = subprocess.Popen(
                ["/bin/bash", "-c", WATCHER_HARNESS, "watcher-harness", str(work)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                first_hash = hashlib.sha256(b"sk-one").hexdigest()
                second_hash = hashlib.sha256(b"sk-two").hexdigest()
                _wait_for_log(log, first_hash)

                put_provider_secret(secret_id, "sk-two", db_path=db_path, now=1001, approved_origin="https://openrouter.ai/api/v1")
                _wait_for_log(log, second_hash)

                delete_provider_secret(secret_id, db_path=db_path)
                _wait_for_log(log, "litellm started missing")
            finally:
                Path(str(log) + ".stop").write_text("stop\n", encoding="utf-8")
                try:
                    _, stderr = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    raise AssertionError("watcher harness did not stop after stop file")

            text = log.read_text(encoding="utf-8")
            assert first_hash in text
            assert second_hash in text
            assert "litellm started missing" in text
            assert text.count("litellm started") >= 3
            assert "sk-one" not in text
            assert "sk-two" not in text
            assert "sk-one" not in stderr
            assert "sk-two" not in stderr
            child_stderr = Path(str(log) + ".err").read_text(encoding="utf-8")
            assert "sk-one" not in child_stderr
            assert "sk-two" not in child_stderr


if __name__ == "__main__":
    unittest.main()


def _prepare_start_fixture(work: Path) -> None:
    shutil.copy(ROOT / "start.sh", work / "start.sh")
    shutil.copytree(ROOT / "gateway", work / "gateway")
    shutil.copytree(ROOT / "registry", work / "registry")
    # Keep the real sidecar/config/secret path, but replace its final runtime.
    # The pinned launcher itself is exercised by the real-package canary.
    (work / "gateway" / "litellm_launcher.py").write_text(
        "import os,re,sys\nfrom pathlib import Path\n"
        "args=sys.argv[1:]\n"
        "if '--content-proof' in args:\n i=args.index('--content-proof'); del args[i:i+2]\n"
        "os.environ['FAKE_LOG']=str(Path.cwd()/'log.txt')\n"
        "config=Path(args[args.index('--config')+1]).read_text()\n"
        "refs=re.findall(r'os.environ/(AZIZAM_PROVIDER_SECRET_[A-Z0-9]+)',config)\n"
        "expected=Path.cwd()/'expected-env-name'\n"
        "os.environ['EXPECTED_PROVIDER_ENV']=expected.read_text() if expected.exists() else (refs[0] if refs else 'UNUSED_PROVIDER_ENV')\n"
        "os.execvp('litellm',['litellm',*args])\n"
    )
    (work / "start.sh").chmod(0o755)
    requirements = work / "requirements.txt"
    requirements.write_text("# none\n", encoding="utf-8")
    (work / "venv" / "bin").mkdir(parents=True)
    (work / "venv" / "bin" / "activate").write_text("", encoding="utf-8")
    (work / "venv" / ".requirements.sha256").write_text(
        hashlib.sha256(requirements.read_bytes()).hexdigest(),
        encoding="utf-8",
    )
    (work / "data").mkdir()
    (work / "data" / "litellm.config.yaml").write_text("model_list: []\n", encoding="utf-8")
    (work / "proxy.py").write_text(FAKE_PROXY, encoding="utf-8")
    (work / "bin").mkdir()
    (work / "schema.prisma").write_text("// fixture schema\n")


def _write_python_wrapper(path: Path) -> None:
    # When the harness configures the DB path, avoid a cold Python process for
    # this simple lookup. The real provider-sidecar fingerprint and run
    # commands still execute below.
    path.write_text(
        "#!/bin/bash\n"
        'if [[ $1 == -c && $2 == *find_spec* ]]; then echo "$PWD/schema.prisma"; exit 0; fi\n'
        'if [[ $1 == -c && $2 == "from prisma import Prisma" ]]; then test -f venv/.fake-prisma-client; exit $?; fi\n'
        'if [[ $1 == -m && $2 == prisma && $3 == generate ]]; then\n'
        '  echo "prisma generate" >> "$FAKE_LOG"\n'
        '  if [[ ${FAKE_PRISMA_GENERATE_EXIT:-0} != 0 ]]; then exit "$FAKE_PRISMA_GENERATE_EXIT"; fi\n'
        '  touch venv/.fake-prisma-client; exit 0\n'
        'fi\n'
        'if [[ $1 == -m && $2 == gateway.provider_sidecar && $3 == db-path && ${AZIZAM_ADMIN_DB:-} == /* ]]; then echo "$AZIZAM_ADMIN_DB"; exit 0; fi\n'
        f"exec {os.environ.get('PYTHON', os.sys.executable)!r} \"$@\"\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _write_awk_wrapper(path: Path) -> None:
    path.write_text("#!/bin/bash\nexec cut -d' ' -f1\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _write_registry_json(db_path: Path, registry: dict[str, object]) -> None:
    import json
    import sqlite3

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS operator_settings (
              key TEXT PRIMARY KEY,
              value_json TEXT NOT NULL,
              updated_at INTEGER NOT NULL,
              updated_by TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO operator_settings(key, value_json, updated_at, updated_by)
            VALUES ('service_registry', ?, 1, 'test')
            ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
            """,
            (json.dumps(registry, sort_keys=True),),
        )


def _wait_for_log(path: Path, needle: str) -> None:
    import time

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if path.exists() and needle in path.read_text(encoding="utf-8"):
            return
        time.sleep(0.05)
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    raise AssertionError(f"timed out waiting for {needle!r}; log was {content!r}")


def test_wait_for_log_accepts_reload_event_after_ten_seconds(tmp_path, monkeypatch):
    import time

    log = tmp_path / "reload.log"
    clock = [0.0]

    def advance(seconds):
        clock[0] += seconds
        if clock[0] >= 15:
            log.write_text("litellm started rotated-secret-hash\n", encoding="utf-8")

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", advance)
    _wait_for_log(log, "rotated-secret-hash")
    assert 15 <= clock[0] < 16, "return as soon as the observable event arrives"
