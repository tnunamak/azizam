"""Member adapter uploads: the checks on an uploaded safetensors file, and how the engine is given a copy.

What an uploader controls is the file's bytes, its name and its size. This module reads 8 bytes, a bounded JSON header and
the file's length. It never loads a tensor, never unpickles, and never uses the uploader's name for a path: a stored
upload is named by the hash of its content, and so is its copy in the engine's folder.

The module names no engine, model or node class. Which base a file is for comes from ``registry/adapter_architectures.yml``,
the table the operator's own files are judged by. Where the engine's loras folder is comes from the engine directory the
registry names (``host_memory_admission.engine_models_dir``) and the folder the workflow binding declares.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import struct
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, BinaryIO

import yaml

SETTING_KEY = "adapter_uploads"      # the owner's setting in admin.db: {"enabled": bool, "max_bytes": int}
SUBFOLDER = "azizam-uploads"         # the gateway's own folder inside the engine's adapter folder
SUFFIX = ".safetensors"
DEFAULT_MAX_BYTES = 2 * (1 << 30)    # a file over this is refused
MAX_HEADER_BYTES = 8 << 20           # a header over this is refused before it is read
IDLE_S = 24 * 3600.0                 # a copy no request used for this long is removed
CACHE_MAX_BYTES = 16 * (1 << 30)     # all copies together stay under this; the least recently used go first
TABLE_PATH = Path(__file__).resolve().parents[1] / "registry" / "adapter_architectures.yml"

_METADATA = "__metadata__"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_COPY_NAME = re.compile(r"^([0-9a-f]{64})" + re.escape(SUFFIX) + r"$")
_FOLDER = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ASSET_ID = re.compile(r"^ast_([0-9a-f]{64})$")
_CHUNK = 1 << 20
_LOOKS_LIKE_JSON = b"{"


class Refused(Exception):
    """An upload or a copy the gateway refuses. ``code`` is stable; ``message`` is plain words for the person."""

    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code, self.status, self.message = code, status, message


# ------------------------------------------------------------------------------------------------------ the file


@dataclass(frozen=True)
class Header:
    metadata: Mapping[str, str]
    tensor_count: int


def _no_repeats(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key is repeated")
    return dict(pairs)


def _is_count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _invalid(why: str) -> Refused:
    return Refused("adapter_header_invalid", 400, f"The file's header is not a valid safetensors header: {why}.")


def _not_safetensors() -> Refused:
    return Refused("not_safetensors", 400, "The file is not a safetensors file. Only safetensors adapters are accepted.")


def read_header(stream: BinaryIO, size: int, *, max_header_bytes: int = MAX_HEADER_BYTES) -> Header:
    """Check a safetensors file by its header alone, and return its metadata. ``size`` is the file's length in bytes.

    The header's length field is checked against the bound and the file's length before anything is read with it. Every
    tensor must lie inside the data area, the tensors must tile it with no gap, overlap or trailing byte, and no key may
    repeat. Nothing is allocated from a declared size."""
    if size < 8:
        raise _not_safetensors()
    stream.seek(0)
    prefix = stream.read(8)
    if len(prefix) != 8:
        raise _not_safetensors()
    (length,) = struct.unpack("<Q", prefix)
    if length == 0 or length > max_header_bytes or 8 + length > size:
        # A file whose first header byte is a brace means to be safetensors and lies about its length; anything else is another format.
        stream.seek(8)
        if size > 8 and stream.read(1) == _LOOKS_LIKE_JSON:
            raise _invalid("its length does not fit the file or the size bound")
        raise _not_safetensors()
    blob = stream.read(length)
    if len(blob) != length or not blob.startswith(_LOOKS_LIKE_JSON):
        raise _not_safetensors()
    try:
        header = json.loads(blob.decode("utf-8"), object_pairs_hook=_no_repeats)
    except (UnicodeDecodeError, ValueError) as exc:
        raise _invalid("it is not JSON with distinct keys") from exc
    if not isinstance(header, dict):
        raise _invalid("it is not an object")
    metadata = header.pop(_METADATA, {})
    if not isinstance(metadata, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()):
        raise _invalid("its metadata must map text to text")
    spans: list[tuple[int, int]] = []
    for name, entry in header.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("dtype"), str):
            raise _invalid(f"tensor {name!r} has no type")
        shape, offsets = entry.get("shape"), entry.get("data_offsets")
        if not isinstance(shape, list) or not all(_is_count(dim) for dim in shape):
            raise _invalid(f"tensor {name!r} has no valid shape")
        if not (isinstance(offsets, list) and len(offsets) == 2 and all(_is_count(o) for o in offsets) and offsets[0] <= offsets[1]):
            raise _invalid(f"tensor {name!r} has no valid offsets")
        spans.append((offsets[0], offsets[1]))
    if not spans:
        raise _invalid("it holds no tensor")
    cursor = 0
    for begin, end in sorted(spans):
        if begin != cursor:
            raise _invalid("tensor offsets overlap or leave a gap")
        cursor = end
    if cursor != size - 8 - length:
        raise _invalid("tensor offsets do not end where the file ends")
    return Header(metadata, len(spans))


@lru_cache(maxsize=1)
def architecture_table() -> Mapping[str, Mapping[str, tuple[str, ...]]]:
    raw = yaml.safe_load(TABLE_PATH.read_text(encoding="utf-8")) or {}
    return {arch: {key: tuple(values) for key, values in keys.items()} for arch, keys in raw.items()}


def architectures(metadata: Mapping[str, str], table: Mapping[str, Mapping[str, Sequence[str]]]) -> tuple[str, ...]:
    """The architectures the table lists for a file: those with a header key that holds one of the architecture's values.
    Empty when the file names no base the table knows."""
    return tuple(sorted(
        arch for arch, keys in table.items()
        if any(isinstance(metadata.get(key), str) and metadata[key] in values for key, values in keys.items())))


# ------------------------------------------------------------------------------------------------ the owner's setting


@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    max_bytes: int = DEFAULT_MAX_BYTES


def policy(setting: Any) -> Policy:
    """The owner's setting as a policy. Anything not shaped as expected leaves uploads off and the cap at its default."""
    if not isinstance(setting, Mapping):
        return Policy()
    cap = setting.get("max_bytes")
    return Policy(setting.get("enabled") is True, cap if type(cap) is int and cap > 0 else DEFAULT_MAX_BYTES)


