"""Prevent disposable gateway fixtures from reaching operator services.

The production Status implementation includes legacy host probes. Test gateways
must reject those probes before any socket connection or host-control process
is created. This guard lives only in fixture entrypoints, never production.
"""
import socket
import sys

def check_event(event, args, allowed_ports):
    if event == "socket.getaddrinfo":
        if args[0] != "127.0.0.1" or args[1] not in allowed_ports:
            raise RuntimeError("Fixture blocked name resolution outside its declared loopback ports")
    elif event in {"socket.connect", "socket.sendto"}:
        connection, address = args
        if (connection.family != socket.AF_INET or not isinstance(address, tuple)
                or address[0] != "127.0.0.1" or address[1] not in allowed_ports):
            raise RuntimeError("Fixture blocked a connection outside its declared loopback ports")
    elif event in {"os.system", "subprocess.Popen", "os.posix_spawn", "os.exec"}:
        raise RuntimeError("Fixture blocked a child process")


def install(allowed_ports):
    ports = frozenset(allowed_ports)
    if not ports or any(not isinstance(port, int) or not 18000 <= port <= 18999 for port in ports):
        raise ValueError("Fixture ports must be in 18000–18999")
    sys.addaudithook(lambda event, args: check_event(event, args, ports))
