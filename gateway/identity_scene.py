"""Identity-preserving scenes: put real people from reference photos into a new scene.

Why this module exists. A reference-edit model keeps a person's face only when it
sees the whole photo plus a roomy, sharp head reference per person, each tagged
``<imageN>`` in the prompt. Tiny face crops and free-text references make the
model invent a similar-looking stranger. This module builds that input, runs the
composition several times, scores every result with :mod:`gateway.likeness`
against the real faces, and returns the best one with the scores.

Measured on the baby-and-grandmother photo (docs/IDENTITY_LIKENESS.md): the
tagged multi-reference edit alone reaches 0.6 similarity for the adult and about
0.5 for the infant. A head-swap LoRA pass after it lowered both scores, so this
pipeline has no swap pass.

Version 2 adds several reference photos per person, per-person head refinement,
prompt steering away from profile poses and progress callbacks for async jobs.

The module is pure apart from Pillow image handling. The face sidecar and the
ComfyUI call arrive through :class:`IdentityDeps`, so it is tested with fakes.
"""

from __future__ import annotations

import copy
import io
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from PIL import Image, ImageOps

from gateway import likeness
from gateway.likeness import FaceRecord, LikenessError, Person

MAX_ATTEMPTS = 8
MAX_PEOPLE = 5
MAX_IMAGE_SLOTS = 10      # Qwen-Image 2.1 takes up to 10 reference images; the template has 10 slots
MAX_REFS_PER_PERSON = 5
DEFAULT_REFS_PER_PERSON = 3
REF_MATCH_MIN = 0.25          # a face in an extra photo must look this much like the anchor face to be used
REF_MATCH_MIN_INFANT = 0.15   # ArcFace is weaker on infants, so accept a looser match there
HEAD_LONG_SIDE = 896      # reference heads are scaled to this long side (never smaller)
HEAD_BOX_SCALE = 2.6      # head crop side as a multiple of the detected face width
HEAD_RISE = 0.12          # shift the crop up by this share of the face height to keep the hair
MIN_REF_FACE_PX = 32      # a reference face narrower than this carries too little identity


class IdentityError(ValueError):
    """The request cannot be run (no face found, bad box, too many people)."""

    def __init__(self, message: str, *, param: str | None = None, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.param = param
        self.code = code


@dataclass
class RefSource:
    """One reference photo of a person: the photo index and, optionally, where the face is."""

    image: int = 0
    box: tuple[float, float, float, float] | None = None
    generate: bool = True   # False: use this photo for scoring only (sunglasses, profile, tiny face)


@dataclass
class PersonSpec:
    label: str | None = None
    image: int = 0                                   # index into the reference photos
    box: tuple[float, float, float, float] | None = None  # x1,y1,x2,y2 in that photo's pixels
    description: str | None = None
    infant: bool | None = None
    more: list[RefSource] = field(default_factory=list)  # further photos of the same person
    generate_first: bool = True

    def sources(self) -> list[RefSource]:
        return [RefSource(self.image, self.box, self.generate_first), *self.more]


ProgressFn = Callable[[str, float, dict[str, Any]], None]


@dataclass
class SceneRequest:
    photos: list[bytes]
    prompt: str
    people: list[PersonSpec] = field(default_factory=list)
    attempts: int = 3
    seed: int = 0
    resolution: int | None = None                    # None: chosen from the number of reference images
    steps: int = 25
    cfg: float = 1.0
    only_listed_people: bool = True
    photo_as_canvas: bool = True
    stop_when_pass: bool = True
    facing_each_other: bool = False   # require both heads turned toward each other (two people)
    pooling: str = "best"             # how several reference photos of one person are scored
    max_refs_per_person: int = DEFAULT_REFS_PER_PERSON  # reference heads sent to the generator per person
    refine: str = "auto"              # off | auto (people below the match threshold) | all
    refine_attempts: int = 2          # seeds tried per person in the refinement pass
    pose_policy: str = "likeness"     # likeness: steer away from profile; as_written: leave the prompt alone
    thresholds: Mapping[str, float] | None = None
    on_progress: ProgressFn | None = None


@dataclass
class IdentityDeps:
    analyze: Callable[[bytes, str], Awaitable[list[dict[str, Any]]]]   # (image, model) -> faces
    run_workflow: Callable[[dict[str, Any], dict[str, bytes]], Awaitable[bytes]]  # (graph, {LoadImage node: png}) -> png
    models: Sequence[str] = likeness.DEFAULT_MODELS


@dataclass
class ResolvedPerson:
    label: str
    spec: PersonSpec
    person: Person
    heads: list[bytes]                 # roomy reference heads, best first
    face_bbox: tuple[float, float, float, float]
    age: int | None
    gender: str | None
    sources_used: list[tuple[FaceRecord, int]] = field(default_factory=list)   # (face, photo) behind the heads
    primary_photo: int = 0                                                      # photo of the best reference face

    @property
    def head_png(self) -> bytes:
        return self.heads[0] if self.heads else b""


@dataclass
class SceneResult:
    image: bytes
    report: dict[str, Any]
    attempts: list[dict[str, Any]]
    people: list[dict[str, Any]]
    seconds: float
    refinements: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)
    prompt_used: str = ""


