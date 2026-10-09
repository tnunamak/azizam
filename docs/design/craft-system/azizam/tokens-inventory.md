# Azizam token inventory (type, spacing, radius, shadow, control size)

Source of truth: `web/src/styles/tokens.css` (imported by `web/src/app/globals.css:4`). `globals.css` defines **no** custom properties of its own; it consumes tokens through `@apply` (listed below). Paths relative to `web/src/`.

## Font family
| Token | Value | Ref |
|---|---|---|
| `--font-sans` | "Hanken Grotesk Variable", ui-sans-serif, system-ui, sans-serif | styles/tokens.css:182 |
| `--font-display` | var(--font-sans) | styles/tokens.css:183 |
| `--font-mono` | "Geist Mono", ui-monospace, monospace | styles/tokens.css:184 |

(README still says Geist Sans; the computed font on every measured text node is Hanken Grotesk Variable.)

## Font size / line height (Tailwind `@theme inline`, styles/tokens.css:181)
Comment at tokens.css:186 declares the scale as "12 · 13 · 14 · 16 · 20 · 24 · 30 · 40"; there is **no 13 px token**, and `text-base` is remapped to **15 px**, `text-lg` to **16 px**.

| Token | Value | px | Ref |
|---|---|---|---|
| `--text-2xs` / `--line-height` | 0.625rem / 0.75rem | 10 / 12 | styles/tokens.css:188-189 |
| `--text-xs` / `--line-height` | 0.75rem / 1rem | 12 / 16 | styles/tokens.css:190-191 |
| `--text-sm` / `--line-height` | 0.875rem / 1.25rem | 14 / 20 | styles/tokens.css:192-193 |
| `--text-base` / `--line-height` | 0.9375rem / 1.375rem | 15 / 22 | styles/tokens.css:194-195 |
| `--text-lg` / `--line-height` | 1rem / 1.5rem | 16 / 24 | styles/tokens.css:196-197 |
| `--text-xl` / `--line-height` | 1.25rem / 1.75rem | 20 / 28 | styles/tokens.css:198-199 |
| `--text-2xl` / `--line-height` | 1.5rem / 1.875rem | 24 / 30 | styles/tokens.css:200-201 |
| `--text-3xl` / `--line-height` | 1.875rem / 2.25rem | 30 / 36 | styles/tokens.css:202-203 |
| `--text-4xl` / `--line-height` | 2.5rem / 2.75rem | 40 / 44 | styles/tokens.css:204-205 |
| `--tracking-tight` | -0.015em | | styles/tokens.css:206 |
| `--tracking-display` | -0.025em | | styles/tokens.css:207 |
| `--text-vf-micro/small/body/heading/title/display` | aliases → xs/sm/base/lg/xl/3xl | | styles/tokens.css:307-312 |
| `--vf-text-xs/sm/base/md/lg` | 0.75 / 0.875 / 0.9375 / 1.0625 / 1.25rem (raw values; md = 17 px) | | styles/tokens.css:351-355 |
| `--vf-type-title` / `--vf-type-title-leading` | 1.25rem / 1.3 | | styles/tokens.css:356-357 |

Base application: `body { @apply ... font-sans text-base }` → 15 px/22 body default (app/globals.css:16); `h1,h2,h3 { @apply tracking-tight text-balance }` (app/globals.css:20-23).

## Spacing
No `--spacing` override: Tailwind v4 default 4 px step. Layout tokens:
| Token | Value | Ref |
|---|---|---|
| `--sidebar-width` | 232px | styles/tokens.css:109 |
| `--rail-width` | 72px | styles/tokens.css:110 |
| `--content-max` | 1200px | styles/tokens.css:111 |
| `--vf-context-width` | 300px (legacy) | styles/tokens.css:359 |
| `.shell-content` padding | `px-5 pt-6 pb-10 sm:px-8 sm:pt-8 lg:px-12 lg:pt-10` → 20/32/48 px sides, 24/32/40 px top | app/globals.css:115-117 |
| `.shell-sidebar` padding | `px-3 py-4` | app/globals.css:82-84 |

