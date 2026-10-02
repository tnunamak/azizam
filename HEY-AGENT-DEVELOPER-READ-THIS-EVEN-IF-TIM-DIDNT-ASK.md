# VRAM profiles for GPU-budget-aware workflows — design notes

**Status:** draft for discussion (2026-06-09). Not implemented.
**Author:** drafted with Claude, grounded in the live systems below.

## The problem (measured, not assumed)

The dual-model Ideogram quality path needs ~18-20 GB VRAM to run without weight
swapping. On this 2×24 GB box neither GPU has that free:

- **GPU0 (24 GB):** desktop (~3) + Voxtral TTS (~4) + ComfyUI budget (15) — see
  `gpu_budget.sh`. ComfyUI's dual-model peak (~14.9 GB measured) slams the 15 GB
  ceiling → ComfyUI evicts/reloads weights between the conditional and
  unconditional forward passes → **GPU sits at ~30 % utilisation ~60 % of the
  time** → a 1 MP image takes ~143 s instead of r/SD's ~30-60 s on the same 3090.
- **GPU1 (24 GB):** the 27B JSON-converter LLM (`llama-server`, ~21.5 GB resident).

Evidence (2026-06-09): `nvidia-smi` util trace during a generation alternated
`35% 34% 35% 29%` (swapping) with short `100% 100%` bursts (real compute).
Removing `--lowvram` changed nothing; fuseblk model reads measured 745 MB/s vs
780 MB/s local (negligible); the disk-full was a transient `/` spike. **Root
cause is the static 15 GB budget starving a workload that needs ~20 GB.**

## Why it's hard

The peak VRAM needs of GPU0's workloads are **mutually exclusive in time but the
allocation is static**:

| Workload (GPU0) | Peak VRAM | When |
|---|---|---|
| Voxtral TTS | ~4 GB resident | whenever someone talks to it |
| LTX-2.5 video (int8, streamed past the 15 GB budget) | ~15 GB | video jobs |
| Ideogram dual-model image | ~20 GB | image jobs |
| Klein-4b image | ~8 GB | image jobs |

`gpu_budget.sh` must pick ONE number for `GPU0_COMFYUI`. Today it's 15 (sized for
video). That's too small for dual-model Ideogram and too big to coexist with TTS
at Ideogram's peak. No single static number is right.

## What already exists (don't reinvent)

1. **`gpu_budget.sh`** — static OS-level single-source-of-truth, consumed at
   *service-launch* by ~7 `start_service.sh` scripts via `--reserve-vram` /
   `gpu_memory_utilization`. **`--reserve-vram` is argv-only → changing a GPU's
   ceiling requires restarting that service.**
2. **Proxy `capability_resource_tier`** on every route: `interactive` / `batch` /
   `expensive`. Image-gen is `batch`. This is already the "how heavy is this
   request" axis.
3. **Proxy already shells `systemctl --user start/restart/daemon-reload`** — it
   *can* own service lifecycle.
4. **Proxy already has per-workflow VRAM awareness** — `vram_estimates_file`,
   `vram_reserve_bytes`, a locked VRAM-estimates cache.
5. **Workflow resolution is `model_name → {model_name}.json`** (proxy.py:3232).
6. **Profiles** gate by `protocols` today, NOT by model/workflow — a clean place
   to add workflow gating.

## The core design question (yours, restated)

Can/should the proxy *own* a budget that has OS-level inputs, switch "profiles"
that re-allocate VRAM, and respect user roles — so a workflow that needs 20 GB
can only run when the box is in a profile that grants ComfyUI 20 GB?

### Ownership: who owns the budget?

**Recommendation: `gpu_budget.sh` stays the source of truth; the proxy becomes a
*budget-aware scheduler*, not the owner.** Rationale:

- The budget has non-proxy consumers (TTS, LLM, video, desktop). The proxy must
  not become a hard dependency for them to start.
