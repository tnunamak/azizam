"""Sample recordings with human reference transcripts, fetched on demand.

``registry/stt_samples.yml`` lists each sample as data: its licence, the files to download (URL, sha256, size) and a
recipe that says how to read the reference words. ``load_manifest`` reads and checks that file. ``fetch_sample``
downloads the sources into a cache (one file per sha256, so an archive that several samples share downloads once),
converts the audio to a 16 kHz mono PCM16 WAV with ffmpeg and builds the word list ``[(word, t0, t1), ...]`` sorted by
time.

The three recipes port ``build.py`` of the private evaluation set (~/Documents/azizam/stt-eval-data/scripts): the same
normalisation, the same gap filling for ICSI and the same interpolation for Earnings-21, so the output equals its
``ref.json``. Every function here blocks; a caller on an event loop runs them in a thread. The network is used only
inside ``fetch_sample``, through ``client_factory``.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import wave
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import httpx
import yaml

DEFAULT_MANIFEST = Path(__file__).resolve().parent.parent / "registry" / "stt_samples.yml"
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
SAMPLE_RATE = 16000

Word = tuple[str, float, float]
Progress = Callable[[int, int, str], None]

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TIMINGS = ("word", "segment-interpolated")
_SAMPLE_KEYS = {"id", "corpus", "title", "licence", "duration_seconds", "speakers", "timing", "sources", "recipe"}
_LICENCE_KEYS = {"name", "url", "attribution", "note"}
_SOURCE_KEYS = {"name", "url", "sha256", "bytes"}


class SampleError(Exception):
    """A manifest, download or conversion problem, told as one plain sentence."""


class SampleCancelled(SampleError):
    """The caller's ``cancelled()`` answered True during a download."""


@dataclass(frozen=True)
class Licence:
    name: str
    url: str
    attribution: str
    note: str


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class SampleSpec:
    id: str
    corpus: str
    title: str
    licence: Licence
    duration_seconds: float
    speakers: int
    timing: str
    sources: tuple[Source, ...]
    recipe_kind: str
    recipe_params: Mapping[str, Any]

    def download_bytes(self) -> int:
        """Bytes to download for this sample when the cache is empty (a file listed twice counts once)."""
        return sum({s.sha256: s.bytes for s in self.sources}.values())


@dataclass(frozen=True)
class PreparedSample:
    audio_path: Path
    words: list[Word]
    duration_seconds: float
    spec: SampleSpec


# ---------------------------------------------------------------------------------------------------------------------
# Reference words: the three recipes


def norm_token(tok: str) -> list[str]:
    """Lowercase; drop punctuation (hyphens split words); keep internal apostrophes; drop tags like <inaudible>."""
    t = tok.lower().replace("’", "'")
    if re.search(r"[<\[{].*[>\]}]", t):
        return []
    t = re.sub(r"[^\w']", " ", t)
    return [p for p in (x.strip("'") for x in t.split()) if p and p != "_"]


def _xml_members(archive: Path, members: list[str]) -> list[ET.Element]:
    """The root element of each named member, in sorted member order."""
    roots = []
    with zipfile.ZipFile(archive) as zf:
        for member in sorted(members):
            try:
                with zf.open(member) as f:
                    roots.append(ET.parse(f).getroot())
            except KeyError:
                raise SampleError(f"The archive has no member named {member}.") from None
    return roots


def _ami_words(params: Mapping[str, Any], files: Mapping[str, Path]) -> list[Word]:
    """AMI words XML: one timed ``<w>`` per word; punctuation and truncated fragments are dropped."""
    words: list[Word] = []
    for root in _xml_members(files[params["annotations"]], params["word_members"]):
        for el in root:
            if el.tag != "w" or el.get("punc") == "true" or el.get("trunc") == "true" or not el.text:
                continue
            for w in norm_token(el.text):
                words.append((w, float(el.get("starttime")), float(el.get("endtime"))))
    return words