## Radius
| Token | Value | Ref |
|---|---|---|
| `--radius` | 6px (controls) | styles/tokens.css:95 |
| `--radius-card` | 10px (panels, dialogs) | styles/tokens.css:96 |
| `--radius-media` | 6px (creation tiles) | styles/tokens.css:97 |
| `--radius-xs` | 2px (uptime bar only) | styles/tokens.css:277 |
| `--radius-sm` | calc(var(--radius) - 2px) = 4px | styles/tokens.css:278 |
| `--radius-md` | var(--radius) = 6px | styles/tokens.css:279 |
| `--radius-lg` / `--radius-xl` | var(--radius-card) = 10px | styles/tokens.css:280-281 |
| `--radius-control` / `--radius-panel` / `--radius-media` | 6 / 10 / 6px | styles/tokens.css:282-284 |
| `--radius-vf-sm/md/lg` | 6 / 6 / 10px (legacy) | styles/tokens.css:313-315 |
| `--vf-radius-sm/md/lg/pill` | 6 / 6 / 10 / 999px (legacy) | styles/tokens.css:347-350 |

## Shadow / elevation
| Token | Value | Ref |
|---|---|---|
| `--shadow-overlay` (light) | 0 16px 48px rgb(8 28 34 / 18%), 0 2px 6px rgb(8 28 34 / 8%) | styles/tokens.css:100 |
| `--shadow-float` (light) | 0 6px 20px rgb(8 28 34 / 14%) | styles/tokens.css:101 |
| `--shadow-overlay` / `--shadow-float` (dark) | 45%/30% and 40% black | styles/tokens.css:169-170 |
| `--shadow-lg`, `--shadow-xl`, `--shadow-overlay`, `--shadow-float` (theme) | → overlay / float | styles/tokens.css:285-288 |
| `--vf-shadow-panel/row/caustic-2`, `--vf-elevation-base` | none | styles/tokens.css:360-363 |
| `--vf-elevation-raised` | var(--shadow-overlay) | styles/tokens.css:364 |
Rule stated at tokens.css:99: "borders build structure; shadows only lift overlays".

## Control size
No control-height custom properties exist except legacy `--vf-hit-target: 44px` (styles/tokens.css:358). Heights live in component classes:
- Button (components/ui/button.tsx:22-30): default `h-10` (40), xs `h-7` (28), sm `h-8` (32), lg `h-11` (44), icon `size-10`, icon-xs `size-7`, icon-sm `size-8`, icon-lg `size-11`; base adds `max-sm:min-h-11 max-sm:min-w-11` (44 on phones) and `rounded-control text-sm font-medium` (button.tsx:7).
- Input `h-10 rounded-control text-base` (components/ui/input.tsx:10).
- Select trigger `data-[size=default]:h-11`, `data-[size=sm]:h-8` (components/ui/select.tsx:39).
- Textarea `min-h-16` (components/ui/textarea.tsx:9).
- Tabs list `h-11`, `max-sm:min-h-12.5`, `rounded-lg p-0.75` (components/ui/tabs.tsx:28).
- Badge `rounded-full px-2 py-0.5 text-xs` (components/ui/badge.tsx:7).
- Card primitive `rounded-xl border py-6 shadow-sm gap-6` (components/ui/card.tsx:9).
- Shell nav rows `min-h-11 lg:min-h-8 text-sm` (app/globals.css:89, 113); mobile tab `min-h-14 text-xs` (app/globals.css:131); mobile tab icon `size-5.5` (app/globals.css:140).

## Motion (for completeness)
`--motion-duration` 200ms, `--motion-fast` 120ms, `--motion-ease` cubic-bezier(0.2,0,0,1) (styles/tokens.css:104-106; 0ms under reduced motion, :174-178).

