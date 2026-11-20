"""Member adapter uploads: the checks on an uploaded safetensors file, and how the engine is given a copy.

What an uploader controls is the file's bytes, its name and its size. This module reads 8 bytes, a bounded JSON header and
the file's length. It never loads a tensor, never unpickles, and never uses the uploader's name for a path: a stored
upload is named by a digest of the person and the content (so each person's upload is their own asset), and its copy in
the engine's folder is named by the hash of its content.

The module names no engine, model or node class. Which base a file is for comes from ``registry/adapter_architectures.yml``,
the table, and the header test, that the operator's own files are judged by (``adapter_choices``). Where the engine's loras folder is comes from the engine directory the
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
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from gateway.adapter_choices import architecture_table, compatible

SETTING_KEY = "adapter_uploads"      # the owner's setting in admin.db: {"enabled": bool, "max_bytes": int, "max_total_bytes": int}
SUBFOLDER = "azizam-uploads"         # the gateway's own folder inside the engine's adapter folder
SUFFIX = ".safetensors"
DEFAULT_MAX_BYTES = 2 * (1 << 30)    # a file over this is refused
MAX_BYTES_CEILING = 8 * (1 << 30)    # the owner may raise the cap to this, and the request body limit follows it
MAX_HEADER_BYTES = 8 << 20           # a header over this is refused before it is read
IDLE_S = 24 * 3600.0                 # a copy no request used for this long is removed
CACHE_MAX_BYTES = 16 * (1 << 30)     # all copies together stay under this; the least recently used go first
DEFAULT_TOTAL_BYTES = 10 * (1 << 30)  # one person's uploads together stay under this
TOTAL_BYTES_CEILING = 512 * (1 << 30)  # the owner may raise the per-person total to this
HOLD_TTL_S = 6 * 3600.0              # a hold the run never released (a lost worker) ends after this
SCRATCH_MAX_AGE_S = 3600.0           # a half-written copy older than this is a crash leftover
FRAMING_BYTES = 1 << 20              # what a multipart body may carry beyond the file
SWEEP_EVERY_S = 600.0                # how often the gateway sweeps the engine's folders

_METADATA = "__metadata__"
# Every pattern is used with ``fullmatch``: ``$`` would also accept a trailing newline.
_DIGEST = re.compile(r"[0-9a-f]{64}")
_COPY_NAME = re.compile(r"([0-9a-f]{64})" + re.escape(SUFFIX))
_SCRATCH_NAME = re.compile(r"\.[0-9a-f]{64}\.\d+\.\d+\.tmp")
_FOLDER = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ASSET_ID = re.compile(r"ast_([0-9a-f]{64})")
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
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:   # a deeply nested header is not a header
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


def architectures(metadata: Mapping[str, str], table: Mapping[str, Mapping[str, Sequence[str]]]) -> tuple[str, ...]:
    """The architectures the table lists for a file, by the test the operator's own files meet (``compatible``): a header
    key that holds one of the architecture's values. Empty when the file names no base the table knows."""
    probe = "upload"
    return tuple(sorted(arch for arch in table if compatible([probe], {probe: metadata}, arch, table, ())))


# ------------------------------------------------------------------------------------------------ the owner's setting


@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    max_bytes: int = DEFAULT_MAX_BYTES                # the largest file
    max_total_bytes: int = DEFAULT_TOTAL_BYTES        # the most one person stores in all


def _bounded(value: Any, default: int, ceiling: int) -> int:
    return min(value, ceiling) if type(value) is int and value > 0 else default


def policy(setting: Any) -> Policy:
    """The owner's setting as a policy. Anything not shaped as expected leaves uploads off and the caps at their defaults."""
    if not isinstance(setting, Mapping):
        return Policy()
    return Policy(setting.get("enabled") is True,
                  _bounded(setting.get("max_bytes"), DEFAULT_MAX_BYTES, MAX_BYTES_CEILING),
                  _bounded(setting.get("max_total_bytes"), DEFAULT_TOTAL_BYTES, TOTAL_BYTES_CEILING))


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