- OS-level allocation (what each service reserves at launch) is genuinely an OS
  concern. The proxy's job is *admission control*: "given the current profile,
  is this request's workflow allowed to run right now?"
- Keeps the blast radius small: if the proxy is down, services still start with
  their static budgets.

### Profiles as named budget *presets*

Define profiles in `gpu_budget.sh` (or a sibling `gpu_profiles.yml`) as named
allocations instead of one static set:

```
profiles:
  default:        { GPU0_COMFYUI: 15, GPU0_TTS: 4 }   # video-capable, today's
  image-quality:  { GPU0_COMFYUI: 20, GPU0_TTS: 0 }   # dual-model Ideogram; TTS off
  balanced:       { GPU0_COMFYUI: 10, GPU0_TTS: 4 }   # Klein-4b + TTS coexist
```

Switching a profile = rewrite the active allocation + `systemctl restart` the
affected services (TTS off, ComfyUI with new `--reserve-vram`). ~5-10 s downtime
on those services. **This restart cost is unavoidable** (argv-only flag) and must
be surfaced in the UX, not hidden.

### How the proxy uses it

1. Each image workflow's `_meta` declares a `min_vram_gb` (or reuses
   `resource_tier`: dual-model = `expensive`, Klein = `batch`).
2. On an image request, the proxy checks: does the *active profile* grant ComfyUI
   ≥ the workflow's `min_vram_gb`?
   - **Yes** → run normally.
   - **No** → three sub-options (decision needed):
     a. **Reject** with a clear error ("ideogram4 requires the image-quality
        profile; ask an operator to switch").
     b. **Auto-switch** the profile (restart TTS/ComfyUI), run, optionally switch
        back after idle. Powerful but disruptive — TTS drops mid-call.
     c. **Queue** until an operator/role switches the profile.
3. The proxy already knows the active profile because it would be the thing that
   wrote it (via the existing systemctl plumbing).

### Roles

This is where profiles + the existing profile/role system intersect:

- **Operator/owner role** → may trigger a profile switch (it's destructive to
  other services). Maps to the existing admin-role-assignments table.
- **Standard app keys (daisy, librechat)** → may only *use* workflows allowed by
  the *current* profile; cannot switch. A request for an `expensive` workflow in
  the wrong profile gets option (a) reject, not an auto-switch.
- Per-profile workflow allow-lists slot into the proxy `profiles` table
  (currently `protocols`-only) — add an `allowed_workflows` / `max_resource_tier`
  field.

## Open decisions (need your call)

1. **Auto-switch vs reject vs queue** when a request needs a profile that isn't
   active (the 2b/2a/2c above). Auto-switch is slick but yanks TTS mid-call.
2. **Where profiles live:** extend `gpu_budget.sh` (bash, OS-native, what services
   already source) vs a new `gpu_profiles.yml` the proxy reads. Bash keeps one
   mechanism; YAML is friendlier for the proxy to parse and for an admin UI.
3. **Whether the proxy may restart non-proxy services** (TTS, video). It already
   restarts its own kobold; restarting TTS for an image request is a bigger
   ownership claim.
4. **MPS as an alternative/complement:** NVIDIA MPS with
   `CUDA_MPS_PINNED_DEVICE_MEM_LIMIT` can *hard-cap* each client's VRAM on a
   shared GPU (the only real strict-enforcement option on 3090 — MIG is
   datacenter-only). This would let profiles be enforced by the GPU itself rather
   than trusted cooperation, but adds an MPS daemon. Worth it iff cooperative
   `--reserve-vram` proves too leaky.

## Smallest first step (if we proceed)

Before any of the above: just add a second static profile and a manual switch
script. `gpu-profile image-quality` rewrites the allocation + restarts TTS(off) +
ComfyUI(20 GB), `gpu-profile default` reverts. No proxy changes, no roles, no
auto-switch. Proves the restart-to-reallocate flow and gives immediate fast
Ideogram when you want it, before building the proxy-integrated scheduler.