# ----------------------------------------------------------------- pure helpers


def head_crop_box(
    face_bbox: Sequence[float], image_size: tuple[int, int]
) -> tuple[int, int, int, int]:
    """A roomy square box around the head (hair and some shoulder), inside the image."""
    x1, y1, x2, y2 = face_bbox
    width, height = image_size
    side = max(x2 - x1, y2 - y1) * HEAD_BOX_SCALE
    side = min(side, float(min(width, height)))
    cx = (x1 + x2) / 2
    cy = (y1 + y2) / 2 - (y2 - y1) * HEAD_RISE
    left = min(max(cx - side / 2, 0.0), width - side)
    top = min(max(cy - side / 2, 0.0), height - side)
    return int(round(left)), int(round(top)), int(round(left + side)), int(round(top + side))


def head_reference_png(photo: bytes, face_bbox: Sequence[float]) -> bytes:
    """Crop a roomy head from the ORIGINAL photo and scale it up to a usable size."""
    with Image.open(io.BytesIO(photo)) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    crop = image.crop(head_crop_box(face_bbox, image.size))
    longest = max(crop.size)
    if longest < HEAD_LONG_SIDE:
        factor = HEAD_LONG_SIDE / longest
        crop = crop.resize((round(crop.width * factor), round(crop.height * factor)), Image.LANCZOS)
    out = io.BytesIO()
    crop.save(out, format="PNG")
    return out.getvalue()


def describe(age: int | None, gender: str | None) -> str:
    """A short visual description used to tell the people apart in the prompt."""
    if age is not None and age <= likeness.INFANT_MAX_AGE:
        return "a baby"
    noun = {"female": "woman", "male": "man"}.get(gender or "", "person")
    if age is not None and age <= 12:
        return "a " + {"female": "girl", "male": "boy"}.get(gender or "", "child")
    if age is not None and age >= 60:
        return f"an older {noun}"
    return f"a {noun}"


def reference_quality(face: FaceRecord) -> float:
    """How useful a face is as a generation reference: big, confidently detected, facing the camera."""
    size = min(face.width, 400.0) / 400.0
    yaw = face.yaw
    frontal = 1.0 if yaw is None else max(0.0, 1.0 - abs(yaw) / 0.6)
    return size * face.det_score * (0.4 + 0.6 * frontal)


def best_match_face(
    faces: Sequence[FaceRecord], anchors: Sequence[FaceRecord], minimum: float
) -> tuple[FaceRecord | None, float]:
    """The face in a photo that looks most like any anchor face, and its similarity."""
    best: FaceRecord | None = None
    best_sim = -1.0
    for face in faces:
        if face.width < MIN_REF_FACE_PX:
            continue
        for anchor in anchors:
            shared = [m for m in face.embeddings if m in anchor.embeddings]
            if not shared:
                continue
            sim = sum(likeness.cosine(face.embeddings[m], anchor.embeddings[m]) for m in shared) / len(shared)
            if sim > best_sim:
                best, best_sim = face, sim
    return (best, best_sim) if best is not None and best_sim >= minimum else (None, best_sim)


