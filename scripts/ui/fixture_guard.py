"""Prevent disposable gateway fixtures from reaching operator services.

The production Status implementation includes legacy host probes. Test gateways
must reject those probes before any socket connection or host-control process
is created. This guard lives only in fixture entrypoints, never production.
"""
import os
from pathlib import Path
import socket
import sys

# Status may collect these read-only diagnostics. Reject every other process,
# including shell wrappers that could evade an executable-name denylist.
ALLOWED_COMMANDS = frozenset({"git", "df", "du", "nvidia-smi", "ffprobe"})


def check_event(event, args, allowed_ports):
    if event == "socket.connect":
        connection, address = args
        if (connection.family != socket.AF_INET or not isinstance(address, tuple)
                or address[0] != "127.0.0.1" or address[1] not in allowed_ports):
            raise RuntimeError("Fixture blocked a connection outside its declared loopback ports")
    elif event == "os.system":
        raise RuntimeError("Fixture blocked a host-control or process-inventory command")
    elif event in {"subprocess.Popen", "os.posix_spawn", "os.exec"}:
        executable, command = args[:2]
        if not executable:
            executable = command[0] if isinstance(command, (list, tuple)) else command
        if Path(os.fsdecode(executable)).name not in ALLOWED_COMMANDS:
            raise RuntimeError("Fixture blocked a host-control or process-inventory command")


def install(allowed_ports):
    ports = frozenset(allowed_ports)
    if not ports or any(not isinstance(port, int) or not 18000 <= port <= 18999 for port in ports):
        raise ValueError("Fixture ports must be in 18000–18999")
    sys.addaudithook(lambda event, args: check_event(event, args, ports))
