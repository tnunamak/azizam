# Pixal3D image to 3D, as an owner override

Pixal3D (MIT licence) runs inside ComfyUI. This directory shows how to serve the gateway's image-to-3D route with it
instead of the bundled TRELLIS.2 service. Nothing here changes a bundled manifest.

Parts:

- `engines/pixal3d_comfy_service.py` is a small service with the same contract as the TRELLIS.2 service: one multipart
  `POST /generate` (`image`, `seed`, `resolution`) that returns raw GLB bytes. It copies the picture to ComfyUI,
  patches the shipped API prompt (`engines/pixal3d_prompt.json`), waits for the run and returns the textured GLB. It
  answers 503 while ComfyUI has queued or running work. Start it with
  `COMFYUI_URL=<comfyui address> uvicorn engines.pixal3d_comfy_service:app --port 8892`. `PIXAL3D_POLL_SECONDS` and
  `PIXAL3D_TIMEOUT_SECONDS` tune polling.
- `service-record.example.yml` is the `pixal3d` service record for the deployment config.
- `mesh.generate.pixal3d.example.yml` is the workflow document. Save it as an owner override of the bundled mesh record
  (it keeps that record's id, because the 3D routes serve exactly one workflow id) so the route calls `pixal3d`.

The ComfyUI install needs the Pixal3D models and nodes that `pixal3d_prompt.json` names. `bg_removal` is accepted by the
service and ignored, since the prompt removes the background itself. `resolution` sets the upsample stage; 512 has not
been run against it. Running two mesh workflows side by side needs a gateway change:
see `docs/design/MESH_WORKFLOW_SELECTION.md`.
