"""Media types and kinds of stored assets, as data.

An asset's kind is the top-level part of its IANA media type (``image``, ``video``, ``audio``, ``model``,
``text``...), or ``file`` when the type is unknown. Nothing here names an engine or an output format that
only one engine makes: a new output type is one row in ``SUFFIX_MEDIA_TYPES``, or nothing at all when the
standard library already knows the suffix.
"""
from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path

import yaml

UNKNOWN_MEDIA_TYPE = "application/octet-stream"
UNKNOWN_KIND = "file"

# The words for a kind live in the shipped data file ``registry/asset_kinds.yml``, one row per IANA top-level type this
# product has words for. The web reads them from ``GET /me/api/media`` and ``GET /me/api/home`` and keeps only icons of
# its own. A kind with no row is named from its own type, so a new media type needs no entry to list, filter and download.
KIND_NAMES_PATH = Path(__file__).resolve().parent.parent / "registry" / "asset_kinds.yml"
KIND_WORDS = ("label", "plural", "untitled", "input")


def load_kind_names(path: Path = KIND_NAMES_PATH) -> dict[str, dict[str, str]]:
    """The kind table in a data file: ``{kind: {label, plural, untitled, input}}``. A file that is missing a word, or holds
    anything but plain text for one, is a fault in what was shipped, so it raises."""
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = document.get("kinds") if isinstance(document, dict) else None
    if not isinstance(rows, dict) or not rows:
        raise ValueError(f"{path.name}: expected a 'kinds' mapping")
    table: dict[str, dict[str, str]] = {}
    for kind, words in rows.items():
        if not isinstance(kind, str) or not kind or kind != kind.lower() or not isinstance(words, dict):
            raise ValueError(f"{path.name}: kind {kind!r} must be a lower-case name with a mapping of words")
        if set(words) != set(KIND_WORDS) or not all(isinstance(words[w], str) and words[w].strip() for w in KIND_WORDS):
            raise ValueError(f"{path.name}: kind {kind!r} needs exactly {', '.join(KIND_WORDS)}, each a non-empty string")
        table[kind] = {word: words[word].strip() for word in KIND_WORDS}
    return table


KIND_NAMES: dict[str, dict[str, str]] = load_kind_names()

# Suffix -> media type. These rows win over the standard library, which lacks some types and
# disagrees on others (for example ``.m4a``).
SUFFIX_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".obj": "model/obj",
    ".stl": "model/stl",
    ".ply": "model/ply",
    ".usdz": "model/vnd.usdz+zip",
}


def media_type_for_suffix(suffix: str) -> str:
    """The media type for a file suffix such as ``.glb``; unknown suffixes give ``application/octet-stream``."""
    suffix = (suffix or "").lower()
    known = SUFFIX_MEDIA_TYPES.get(suffix)
    if known:
        return known
    guessed, _encoding = mimetypes.guess_type(f"x{suffix}") if suffix else (None, None)
    return guessed or UNKNOWN_MEDIA_TYPE


def kind_for_media_type(media_type: str | None) -> str:
    """The kind of an asset with this media type: its top-level type, or ``file`` when unknown."""
    top, _, rest = str(media_type or "").partition("/")
    top = top.strip().lower()
    if not top or not rest or (top == "application" and rest.strip().lower() == "octet-stream"):
        return UNKNOWN_KIND
    return top


def kind_names(kind: str) -> dict[str, str]:
    """``{kind, label, plural, untitled, input}`` for a kind; a kind with no row is named from its own type."""
    row = KIND_NAMES.get(kind)
    if row is None:
        word = kind[:1].upper() + kind[1:] if kind else "File"
        row = {"label": word, "plural": word, "untitled": word, "input": word.lower()}
    return {"kind": kind, **row}


def input_word(modality: str) -> str:
    """What a person calls a file of this modality when a workflow asks for it: "picture", "recording", "clip"."""
    return kind_names(str(modality or "").strip().lower())["input"]


def file_words(media_type: str | None) -> dict[str, str | None]:
    """How a person names a file of this media type: ``{"word": "picture", "format": "PNG"}``. The format is None when
    the type has no known suffix."""
    suffix = suffix_for_media_type(media_type)
    return {"word": input_word(kind_for_media_type(media_type)), "format": suffix.lstrip(".").upper() if suffix != ".bin" else None}


def suffix_for_media_type(media_type: str | None) -> str:
    """A file suffix for a media type, such as ``.glb`` for ``model/gltf-binary``; ``.bin`` when none is known."""
    clean = str(media_type or "").split(";")[0].strip().lower()
    for suffix, known in SUFFIX_MEDIA_TYPES.items():
        if known == clean:
            return suffix
    guessed = mimetypes.guess_extension(clean) if clean else None
    return guessed or ".bin"


def download_file_name(stem: str | None, suffix: str | None, media_type: str | None, fallback: str = "download") -> str:
    """The name a saved copy gets: a safe stem plus the stored suffix, or the suffix the media type gives."""
    clean = "".join(ch if ch.isalnum() or ch in "-_. ()" else "-" for ch in str(stem or "")).strip(" .-")[:80]
    extension = str(suffix or "").lower()
    if not extension.startswith(".") or len(extension) < 2:
        extension = suffix_for_media_type(media_type)
    return f"{clean or fallback}{extension}"


# The stored-id prefix of the types that have a fixed one (see ``proxy._ASSET_SUFFIXES_BY_KIND``); any other type
# is stored under ``ast``.
ASSET_PREFIX_BY_TOP_TYPE: dict[str, str] = {"image": "img", "audio": "aud", "video": "vid"}
OTHER_ASSET_PREFIX = "ast"


def asset_prefix_for_media_type(media_type: str | None) -> str:
    """The asset id prefix a stored file of this media type gets."""
    return ASSET_PREFIX_BY_TOP_TYPE.get(kind_for_media_type(media_type), OTHER_ASSET_PREFIX)


@dataclass(frozen=True)
class OutputInput:
    """One file a workflow was given, kept so the output can say what it was made from."""

    data: bytes
    #: The name the caller gave it (``garden-gnome.png``); only its stem is shown.
    name: str
    media_type: str


@dataclass(frozen=True)
class WorkflowOutput:
    """A file a workflow made, with what made it. Any workflow route can hand this to the asset store."""

    data: bytes
    suffix: str
    workflow_name: str | None
    request_id: str
    inputs: tuple[OutputInput, ...] = ()
    #: Whether the workflow takes words from the caller. When it does not, the Library does not say a prompt is missing.
    takes_text: bool = True
