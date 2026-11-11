# ComfyUI additions for the 3D stages

The texture-refine stage runs the UltraTex nodes inside ComfyUI. This directory records what a rebuild of the ComfyUI
venv needs to get that stage back.

- `requirements.txt`: the four packages the nodes add. Installing them changed no existing package.
- `ultratex-egl.patch`: a one-line change to the node pack (visualbruno/ComfyUI-UltraTex at commit
  `1f2392308e7d08374cb1c3844afe7e3ee2fadbe0`). Without it, moderngl picks the GLX backend when a display variable is
  set, and the render step hangs inside a headless ComfyUI service. The patch asks for the EGL backend, which needs no
  display. Apply it again after every update of the pack.
- `install.sh <comfyui-dir> <comfyui-venv-python>`: installs the packages, checks out the pack at that commit and
  applies the patch. Run it while the ComfyUI queue is empty, then restart ComfyUI.

Before any ComfyUI restart, check that its queue is empty (`GET /queue`). A restart kills the running prompt and clears
the in-memory history.

The FLUX.2 base model that UltraTex uses has a non-commercial licence.
