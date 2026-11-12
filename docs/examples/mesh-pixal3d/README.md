# Pixal3D image to 3D

Pixal3D (MIT licence) runs inside ComfyUI. The gateway bundles it as the second image-to-3D workflow,
`manifests/workflows/mesh.generate.pixal3d.yml`, beside the TRELLIS.2 one. A request names one with `model`; a request
that names none runs the first choice (the lowest `selection_rank` fact in `registry/workflow_policy.yml`; an owner changes
it by writing the record's `facts.selection_rank`). `docs/design/MESH_WORKFLOW_SELECTION.md` has the design.

Parts:

- `engines/pixal3d_comfy_service.py` is a small service with the same contract as the TRELLIS.2 service: one multipart
  `POST /generate` (`image`, `seed`, `resolution`) that returns raw GLB bytes. It copies the picture to ComfyUI,
  patches the shipped API prompt (`engines/pixal3d_prompt.json`), waits for the run and returns the textured GLB. It
  answers 503 while ComfyUI has queued or running work. Start it with
  `COMFYUI_URL=<comfyui address> uvicorn engines.pixal3d_comfy_service:app --port 8892`. `PIXAL3D_POLL_SECONDS` and
  `PIXAL3D_TIMEOUT_SECONDS` tune polling and the history-wait limit: `PIXAL3D_TIMEOUT_SECONDS` bounds how long the service
  waits for the prompt to finish after it is submitted. It is not a total deadline: the queue check, the upload, the submit and
  the download each have the 60 second HTTP limit of the client and do not count against it.
  `PIXAL3D_CLEANUP_TIMEOUT_SECONDS` bounds total timeout cleanup (default 5 seconds).
  Cleanup deletes only this prompt from the pending queue; a running prompt is left alone because the interrupt handler cannot cancel it atomically.
- The `pixal3d` service record is in `tests/fixtures/config_homes/deployment_config.yml`, beside `trellis`. Both declare
  the same exclusive pool, `gpu0`, so one request at a time holds the card.

The ComfyUI install needs the Pixal3D models and nodes that `pixal3d_prompt.json` names. `bg_removal` is accepted by the
service and ignored, since the prompt removes the background itself. `resolution` sets the upsample stage; 512 has not
been run against it.
