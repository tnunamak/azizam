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