def admit(rules: Policy, *, size: int, engine_local: bool) -> None:
    """Refuse an upload before its bytes are read: setting off, engine on another computer, empty, or over the cap."""
    if not rules.enabled:
        raise Refused("adapter_uploads_off", 403, "Adapter uploads are off. The owner can turn them on.")
    if not engine_local:
        raise Refused("adapter_engine_not_local", 409,
                      "Uploads are offered only when the image engine runs on this computer, and it does not.")
    if size <= 0:
        raise _not_safetensors()
    if size > rules.max_bytes:
        raise Refused("adapter_too_large", 413, f"The file is larger than the {rules.max_bytes}-byte limit for adapters.")


@dataclass(frozen=True)
class Validated:
    digest: str
    size: int
    header: Header


def validate(path: Path, rules: Policy) -> Validated:
    """Check a spooled upload and hash it in one streamed pass. The file is not loaded into memory."""
    size = path.stat().st_size
    if size > rules.max_bytes:
        raise Refused("adapter_too_large", 413, f"The file is larger than the {rules.max_bytes}-byte limit for adapters.")
    with path.open("rb") as stream:
        header = read_header(stream, size)
        stream.seek(0)
        digest = hashlib.sha256()
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
    return Validated(digest.hexdigest(), size, header)


# ------------------------------------------------------------------------------------- the engine's folder, a copy


def lora_root(models_dir: Path | None, folder: str) -> Path | None:
    """The engine's folder for one kind of adapter file: ``folder`` (the workflow binding's) inside the engine's models
    folder. None when the engine's models folder is not on this computer, or the folder name is not a plain name."""
    if models_dir is None or not _FOLDER.match(folder):
        return None
    root = models_dir / folder
    return root if root.is_dir() and not root.is_symlink() else None


def _check_digest(digest: str) -> str:
    if not isinstance(digest, str) or not _DIGEST.match(digest):
        raise ValueError("a digest is 64 lowercase hex characters")
    return digest