def _icsi_words(params: Mapping[str, Any], files: Mapping[str, Path]) -> list[Word]:
    """ICSI words XML. Some words have no start or end time: an untimed run is spread evenly between the previous
    known end and the next known start, and a missing end becomes the next start. A clitic ("'s", "'re") joins the
    previous word without its apostrophe ("thats"), as build.py does, so the words equal its ref.json. Only classes W
    and LET are words."""
    words: list[list] = []
    for root in _xml_members(files[params["annotations"]], params["word_members"]):
        prev = None
        items = [el for el in root if el.tag == "w" and el.text]

        def tm(el: ET.Element, k: str) -> float | None:
            v = el.get(k)
            return float(v) if v else None

        st = [tm(e, "starttime") for e in items]
        en = [tm(e, "endtime") for e in items]
        i = 0
        while i < len(items):
            if st[i] is not None:
                i += 1
                continue
            j = i
            while j < len(items) and st[j] is None:
                j += 1
            hi = st[j] if j < len(items) else None
            lo = next((en[k] if en[k] is not None else st[k] for k in range(i - 1, -1, -1)
                       if en[k] is not None or st[k] is not None), None)
            if lo is None:
                lo = (hi if hi is not None else 0.0) - 0.25 * (j - i)
            if hi is None or hi < lo:
                hi = lo + 0.25 * (j - i)
            for k in range(i, j):
                st[k] = lo + (k - i) * (hi - lo) / (j - i)
                en[k] = lo + (k - i + 1) * (hi - lo) / (j - i)
            i = j
        for i, el in enumerate(items):
            t0, t1 = st[i], en[i]
            if t1 is None or t1 < t0:
                nxt = st[i + 1] if i + 1 < len(items) else None
                t1 = nxt if nxt is not None and t0 <= nxt <= t0 + 1.0 else t0 + 0.25
            c = el.get("c")
            if el.text.startswith("'") and c in ("W", "APOSS") and prev is not None:
                prev[0] += "".join(norm_token(el.text))
                prev[2] = max(prev[2], t1)
                continue
            if c not in ("W", "LET"):
                continue
            prev = None
            for w in norm_token(el.text):
                prev = [w, t0, t1]
                words.append(prev)
    return [(w, t0, t1) for w, t0, t1 in words]


def _earnings21_words(params: Mapping[str, Any], files: Mapping[str, Path]) -> list[Word]:
    """Earnings-21 nlp file (token|speaker|..., no word times) plus RTTM speech segments. The speaker runs of the two
    must match one to one; each run's tokens are spread over its segments in proportion to segment length."""
    runs: list[list] = []
    with open(files[params["transcript"]], encoding="utf-8") as f:
        next(f)
        for line in f:
            p = line.rstrip("\n").split("|")
            ws = norm_token(p[0])
            if not ws:
                continue
            if not runs or runs[-1][0] != p[1]:
                runs.append([p[1], []])
            runs[-1][1].extend(ws)
    segs = []
    with open(files[params["segments"]], encoding="utf-8") as f:
        for line in f:
            p = line.split()
            if p:
                segs.append((float(p[3]), float(p[3]) + float(p[4]), p[7]))
    segs.sort()
    rruns: list[list] = []
    for s0, s1, sp in segs:
        if not rruns or rruns[-1][0] != sp:
            rruns.append([sp, []])
        rruns[-1][1].append((s0, s1))
    if [r[0] for r in runs] != [r[0] for r in rruns]:
        raise SampleError("The speaker turns of the transcript do not match the speaker turns of the segments file.")
    words: list[Word] = []
    for (_, ws), (_, sg) in zip(runs, rruns):
        sg = [(a, max(b, a + 1e-3)) for a, b in sg]
        total = sum(b - a for a, b in sg)

        def at(frac: float) -> float:  # position in the run's concatenated speech time -> wall clock
            x = frac * total
            for a, b in sg:
                if x <= b - a:
                    return a + x
                x -= b - a
            return sg[-1][1]

        n = len(ws)
        for k, w in enumerate(ws):
            words.append((w, round(at(k / n), 3), round(at((k + 1) / n), 3)))
    return words


