"""Every local service the gateway talks to must have its own port.

Two on-demand services on one port fail at random: whichever starts first holds the port and
the other cannot start. This has happened twice (Chatterbox on diarize's 8014, Realtime on the
face service's 8021).
"""

from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _local_ports(node, path=""):
    if isinstance(node, dict):
        port = node.get("port")
        if isinstance(port, int) and node.get("host", "127.0.0.1") in ("127.0.0.1", "localhost"):
            yield path, port
        for key, value in node.items():
            yield from _local_ports(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _local_ports(value, f"{path}[{i}]")


def test_local_service_ports_are_unique():
    config = yaml.safe_load((ROOT / "proxy_config.yml").read_text(encoding="utf-8"))
    users = defaultdict(list)
    for path, port in _local_ports(config):
        users[port].append(path)
    clashes = {port: paths for port, paths in users.items() if len(paths) > 1}
    assert not clashes, f"local ports used by more than one service: {clashes}"