# Phrases that force a profile or an occluded face. Profile views and foreheads touching
# remove the features ArcFace needs; a three-quarter view keeps them.
_PROFILE_RULES: tuple[tuple[re.Pattern[str], str | None], ...] = (
    (re.compile(r"\b(?:strict |full |pure )?profile(?: view| shot| portrait)?\b", re.I), "three-quarter view"),
    (re.compile(r"\bside[- ](?:view|on|profile)\b", re.I), "three-quarter view"),
    (re.compile(r"\bin silhouette\b", re.I), "in three-quarter view"),
    (re.compile(r"\bfore?heads?\s+(?:nearly |almost |gently |softly )?(?:touching|together|pressed|to\s+forehead)\b", re.I), "heads close together"),
    (re.compile(r"\bnose[- ]to[- ]nose\b|\beskimo kiss\b", re.I), "heads close together"),
    (re.compile(r"\bcheek[- ]to[- ]cheek\b", re.I), "heads close together"),
)
_NEGATED = re.compile(r"\b(?:not|no|never|without|avoid|isn't|non)\W+(?:\w+\W+){0,2}$", re.I)
STEERING_SENTENCE = (
    "Keep each person's whole face clearly visible, near-frontal or in three-quarter view with both eyes visible, "
    "not in profile and not hidden behind the other person."
)


def steer_prompt(prompt: str, policy: str) -> tuple[str, list[dict[str, str]]]:
    """Move a pose request toward what keeps likeness. Returns (prompt, warnings).

    Under ``likeness`` a profile or forehead-touching phrase is rewritten and a warning says so;
    under ``as_written`` the prompt is untouched and the warning only states the risk.
    """
    warnings: list[dict[str, str]] = []
    out = prompt
    hit = False
    for pattern, replacement in _PROFILE_RULES:
        def rewrite(match: re.Match[str]) -> str:
            nonlocal hit
            if _NEGATED.search(match.string[max(0, match.start() - 24): match.start()]):
                return match.group(0)  # "not in profile" asks for the opposite: leave it alone
            hit = True
            return replacement if policy == "likeness" and replacement else match.group(0)

        out = pattern.sub(rewrite, out)
    if hit:
        if policy == "likeness":
            warnings.append({
                "code": "pose_rewritten",
                "param": "prompt",
                "message": (
                    "The prompt asked for a profile or touching foreheads. Profile views and overlapping faces destroy "
                    "likeness, so the pose was changed to a three-quarter view with the heads close together. "
                    "Send pose_policy=as_written to keep your wording, at the cost of likeness."
                ),
            })
        else:
            warnings.append({
                "code": "pose_risks_likeness",
                "param": "prompt",
                "message": "The prompt asks for a profile or touching foreheads; expect low likeness scores.",
            })
    if policy == "likeness":
        out = f"{out.strip()} {STEERING_SENTENCE}"
    return out, warnings


def auto_resolution(image_count: int) -> int:
    """Per-reference pixel budget: fewer pixels per image as the number of references grows."""
    if image_count <= 3:
        return 1536
    if image_count <= 5:
        return 1280
    return 1024


def build_prompt(
    user_prompt: str,
    people: Sequence[ResolvedPerson],
    *,
    photo_as_canvas: bool,
    only_listed_people: bool,
) -> str:
    """Tag every input ``<imageN>`` and say what each one is for."""
    parts: list[str] = []
    slot = 1
    if photo_as_canvas:
        parts.append(
            f"<image{slot}> is the original photo. Use it only for clothing, setting and light: "
            "do not copy its composition, its poses or any other person in it."
        )
        slot += 1
    for rp in people:
        desc = rp.spec.description or describe(rp.age, rp.gender)
        tags = [f"<image{slot + k}>" for k in range(len(rp.heads))]
        slot += len(rp.heads)
        if len(tags) == 1:
            parts.append(
                f"{tags[0]} is the identity reference for {rp.label} ({desc}): "
                "copy this exact face, hair colour and style, eye colour and any glasses."
            )
        else:
            joined = ", ".join(tags[:-1]) + f" and {tags[-1]}"
            parts.append(
                f"{joined} are {len(tags)} photos of the same person, {rp.label} ({desc}): "
                "combine them into one consistent face, with this exact hair colour and style, eye colour and any glasses."
            )
    parts.append(user_prompt.strip())
    if only_listed_people:
        names = ", ".join(rp.label for rp in people)
        parts.append(f"Create a new photograph that shows only these people and nobody else: {names}.")
    parts.append(
        "Every face must be the same person as its identity reference, "
        "photorealistic, sharp, natural skin."
    )
    return " ".join(parts)