# kind -> (function, params that name a source, other required params)
RECIPES: dict[str, tuple[Callable[[Mapping[str, Any], Mapping[str, Path]], list[Word]], tuple[str, ...], tuple[str, ...]]] = {
    "ami-words": (_ami_words, ("audio", "annotations"), ("word_members",)),
    "icsi-words": (_icsi_words, ("audio", "annotations"), ("word_members",)),
    "earnings21": (_earnings21_words, ("audio", "transcript", "segments"), ()),
}
_COMMON_PARAMS = ("cut_seconds",)


def build_reference(spec: SampleSpec, files: Mapping[str, Path]) -> list[Word]:
    """The sample's reference words from its downloaded sources (``files``: source name -> path), sorted by time."""
    function = RECIPES[spec.recipe_kind][0]
    words = function(spec.recipe_params, files)
    cut = spec.recipe_params.get("cut_seconds")
    if cut is not None:
        words = [w for w in words if w[2] <= cut]
    return sorted(words, key=lambda w: (w[1], w[2]))


def convert_audio(source: Path, target: Path, *, cut_seconds: float | None = None) -> float:
    """Write ``target`` as a 16 kHz mono PCM16 WAV (atomically) and return its duration in seconds, to 3 decimals."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SampleError("ffmpeg is needed to convert sample audio and was not found on PATH.")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".convert-", suffix=".wav")
    os.close(fd)
    cut = ["-t", str(cut_seconds)] if cut_seconds else []
    cmd = [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(source), *cut,
           "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", "-f", "wav", tmp]
    try:
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0:
            raise SampleError(f"ffmpeg could not convert {source.name} (exit status {result.returncode}).")
        with wave.open(tmp, "rb") as w:
            duration = round(w.getnframes() / w.getframerate(), 3)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return duration


# ---------------------------------------------------------------------------------------------------------------------
# The manifest


def _require(value: Any, kind: type | tuple[type, ...], where: str) -> Any:
    if not isinstance(value, kind) or isinstance(value, bool):
        raise SampleError(f"{where} is missing or has the wrong type.")
    return value


def _check_keys(item: Any, allowed: set[str], where: str) -> dict:
    _require(item, dict, where)
    unknown = sorted(set(item) - allowed)
    if unknown:
        raise SampleError(f"{where} has unknown keys: {', '.join(unknown)}.")
    missing = sorted(allowed - set(item))
    if missing:
        raise SampleError(f"{where} is missing keys: {', '.join(missing)}.")
    return item


def _https_url(value: Any, where: str) -> str:
    url = _require(value, str, where)
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise SampleError(f"{where} must be an https URL.")
    return url


def _parse_sample(raw: Any, index: int) -> SampleSpec:
    where = f"Sample {index + 1}"
    _check_keys(raw, _SAMPLE_KEYS, where)
    sample_id = _require(raw["id"], str, f"{where} id")
    if not _ID.match(sample_id):
        raise SampleError(f"{where} has id {sample_id!r}; an id may hold only letters, digits, '.', '_' and '-'.")
    where = f"Sample {sample_id}"
    lic = _check_keys(raw["licence"], _LICENCE_KEYS, f"{where} licence")
    licence = Licence(
        name=_require(lic["name"], str, f"{where} licence name"),
        url=_https_url(lic["url"], f"{where} licence url"),
        attribution=_require(lic["attribution"], str, f"{where} licence attribution"),
        note=_require(lic["note"], str, f"{where} licence note"),
    )
    timing = _require(raw["timing"], str, f"{where} timing")
    if timing not in _TIMINGS:
        raise SampleError(f"{where} timing must be one of: {', '.join(_TIMINGS)}.")
    duration = _require(raw["duration_seconds"], (int, float), f"{where} duration_seconds")
    speakers = _require(raw["speakers"], int, f"{where} speakers")
    if duration <= 0 or speakers <= 0:
        raise SampleError(f"{where} needs a positive duration_seconds and speakers.")

    sources = []
    for k, src in enumerate(_require(raw["sources"], list, f"{where} sources")):
        swhere = f"{where} source {k + 1}"
        _check_keys(src, _SOURCE_KEYS, swhere)
        sha = _require(src["sha256"], str, f"{swhere} sha256")
        if not _SHA256.match(sha):
            raise SampleError(f"{swhere} sha256 must be 64 lowercase hex digits.")
        size = _require(src["bytes"], int, f"{swhere} bytes")
        if size <= 0:
            raise SampleError(f"{swhere} bytes must be positive.")
        sources.append(Source(name=_require(src["name"], str, f"{swhere} name"),
                              url=_https_url(src["url"], f"{swhere} url"), sha256=sha, bytes=size))
    names = [s.name for s in sources]
    if not sources or len(set(names)) != len(names):
        raise SampleError(f"{where} needs at least one source, each with its own name.")

    recipe = _check_keys(raw["recipe"], {"kind", "params"}, f"{where} recipe")
    kind = _require(recipe["kind"], str, f"{where} recipe kind")
    if kind not in RECIPES:
        raise SampleError(f"{where} has recipe kind {kind!r}; known kinds are: {', '.join(sorted(RECIPES))}.")
    params = _require(recipe["params"], dict, f"{where} recipe params")
    _, source_params, other_params = RECIPES[kind]
    unknown = sorted(set(params) - set(source_params) - set(other_params) - set(_COMMON_PARAMS))
    missing = sorted(set(source_params + other_params) - set(params))
    if unknown or missing:
        raise SampleError(f"{where} recipe params do not fit kind {kind}: "
                          f"unknown {', '.join(unknown) or 'none'}; missing {', '.join(missing) or 'none'}.")
    for p in source_params:
        if params[p] not in names:
            raise SampleError(f"{where} recipe param {p} names no source of the sample.")
    if "word_members" in params:
        members = _require(params["word_members"], list, f"{where} recipe word_members")
        if not members or not all(isinstance(m, str) for m in members):
            raise SampleError(f"{where} recipe word_members must be a list of archive member paths.")
    if params.get("cut_seconds") is not None:
        cut = _require(params["cut_seconds"], (int, float), f"{where} recipe cut_seconds")
        if cut <= 0:
            raise SampleError(f"{where} recipe cut_seconds must be positive.")

    return SampleSpec(id=sample_id, corpus=_require(raw["corpus"], str, f"{where} corpus"),
                      title=_require(raw["title"], str, f"{where} title"), licence=licence,
                      duration_seconds=float(duration), speakers=speakers, timing=timing, sources=tuple(sources),
                      recipe_kind=kind, recipe_params=dict(params))


def load_manifest(path: Path | str = DEFAULT_MANIFEST) -> list[SampleSpec]:
    """Read and check the samples manifest. Top-level keys that start with ``x-`` hold YAML anchors and are ignored."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    _require(data, dict, "The manifest")
    unknown = sorted(k for k in data if k != "samples" and not str(k).startswith("x-"))
    if unknown:
        raise SampleError(f"The manifest has unknown top-level keys: {', '.join(map(str, unknown))}.")
    specs = [_parse_sample(raw, i) for i, raw in enumerate(_require(data.get("samples"), list, "The manifest samples"))]
    seen: set[str] = set()
    for spec in specs:
        if spec.id in seen:
            raise SampleError(f"The manifest lists sample {spec.id} more than once.")
        seen.add(spec.id)
    return specs


