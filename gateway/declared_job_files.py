"""Bind Library assets to the named file inputs of a declared operation, at submit.

A caller of a declared media job may send ``"files": {"<name>": {"asset_id": "<id>"}}``. The operation's description
declares which names exist and which media types each takes (``Operation.files``); this module reads nothing else about
the operation. It names no operation, engine, model or input: the declared inputs arrive as data.

Each asset passes through the media input resolver (``gateway/media_inputs.py``), the one place that applies the
caller's read check, measures the bytes, matches the media type and holds a lease for the job. Every refusal comes before
a job exists, with a stable code, so nothing runs for a request that cannot be served.

An asset the caller may not read and an asset that does not exist get the same answer, word for word, so the answer
never confirms that someone else's asset exists. Errors carry the input name the caller sent and the declared types;
never an internal path, another principal's id or any bytes.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from gateway import media_inputs
from gateway.engine_protocol.description import FileInput
from gateway.engine_protocol.types import AssetRef


class FileBindingError(Exception):
    """A ``files`` entry was refused. Nothing has run and no lease is held. ``message`` is safe to show the caller."""

    def __init__(self, status: int, code: str, message: str, param: str = "files") -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.param = param


@dataclass(frozen=True)
class BoundFiles:
    """The refs the engine request names, and the bytes core already read and checked for them (by asset id)."""

    refs: Mapping[str, AssetRef]
    held: Mapping[str, bytes]


NOT_FOUND = "The asset for this file input was not found."
_NOT_ACCEPTED = "The asset is not a file type this input accepts"


def _refusal(name: str, error: media_inputs.MediaInputError, declared: FileInput) -> FileBindingError:
    param = f"files.{name}"
    if error.code in (media_inputs.UNSUPPORTED, media_inputs.EMPTY):
        return FileBindingError(400, "file_type_not_accepted", f"{_NOT_ACCEPTED}: {', '.join(declared.accepts)}.", param)
    if error.code == media_inputs.DISCLOSURE_REFUSED:
        return FileBindingError(403, "file_disclosure_refused", error.message, param)
    if error.code == media_inputs.TOO_LARGE:
        return FileBindingError(413, "files_too_large", "The file is larger than the size limit.", param)
    # Malformed id, unknown asset and an asset this caller may not read: one answer, so existence is never confirmed.
    return FileBindingError(400, "file_asset_not_found", NOT_FOUND, param)


def _asset_id(name: str, entry: Any) -> str:
    if not isinstance(entry, Mapping) or set(entry) != {"asset_id"} or not isinstance(entry["asset_id"], str):
        raise FileBindingError(400, "invalid_files", 'Each file input takes {"asset_id": "<id>"}.', f"files.{name}")
    return entry["asset_id"]


def bind(files: Any, declared: Mapping[str, FileInput], *, resolver: media_inputs.MediaInputResolver, caller: Any,
         holder: media_inputs.Holder, destination: media_inputs.Destination, max_upload_bytes: int) -> BoundFiles:
    """Resolve ``files`` against the operation's declared inputs. Raises ``FileBindingError`` before any effect.

    ``caller``, ``holder``, ``destination``, ``declared`` and ``max_upload_bytes`` come from core, never from the
    request. On a refusal after some asset was leased, the holder's leases are released."""
    if files is None:
        files = {}
    if not isinstance(files, Mapping):
        raise FileBindingError(400, "invalid_files", "files must be an object that maps an input name to an asset.")
    if files and not declared:
        raise FileBindingError(400, "files_not_accepted", "This operation takes no file inputs.")
    for name in files:
        if name not in declared:
            raise FileBindingError(400, "unknown_file_input",
                                   f"This operation has no file input named '{name}'. It takes: {', '.join(sorted(declared))}.",
                                   f"files.{name}")
    ids = {name: _asset_id(name, entry) for name, entry in files.items()}
    for name, spec in declared.items():
        if spec.required and name not in ids:
            raise FileBindingError(400, "missing_file_input", f"The file input '{name}' is required.", f"files.{name}")
    accepts = {name: spec.accepts for name, spec in declared.items()}
    refs: dict[str, AssetRef] = {}
    held: dict[str, bytes] = {}
    try:
        for name, asset_id in ids.items():
            try:
                _, resolved = resolver.resolve_named_asset(
                    name, asset_id, caller, holder=holder, destination=destination, declared_inputs=accepts)
            except media_inputs.MediaInputError as error:
                raise _refusal(name, error, declared[name]) from None
            refs[name] = AssetRef(asset_id, resolved.ref.media_type, resolved.ref.size_bytes)
            held[asset_id] = resolved.data
        if sum(ref.size for ref in refs.values()) > max_upload_bytes:  # each part counts, as the executor's budget does
            raise FileBindingError(413, "files_too_large", "The files are larger than the upload size limit.")
    except BaseException:
        resolver.release(holder)
        raise
    return BoundFiles(refs, held)
