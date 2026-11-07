"""The release defaults are neutral: no host path of one machine and no IP address (docs/design/CONFIG_HOMES.md, 10.2 item 3)."""
import re
from pathlib import Path

DEFAULTS = Path(__file__).resolve().parents[1] / "registry" / "defaults"

HOST_PATH = re.compile(r"(?<![\w.])/(?:media|mnt|home|Users)/[\w.-]+")
IP_ADDRESS = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.])")


def _lines():
    for path in sorted(DEFAULTS.glob("*.yml")) + sorted(DEFAULTS.glob("*.schema.json")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            yield path.name, number, line


def test_no_default_names_a_host_path():
    found = [f"{name}:{number}: {line.strip()}" for name, number, line in _lines() if HOST_PATH.search(line)]
    assert not found, "\n".join(found)


def test_no_default_names_an_ip_address():
    found = [f"{name}:{number}: {line.strip()}" for name, number, line in _lines()
             if IP_ADDRESS.search(line) and not re.search(r"127\.0\.0\.1|0\.0\.0\.0", line)]
    assert not found, "\n".join(found)
