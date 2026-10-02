"""Objective likeness check: do the faces in an image match reference faces?

Pure module. A face sidecar (``services/face-service``) detects faces and
returns ArcFace embeddings; this module turns those into per-person scores.

Scoring rules, all verified against real data in ``docs/IDENTITY_LIKENESS.md``:

* The score is the cosine similarity of L2-normalised embeddings, averaged over
  the recognisers that were run (``buffalo_l`` and ``antelopev2`` by default).
* Each reference person is matched to exactly one candidate face by the
  assignment that maximises the summed score, so two people can never claim the
  same output face.
* ``margin`` is the score minus the best score the same candidate face reaches
  against any *other* reference person. A face that looks like everybody has
  no identity, whatever its absolute score.
* Infant faces are scored but flagged ``reliability: "low"``: ArcFace-class
  recognisers are weak on infants, so the score only ranks candidates.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

DEFAULT_MODELS: tuple[str, ...] = ("buffalo_l", "antelopev2")

# Verdict bands on the mean cosine similarity. Calibrated on LFW pairs and on
# real family photos (see docs/IDENTITY_LIKENESS.md); override through
# ``thresholds`` when a client has its own data.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "match": 0.45,       # at or above: the same person
    "uncertain": 0.30,   # between uncertain and match: needs a human look
    "min_margin": 0.10,  # a match must beat every other reference person by this much
}

INFANT_MAX_AGE = 3        # estimated age at or below this flags low reliability
SMALL_FACE_PX = 48        # a face narrower than this carries little identity signal
LOW_DET_SCORE = 0.6       # a shaky detection is not a trustworthy crop

MAX_PEOPLE = 8            # assignment is brute-forced; keep the search space small


class LikenessError(ValueError):
    """The request cannot be scored (no faces, bad box, too many people)."""


@dataclass(frozen=True)
class FaceRecord:
    bbox: tuple[float, float, float, float]
    det_score: float
    age: int | None
    embeddings: Mapping[str, tuple[float, ...]]
    gender: str | None = None
    kps: tuple[tuple[float, float], ...] = ()

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def yaw(self) -> float | None:
        """Head turn from the five landmarks: nose offset over eye distance.

        0 is frontal, positive is turned toward the right edge of the image,
        about +-0.5 is a hard profile. None when landmarks are missing.
        """
        if len(self.kps) < 3:
            return None
        (lx, _), (rx, _), (nx, _) = self.kps[0], self.kps[1], self.kps[2]
        eye_dist = abs(rx - lx)
        return (nx - (lx + rx) / 2) / eye_dist if eye_dist > 1e-6 else None


@dataclass(frozen=True)
class Person:
    label: str
    faces: tuple[FaceRecord, ...]  # one or more reference faces of this person
    infant: bool | None = None     # caller override of the age-based infant flag

    def is_infant(self) -> bool:
        if self.infant is not None:
            return self.infant
        ages = [f.age for f in self.faces if f.age is not None]
        return bool(ages) and min(ages) <= INFANT_MAX_AGE


def face_from_service(item: Mapping[str, Any], model: str) -> FaceRecord:
    bbox = item["bbox"]
    return FaceRecord(
        bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
        det_score=float(item.get("det_score", 0.0)),
        age=item.get("age"),
        embeddings={model: tuple(float(v) for v in item["embedding"])},
        gender=item.get("gender"),
        kps=_kps(item),
    )


def merge_models(per_model: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[FaceRecord]:
    """Join the per-model detections of one image into one record per face.

    The detector is the same SCRFD weights in both packs, so boxes line up; a
    face is joined to the other model's box with the highest overlap.
    """
    models = list(per_model)
    if not models:
        return []
    base = models[0]
    out: list[FaceRecord] = []
    for item in per_model[base]:
        embeddings = {base: tuple(float(v) for v in item["embedding"])}
        for other in models[1:]:
            best, best_iou = None, 0.3
            for cand in per_model[other]:
                overlap = _iou(item["bbox"], cand["bbox"])
                if overlap > best_iou:
                    best, best_iou = cand, overlap
            if best is not None:
                embeddings[other] = tuple(float(v) for v in best["embedding"])
        bbox = item["bbox"]
        out.append(FaceRecord(
            bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
            det_score=float(item.get("det_score", 0.0)),
            age=item.get("age"),
            embeddings=embeddings,
            gender=item.get("gender"),
            kps=_kps(item),
        ))
    return out


def _kps(item: Mapping[str, Any]) -> tuple[tuple[float, float], ...]:
    return tuple((float(x), float(y)) for x, y in (item.get("kps") or ()))


def _iou(a: Sequence[float], b: Sequence[float]) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


POOLINGS = ("best", "mean")
PROFILE_YAW = 0.45   # |yaw| above this is a near-profile head: ArcFace likeness drops sharply


def face_scores(
    person: Person, face: FaceRecord, models: Sequence[str], pooling: str = "best"
) -> dict[str, float]:
    """Cosine to the person's reference faces, per model, pooled by ``pooling``.

    ``best`` takes the closest reference photo; ``mean`` averages over all of them.
    With one reference photo the two are the same.
    """
    scores: dict[str, float] = {}
    for model in models:
        values = [
            cosine(ref.embeddings[model], face.embeddings[model])
            for ref in person.faces
            if model in ref.embeddings and model in face.embeddings
        ]
        if values:
            scores[model] = max(values) if pooling == "best" else sum(values) / len(values)
    return scores


def per_reference_scores(person: Person, face: FaceRecord, models: Sequence[str]) -> list[float]:
    """Mean-over-models cosine to each reference photo, in the order they were given."""
    out: list[float] = []
    for ref in person.faces:
        values = [
            cosine(ref.embeddings[m], face.embeddings[m])
            for m in models
            if m in ref.embeddings and m in face.embeddings
        ]
        out.append(round(sum(values) / len(values), 4) if values else -1.0)
    return out


def _mean(scores: Mapping[str, float]) -> float:
    return sum(scores.values()) / len(scores) if scores else -1.0


def _assign(matrix: list[list[float]]) -> list[int | None]:
    """Row i -> column, maximising the total. Rows beyond the columns get None."""
    rows, cols = len(matrix), len(matrix[0]) if matrix else 0
    if not rows or not cols:
        return [None] * rows
    best_total, best = -math.inf, [None] * rows
    if rows <= cols:
        for chosen in itertools.permutations(range(cols), rows):
            total = sum(matrix[r][c] for r, c in enumerate(chosen))
            if total > best_total:
                best_total, best = total, list(chosen)
    else:
        for chosen_rows in itertools.permutations(range(rows), cols):
            total = sum(matrix[r][c] for c, r in enumerate(chosen_rows))
            if total > best_total:
                best_total = total
                best = [None] * rows
                for c, r in enumerate(chosen_rows):
                    best[r] = c
    return best


def check_likeness(
    people: Sequence[Person],
    candidate_faces: Sequence[FaceRecord],
    *,
    models: Sequence[str] = DEFAULT_MODELS,
    thresholds: Mapping[str, float] | None = None,
    allow_extra_faces: bool = True,
    pooling: str = "best",
) -> dict[str, Any]:
    """Score ``candidate_faces`` against the reference ``people``.

    A candidate face that no reference person claimed is an *extra face*. It is
    always listed; with ``allow_extra_faces=False`` it also stops the overall
    result from meeting the threshold (the caller asked for only these people).
    """
    if not people:
        raise LikenessError("at least one reference person is required")
    if len(people) > MAX_PEOPLE:
        raise LikenessError(f"at most {MAX_PEOPLE} reference people are supported")
    if pooling not in POOLINGS:
        raise LikenessError(f"pooling must be one of {', '.join(POOLINGS)}")
    limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    models = list(models)

    matrix = [
        [_mean(face_scores(person, face, models, pooling)) for face in candidate_faces]
        for person in people
    ]
    assignment = _assign(matrix)

    results: list[dict[str, Any]] = []
    for index, person in enumerate(people):
        column = assignment[index]
        notes: list[str] = []
        entry: dict[str, Any] = {
            "label": person.label,
            "reference_faces": len(person.faces),
            "infant": person.is_infant(),
            "reliability": "low" if person.is_infant() else "normal",
        }
        if person.is_infant():
            notes.append(
                "infant face: ArcFace-class recognisers are weak on infants; "
                "use the score to rank candidates and check by eye"
            )
        if column is None:
            entry.update(match=None, verdict="missing", meets_threshold=False)
            notes.append("no candidate face left for this person")
            entry["notes"] = notes
            results.append(entry)
            continue
        face = candidate_faces[column]
        scores = face_scores(person, face, models, pooling)
        score = _mean(scores)
        others = [matrix[o][column] for o in range(len(people)) if o != index]
        margin = score - max(others) if others else None
        if face.width < SMALL_FACE_PX:
            notes.append(f"candidate face is only {face.width:.0f}px wide; the score is unreliable")
        if face.yaw is not None and abs(face.yaw) > PROFILE_YAW:
            notes.append("near-profile head: likeness is much less reliable in profile; ask for a three-quarter view")
        if face.det_score < LOW_DET_SCORE:
            notes.append(f"weak face detection (score {face.det_score:.2f})")
        verdict = _verdict(score, margin, limits)
        entry["meets_threshold"] = verdict == "match"
        if verdict == "match" and entry["reliability"] == "low":
            verdict = "uncertain"
            notes.append("a match on an infant face is downgraded to uncertain")
        entry.update(
            match={
                "candidate_face": column,
                "bbox": list(face.bbox),
                "scores": {k: round(v, 4) for k, v in scores.items()},
                "score": round(score, 4),
                "margin": None if margin is None else round(margin, 4),
                "yaw": None if face.yaw is None else round(face.yaw, 3),
                "per_reference": per_reference_scores(person, face, models),
            },
            verdict=verdict,
            notes=notes,
        )
        results.append(entry)

    claimed = {a for a in assignment if a is not None}
    extras = [
        {"bbox": list(f.bbox), "det_score": round(f.det_score, 4), "age": f.age}
        for i, f in enumerate(candidate_faces)
        if i not in claimed and f.width >= SMALL_FACE_PX and f.det_score >= LOW_DET_SCORE
    ]
    overall = _overall(results)
    overall["extra_faces"] = len(extras)
    overall["facing_each_other"] = _facing_each_other(results)
    if extras and not allow_extra_faces:
        overall["all_meet_threshold"] = False
        if overall["status"] == "pass":
            overall["status"] = "review"
        overall["needs_human_check"] = True
    return {
        "object": "likeness.check",
        "models": models,
        "thresholds": limits,
        "pooling": pooling,
        "candidate_faces": [
            {"bbox": list(f.bbox), "det_score": round(f.det_score, 4), "age": f.age}
            for f in candidate_faces
        ],
        "people": results,
        "extra_faces": extras,
        "overall": overall,
    }


FACING_MIN_YAW = 0.12


def _facing_each_other(results: Sequence[Mapping[str, Any]]) -> bool | None:
    """For exactly two matched people: is the left one turned right and the right one turned left?

    Head orientation, not gaze: a three-quarter turn toward each other counts.
    """
    matched = [r["match"] for r in results if r.get("match") and r["match"].get("yaw") is not None]
    if len(results) != 2 or len(matched) != 2:
        return None
    left, right = sorted(matched, key=lambda m: m["bbox"][0])
    return left["yaw"] >= FACING_MIN_YAW and right["yaw"] <= -FACING_MIN_YAW


def _verdict(score: float, margin: float | None, limits: Mapping[str, float]) -> str:
    if score >= limits["match"] and (margin is None or margin >= limits["min_margin"]):
        return "match"
    if score >= limits["uncertain"]:
        return "uncertain"
    return "mismatch"


def _overall(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    verdicts = [r["verdict"] for r in results]
    if all(v == "match" for v in verdicts):
        status = "pass"
    elif any(v in ("mismatch", "missing") for v in verdicts):
        status = "fail"
    else:
        status = "review"
    scores = [r["match"]["score"] for r in results if r.get("match")]
    return {
        "status": status,
        "all_meet_threshold": all(r.get("meets_threshold") for r in results),
        "min_score": min(scores) if scores else None,
        "mean_score": round(sum(scores) / len(scores), 4) if scores else None,
        "needs_human_check": any(r.get("reliability") == "low" for r in results) or status != "pass",
    }


def rank_key(
    report: Mapping[str, Any], *, penalise_extra_faces: bool = False, want_facing: bool = False
) -> tuple[float, float, float, float]:
    """Sort key for choosing the best of several candidates (higher is better).

    Fewer extra faces first when the caller wants only the listed people, then facing each
    other when asked, then the
    worst person (one wrong face outweighs a great second face), then the mean.
    """
    people = report.get("people") or []
    scores = [p["match"]["score"] if p.get("match") else -1.0 for p in people]
    extras = -float(len(report.get("extra_faces") or [])) if penalise_extra_faces else 0.0
    facing = 1.0 if (want_facing and (report.get("overall") or {}).get("facing_each_other")) else 0.0
    if not scores:
        return (extras, facing, -1.0, -1.0)
    return (extras, facing, min(scores), sum(scores) / len(scores))