class Materializer:
    """Copies of uploaded adapters inside the engine's folder, named by content hash and found by the engine as
    ``<SUBFOLDER>/<hash>.safetensors``. A copy is made when a request uses the upload. The cache is bounded by idle time
    and total size. Only files this class names (64 hex characters and the suffix) are ever listed or removed."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._lock = threading.Lock()
        self._held: dict[str, int] = {}

    @property
    def folder(self) -> Path:
        return self._root / SUBFOLDER

    def name_for(self, digest: str) -> str:
        return f"{SUBFOLDER}/{_check_digest(digest)}{SUFFIX}"

    def _safe_folder(self, *, create: bool) -> Path | None:
        folder = self.folder
        if folder.is_symlink():
            raise Refused("adapter_folder_unsafe", 500, "The gateway's adapter folder is not a plain folder.")
        if not folder.exists():
            if not create:
                return None
            folder.mkdir()
        if folder.resolve().parent != self._root.resolve():
            raise Refused("adapter_folder_unsafe", 500, "The gateway's adapter folder is not inside the engine's folder.")
        return folder

    def materialize(self, digest: str, source: Path) -> str:
        """The name the engine lists for a copy of ``source``, which must hold the content ``digest`` names."""
        _check_digest(digest)
        with self._lock:
            folder = self._safe_folder(create=True)
            assert folder is not None
            target = folder / f"{digest}{SUFFIX}"
            if target.is_file() and not target.is_symlink() and target.stat().st_size == source.stat().st_size:
                os.utime(target, None)
                return self.name_for(digest)
            scratch = folder / f".{digest}.{os.getpid()}.{threading.get_ident()}.tmp"
            actual = hashlib.sha256()
            try:
                with source.open("rb") as src, scratch.open("wb") as dst:
                    while chunk := src.read(_CHUNK):
                        actual.update(chunk)
                        dst.write(chunk)
                if actual.hexdigest() != digest:
                    raise ValueError("the source does not hold the content its digest names")
                os.replace(scratch, target)
            finally:
                scratch.unlink(missing_ok=True)
        return self.name_for(digest)

    def remove(self, digest: str) -> None:
        with self._lock:
            folder = self._safe_folder(create=False)
            if folder is not None:
                (folder / f"{_check_digest(digest)}{SUFFIX}").unlink(missing_ok=True)

    @contextlib.contextmanager
    def hold(self, digest: str) -> Iterator[None]:
        """While held, ``reap`` leaves the copy alone: a request that has chosen it is still being served."""
        _check_digest(digest)
        with self._lock:
            self._held[digest] = self._held.get(digest, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self._held[digest] -= 1
                if not self._held[digest]:
                    del self._held[digest]

    def reap(self, *, now: float | None = None, idle_s: float = IDLE_S, max_bytes: int = CACHE_MAX_BYTES) -> list[str]:
        """Remove copies idle longer than ``idle_s``, then the least recently used until the total is within
        ``max_bytes``. Returns the digests removed."""
        now = time.time() if now is None else now
        removed: list[str] = []
        with self._lock:
            folder = self._safe_folder(create=False)
            if folder is None:
                return removed
            found = []
            for entry in os.scandir(folder):
                match = _COPY_NAME.match(entry.name)
                if match is None or not entry.is_file(follow_symlinks=False) or match.group(1) in self._held:
                    continue
                stat = entry.stat(follow_symlinks=False)
                found.append((stat.st_mtime, stat.st_size, match.group(1), Path(entry.path)))
            found.sort()
            total = sum(size for _, size, _, _ in found)
            for mtime, size, digest, path in found:
                if now - mtime > idle_s or total > max_bytes:
                    path.unlink(missing_ok=True)
                    removed.append(digest)
                    total -= size
        return removed


# ------------------------------------------------------------------------------------------------------ the pick-list


@dataclass(frozen=True)
class Offer:
    """One of a person's own uploads: the asset id a request names it by, the label the person gave it, and the
    architectures its header names."""

    name: str
    label: str
    architectures: tuple[str, ...]


def join(operator_names: Sequence[str], uploads: Sequence[Offer], architecture: str) -> list[str]:
    """The names a person may choose for a workflow of ``architecture``: the operator's files, then the person's own
    uploads that name that architecture. An upload with no known architecture is not offered."""
    chosen = list(operator_names)
    seen = set(chosen)
    for upload in uploads:
        if architecture in upload.architectures and upload.name not in seen:
            chosen.append(upload.name)
            seen.add(upload.name)
    return chosen


def digest_of(name: str) -> str | None:
    """The content digest an offered upload name carries, or None for a name that is not an upload's asset id."""
    match = _ASSET_ID.match(name) if isinstance(name, str) else None
    return match.group(1) if match else None
