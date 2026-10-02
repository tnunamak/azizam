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
    echo "litellm started" >> "$FAKE_LOG"
    trap 'echo "litellm terminated" >> "$FAKE_LOG"; exit 0' TERM
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
    kill -TERM "$pid"
    wait "$pid"
    echo "start.sh exit=$?" >> "$FAKE_LOG"
    """
)


class StartScriptGracefulStopTests(unittest.TestCase):
    def test_sigterm_is_forwarded_and_litellm_outlives_the_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            shutil.copy(ROOT / "start.sh", work / "start.sh")
            (work / "start.sh").chmod(0o755)
            requirements = work / "requirements.txt"
            requirements.write_text("# none\n")
            (work / "venv" / "bin").mkdir(parents=True)
            (work / "venv" / "bin" / "activate").write_text("")
            (work / "venv" / ".requirements.sha256").write_text(
                hashlib.sha256(requirements.read_bytes()).hexdigest()
            )
            (work / "data").mkdir()
            (work / "data" / "litellm.config.yaml").write_text("model_list: []\n")
            (work / "proxy.py").write_text(FAKE_PROXY)
            bindir = work / "bin"
            bindir.mkdir()
            litellm = bindir / "litellm"
            litellm.write_text(FAKE_LITELLM)
            litellm.chmod(litellm.stat().st_mode | stat.S_IXUSR)
            # The hermetic sandbox ships no awk; start.sh only uses `awk '{print $1}'`.
            awk = bindir / "awk"
            awk.write_text("#!/bin/bash\nexec cut -d' ' -f1\n")
            awk.chmod(awk.stat().st_mode | stat.S_IXUSR)
            log = work / "log.txt"
            env = {
                **os.environ,
                "FAKE_LOG": str(log),
                "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "AI_GATEWAY_LITELLM_RELOAD_INTERVAL": "1",
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


if __name__ == "__main__":
    unittest.main()
