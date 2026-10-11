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
