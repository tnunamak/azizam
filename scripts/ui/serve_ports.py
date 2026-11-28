"""Preflight for the browser test server's loopback ports."""
from collections.abc import Iterable
import errno
import socket


def busy_ports(ports: Iterable[int]) -> list[int]:
    busy = []
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            # uvicorn binds with SO_REUSEADDR, so a port left in TIME_WAIT is free for it: probe the same way.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(('127.0.0.1', port))
            except OSError as error:
                if error.errno != errno.EADDRINUSE:
                    raise
                busy.append(port)
    return busy


def busy_port_message(ports: Iterable[int]) -> str:
    return '\n'.join(
        f'port {port} is already in use; set UI_E2E_PORT to a free range' for port in ports
    )