## Utility usage across web/src
Scanned 217 .tsx/.ts/.css files under web/src (variant prefixes such as sm:, max-sm:, [&_svg]: are folded into the base utility; test files included).

### text size utilities

| Utility | Count | Files | Top files |
|---|---|---|---|
| `text-sm` | 366 | 54 | app/providers/page.tsx (47), app/try/page.tsx (28), app/home/page.tsx (28) |
| `text-xs` | 150 | 40 | app/library/page.tsx (14), app/models/models-client.tsx (12), app/providers/page.tsx (10) |
| `text-base` | 17 | 13 | components/product/status-ui.tsx (3), app/status/status-client.tsx (2), components/shell/app-shell.tsx (2) |
| `text-lg` | 10 | 10 | app/workflows/page.tsx (1), app/library/page.tsx (1), app/design/page.tsx (1) |
| `text-2xl` | 6 | 6 | app/library/page.tsx (1), app/usage/page.tsx (1), app/sign-in/page.tsx (1) |
| `text-xl` | 2 | 2 | app/library/page.tsx (1), components/product/status-ui.tsx (1) |
| `text-2xs` | 1 | 1 | components/product/person-mark.tsx (1) |
| `text-[10px]` | 1 | 1 | components/ui/operational.tsx (1) |

### height utilities

