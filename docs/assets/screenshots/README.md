# Documentation screenshots

These are real pages from the current static UI export, served by a throwaway gateway. The existing browser-test seed creates an example owner, service, access plan, App key and successful request through HTTP APIs. The engine returns a deterministic test answer. No operator database, provider key or live service is used.

The screenshots use light mode, a 1440 × 1000 viewport and a device scale of 1. Each WebP is under 300 KB. Home shows app connection settings, Models shows the current catalog, and People shows the example owner. Missing health and monthly usage measurements remain visible; no response or telemetry is fabricated for the capture.

Reproduce from a dependency-installed checkout with the UI built. Run `web/e2e/seed-review.mjs` from `web/`, passing `node ../docs/assets/screenshots/capture.mjs` as its review command. Set `UI_E2E_PORT` to an unused port in 18000–18998. The capture script requires the worktree's `venv/bin/python` with Pillow to compress the images. The supervisor stops both test processes and removes temporary gateway state after capture.

## Tour

![Model catalog with an explicitly unavailable health measurement](models-light-1440.webp)

![People page with an example owner and unmeasured monthly usage](people-light-1440.webp)
