# Background removal for openai-proxy image generation

## Goal

Allow the model to decide, per image-gen request, whether the generated image should have its background removed — and return the result through the existing OpenAI-compatible `/v1/images/generations` (and `/v1/images/edits`) surface. No new endpoints, no new request fields the user has to set. The decision is made by an LLM, not by keyword matching.

## Constraints (decided)

- **Trigger:** LLM heuristic. The proxy already routes prompts through an LLM for normalization; that same call should emit a structured `{remove_background: bool}` decision. No keyword matching, no client-supplied flag.
- **Output format:** OpenAI-compatible. Same response shape as today — PNG bytes returned via the signed-URL image-content endpoint. Transparency goes in the alpha channel of a PNG.
- **Model:** RMBG-2.0 (briaai/RMBG-2.0) via the [ComfyUI-RMBG](https://github.com/1038lab/ComfyUI-RMBG) custom node pack. Best general-purpose quality at small VRAM cost (~1.5 GB). Falls back gracefully — if the node isn't installed, the proxy should log and skip removal rather than fail the generation.
- **Workflow approach:** **Modify the existing image workflows** in `workflows/images/` (`flux2-klein.json`, `flux2-klein-4b.json`, `flux-dev.json`, plus the `*-edit.json` variants) to include an optional RMBG-2.0 node after the SaveImage tap point. Toggle the node's execution per-request rather than maintaining parallel workflow files. (ComfyUI nodes can be bypassed by routing inputs around them or by setting them to `mode: 4` "bypass" in the prompt JSON — verify which approach the proxy's workflow injection supports.)

## Where this slots in

- **Entry points:** `proxy.py` — `/v1/images/generations` (line 5694) and `/v1/images/edits` (line 5703). Both flow into `_generate_images_with_adaptive_batching()` (line 3124).
- **Workflow injection:** the proxy already mutates workflows at runtime (look at how `image_node` / `target_field` plumbing works near line 4739; that pattern is the template).
- **LLM decision call:** wherever prompt normalization happens today, extend the structured output schema to include `remove_background: bool` (and probably `confidence` or reasoning if you want it observable on the status page).

## What to build

1. **Install ComfyUI-RMBG** in the ComfyUI custom_nodes dir; verify RMBG-2.0 loads and produces a clean alpha mask on a test image. Confirm VRAM footprint and add it to `gpu_budget.sh` accounting if non-trivial.
2. **Extend the LLM normalization step** (the same call that turns a user prompt into a final positive/negative pair) to also return `remove_background: bool`. Use structured output (JSON schema) — don't parse free text.
3. **Modify each image workflow JSON** in `workflows/images/` to include an RMBG-2.0 node between the existing SaveImage (or VAEDecode → SaveImage chain) and a new "SaveImage (with alpha)" output. Make sure the node is wired such that the proxy can bypass it cheaply when the LLM says `remove_background: false`.
4. **Wire the toggle in `proxy.py`** — in the workflow-injection block, if the decision is `true`, route the final image through RMBG; otherwise bypass. The output must still be a PNG; the alpha channel is the only difference.
5. **Update the response payload builder** (`_build_image_response_payload`, line 3096) if needed — the OpenAI shape is unchanged but the content-type stays `image/png` regardless.
6. **Status page surfacing:** the firehose / status endpoint should show whether background removal was triggered per request (good for observability and debugging when the LLM makes bad calls).
7. **Test cases to pass:**
   - "a portrait of a cat" → no removal (full scene context)
   - "a logo of a fish" → removal (clear product/asset intent)
   - "a sticker of a tomato" → removal
   - "a landscape painting" → no removal
   - "a coffee mug on a white background, for a product page" → removal
   - Edit endpoint with `mask` set: bypass removal entirely (user already specified the alpha they want)

## Non-goals

- Don't expose a `background: "transparent"` request param. OpenAI doesn't have one and we want the model to decide.
- Don't add a separate `/v1/images/remove-background` endpoint. Keep the surface unchanged.
- Don't ship multiple workflow files per "with/without removal" variant — too much surface area to keep in sync.
- Don't do this client-side. The proxy owns the decision so every client (daisy, SillyTavern, the dev agent stack) benefits without changes.

## Open questions for the implementer

- Does the current workflow-injection code support setting a node to `mode: 4` (bypass) per-request, or does it only set input values? If only the latter, the cleanest path is two output taps on the workflow and choosing which one to read in the proxy.
- Is the LLM normalization step on the hot path for *every* image gen, or only when the user prompt is short/ambiguous? If it's conditional, the `remove_background` decision needs a default for the bypass case (default: false).
- RMBG-2.0 license is non-commercial. Confirm this is OK for the use case or pick BiRefNet / BEN2 from the same node pack if it isn't.

## Files to read first

- `/home/tnunamak/applications/openai-proxy/proxy.py:5694` — `/v1/images/generations` handler
- `/home/tnunamak/applications/openai-proxy/proxy.py:3124` — `_generate_images_with_adaptive_batching`
- `/home/tnunamak/applications/openai-proxy/proxy.py:4700-4750` — workflow injection pattern
- `/home/tnunamak/applications/openai-proxy/workflows/images/flux2-klein.json` — current image workflow shape
- `/home/tnunamak/applications/openai-proxy/proxy_config.yml` — model/workflow alias mappings
- ComfyUI-RMBG README: https://github.com/1038lab/ComfyUI-RMBG
