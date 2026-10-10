# Flux2 Klein 4B Seamless Tile: workflow notes

Workflow `image.generate.flux2-klein-4b-seamless-tile` (manifest `manifests/workflows/flux2-klein-4b-seamless-tile.yml`).

Research and provenance notes for owners and maintainers. No screen or API renders this file.

## Notes moved from the manifest (2026-10-10)

### Description

Gateway-composed seamless texture generation. The proxy generates with Flux2, circularly offsets the image, masks the centered seam cross, repairs it with the Flux2 inpaint workflow, then offsets the result back.
