"""Native metadata-tool fixture, restricted to the benchmark's PCM WAVs."""
import struct
from pathlib import Path
from subprocess import CompletedProcess


def ffmpeg_metadata(command):
    if Path(command[0]).name != "ffmpeg":
        raise RuntimeError("unexpected metadata tool")
    source = Path(command[command.index("-i") + 1]).read_bytes()
    if source[:4] != b"RIFF" or source[8:12] != b"WAVE":
        raise RuntimeError("metadata fixture supports only RIFF WAV")
    chunks = []
    offset = 12
    while offset + 8 <= len(source):
        size = struct.unpack_from("<I", source, offset + 4)[0]
        end = offset + 8 + size + (size % 2)
        if source[offset:offset + 4] != b"LIST":
            chunks.append(source[offset:end])
        offset = end
    if "-metadata" in command:
        tag = command[command.index("-metadata") + 1].encode() + b"\x00"
        info = b"INFOICMT" + struct.pack("<I", len(tag)) + tag + (b"\x00" if len(tag) % 2 else b"")
        chunks.append(b"LIST" + struct.pack("<I", len(info)) + info)
    body = b"WAVE" + b"".join(chunks)
    Path(command[-1]).write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return CompletedProcess(command, 0, b"", b"")