# ---------------------------------------------------------------------------------------------------------------------
# Downloads


def _default_client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=True,
                        headers={"User-Agent": "azizam-stt-samples/1"})


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK_BYTES), b""):
            h.update(block)
    return h.hexdigest()


def _download(client: httpx.Client, spec: SampleSpec, src: Source, target: Path, done: int, total: int,
              progress: Progress | None, cancelled: Callable[[], bool] | None) -> int:
    """Stream ``src`` to ``target`` through a temporary file, checking size and sha256. Returns the new done count."""
    label = f"Downloading {src.name} of {spec.id}"
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{src.sha256[:16]}-", suffix=".part")
    h = hashlib.sha256()
    got = 0
    try:
        with os.fdopen(fd, "wb") as out:
            try:
                with client.stream("GET", src.url) as response:
                    if response.status_code != 200:
                        raise SampleError(f"{label} failed: the server answered HTTP {response.status_code}.")
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > src.bytes:
                        raise SampleError(f"{label} stopped: the server offers {int(declared)} bytes, "
                                          f"more than the {src.bytes} the manifest declares.")
                    for chunk in response.iter_bytes(CHUNK_BYTES):
                        if cancelled is not None and cancelled():
                            raise SampleCancelled(f"{label} was cancelled.")
                        got += len(chunk)
                        if got > src.bytes:
                            raise SampleError(f"{label} stopped: the file is larger than the {src.bytes} bytes "
                                              "the manifest declares.")
                        h.update(chunk)
                        out.write(chunk)
                        if progress is not None:
                            progress(done + got, total, label)
            except httpx.HTTPError:
                raise SampleError(f"{label} failed: the connection broke before the file was complete.") from None
        if got != src.bytes:
            raise SampleError(f"{label} failed: it gave {got} bytes, but the manifest declares {src.bytes}.")
        if h.hexdigest() != src.sha256:
            raise SampleError(f"{label} failed: the file does not match its sha256 checksum, so it was deleted.")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return done + got