def prepare_workflow(
    template: Mapping[str, Any],
    *,
    image_nodes: Sequence[str],
    used_images: int,
    prompt_node: str,
    seed_node: str,
    prompt: str,
    seed: int,
    resolution: int,
    steps: int,
    cfg: float,
) -> dict[str, Any]:
    """Copy the template, drop unused image slots, fill prompt, seed and sizes."""
    if used_images > len(image_nodes):
        raise IdentityError(
            f"the workflow has {len(image_nodes)} image slots; {used_images} were needed",
            param="people",
        )
    graph = copy.deepcopy(dict(template))
    graph.pop("_meta", None)
    for index, node_id in enumerate(image_nodes, start=1):
        if index <= used_images:
            continue
        graph.pop(node_id, None)
        for node in graph.values():
            if isinstance(node, dict):
                node.get("inputs", {}).pop(f"images.image_{index}", None)
    encoder = graph[prompt_node]["inputs"]
    encoder["prompt"] = prompt
    encoder["resolution"] = int(resolution)
    sampler = graph[seed_node]["inputs"]
    sampler["seed"] = int(seed)
    sampler["steps"] = int(steps)
    sampler["cfg"] = float(cfg)
    return graph


def _inside(point: tuple[float, float], box: Sequence[float]) -> bool:
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def pick_face(
    faces: Sequence[Mapping[str, Any]], box: Sequence[float] | None
) -> Mapping[str, Any] | None:
    """The face a person spec points at: inside ``box``, else the largest face."""
    if not faces:
        return None
    if box is None:
        return max(faces, key=lambda f: f["bbox"][2] - f["bbox"][0])
    inside = [
        f for f in faces
        if _inside(((f["bbox"][0] + f["bbox"][2]) / 2, (f["bbox"][1] + f["bbox"][3]) / 2), box)
    ]
    return max(inside, key=lambda f: f["bbox"][2] - f["bbox"][0]) if inside else None


# ---------------------------------------------------------------- orchestration


def _emit(req: SceneRequest, phase: str, fraction: float, **detail: Any) -> None:
    if req.on_progress is not None:
        try:
            req.on_progress(phase, max(0.0, min(1.0, fraction)), detail)
        except Exception:  # noqa: BLE001  progress must never break the run
            pass


async def _faces(deps: IdentityDeps, image: bytes) -> list[FaceRecord]:
    per_model = {model: await deps.analyze(image, model) for model in deps.models}
    return likeness.merge_models(per_model)


