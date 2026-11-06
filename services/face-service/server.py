"""CPU face detection + identity embeddings behind a tiny HTTP contract.

The gateway calls this sidecar for the likeness check and the identity
workflow. It runs insightface ONNX models on the CPU so GPU0 stays free.

POST /v1/faces/analyze   multipart: image (file), model (buffalo_l|antelopev2),
                         min_det_score (float, default 0.5)
  -> {"width", "height", "model", "faces": [{bbox, kps, det_score, age, gender,
      embedding}]}   embedding is the L2-normalised ArcFace vector (512 floats).
GET  /health

The service exits cleanly after --idle-exit-s idle seconds (status 0). The
gateway lifecycle driver starts it again on demand, as for the TTS sidecars.
"""
from __future__ import annotations

import argparse
import os
import threading
import time

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from facecore import KNOWN_MODELS, analyze as analyze_faces

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8021)
parser.add_argument("--idle-exit-s", type=int, default=900, help="exit after this many idle seconds (0 = never)")
args, _ = parser.parse_known_args()

app = FastAPI(title="face-service")
_lock = threading.Lock()
_last_used = time.monotonic()


def _idle_watchdog() -> None:
    while True:
        time.sleep(30)
        if args.idle_exit_s and time.monotonic() - _last_used > args.idle_exit_s:
            os._exit(0)  # status 0: systemd Restart=on-failure leaves the unit stopped


threading.Thread(target=_idle_watchdog, daemon=True).start()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "known_models": list(KNOWN_MODELS)}


@app.post("/v1/faces/analyze")
async def analyze(
    image: UploadFile = File(...),
    model: str = Form("buffalo_l"),
    min_det_score: float = Form(0.5),
) -> dict:
    global _last_used
    _last_used = time.monotonic()
    data = await image.read()
    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(400, "image could not be decoded")
    height, width = frame.shape[:2]
    if model not in KNOWN_MODELS:
        raise HTTPException(400, f"model must be one of {', '.join(KNOWN_MODELS)}")
    def _run() -> list[dict]:
        with _lock:
            return analyze_faces(model, frame, min_det_score)

    faces = await run_in_threadpool(_run)
    _last_used = time.monotonic()
    return {"width": width, "height": height, "model": model, "faces": faces}


if __name__ == "__main__":
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