def fetch_sources(spec: SampleSpec, cache_dir: Path | str, *, progress: Progress | None = None,
                  cancelled: Callable[[], bool] | None = None, max_bytes: int = DEFAULT_MAX_BYTES,
                  client_factory: Callable[[], httpx.Client] = _default_client) -> dict[str, Path]:
    """Make every source of ``spec`` present and checked in ``cache_dir``; return source name -> cached path.

    A cached file whose sha256 matches is reused. A cached file that does not match is deleted and reported.
    """
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    total = spec.download_bytes()
    if total > max_bytes:
        raise SampleError(f"Sample {spec.id} needs {total} bytes of downloads, more than the limit of {max_bytes}.")
    done = 0
    files: dict[str, Path] = {}
    client = None
    try:
        for src in spec.sources:
            target = cache / src.sha256
            if target in files.values():
                files[src.name] = target  # the same file listed twice
                continue
            files[src.name] = target
            if target.exists():
                if target.stat().st_size == src.bytes and _file_sha256(target) == src.sha256:
                    done += src.bytes
                    if progress is not None:
                        progress(done, total, f"Using the cached {src.name} of {spec.id}")
                    continue
                target.unlink()
                raise SampleError(f"The cached {src.name} of {spec.id} did not match its sha256 checksum, so it was "
                                  "deleted; fetch the sample again to download it.")
            if client is None:
                client = client_factory()
            done = _download(client, spec, src, target, done, total, progress, cancelled)
    finally:
        if client is not None:
            client.close()
    return files


def fetch_sample(spec: SampleSpec, cache_dir: Path | str, *, progress: Progress | None = None,
                 cancelled: Callable[[], bool] | None = None, max_bytes: int = DEFAULT_MAX_BYTES,
                 client_factory: Callable[[], httpx.Client] = _default_client) -> PreparedSample:
    """Download (or reuse) the sources of ``spec`` and build its WAV and reference words.

    The WAV is written to ``cache_dir/prepared/<id>.wav``. Raises ``SampleError`` (``SampleCancelled`` on cancel) with
    a plain sentence.
    """
    files = fetch_sources(spec, cache_dir, progress=progress, cancelled=cancelled, max_bytes=max_bytes,
                          client_factory=client_factory)
    return prepare_sample(spec, files, Path(cache_dir) / "prepared" / f"{spec.id}.wav", progress=progress)


def prepare_sample(spec: SampleSpec, files: Mapping[str, Path], audio_path: Path, *,
                   progress: Progress | None = None) -> PreparedSample:
    """Build the WAV and reference words of ``spec`` from sources already on disk (no network, no checksum check)."""
    total = spec.download_bytes()
    if progress is not None:
        progress(total, total, f"Converting the audio of {spec.id}")
    duration = convert_audio(files[spec.recipe_params["audio"]], audio_path,
                             cut_seconds=spec.recipe_params.get("cut_seconds"))
    if progress is not None:
        progress(total, total, f"Reading the reference words of {spec.id}")
    words = build_reference(spec, files)
    return PreparedSample(audio_path=audio_path, words=words, duration_seconds=duration, spec=spec)
