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

The module is pure apart from Pillow image handling. The face sidecar and the
ComfyUI call arrive through :class:`IdentityDeps`, so it is tested with fakes.
"""

from __future__ import annotations

import copy
import io
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from PIL import Image, ImageOps

from gateway import likeness
from gateway.likeness import FaceRecord, LikenessError, Person

MAX_ATTEMPTS = 8
MAX_PEOPLE = 5            # the workflow template has six image slots: photo + five heads
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
class PersonSpec:
    label: str | None = None
    image: int = 0                                   # index into the reference photos
    box: tuple[float, float, float, float] | None = None  # x1,y1,x2,y2 in that photo's pixels
    description: str | None = None
    infant: bool | None = None


@dataclass
class SceneRequest:
    photos: list[bytes]
    prompt: str
    people: list[PersonSpec] = field(default_factory=list)
    attempts: int = 3
    seed: int = 0
    resolution: int = 1536
    steps: int = 25
    cfg: float = 1.0
    only_listed_people: bool = True
    photo_as_canvas: bool = True
    stop_when_pass: bool = True
    thresholds: Mapping[str, float] | None = None


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
    head_png: bytes
    face_bbox: tuple[float, float, float, float]
    age: int | None
    gender: str | None


@dataclass
class SceneResult:
    image: bytes
    report: dict[str, Any]
    attempts: list[dict[str, Any]]
    people: list[dict[str, Any]]
    seconds: float


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
        parts.append(
            f"<image{slot}> is the identity reference for {rp.label} ({desc}): "
            "copy this exact face, hair colour and style, eye colour and any glasses."
        )
        slot += 1
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


async def _faces(deps: IdentityDeps, image: bytes) -> list[FaceRecord]:
    per_model = {model: await deps.analyze(image, model) for model in deps.models}
    return likeness.merge_models(per_model)


def _slim(face: FaceRecord) -> dict[str, Any]:
    return {"bbox": [round(v, 1) for v in face.bbox], "age": face.age, "det_score": round(face.det_score, 3)}


async def resolve_people(
    req: SceneRequest, deps: IdentityDeps, *, with_heads: bool = True
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
        faces = await faces_of(spec.image)
        chosen = pick_face([{"bbox": f.bbox, "face": f} for f in faces], spec.box)
        label = spec.label or f"person {number}"
        if chosen is None:
            raise IdentityError(
                f"no face was found for {label}" + (" inside the given box" if spec.box else ""),
                param="people",
                code="no_face",
            )
        face: FaceRecord = chosen["face"]
        if face.width < MIN_REF_FACE_PX:
            raise IdentityError(
                f"the face of {label} is only {face.width:.0f}px wide; use a photo with a larger face",
                param="people",
                code="face_too_small",
            )
        resolved.append(
            ResolvedPerson(
                label=label,
                spec=spec,
                person=Person(label=label, faces=(face,), infant=spec.infant),
                head_png=head_reference_png(req.photos[spec.image], face.bbox) if with_heads else b"",
                face_bbox=face.bbox,
                age=face.age,
                gender=face.gender,
            )
        )
    labels = [r.label for r in resolved]
    if len(set(labels)) != len(labels):
        raise IdentityError("people labels must be unique", param="people")
    return resolved


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
    people = await resolve_people(req, deps)
    prompt = build_prompt(
        req.prompt, people, photo_as_canvas=req.photo_as_canvas, only_listed_people=req.only_listed_people
    )

    uploads: dict[str, bytes] = {}
    slot = 0
    if req.photo_as_canvas:
        uploads[image_nodes[slot]] = req.photos[0]
        slot += 1
    for rp in people:
        uploads[image_nodes[slot]] = rp.head_png
        slot += 1

    best: tuple[tuple[float, float], bytes, dict[str, Any], int] | None = None
    attempts: list[dict[str, Any]] = []
    for index in range(req.attempts):
        seed = req.seed + index
        graph = prepare_workflow(
            template,
            image_nodes=image_nodes,
            used_images=slot,
            prompt_node=prompt_node,
            seed_node=seed_node,
            prompt=prompt,
            seed=seed,
            resolution=req.resolution,
            steps=req.steps,
            cfg=req.cfg,
        )
        image = await deps.run_workflow(graph, uploads)
        candidate = await _faces(deps, image)
        report = likeness.check_likeness(
            [rp.person for rp in people], candidate, models=deps.models, thresholds=req.thresholds,
            allow_extra_faces=not req.only_listed_people,
        )
        key = likeness.rank_key(report, penalise_extra_faces=req.only_listed_people)
        attempts.append({
            "attempt": index + 1,
            "seed": seed,
            "status": report["overall"]["status"],
            "min_score": report["overall"]["min_score"],
            "extra_faces": report["overall"]["extra_faces"],
            "scores": {p["label"]: (p["match"] or {}).get("score") for p in report["people"]},
        })
        if best is None or key > best[0]:
            best = (key, image, report, index)
        if req.stop_when_pass and report["overall"]["all_meet_threshold"]:
            break
    assert best is not None
    for number, item in enumerate(attempts):
        item["selected"] = number == best[3]
    return SceneResult(
        image=best[1],
        report=best[2],
        attempts=attempts,
        people=[
            {
                "label": rp.label,
                "photo": rp.spec.image,
                "face_bbox": [round(v, 1) for v in rp.face_bbox],
                "age_estimate": rp.age,
                "reference_head_px": list(Image.open(io.BytesIO(rp.head_png)).size),
            }
            for rp in people
        ],
        seconds=round(time.monotonic() - started, 1),
    )