async def resolve_people(
    req: SceneRequest,
    deps: IdentityDeps,
    *,
    with_heads: bool = True,
    warnings: list[dict[str, str]] | None = None,
) -> list[ResolvedPerson]:
    if not req.photos:
        raise IdentityError("at least one reference photo is required", param="image")
    detected: dict[int, list[FaceRecord]] = {}

    async def faces_of(index: int) -> list[FaceRecord]:
        if index not in detected:
            if not 0 <= index < len(req.photos):
                raise IdentityError(f"people[].image {index} has no matching photo", param="people")
            detected[index] = await _faces(deps, req.photos[index])
        return detected[index]

    specs = list(req.people)
    if not specs and len(req.photos) == 1:  # auto: every usable face in the photo, left to right
        faces = [f for f in await faces_of(0) if f.width >= MIN_REF_FACE_PX]
        if not faces:
            raise IdentityError("no face was found in the reference photo", param="image", code="no_face")
        specs = [PersonSpec(label=f"person {i + 1}", image=0, box=tuple(f.bbox)) for i, f in enumerate(faces)]
    elif not specs:  # auto with several photos: the largest face of each photo is one person
        specs = [PersonSpec(label=f"person {i + 1}", image=i) for i in range(len(req.photos))]
    if len(specs) > MAX_PEOPLE:
        raise IdentityError(f"at most {MAX_PEOPLE} people are supported", param="people")

    resolved: list[ResolvedPerson] = []
    for number, spec in enumerate(specs, start=1):
        label = spec.label or f"person {number}"
        found: list[tuple[FaceRecord, int]] = []   # (face, photo index)
        scoring_only: set[int] = set()              # photo indices used only for scoring
        for position, source in enumerate(spec.sources()):
            faces = await faces_of(source.image)
            if position == 0 or source.box is not None:
                # The anchor photo, or a photo where the caller says where the face is.
                chosen_face = (pick_face([{"bbox": f.bbox, "face": f} for f in faces], source.box) or {}).get("face")
                if chosen_face is None:
                    raise IdentityError(
                        f"no face was found for {label} in photo {source.image}"
                        + (" inside the given box" if source.box else ""),
                        param="people",
                        code="no_face",
                    )
            else:
                # An extra photo without a box may hold several people: take the face that looks like this person.
                minimum = REF_MATCH_MIN_INFANT if likeness.Person(label, tuple(f for f, _ in found), spec.infant).is_infant() else REF_MATCH_MIN
                chosen_face, similarity = best_match_face(faces, [f for f, _ in found], minimum)
                if chosen_face is None:
                    if warnings is not None:
                        warnings.append({
                            "code": "reference_skipped",
                            "param": label,
                            "message": (
                                f"photo {source.image} was not used for {label}: no face in it looks like them "
                                f"(best match {max(similarity, 0):.2f}). Give a box to force it."
                            ),
                        })
                    continue
            face: FaceRecord = chosen_face
            if face.width < MIN_REF_FACE_PX:
                if position == 0:
                    raise IdentityError(
                        f"the face of {label} in photo {source.image} is only {face.width:.0f}px wide; "
                        "use a photo with a larger face",
                        param="people",
                        code="face_too_small",
                    )
                continue
            found.append((face, source.image))
            if not source.generate:
                scoring_only.add(len(found) - 1)
        # Best reference first: a larger, sharper face gives the generator more to copy.
        eligible = [fp for k, fp in enumerate(found) if k not in scoring_only] or list(found)
        ranked = sorted(eligible, key=lambda fp: reference_quality(fp[0]), reverse=True)
        keep = ranked[: max(1, min(req.max_refs_per_person, MAX_REFS_PER_PERSON))]
        primary = ranked[0][0]
        resolved.append(
            ResolvedPerson(
                label=label,
                spec=spec,
                person=Person(label=label, faces=tuple(f for f, _ in found), infant=spec.infant),
                heads=[head_reference_png(req.photos[i], f.bbox) for f, i in keep] if with_heads else [],
                face_bbox=primary.bbox,
                age=primary.age,
                gender=primary.gender,
                sources_used=keep,
                primary_photo=ranked[0][1],
            )
        )
    labels = [r.label for r in resolved]
    if len(set(labels)) != len(labels):
        raise IdentityError("people labels must be unique", param="people")
    return resolved


# -------------------------------------------------------------------- refinement

REFINE_MIN_GAIN = 0.03        # a refinement must raise the person's score by this much
REFINE_MAX_OTHER_DROP = 0.03  # and must not lower anybody else's by more than this
REFINE_PROMPT = (
    "Edit <image1>: the {where} person in <image1> ({who}) must become the person shown in {refs}. "
    "Replace that person's face and head with the face and head from {refs}: the same face shape, hair colour "
    "and style, eye colour and any glasses. Keep every other person, the poses, head angles, expressions, "
    "clothing, lighting and background of <image1> unchanged."
)


def where_in_frame(face_bbox: Sequence[float], image_width: int) -> str:
    """Where a face sits horizontally, in words the edit model can follow."""
    centre = (face_bbox[0] + face_bbox[2]) / 2 / max(1, image_width)
    return "left" if centre < 0.4 else "right" if centre > 0.6 else "middle"


def _score_of(report: Mapping[str, Any], label: str) -> float:
    for entry in report["people"]:
        if entry["label"] == label:
            return entry["match"]["score"] if entry.get("match") else -1.0
    return -1.0


def refinement_improves(before: Mapping[str, Any], after: Mapping[str, Any], label: str) -> bool:
    """Keep a pass only if the person gained and nobody else lost (and no new face appeared)."""
    if _score_of(after, label) < _score_of(before, label) + REFINE_MIN_GAIN:
        return False
    for entry in before["people"]:
        if entry["label"] != label and _score_of(after, entry["label"]) < _score_of(before, entry["label"]) - REFINE_MAX_OTHER_DROP:
            return False
    return after["overall"]["extra_faces"] <= before["overall"]["extra_faces"]


