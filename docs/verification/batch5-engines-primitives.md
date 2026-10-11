# Engines primitive migration verification

Base: `5f7ddc4bf4ce38c0575999c8a486dbf347bf6fca` (`feat/categories-kernel`).
The Button/Input/Dialog/Skeleton migration already landed in ancestor
`bca4e191`. This batch completes the remaining Engines callouts, empty states
and checkboxes, and checks the earlier migration's behavior.

## Reproduced defects

- The clipboard fallback left its textarea mounted when `execCommand` threw.
  `copy-fallback-cleanup.spec.tsx` failed before the `finally` cleanup.
- Escape from Remove engine tried to restore focus to an unmounted menu item.
  The fixture browser test failed at 100% text before an explicit row-menu
  invoker was passed to ConfirmDialog.
- At 390 × 844 and 200% root text size, the Add dialog footer's bottom was
  1474.3px. The shared Dialog's grid utility won over the consumer's flex
  utility because `cn` joins classes without merging. Explicit flex layout
  and moving the description into the scrolling body keep the footer reachable.

## CSS deletion evidence

Only these 11 class names were removed from `legacy-components.css`:

```
vf-button-secondary
vf-button-danger
vf-button-lg
vf-button-full
vf-field
vf-field-label
vf-field-input
vf-select
vf-textarea
vf-field-help
vf-field-error
```

Their associated hover, placeholder, invalid and focus selectors were removed
with them. Before deletion, this scan over all source files (including tests)
returned only the stylesheet and the unrelated `--vf-field` token declaration:

```
rg -n 'vf-(field|select|textarea|button-secondary|button-danger|button-lg|button-full)' web/src
```

The production-source sweep for interpolated class construction found
`icons.tsx` building `vf-status-icon` classes, unrelated to these families:

```
rg -n 'vf-.*\$\{|`vf-|"vf-"' web/src --glob '!*.css' --glob '!*.spec.*' --glob '!*.e2e.*'
```

The base `.vf-button`, `.vf-button-ghost` and `.vf-button-sm` remain consumed
by `operational.tsx`; their rules remain. The global stylesheet remains imported.
Gallery, Showcase and server-rendered pages are unchanged.

## Behavioral evidence

`engines-primitives.spec.ts` captures Engines and Add engine at 1440 and 390,
checks 44px controls and fixed footer positioning at 100% and 200% text size,
checks Escape/focus return, and creates a real concurrent registry edit to
verify 412 rejection, preview retention and successful retry.

`engines-real.spec.ts` adds an engine through the form, tests its endpoint,
detects models, edits a public mapping and applies it with If-Match. It edits
transforms and lifecycle through Advanced JSON, verifies their stored values
alongside unknown fields and model mappings, and requires exact-name removal.

Existing `engines-detect-apply.spec.ts` verifies routing through a strict
fixture engine at desktop/phone widths and in light/dark themes.
All browser work uses the fixture server at port 18640, never live state.

## Check results (2026-10-10)

- `npm --prefix web run lint`: passed, no warnings or errors.
- `npx --prefix web tsc --noEmit -p web`: passed after the build completed.
- `npm --prefix web run build`: passed, 26 static pages generated.
- `npm --prefix web run test:components -- --maxWorkers=2`: 1,215 passed
  across 92 files. The first unrestricted run timed out in two unrelated
  Categories tests; the two-worker run kept their original timeouts/assertions.
- The five focused component files (engine checks, service dialog, confirmation,
  phone targets and clipboard cleanup): 24 passed after adding a second
  clipboard-failure regression test.
- Fixture Playwright: nine strict-engine detect/apply/routing checks passed;
  all six focused primitive and real-registry journey checks passed on rerun.
  The first combined run reached the real journey's last registry read and
  timed out at 60 seconds; the unchanged journey passed in 49.3 seconds alone
  with the focused tests. No timeout or assertion was weakened.
- `scripts/run-hermetic-pytest.sh -q tests/test_service_registry.py
  tests/test_web_ui_static_mount.py tests/test_browser_route_cutover.py`:
  198 passed after commit `8c0d825e`.

Browser command, from `web/`:

```
UI_E2E_PORT=18640 UI_E2E_PYTHON=/home/tnunamak/applications/azizam/venv/bin/python npx playwright test --config playwright.config.ts engines-primitives.spec.ts engines-real.spec.ts engines-detect-apply.spec.ts
```

Before screenshots are under `web/test-results/batch5-first-run/screens/`:
`batch5-before-{engines,add}-{1440,390}.png`.
After screenshots are under `web/test-results/screens/`:
`batch5-after-{engines,add}-{1440,390}.png`.
The final focused run uses the same fresh fixture seed as the before captures.

Not checked: live backends, operator data, the complete Python suite, or
Gallery/Showcase/server-rendered browser journeys. Those surfaces are unchanged.
