# Changelog

All notable changes to Azizam are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Azizam has no
tagged release yet.

## Unreleased

### Added

- First public source release.
- Members can upload their own LoRA adapters (`.safetensors` only) when the owner turns on
  "members may upload adapters" (`GET`/`PUT /admin/api/operator/adapter-uploads`, off by default,
  with a size cap that defaults to 2 GiB). An upload is stored as a private Library asset of the
  person who made it, shown only to that person. It joins that person's pick-list when its
  architecture fits the workflow, and the gateway copies it into a gateway-owned folder of the
  engine's loras folder only when a request uses it. Deleting the asset removes the list entry and
  the copy. A file with an unknown architecture is stored but not offered, and the response says so.
  The body limit of `POST /me/api/adapters` is the size ceiling plus 1 MiB.
- The release gate's policy step fails when a tracked file holds a private
  (LAN or CGNAT) address, a home path or a non-documentation email address
  (`scripts/ci/public_leak_scan.py`).

### Fixed

- In a Docker Compose install, Status no longer reports "LiteLLM is not accepting
  connections" while chat works. The Status page and the model catalog probed the
  record's loopback default address instead of the address `AZIZAM_LITELLM_BASE_URL`
  names. Chat, Status and the catalog now read that variable through one function.