| Utility | Count | Files | Top files |
|---|---|---|---|
| `size-4` | 95 | 47 | app/home/page.tsx (6), components/ui/select.tsx (6), components/ui/dropdown-menu.tsx (6) |
| `min-h-11` | 92 | 34 | app/home/page.tsx (10), app/engines/page.tsx (7), app/try/page.tsx (7) |
| `h-11` | 23 | 13 | app/try/workflow-media-input.tsx (3), app/setup/page.tsx (3), app/models/models-client.tsx (3) |
| `size-8` | 20 | 17 | app/library/page.tsx (2), app/usage/page.tsx (2), components/ui/avatar.tsx (2) |
| `size-5` | 16 | 10 | app/library/page.tsx (4), components/product/status-ui.tsx (3), app/home/page.tsx (2) |
| `min-h-0` | 12 | 5 | app/library/page.tsx (5), components/ui/legacy/dialog.tsx (3), app/home/page.tsx (2) |
| `size-3.5` | 12 | 9 | app/try/workflow-inputs.tsx (2), components/ui/checkbox.tsx (2), components/ui/dropdown-menu.tsx (2) |
| `h-10` | 12 | 8 | app/design/page.tsx (3), components/admin/add-service-dialog.tsx (3), app/library/page.tsx (1) |
| `h-4` | 11 | 6 | app/design/page.tsx (4), components/product/uptime-bar.tsx (2), components/ui/data-table.tsx (2) |
| `size-2` | 11 | 7 | components/shell/app-shell.tsx (3), components/ui/avatar.tsx (3), app/usage/page.tsx (1) |
| `size-3` | 11 | 8 | app/providers/page.tsx (2), components/ui/charts.tsx (2), components/ui/avatar.tsx (2) |
| `h-64` | 10 | 8 | app/showcase/page.tsx (2), app/providers/page.tsx (2), app/workflows/page.tsx (1) |
| `size-11` | 10 | 7 | app/library/page.tsx (3), components/shell/app-shell.tsx (2), app/try/receipt-link.tsx (1) |
| `size-10` | 10 | 9 | components/ui/avatar.tsx (2), app/design/page.tsx (1), app/usage/page.tsx (1) |
| `size-6` | 9 | 7 | app/home/page.tsx (2), components/ui/avatar.tsx (2), app/library/page.tsx (1) |
| `h-8` | 9 | 7 | app/providers/page.tsx (2), components/product/uptime-bar.tsx (2), app/invite/page.tsx (1) |
| `min-h-8` | 7 | 6 | app/globals.css (2), app/library/page.tsx (1), app/status/status-view.tsx (1) |
| `h-6` | 7 | 5 | components/product/audio-cover.tsx (3), app/library/page.spec.tsx (1), app/try/page.tsx (1) |
| `size-9` | 6 | 6 | app/library/page.tsx (1), app/activity/requests.tsx (1), app/providers/page.tsx (1) |
| `max-h-96` | 6 | 3 | app/try/page.tsx (3), app/showcase/page.tsx (2), app/status/status-client.tsx (1) |
| `h-9` | 5 | 5 | app/design/page.tsx (1), components/ui/command.tsx (1), components/ui/legacy/button.tsx (1) |
| `h-5` | 5 | 3 | components/ui/command.tsx (2), components/ui/legacy/switch.tsx (2), app/home/page.tsx (1) |
| `h-14` | 4 | 4 | app/library/page.tsx (1), app/activity/page.tsx (1), app/home/page.tsx (1) |
| `h-20` | 4 | 3 | app/engines/page.tsx (2), app/try/page.tsx (1), app/activity/requests.tsx (1) |
| `h-24` | 3 | 2 | app/apps/page.tsx (2), app/workflows/page.tsx (1) |
| `h-3` | 3 | 2 | components/ui/data-table.tsx (2), app/design/page.tsx (1) |
| `h-72` | 3 | 2 | app/engines/page.tsx (2), app/usage/page.tsx (1) |
| `h-16` | 3 | 3 | app/usage/page.tsx (1), app/people/page.tsx (1), app/home/page.tsx (1) |
| `max-h-[85dvh]` | 3 | 3 | app/people/plans.tsx (1), components/ui/legacy/sheet.tsx (1), components/ui/legacy/dialog.tsx (1) |
| `h-32` | 3 | 2 | app/providers/page.tsx (2), app/home/page.tsx (1) |
| `h-40` | 3 | 3 | app/status/status-client.tsx (1), app/invite/page.tsx (1), components/shell/app-shell.tsx (1) |
| `min-h-4` | 3 | 2 | app/models/models-client.tsx (2), components/ui/alert.tsx (1) |
| `size-2.5` | 3 | 3 | components/product/uptime-bar.tsx (1), components/ui/avatar.tsx (1), components/ui/tooltip.tsx (1) |
| `h-48` | 2 | 2 | app/page.tsx (1), app/status/status-client.tsx (1) |
| `min-h-32` | 2 | 1 | app/library/page.tsx (2) |
| `min-h-10` | 2 | 2 | app/library/page.tsx (1), components/product/page-header.tsx (1) |
| `h-36` | 2 | 2 | app/design/page.tsx (1), app/usage/page.tsx (1) |
| `max-h-[85vh]` | 2 | 2 | app/try/workflow-media-input.tsx (1), app/providers/page.tsx (1) |
| `min-h-5` | 2 | 2 | app/home/page.tsx (1), app/models/models-client.tsx (1) |
| `min-h-16` | 2 | 2 | app/models/models-client.tsx (1), components/ui/textarea.tsx (1) |
| `max-h-[calc(100dvh-2rem)]` | 2 | 1 | app/apps/page.tsx (2) |
| `h-2.5` | 2 | 2 | components/product/uptime-bar.tsx (1), components/ui/scroll-area.tsx (1) |
| `h-7` | 2 | 2 | components/ui/button.tsx (1), components/ui/input.tsx (1) |
| `h-12` | 2 | 1 | components/ui/command.tsx (2) |
| `h-4.5` | 2 | 1 | components/ui/switch.tsx (2) |
| `h-3.5` | 2 | 1 | components/ui/switch.tsx (2) |
| `min-h-14` | 1 | 1 | app/globals.css (1) |
| `size-5.5` | 1 | 1 | app/globals.css (1) |
| `min-h-80` | 1 | 1 | app/globals.css (1) |
| `size-16` | 1 | 1 | app/library/page.tsx (1) |
| `min-h-[50dvh]` | 1 | 1 | app/library/page.tsx (1) |
| `max-h-40` | 1 | 1 | app/library/page.tsx (1) |
| `max-h-72` | 1 | 1 | app/engines/page.tsx (1) |
| `min-h-36` | 1 | 1 | app/engines/page.tsx (1) |
| `h-80` | 1 | 1 | app/usage/page.tsx (1) |
| `size-24` | 1 | 1 | app/try/workflow-media-input.tsx (1) |
| `min-h-40` | 1 | 1 | app/try/page.tsx (1) |
| `h-28` | 1 | 1 | app/try/page.tsx (1) |
| `max-h-80` | 1 | 1 | app/try/page.tsx (1) |
| `h-13` | 1 | 1 | app/home/page.tsx (1) |
| `h-1.5` | 1 | 1 | app/status/status-client.tsx (1) |
| `min-h-6` | 1 | 1 | app/providers/page.tsx (1) |
| `max-h-[92dvh]` | 1 | 1 | app/providers/page.tsx (1) |
| `size-12` | 1 | 1 | app/providers/page.tsx (1) |
| `max-h-[90dvh]` | 1 | 1 | components/shell/app-shell.tsx (1) |
| `h-1` | 1 | 1 | components/product/audio-cover.tsx (1) |
| `min-h-9` | 1 | 1 | components/product/segmented.tsx (1) |
| `size-7` | 1 | 1 | components/ui/button.tsx (1) |
| `h-[var(--radix-select-trigger-height)]` | 1 | 1 | components/ui/select.tsx (1) |
| `max-h-75` | 1 | 1 | components/ui/command.tsx (1) |
| `min-h-12.5` | 1 | 1 | components/ui/tabs.tsx (1) |
| `h-[calc(100%-1px)]` | 1 | 1 | components/ui/tabs.tsx (1) |
| `h-0.5` | 1 | 1 | components/ui/tabs.tsx (1) |
| `h-2` | 1 | 1 | components/ui/progress.tsx (1) |
| `min-h-[var(--vf-hit-target)]` | 1 | 1 | components/ui/overflow-menu.tsx (1) |