async def refine_people(
    req: SceneRequest,
    deps: IdentityDeps,
    people: Sequence[ResolvedPerson],
    image: bytes,
    report: dict[str, Any],
    *,
    template: Mapping[str, Any],
    image_nodes: Sequence[str],
    prompt_node: str,
    seed_node: str,
    resolution: int,
    progress_range: tuple[float, float] = (0.6, 0.95),
) -> tuple[bytes, dict[str, Any], list[dict[str, Any]]]:
    """Refine each person in its own pass: edit the whole picture with only that person's references.

    A crop-and-paste of the head was tried first and rejected: the edit model re-poses and re-sizes the
    head, so the pasted face does not fit the old one and the picture shows two heads. Editing the whole
    picture keeps the layout; the guard below catches any pass that changes the other person.

    People are handled worst score first. A pass is kept only when it helps that person and hurts nobody.
    """
    log: list[dict[str, Any]] = []
    if req.refine == "off" or not people:
        return image, report, log
    by_label = {rp.label: rp for rp in people}
    order = sorted(by_label, key=lambda label: _score_of(report, label))
    todo = [
        label for label in order
        if req.refine == "all" or not next(e for e in report["people"] if e["label"] == label).get("meets_threshold")
    ]
    total_passes = max(1, len(todo) * req.refine_attempts)
    done = 0
    for label in todo:
        rp = by_label[label]
        for attempt in range(req.refine_attempts):
            entry = next(e for e in report["people"] if e["label"] == label)
            if not entry.get("match"):
                break
            if req.refine == "auto" and entry.get("meets_threshold"):
                break
            _emit(req, "refine", progress_range[0] + (progress_range[1] - progress_range[0]) * done / total_passes,
                  person=label, attempt=attempt + 1)
            with Image.open(io.BytesIO(image)) as current:
                width = current.width
            uploads = {image_nodes[0]: image}
            for k, head in enumerate(rp.heads, start=1):
                uploads[image_nodes[k]] = head
            tags = [f"<image{k + 1}>" for k in range(1, len(uploads))]
            refs = tags[0] if len(tags) == 1 else ", ".join(tags[:-1]) + f" and {tags[-1]} (photos of the same person)"
            who = rp.spec.description or describe(rp.age, rp.gender)
            graph = prepare_workflow(
                template, image_nodes=image_nodes, used_images=len(uploads),
                prompt_node=prompt_node, seed_node=seed_node,
                prompt=REFINE_PROMPT.format(where=where_in_frame(entry["match"]["bbox"], width), who=who, refs=refs),
                seed=req.seed + 1000 + 17 * attempt + len(log), resolution=resolution,
                steps=req.steps, cfg=req.cfg,
            )
            candidate = await deps.run_workflow(graph, uploads)
            cand_report = likeness.check_likeness(
                [p.person for p in people], await _faces(deps, candidate), models=deps.models,
                thresholds=req.thresholds, allow_extra_faces=not req.only_listed_people, pooling=req.pooling,
            )
            accepted = refinement_improves(report, cand_report, label)
            log.append({
                "person": label,
                "pass": attempt + 1,
                "score_before": round(_score_of(report, label), 4),
                "score_after": round(_score_of(cand_report, label), 4),
                "others_before": {e["label"]: round(_score_of(report, e["label"]), 4) for e in report["people"] if e["label"] != label},
                "others_after": {e["label"]: round(_score_of(cand_report, e["label"]), 4) for e in cand_report["people"] if e["label"] != label},
                "accepted": accepted,
            })
            done += 1
            if accepted:
                image, report = candidate, cand_report
    return image, report, log