def admit_total(rules: Policy, *, used: int, size: int) -> None:
    """Refuse an upload that would take a person past the owner's total: ``used`` is what their other uploads hold."""
    if used + size > rules.max_total_bytes:
        raise Refused("adapter_total_too_large", 413,
                      f"Your adapters already use {used} of the {rules.max_total_bytes} bytes the owner allows each person, "
                      f"and this file adds {size}. Delete one from your Library first.")


@dataclass(frozen=True)
class Validated:
    digest: str
    size: int
    header: Header


def validate_stream(stream: BinaryIO, size: int, rules: Policy) -> Validated:
    """Check an upload and hash it in one streamed pass. The file is not loaded into memory."""
    if size > rules.max_bytes:
        raise Refused("adapter_too_large", 413, f"The file is larger than the {rules.max_bytes}-byte limit for adapters.")
    header = read_header(stream, size)
    stream.seek(0)
    digest = hashlib.sha256()
    while chunk := stream.read(_CHUNK):
        digest.update(chunk)
    return Validated(digest.hexdigest(), size, header)


def validate(path: Path, rules: Policy) -> Validated:
    """``validate_stream`` for a file on disk."""
    with path.open("rb") as stream:
        return validate_stream(stream, path.stat().st_size, rules)


def label_of(filename: str | None) -> str:
    """What the person called the file, kept to show it back to them. It is text only: never a path, never a stored name."""
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[:120] or "adapter"


# ------------------------------------------------------------------------------------- the engine's folder, a copy


def lora_root(models_dir: Path | None, folder: str) -> Path | None:
    """The engine's folder for one kind of adapter file: ``folder`` (the workflow binding's) inside the engine's models
    folder. None when the engine's models folder is not on this computer, or the folder name is not a plain name."""
    if models_dir is None or not _FOLDER.fullmatch(folder):
        return None
    root = models_dir / folder
    return root if root.is_dir() and not root.is_symlink() else None


def _check_digest(digest: str) -> str:
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("a digest is 64 lowercase hex characters")
    return digest


