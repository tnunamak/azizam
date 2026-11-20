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
- A chat `image_url` part can name an uploaded image by id instead of carrying it: `POST /v1/assets` stores one image
  (multipart, outside the JSON body limit), and `image_url.url` takes the returned `img_...` id. The gateway sends the
  engine a data URI after the owner check. An unknown or unreadable id answers 400 with the part named.

### Fixed

- In a Docker Compose install, Status no longer reports "LiteLLM is not accepting
  connections" while chat works. The Status page and the model catalog probed the
  record's loopback default address instead of the address `AZIZAM_LITELLM_BASE_URL`
  names. Chat, Status and the catalog now read that variable through one function.