async def run_identity_scene(
    req: SceneRequest,
    deps: IdentityDeps,
    *,
    template: Mapping[str, Any],
    image_nodes: Sequence[str],
    prompt_node: str,
    seed_node: str,
) -> SceneResult:
    started = time.monotonic()
    if not req.prompt.strip():
        raise IdentityError("prompt is required", param="prompt")
    if not 1 <= req.attempts <= MAX_ATTEMPTS:
        raise IdentityError(f"attempts must be between 1 and {MAX_ATTEMPTS}", param="attempts")
    if req.pooling not in likeness.POOLINGS:
        raise IdentityError(f"pooling must be one of {', '.join(likeness.POOLINGS)}", param="pooling")
    if req.refine not in ("off", "auto", "all"):
        raise IdentityError("refine must be off, auto or all", param="refine")
    if req.pose_policy not in ("likeness", "as_written"):
        raise IdentityError("pose_policy must be likeness or as_written", param="pose_policy")
    _emit(req, "resolve", 0.0)
    warnings: list[dict[str, str]] = []
    people = await resolve_people(req, deps, warnings=warnings)
    steered, pose_warnings = steer_prompt(req.prompt, req.pose_policy)
    warnings.extend(pose_warnings)
    prompt = build_prompt(
        steered, people, photo_as_canvas=req.photo_as_canvas, only_listed_people=req.only_listed_people
    )

    uploads: dict[str, bytes] = {}
    slot = 0
    if req.photo_as_canvas:
        uploads[image_nodes[slot]] = req.photos[0]
        slot += 1
    for rp in people:
        for head in rp.heads:
            if slot >= min(len(image_nodes), MAX_IMAGE_SLOTS):
                raise IdentityError(
                    f"too many reference images ({slot + 1}+); lower max_refs_per_person or the number of people",
                    param="people",
                )
            uploads[image_nodes[slot]] = head
            slot += 1
    resolution = req.resolution if req.resolution is not None else auto_resolution(slot)
    _emit(req, "resolve", 0.05, people=len(people), images=slot)

    best: tuple[tuple[float, ...], bytes, dict[str, Any], int] | None = None
    attempts: list[dict[str, Any]] = []
    compose_end = 0.6 if req.refine != "off" else 0.95
    for index in range(req.attempts):
        _emit(req, "compose", 0.05 + (compose_end - 0.05) * index / req.attempts, attempt=index + 1)
        seed = req.seed + index
        graph = prepare_workflow(
            template,
            image_nodes=image_nodes,
            used_images=slot,
            prompt_node=prompt_node,
            seed_node=seed_node,
            prompt=prompt,
            seed=seed,
            resolution=resolution,
            steps=req.steps,
            cfg=req.cfg,
        )
        image = await deps.run_workflow(graph, uploads)
        candidate = await _faces(deps, image)
        report = likeness.check_likeness(
            [rp.person for rp in people], candidate, models=deps.models, thresholds=req.thresholds,
            allow_extra_faces=not req.only_listed_people, pooling=req.pooling,
        )
        key = likeness.rank_key(
            report, penalise_extra_faces=req.only_listed_people, want_facing=req.facing_each_other
        )
        attempts.append({
            "attempt": index + 1,
            "seed": seed,
            "status": report["overall"]["status"],
            "min_score": report["overall"]["min_score"],
            "extra_faces": report["overall"]["extra_faces"],
            "facing_each_other": report["overall"]["facing_each_other"],
            "scores": {p["label"]: (p["match"] or {}).get("score") for p in report["people"]},
        })
        if best is None or key > best[0]:
            best = (key, image, report, index)
        done = report["overall"]["all_meet_threshold"] and (
            not req.facing_each_other or report["overall"]["facing_each_other"]
        )
        if req.stop_when_pass and done:
            break
    assert best is not None
    for number, item in enumerate(attempts):
        item["selected"] = number == best[3]
    final_image, final_report = best[1], best[2]

    final_image, final_report, refinements = await refine_people(
        req, deps, people, final_image, final_report,
        template=template, image_nodes=image_nodes, prompt_node=prompt_node, seed_node=seed_node,
        resolution=resolution, progress_range=(compose_end, 0.97),
    )
    for entry in final_report["people"]:
        pose = (entry.get("match") or {}).get("yaw")
        if pose is not None and abs(pose) > likeness.PROFILE_YAW:
            warnings.append({
                "code": "profile_result",
                "param": entry["label"],
                "message": f"{entry['label']} is in near-profile in the result; likeness is less reliable there.",
            })
    _emit(req, "done", 1.0)
    return SceneResult(
        image=final_image,
        report=final_report,
        attempts=attempts,
        people=[
            {
                "label": rp.label,
                "photo": rp.primary_photo,
                "face_bbox": [round(v, 1) for v in rp.face_bbox],
                "age_estimate": rp.age,
                "reference_photos": len(rp.person.faces),
                "reference_photo_indices": [i for _, i in rp.sources_used],
                "reference_heads_sent": len(rp.heads),
                "reference_head_px": [list(Image.open(io.BytesIO(h)).size) for h in rp.heads],
            }
            for rp in people
        ],
        seconds=round(time.monotonic() - started, 1),
        refinements=refinements,
        warnings=warnings,
        prompt_used=prompt,
    )