class Materializer:
    """Copies of uploaded adapters inside the engine's folder, named by content hash and found by the engine as
    ``<SUBFOLDER>/<hash>.safetensors``. A copy is made when a request uses the upload. The cache is bounded by idle time
    and total size. Only files this class names (64 hex characters and the suffix) are ever listed or removed.

    One instance serves one folder for the life of the process (``Library`` keeps it), so its lock and its holds are shared
    by every caller. A run holds the copies it names under a key of its own until it ends; a held copy is never reaped, and
    a removal asked for while it is held waits for the last hold to end. A hold also ends by itself after a time bound, so a
    run that is lost cannot keep a file for ever."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._lock = threading.Lock()
        self._holds: dict[str, tuple[set[str], float]] = {}     # key -> (digests, when the hold ends by itself)
        self._removal_waiting: set[str] = set()
        self._verified: set[str] = set()                         # copies whose content this process has checked

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
            folder.mkdir(exist_ok=True)          # another maker may have made it since the look
        if folder.resolve().parent != self._root.resolve():
            raise Refused("adapter_folder_unsafe", 500, "The gateway's adapter folder is not inside the engine's folder.")
        return folder

    # -- holds (the caller holds the lock)

    def _held_now(self, now: float) -> set[str]:
        for key in [key for key, (_, ends) in self._holds.items() if ends <= now]:
            del self._holds[key]
        return set().union(*(digests for digests, _ in self._holds.values())) if self._holds else set()

    def _hold_locked(self, key: str, digests: Iterable[str], ttl_s: float) -> None:
        held, _ = self._holds.get(key, (set(), 0.0))
        self._holds[key] = (held | set(digests), time.time() + ttl_s)

    def _finish_removals(self, folder: Path | None, now: float) -> list[str]:
        """Remove each copy whose removal waited for a hold that has ended."""
        done: list[str] = []
        held = self._held_now(now)
        for digest in sorted(self._removal_waiting - held):
            self._removal_waiting.discard(digest)
            self._verified.discard(digest)
            if folder is not None:
                (folder / f"{digest}{SUFFIX}").unlink(missing_ok=True)
                done.append(digest)
        return done

    def hold_for(self, key: str, digests: Iterable[str], *, ttl_s: float = HOLD_TTL_S) -> None:
        """Hold the copies of ``digests`` for ``key`` until ``release(key)`` or the time bound."""
        wanted = [_check_digest(digest) for digest in digests]
        with self._lock:
            self._hold_locked(key, wanted, ttl_s)

    def release(self, key: str) -> list[str]:
        """End ``key``'s hold. Returns the copies whose removal had waited for it and is now done."""
        with self._lock:
            self._holds.pop(key, None)
            return self._finish_removals(self._safe_folder(create=False), time.time())

    def held_digests(self) -> set[str]:
        with self._lock:
            return self._held_now(time.time())

    @contextlib.contextmanager
    def hold(self, digest: str) -> Iterator[None]:
        """While held, ``reap`` leaves the copy alone: a request that has chosen it is still being served."""
        key = f"scope-{uuid.uuid4().hex}"
        self.hold_for(key, [digest])
        try:
            yield
        finally:
            self.release(key)

    # -- the copies

    def materialize(self, digest: str, source: Path, *, hold: str | None = None, ttl_s: float = HOLD_TTL_S) -> str:
        """The name the engine lists for a copy of ``source``, which must hold the content ``digest`` names. With ``hold``,
        the copy is held for that key from the moment it exists, so no reap can come between."""
        _check_digest(digest)
        with self._lock:
            folder = self._safe_folder(create=True)
            assert folder is not None
            target = folder / f"{digest}{SUFFIX}"
            self._removal_waiting.discard(digest)
            if not self._trusted(digest, target, source):
                self._copy(digest, source, folder, target)
            else:
                os.utime(target, None)
            if hold is not None:
                self._hold_locked(hold, [digest], ttl_s)
        return self.name_for(digest)

    def _trusted(self, digest: str, target: Path, source: Path) -> bool:
        """Whether the copy already there holds the content its name says: the size matches, and this process has read it
        and found its hash right (once per copy per process; a file placed or damaged by someone else is made again)."""
        if not (target.is_file() and not target.is_symlink() and target.stat().st_size == source.stat().st_size):
            return False
        if digest in self._verified:
            return True
        actual = hashlib.sha256()
        with target.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                actual.update(chunk)
        if actual.hexdigest() != digest:
            return False
        self._verified.add(digest)
        return True

    def _copy(self, digest: str, source: Path, folder: Path, target: Path) -> None:
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
            self._verified.add(digest)
        finally:
            scratch.unlink(missing_ok=True)

    def remove(self, digest: str) -> bool:
        """Remove the copy, or, when a run holds it, when the last hold ends. True when it is removed now."""
        _check_digest(digest)
        with self._lock:
            if digest in self._held_now(time.time()):
                self._removal_waiting.add(digest)
                return False
            folder = self._safe_folder(create=False)
            self._verified.discard(digest)
            if folder is not None:
                (folder / f"{digest}{SUFFIX}").unlink(missing_ok=True)
            return True

    def digests(self) -> list[str]:
        with self._lock:
            folder = self._safe_folder(create=False)
            if folder is None:
                return []
            try:
                return sorted(m.group(1) for name in os.listdir(folder) if (m := _COPY_NAME.fullmatch(name)))
            except FileNotFoundError:
                return []

    def reap(self, *, now: float | None = None, idle_s: float = IDLE_S, max_bytes: int = CACHE_MAX_BYTES) -> list[str]:
        """Remove copies idle longer than ``idle_s``, then the least recently used until the total is within
        ``max_bytes``. A held copy stays. Returns the digests removed."""
        now = time.time() if now is None else now
        removed: list[str] = []
        with self._lock:
            folder = self._safe_folder(create=False)
            if folder is None:
                return removed
            held = self._held_now(now)
            found = []
            try:
                entries = list(os.scandir(folder))
            except FileNotFoundError:
                return removed
            for entry in entries:
                match = _COPY_NAME.fullmatch(entry.name)
                if match is None or match.group(1) in held:
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except FileNotFoundError:        # removed since the listing
                    continue
                found.append((stat.st_mtime, stat.st_size, match.group(1), Path(entry.path)))
            found.sort()
            total = sum(size for _, size, _, _ in found)
            for mtime, size, digest, path in found:
                if now - mtime > idle_s or total > max_bytes:
                    path.unlink(missing_ok=True)
                    self._verified.discard(digest)
                    removed.append(digest)
                    total -= size
            removed.extend(self._finish_removals(folder, now))
        return removed

    def clear_scratch(self, *, now: float | None = None, max_age_s: float = SCRATCH_MAX_AGE_S) -> int:
        """Remove half-written copies a crash left (``.<digest>.<pid>.<thread>.tmp``) that are older than ``max_age_s``."""
        now = time.time() if now is None else now
        cleared = 0
        with self._lock:
            folder = self._safe_folder(create=False)
            if folder is None:
                return 0
            try:
                entries = list(os.scandir(folder))
            except FileNotFoundError:
                return 0
            for entry in entries:
                if _SCRATCH_NAME.fullmatch(entry.name) is None:
                    continue
                try:
                    if entry.is_file(follow_symlinks=False) and now - entry.stat(follow_symlinks=False).st_mtime > max_age_s:
                        Path(entry.path).unlink(missing_ok=True)
                        cleared += 1
                except FileNotFoundError:
                    continue
        return cleared


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


