# AI Gateway UI — Design Implementation Plan

> Historical implementation record. Use
> `docs/design/WEB_UI_STEERING.md` for current product IA and presentation
> requirements, and `docs/design/VIVID_FISH_COMPONENT_LIBRARY_SPEC.md` for
> current shared-component contracts. The server-rendered stack and checklist
> below are preserved as history, not as a competing current brief.

Source design: `docs/design/vivid-fish-handoff/` (Claude Design handoff).
Authoritative reference files: `gateway/atoms.jsx` (tokens/icons/primitives),
`gateway/screens-user.jsx`, `gateway/screens-admin.jsx`,
`gateway/screens-admin-v2.jsx`, `gateway/brief.jsx`, `chats/chat1.md`.

## Settled decisions

- **Stack:** implement in the existing server-rendered Python HTML idiom
  (`gateway/setup_render.py`, `gateway/admin_render.py`,
  `gateway/user_render.py`, shared `gateway/gateway_shell.py`). NOT React /
  Next / Tailwind. The handoff README explicitly says to recreate the visual
  output in the target stack, not copy the prototype's React structure.
- **Tokens already match:** `gateway_shell.py` `SHELL_CSS` already ports the
  design's exact palette (`#0A1620`, `#0E1E2C`, `#142A3D`, `#1C3850`,
  `#E8E2D3`, accent `#7BC4C8`), fonts (Geist / Newsreader / JetBrains Mono),
  and the `vivid.fish` brand wordmark. The gap is screens + components, not
  tokens.
- **Form factor:** mobile-first base, desktop expands (same components, more
  columns) — exactly the designer's stated intent. No literal phone chrome
  (status bar / 28px frame / bottom tab bar) on desktop.
- **Brand vs product:** Vivid Fish is Tim's deployment brand, not the product.
  Keep deployment branding swappable; don't hardcode "Tim's gateway" copy into
  product logic.

## Test contract to preserve (76 green render tests)

`tests/test_user_render.py` pins: data-attributes
(`data-create-owned-app`, `data-mint-owned-key`, `data-revoke-owned-key`,
`data-view-owned-key`, `data-delete-owned-app`), IDs (`base-url-display`,
`copy-base-url`), copy ("Create one App for each tool or script", "Use this for
one tool,", "Copy this opk_ App API key now", "only for Apps and SDK tools",
"Create an App", "permission Profiles"), `sessionStorage` (not localStorage),
and signed-out vs signed-in visibility of "Base URL" / "Check an API key".
Refits must keep these or update tests deliberately, never silently.

## Execution order (highest-confidence-first)

1. **Component CSS layer (additive, low risk).** Add design primitives as
   reusable classes in `SHELL_CSS` / page CSS: status dot, calmer cards,
   pill tones (accent/success/warning/danger/info), `Btn` variants
   (primary/secondary/ghost/danger as `.btn` classes), `Section`/eyebrow,
   serif `Sentence`. Reuse existing `--vf-*` tokens. Verify no regression.
2. **User portal Home refit** (`user_render.py` signed-in view): "Your
   gateway" framing, a reachability/health card with a live status dot, app
   cards with per-app status + last-used, base-URL card kept (tests depend on
   it), Recent (last 3) instead of a feed. Preserve all data-attributes.
3. **Create App + Key task-shaped flow** (brief's top win, fixes complaints
   5/6/8/9/10): name → access level (profile, human-sentence framing) → key
   shown once → copy → done. No profile IDs / JSON / service-account language
   in the user path. Reuse existing `/me/api/apps` + `/keys` endpoints.
4. **Verify**: run `tests/test_user_render.py` + setup + admin; drive live
   `/` and `/setup` via `scripts/fresh_instance_harness.sh` + headed browser;
   fix discoveries; rerun from clean.

## Deferred (per brief's own "cut for v1")

- Policy simulator (brief says cut).
- Dedicated Activity feed (fold into Home "Recent · last 3").
- Dedicated Capabilities tab (fold into Home health card).
- Desktop profile editor (mobile editor works on desktop first).
- Admin screens beyond tightening — second slice after user portal lands.

## Status

- [x] 1. Component CSS layer — added `.vf-dot`, `.vf-kicker`, `.vf-sentence`,
      `.vf-pill`, `.vf-card`, `.vf-btn`, `.vf-section`, `.vf-rows` to
      `SHELL_CSS` (additive; 76 render + 86 shell/render tests green).
- [x] 2. User portal refit
      - [x] Signed-out door: killed the OIDC dead-end. `_signed_out_hero` now
            always offers a working action (OIDC when configured + a `/setup`
            link for the local-owner/admin-key paths), copy adapts on
            `owner_exists` (threaded from `_owner_or_admin_exists()`). 3 new
            regression tests; proven on live :15000.
      - [x] Signed-in Home: "your gateway" framing + adaptive gateway line
            (0/1/N apps), live reachability health card (`_health_card`,
            `.vf-dot`, `/health` poll every 30s). Preserves the test contract
            (Your Apps / Create an App / Base URL / View access). 78 render
            tests green; verified on live :15000 signed-in.
- [ ] 3. Create App + Key flow  ← the stack make-or-break test (most React-shaped)
- [ ] 4. Verify clean
