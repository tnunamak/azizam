# Changelog

All notable changes to Azizam are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Azizam has no
tagged release yet.

## Unreleased

### Added

- First public source release.
- The release gate's policy step fails when a tracked file holds a private
  (LAN or CGNAT) address, a home path or a non-documentation email address
  (`scripts/ci/public_leak_scan.py`).

### Fixed

- In a Docker Compose install, Status no longer reports "LiteLLM is not accepting
  connections" while chat works. The Status page and the model catalog probed the
  record's loopback default address instead of the address `AZIZAM_LITELLM_BASE_URL`
  names. Chat, Status and the catalog now read that variable through one function.
