"""Prevent disposable gateway fixtures from reaching operator services.

The production Status implementation includes legacy host probes. Test gateways
must reject those probes before any socket connection or host-control process
is created. This guard lives only in fixture entrypoints, never production.
"""

import socket
import sys
from pathlib import Path


def parse_allowed_ports(raw):
    if not raw:
        return set()
    ports = set()
    for item in str(raw).split(","):
        item = item.strip()
        if not item:
            continue
        ports.add(int(item))
    return ports


def check_event(event, args, allowed_ports, media_root=None):
    if event == "socket.getaddrinfo":
        if args[0] != "127.0.0.1" or args[1] not in allowed_ports:
            raise RuntimeError(
                "Fixture blocked name resolution outside its declared loopback ports"
            )
    elif event in {"socket.connect", "socket.sendto"}:
        connection, address = args
        if (
            connection.family != socket.AF_INET
            or not isinstance(address, tuple)
            or address[0] != "127.0.0.1"
            or address[1] not in allowed_ports
        ):
            raise RuntimeError(
                "Fixture blocked a connection outside its declared loopback ports"
            )
    elif event in {"os.system", "subprocess.Popen", "os.posix_spawn", "os.exec"}:
        if event == "subprocess.Popen" and media_root is not None:
            executable, command, _cwd, _env = args
            expected = [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_entries",
                "format_tags:stream_tags",
            ]
            if (
                executable == "ffprobe"
                and isinstance(command, (list, tuple))
                and list(command[:-1]) == expected
            ):
                target = Path(command[-1]).resolve()
                if target.is_file() and target.parent == Path(media_root).resolve():
                    return
            strip_prefix = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i"]
            strip_middle = [
                "-map",
                "0",
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-map_metadata:s:v",
                "-1",
                "-map_metadata:s:a",
                "-1",
                "-c",
                "copy",
                "-fflags",
                "+bitexact",
                "-f",
            ]
            if (
                executable == "ffmpeg"
                and isinstance(command, (list, tuple))
                and len(command) == 24
                and list(command[:6]) == strip_prefix
                and list(command[7:22]) == strip_middle
            ):
                source, destination = (
                    Path(command[6]).resolve(),
                    Path(command[-1]).resolve(),
                )
                root = Path(media_root).resolve().parent
                if (
                    source.is_file()
                    and source.parent == destination.parent
                    and source.is_relative_to(root)
                    and source.suffix in {".mp3", ".mp4", ".wav"}
                    and destination.suffix == source.suffix
                ):
                    return
        raise RuntimeError("Fixture blocked a child process")


def install(allowed_ports, *, media_root=None):
    ports = frozenset(allowed_ports)
    if not ports or any(
        not isinstance(port, int) or not 18000 <= port <= 18999 for port in ports
    ):
        raise ValueError("Fixture ports must be in 18000–18999")
    sys.addaudithook(lambda event, args: check_event(event, args, ports, media_root))
