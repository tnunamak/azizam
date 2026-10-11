"""Prevent disposable gateway fixtures from reaching operator services.

The production Status implementation includes legacy host probes. Test gateways
must reject those probes before any socket connection or host-control process
is created. This guard lives only in fixture entrypoints, never production.
"""

import os
import socket
import sys
import tempfile
from pathlib import Path


def _media_buffer(path, name):
    """Accept only this process's anonymous media buffer, never a disk fd."""
    target = Path(path)
    if target.parent != Path("/proc/self/fd") or not target.name.isdecimal():
        return False
    try:
        return os.readlink(target) == f"/memfd:azizam-media-{name} (deleted)"
    except OSError:
        return False


def _anonymous_media_command(executable, command):
    if not isinstance(command, (list, tuple)):
        return False
    command = list(command)
    probe = ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams"]
    if executable == "ffprobe" and command[:-1] == probe:
        return _media_buffer(command[-1], "input")
    if not (
        executable == "ffmpeg"
        and len(command) >= 16
        and command[:6] == ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i"]
        and command[7:9] == ["-map", "0"]
        and command[-7:-2] == ["-c", "copy", "-fflags", "+bitexact", "-f"]
        and command[-2] in {"mp4", "mov", "webm", "matroska", "avi", "wav",
                                "flac", "mp3", "ogg", "adts"}
        and _media_buffer(command[6], "input")
        and _media_buffer(command[-1], "output")
    ):
        return False
    middle = command[9:-7]
    if middle == ["-map_metadata", "-1", "-map_chapters", "-1", "-map_metadata:s", "-1"]:
        return True
    if middle[-2:] == ["-movflags", "+use_metadata_tags"]:
        middle = middle[:-2]
    return (
        len(middle) >= 2
        and len(middle) % 2 == 0
        and all(flag == "-metadata" for flag in middle[::2])
        and all("=" in str(value) for value in middle[1::2])
    )


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
            # Metadata reads and rewrites now use inherited anonymous buffers.
            # Keep the exact command boundary and reject ordinary disk fds.
            if _anonymous_media_command(executable, command):
                return
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
            # The provenance stamp (gateway.media_metadata.embed_media_tags):
            # the same stream copy as the strip above, with -metadata pairs
            # instead of the -map_metadata clears (D1.5b: a new speech clip
            # is stamped before it is stored).
            tail = ["-c", "copy", "-fflags", "+bitexact", "-f"]
            if (
                executable == "ffmpeg"
                and isinstance(command, (list, tuple))
                and len(command) >= 17
                and list(command[:6]) == strip_prefix
                and list(command[7:9]) == ["-map", "0"]
                and list(command[-7:-2]) == tail
            ):
                middle = list(command[9:-7])
                if middle[-2:] == ["-movflags", "+use_metadata_tags"]:
                    middle = middle[:-2]
                pairs_ok = (
                    len(middle) >= 2
                    and len(middle) % 2 == 0
                    and all(flag == "-metadata" for flag in middle[0::2])
                    and all("=" in str(value) for value in middle[1::2])
                )
                source, destination = (
                    Path(command[6]).resolve(),
                    Path(command[-1]).resolve(),
                )
                root = Path(media_root).resolve().parent
                if (
                    pairs_ok
                    and source.is_file()
                    and source.parent == destination.parent
                    and source.is_relative_to(root)
                    and source.suffix in {".mp3", ".mp4", ".wav"}
                    and destination.suffix == source.suffix
                ):
                    return
            # The speech route's WAV-to-MP3 encode (proxy.convert_wav_to_mp3),
            # between two temporary files, so Try's "Read text aloud" can
            # finish in the household fixture (D1.5b).
            encode = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i"]
            if (
                executable == "ffmpeg"
                and isinstance(command, (list, tuple))
                and len(command) == 12
                and list(command[:6]) == encode
                and list(command[7:11]) == ["-codec:a", "libmp3lame", "-f", "mp3"]
            ):
                source, destination = Path(command[6]), Path(command[11])
                temp = Path(tempfile.gettempdir()).resolve()
                if (
                    source.resolve().parent == temp
                    and destination.resolve().parent == temp
                    and source.suffix == ".wav"
                    and destination.suffix == ".mp3"
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
