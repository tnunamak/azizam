# Qwen-Image 2.1 Multi-Reference Edit: workflow notes

Workflow `image.edit.qwen-image-2.1-edit-multiref` (manifest `manifests/workflows/qwen-image-2.1-edit-multiref.yml`).

Research and provenance notes for owners and maintainers. No screen or API renders this file.

## Notes moved from the manifest (2026-10-10)

### Description

2-image reference composite/edit via Qwen-Image 2.1's native multi-image conditioning (TextEncodeQwenImage21's Autogrow `images` input, tested up to 16 slots upstream; this workflow wires exactly 2 as required inputs). Verified: convincingly composites a subject from one reference onto the scene/lighting of another in a single pass, no separate masking/compositing step needed. Reference prompts should tag each image explicitly, e.g. "<image1> shows X. <image2> shows Y. Combine them by...". A 3-reference variant is not shipped: the gateway's static node-binding model (gateway/manifests.py _apply_mapping, flat node/input writes) cannot make an Autogrow sub-key conditionally present per request, so a 3rd "optional" slot would always inject a placeholder image as a real (if low-signal) reference. A 3-image variant needs gateway support for per-request Autogrow key pruning; this manifest keeps the tested 2-image shape.