def asset_digest(person_id: str, content_digest: str) -> str:
    """The digest in an upload's Library asset id: one per person and file. The same bytes uploaded by two people are two
    assets, each the sole creation of its person, so the Library's ownership rule shows each to its own person and one
    person's upload can neither hide another's nor tell them whether the file is held."""
    return hashlib.sha256(f"adapter-upload\0{person_id}\0{_check_digest(content_digest)}".encode("utf-8")).hexdigest()


def digest_of(name: str) -> str | None:
    """The digest an upload's asset id carries, or None for a name that is not an upload's asset id."""
    match = _ASSET_ID.fullmatch(name) if isinstance(name, str) else None
    return match.group(1) if match else None


class UnknownAdapter(LookupError):
    """A name that is not one of this person's offered uploads: unknown, someone else's, deleted, or no longer readable.
    The caller refuses it exactly as it refuses any name that is not in the pick-list, so names cannot be probed."""


class Library:
    """A person's uploaded adapters, seen through the ports the gateway gives it: the asset store, the Library's own
    ownership rule (``holds``), the owner's setting and the engine's folder. It adds no rule of its own about who may see
    a file: ``holds`` decides that, and this class asks it for every file before it reads one."""

    def __init__(
        self, *,
        asset_ids: Callable[[], Iterable[str]],
        metadata: Callable[[str], Mapping[str, Any]],
        path: Callable[[str], Path | None],
        holds: Callable[[str, str], bool],
        policy: Callable[[], Policy],
        lora_root: Callable[[str], Path | None],
        note_copy: Callable[[str, str], None],
        table: Mapping[str, Mapping[str, Sequence[str]]] | None = None,
    ) -> None:
        self._asset_ids, self._metadata, self._path, self._holds = asset_ids, metadata, path, holds
        self._policy, self._lora_root, self._note_copy = policy, lora_root, note_copy
        self._table = table
        self._headers: dict[tuple[str, int, int], tuple[str, ...]] = {}
        self._materializers: dict[Path, Materializer] = {}     # one per folder for the process: its lock and holds are shared
        self._seen_folders: set[str] = set()
        self._guard = threading.Lock()

    def _copies(self, root: Path) -> Materializer:
        with self._guard:
            return self._materializers.setdefault(root, Materializer(root))

    def _architectures_of(self, path: Path) -> tuple[str, ...] | None:
        try:
            stat = path.stat()
            key = (str(path), stat.st_size, stat.st_mtime_ns)
            held = self._headers.get(key)
            if held is None:
                with path.open("rb") as stream:
                    header = read_header(stream, stat.st_size)
                held = architectures(header.metadata, architecture_table() if self._table is None else self._table)
                if len(self._headers) > 4096:
                    self._headers.clear()
                self._headers[key] = held
            return held
        except (OSError, Refused):
            return None

    def _offer(self, person_id: str, asset_id: str) -> Offer | None:
        marker = self._metadata(asset_id).get("adapter")
        path = self._path(asset_id)
        if not isinstance(marker, Mapping) or path is None or not self._holds(person_id, asset_id):
            return None
        found = self._architectures_of(path)
        return None if found is None else Offer(asset_id, str(marker.get("label") or ""), found)

    def _content_digest(self, asset_id: str) -> str | None:
        marker = self._metadata(asset_id).get("adapter")
        found = marker.get("content_sha256") if isinstance(marker, Mapping) else None
        return found if isinstance(found, str) and _DIGEST.fullmatch(found) else None

    def _content_digests(self, *, excluding: str | None = None) -> set[str]:
        """The content digests of every stored upload (but ``excluding``), whoever owns it."""
        found = (self._content_digest(asset_id) for asset_id in self._asset_ids()
                 if asset_id != excluding and digest_of(asset_id) is not None)
        return {digest for digest in found if digest is not None}

    def _on(self, folder: str) -> bool:
        return self._policy().enabled and self._lora_root(folder) is not None

    def offers(self, person_id: str, *, folder: str) -> list[Offer]:
        """The person's own uploads, whatever base they name (``join`` keeps the ones that suit a workflow). Empty when
        uploads are off or the engine's folder is not on this computer."""
        if not self._on(folder):
            return []
        offers = (self._offer(person_id, asset_id) for asset_id in self._asset_ids() if digest_of(asset_id) is not None)
        return [offer for offer in offers if offer is not None]

    def offered(self, person_id: str, *, folder: str, architecture: str) -> list[str]:
        """The names the person may choose for a workflow of ``architecture`` (the pick-list's join)."""
        return join([], self.offers(person_id, folder=folder), architecture)

    def labels(self, person_id: str, *, folder: str, architecture: str) -> dict[str, str]:
        """The label the person gave each upload that ``offered`` lists; an upload given no label is left out."""
        offers = {offer.name: offer for offer in self.offers(person_id, folder=folder)}
        return {name: offers[name].label for name in self.offered(person_id, folder=folder, architecture=architecture)
                if offers.get(name) is not None and offers[name].label}

    def is_copy(self, name: str) -> bool:
        """Whether an engine file name is in the gateway's own subfolder, whichever separator the engine lists it with."""
        return isinstance(name, str) and name.replace("\\", "/").lower().startswith(SUBFOLDER + "/")

    def resolve(self, person_id: str, name: str, *, folder: str) -> Offer | None:
        if digest_of(name) is None or not self._on(folder):
            return None
        return self._offer(person_id, name)

    def engine_names(self, person_id: str, names: Iterable[str], *, folder: str, hold: str | None = None,
                     hold_ttl_s: float = HOLD_TTL_S) -> dict[str, str]:
        """The engine's name for each chosen upload, copied into the engine's folder when a request uses it. A name
        that is not an upload's asset id (an operator's file) is not in the answer. A name that is an asset id but not
        one of this person's offered uploads raises ``UnknownAdapter``, whoever owns it, if anyone does.

        With ``hold``, each copy is held for that key from the moment it exists, until ``release(hold)`` or the time
        bound; the caller releases it when the run that names the copy has ended. When this call fails, nothing stays held."""
        wanted = [name for name in dict.fromkeys(names) if digest_of(name) is not None]
        found = {name: self.resolve(person_id, name, folder=folder) for name in wanted}
        if any(offer is None for offer in found.values()):
            raise UnknownAdapter(next(name for name, offer in found.items() if offer is None))
        root = self._lora_root(folder)
        if root is None or not wanted:
            return {}
        copies = self._copies(root)
        copies.reap()
        chosen: dict[str, str] = {}
        try:
            for name in wanted:
                source = self._path(name)
                digest = self._content_digest(name)
                if source is None or digest is None:
                    raise UnknownAdapter(name)
                chosen[name] = copies.materialize(digest, source, hold=hold, ttl_s=hold_ttl_s)
                self._seen_folders.add(folder)
                try:
                    self._note_copy(name, folder)
                except LookupError as gone:       # the upload was deleted while the copy was made
                    self._unwanted(copies, digest, excluding=name)
                    raise UnknownAdapter(name) from gone
        except BaseException:
            if hold is not None:
                self.release(hold)
            raise
        return chosen

    def _unwanted(self, copies: Materializer, digest: str, *, excluding: str) -> None:
        """Remove a copy that no stored upload (but ``excluding``) names."""
        if digest not in self._content_digests(excluding=excluding):
            copies.remove(digest)

    def hold_copies(self, key: str, names: Iterable[Any], *, ttl_s: float = HOLD_TTL_S) -> None:
        """Hold the copies the engine names ``names`` for ``key``: the names a request carries after ``engine_names``. A
        name that is not one of the gateway's copies is left alone. A copy is held in each engine folder this gateway
        uses, so a run that comes back after a restart can hold its copies again."""
        digests = [m.group(1) for name in names if isinstance(name, str) and self.is_copy(name)
                   and (m := _COPY_NAME.fullmatch(name.replace("\\", "/").rsplit("/", 1)[-1])) is not None]
        if not digests:
            return
        for folder in sorted(self.folders()):
            root = self._lora_root(folder)
            if root is not None:
                self._copies(root).hold_for(key, digests, ttl_s=ttl_s)

    def release(self, key: str | None) -> None:
        """End ``key``'s holds in every folder, and finish the removals that waited for them."""
        if key is None:
            return
        for materializer in list(self._materializers.values()):
            materializer.release(key)

    def held_digests(self) -> set[str]:
        return set().union(*(m.held_digests() for m in list(self._materializers.values())), set())

    def used_bytes(self, person_id: str, *, excluding: str | None = None) -> int:
        """What the person's own stored uploads take, but ``excluding`` (the one an upload would replace). Only what the
        Library's ownership rule says the person holds is counted."""
        total = 0
        for asset_id in self._asset_ids():
            if asset_id == excluding or digest_of(asset_id) is None:
                continue
            if not isinstance(self._metadata(asset_id).get("adapter"), Mapping) or not self._holds(person_id, asset_id):
                continue
            path = self._path(asset_id)
            try:
                total += path.stat().st_size if path is not None else 0
            except OSError:
                continue
        return total

    def forget(self, asset_id: str, metadata: Mapping[str, Any]) -> None:
        """The asset is going: remove its copies from the folders its sidecar says it was copied to. A copy is named by the
        file's content, so one that another person's upload of the same bytes also uses stays until that goes too. A copy
        a run still holds goes when the run ends."""
        marker = metadata.get("adapter") if isinstance(metadata, Mapping) else None
        copied = marker.get("copies") if isinstance(marker, Mapping) else None
        digest = marker.get("content_sha256") if isinstance(marker, Mapping) else None
        if not (isinstance(digest, str) and _DIGEST.fullmatch(digest) and isinstance(copied, list)):
            return
        if digest in self._content_digests(excluding=asset_id):
            return
        for folder in copied:
            root = self._lora_root(folder) if isinstance(folder, str) else None
            if root is not None:
                self._copies(root).remove(digest)

    def sweep(self, *, folder: str) -> list[str]:
        """Remove the copies in a folder whose asset is no longer stored, and the idle or over-bound ones, and the scratch
        files a crash left. A copy a run holds stays until the run ends. Returns the digests removed."""
        root = self._lora_root(folder)
        if root is None:
            return []
        copies = self._copies(root)
        stored = self._content_digests()
        removed = copies.reap()
        for digest in copies.digests():
            if digest not in stored and copies.remove(digest):
                removed.append(digest)
        copies.clear_scratch()
        return removed

    def folders(self) -> set[str]:
        """The engine folders this gateway has copied uploads to: the ones this process has used, and the ones the stored
        uploads' sidecars say were used before."""
        found = set(self._seen_folders)
        for asset_id in self._asset_ids():
            marker = self._metadata(asset_id).get("adapter") if digest_of(asset_id) is not None else None
            copied = marker.get("copies") if isinstance(marker, Mapping) else None
            found.update(item for item in copied or () if isinstance(item, str))
        return found

    def sweep_all(self) -> list[str]:
        """``sweep`` for every folder in ``folders``: the periodic task's one call."""
        removed: list[str] = []
        for folder in sorted(self.folders()):
            removed.extend(self.sweep(folder=folder))
        return removed
