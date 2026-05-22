# Registry Seed Catalogs

This directory contains bundled seed catalogs for the gateway registry. Core
code loads these files generically; it must not hardcode particular providers,
services, workflows, targets, or protocol frontends.

At startup, these catalogs may seed read-only registry records. They are not
the ongoing runtime source of truth: UI-managed service/provider records live
in `admin.db` and are edited from `/admin`.

Operators can copy bundled/provisioned records into UI-managed ownership from
the dashboard. Provisioned/bundled records stay read-only until explicitly
copied or detached.
