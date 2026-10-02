"""Multi-scale face detection and ArcFace embedding on top of insightface (CPU).

insightface's ``FaceAnalysis.get`` rescales every image to the detector size.
A 300 px head crop is then upscaled until its face is several hundred pixels
wide, and SCRFD stops finding it. Real inputs here are exactly that kind of
tight crop, so this module places the image on a canvas at several scales and
merges the boxes, which finds faces from about 24 px to about 500 px.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import cv2
import numpy as np
import onnxruntime as ort
from insightface.app import FaceAnalysis
from insightface.app.common import Face

MODEL_ROOT = os.environ.get("FACE_MODEL_ROOT", "/media/windows/AI Models/insightface")
KNOWN_MODELS = ("buffalo_l", "antelopev2")
SCALES = (1.0, 0.5, 2.0, 0.3)
MAX_CANVAS = 1920
NMS_IOU = 0.4

_analyzers: dict[str, FaceAnalysis] = {}

# onnxruntime sizes its thread pool to every core by default. The host is shared
# with the LLM and Daisy, so cap it; override with FACE_ORT_THREADS.
_THREADS = int(os.environ.get("FACE_ORT_THREADS", "4"))
_orig_session_init = ort.InferenceSession.__init__


def _capped_session_init(self, path_or_bytes, sess_options=None, *args, **kwargs):
    if sess_options is None:
        sess_options = ort.SessionOptions()
        sess_options.intra_op_num_threads = _THREADS
        sess_options.inter_op_num_threads = 1
    _orig_session_init(self, path_or_bytes, sess_options, *args, **kwargs)


ort.InferenceSession.__init__ = _capped_session_init


def analyzer(model: str) -> FaceAnalysis:
    if model not in KNOWN_MODELS:
        raise ValueError(f"model must be one of {', '.join(KNOWN_MODELS)}")
    if model not in _analyzers:
        fa = FaceAnalysis(
            name=model,
            root=MODEL_ROOT,
            providers=["CPUExecutionProvider"],
            allowed_modules=["detection", "recognition", "genderage"],
        )
        fa.prepare(ctx_id=-1, det_size=(640, 640), det_thresh=0.3)
        _analyzers[model] = fa
    return _analyzers[model]


@dataclass
class Found:
    bbox: np.ndarray
    kps: np.ndarray
    score: float


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def detect(fa: FaceAnalysis, frame: np.ndarray, min_score: float) -> list[Found]:
    height, width = frame.shape[:2]
    det = fa.det_model
    candidates: list[Found] = []
    for scale in SCALES:
        if max(height, width) * scale > MAX_CANVAS:
            continue
        resized = frame if scale == 1.0 else cv2.resize(
            frame, None, fx=scale, fy=scale,
            interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA,
        )
        side = max(320, int(np.ceil(max(resized.shape[:2]) / 32.0) * 32))
        canvas = np.zeros((side, side, 3), np.uint8)
        canvas[: resized.shape[0], : resized.shape[1]] = resized
        bboxes, kpss = det.detect(canvas, input_size=(side, side), max_num=0, metric="default")
        for i in range(bboxes.shape[0]):
            if bboxes[i, 4] < min_score:
                continue
            candidates.append(Found(bboxes[i, :4] / scale, kpss[i] / scale, float(bboxes[i, 4])))
    candidates.sort(key=lambda c: -c.score)
    kept: list[Found] = []
    for cand in candidates:
        if all(_iou(cand.bbox, k.bbox) < NMS_IOU for k in kept):
            kept.append(cand)
    return sorted(kept, key=lambda c: c.bbox[0])


def analyze(model: str, frame: np.ndarray, min_score: float = 0.5) -> list[dict]:
    """Return one dict per face, left to right: bbox, kps, det_score, age, gender, embedding."""
    fa = analyzer(model)
    out = []
    for found in detect(fa, frame, min_score):
        face = Face(bbox=found.bbox, kps=found.kps, det_score=found.score)
        for name in ("recognition", "genderage"):
            fa.models[name].get(frame, face)
        out.append({
            "bbox": [float(v) for v in found.bbox],
            "kps": [[float(x), float(y)] for x, y in found.kps],
            "det_score": found.score,
            "age": int(face.age) if face.get("age") is not None else None,
            "gender": ("male" if face.gender == 1 else "female") if face.get("gender") is not None else None,
            "embedding": [float(v) for v in face.normed_embedding],
        })
    return out