### radius utilities

| Utility | Count | Files | Top files |
|---|---|---|---|
| `rounded-lg` | 54 | 19 | app/providers/page.tsx (18), app/try/page.tsx (8), app/models/models-client.tsx (6) |
| `rounded-full` | 33 | 16 | app/library/page.tsx (7), components/ui/avatar.tsx (4), components/shell/app-shell.tsx (3) |
| `rounded-md` | 31 | 20 | app/models/models-client.tsx (5), app/showcase/page.tsx (3), app/usage/page.tsx (2) |
| `rounded-control` | 20 | 10 | components/shell/app-shell.tsx (5), app/home/page.tsx (4), app/library/page.tsx (3) |
| `rounded-panel` | 19 | 6 | app/home/page.tsx (5), app/status/status-client.tsx (5), components/product/status-ui.tsx (4) |
| `rounded-xl` | 16 | 9 | app/providers/page.tsx (5), app/workflows/page.tsx (3), app/try/page.tsx (2) |
| `rounded-media` | 10 | 3 | app/library/page.tsx (4), app/home/page.tsx (4), app/try/workflow-media-input.tsx (2) |
| `rounded-sm` | 7 | 4 | components/ui/dropdown-menu.tsx (4), components/ui/checkbox.tsx (1), components/ui/select.tsx (1) |
| `rounded` | 5 | 3 | app/library/page.tsx (3), styles/legacy-components.css (1), app/models/models-client.tsx (1) |
| `rounded-xs` | 3 | 2 | components/product/uptime-bar.tsx (2), components/ui/tooltip.tsx (1) |
| `rounded-[inherit]` | 1 | 1 | components/ui/scroll-area.tsx (1) |
| `rounded-none` | 1 | 1 | components/ui/tabs.tsx (1) |

