# Vivid Fish Component Library Spec

This is the working contract for bringing the gateway UI to the designer's bar
without duplicating branding, design tokens, or component behavior across routes.

This is the canonical brief for shared UI components. Product information
architecture and presentation laws remain in
`docs/design/WEB_UI_STEERING.md`; older pass plans preserve implementation
history but are not competing requirements.

## Goal

Build one coherent, reusable Vivid Fish component system for the gateway:

- `@vivid-fish/brand` owns tokens, semantic CSS primitives, motion, focus,
  surfaces, row grammar, status grammar, and responsive shell layout.
- `web/src/components/ui/*` owns typed React wrappers and behavior over those
  primitives.
- route files own data selection, mutations, and page-specific composition only.

The screenshot grammar in the portable design handoff under
`docs/design/assets/reference-ui/*.png` is authoritative for density,
contrast, surface depth, rows, status bands, side panels, activity feeds, and
mobile navigation when that evidence is useful. Marketing slogans and SaaS
positioning from the screenshots are not product requirements. Portable
requirements are the contracts and rules in this document and in
`WEB_UI_STEERING.md`; see the asset README for the normative/illustrative
boundary.

## Prior-Art Conclusions

High-quality systems converge on the same architecture:

- Design tokens are the named source of truth for visual decisions. Atlassian
  describes tokens as the single source of truth for naming and storing design
  decisions; Material defines tokens as reusable design decisions that form a
  visual style; Shopify Polaris exposes tokens both through layout component
  APIs and CSS variables; Primer keeps color, typography, and spacing primitives
  in a package used by GitHub's design system.
- Low-level component behavior should not force visual style. Radix Primitives
  explicitly position themselves as accessible, unstyled building blocks for
  design systems, while Radix Themes uses vanilla CSS variables rather than
  framework-specific styling.
- Component libraries need isolated verification. Storybook's docs frame stories
  as isolated component development and testing, with accessibility and visual
  test addons available. We do not need to add Storybook in this task, but we
  should structure components so that adding stories later is straightforward.
- Accessibility rules are not optional library polish. WAI-ARIA combobox
  guidance applies to command/search affordances, and WCAG target-size guidance
  requires pointer targets to be reliably activatable, especially on mobile.

Useful references for workers:

- Atlassian Tokens: https://atlassian.design/foundations/tokens/
- Material Design Tokens: https://m3.material.io/foundations/design-tokens
- Shopify Polaris Tokens: https://polaris-react.shopify.com/tokens/color and
  https://polaris-react.shopify.com/design/layout/layout-tokens
- Primer Primitives: https://github.com/primer/primitives
- Radix Primitives: https://www.radix-ui.com/primitives
- Radix styling model: https://www.radix-ui.com/primitives/docs/guides/styling
- Storybook accessibility testing:
  https://storybook.js.org/docs/writing-tests/accessibility-testing
- WAI-ARIA Combobox pattern: https://www.w3.org/WAI/ARIA/apg/patterns/combobox/
- WCAG 2.2 target size:
  https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html

## Non-Negotiables

- No route-local palette, radius, shadow, typography, or spacing decisions when a
  brand token or primitive can express the intent.
- No component should hardcode service/provider semantics. Components accept
  generic labels, statuses, counts, protocols, capabilities, and actions from the
  API layer.
- No fake sparklines, decorative metrics, or dead controls. If a chart has no
  real data, render an honest empty state or omit the chart.
- No raw secrets. One-time secrets may be shown only in reveal/copy flows that
  already have a real secret value in memory.
- No raw IDs as primary text. IDs may appear in secondary metadata, copy blocks,
  debug disclosures, or audit details.
- No horizontal overflow at 390px wide. Interactive targets should use the
  existing `--vf-hit-target` 44px baseline unless the component is non-pointer
  text.
- Route files should not accumulate Tailwind "soup" for reusable patterns.
  Promote repeated visual structure into `@vivid-fish/brand` first, then React.

## Component Layers

### Layer 1: Brand CSS

Add or extend framework-agnostic classes in the source package exported as
`@vivid-fish/brand`, using its package-relative `src/components.css`.

The local checkout used by one deployment is implementation evidence, not a
portable path requirement. A consumer may provide the package through a local
path, workspace, registry, or vendored checkout as long as the package
contract remains the same.

Brand classes must:

- use existing `--vf-*` tokens;
- introduce new tokens only for repeated semantic needs;
- work without React;
- carry responsive behavior when it is part of the visual primitive;
- avoid one-off page names;
- have clear names that describe UI roles, not current route names.

### Layer 2: React Wrappers

Add typed wrappers in `web/src/components/ui/`.

Wrappers must:

- compose brand classes rather than re-implementing visual style;
- expose stable, data-shaped props;
- forward `className` only for layout/rare overrides;
- preserve accessible names and keyboard behavior;
- avoid fetching data internally;
- be exported from `web/src/components/ui/index.ts`.

### Layer 3: Route Adoption

Routes should:

- use shared wrappers for shell, panels, rows, tables, feeds, copy, keys, and
  status;
- keep API calls, mutations, derived page state, and empty/loading/error
  decisions local;
- not invent new visual primitives in page files.

## Component Contracts

### CommandSearch

Purpose: top-bar search/command entry for fast navigation and later command
actions. In the current product, it may launch search/navigation only where
real actions exist. It must not be a dead decorative search box.

Brand primitives:

- `.vf-command-search`
- `.vf-command-search-input`
- `.vf-command-search-kbd`
- `.vf-command-menu`
- `.vf-command-item`
- `.vf-command-item-meta`

React wrapper:

```ts
type CommandSearchItem = {
  id: string;
  label: string;
  description?: string;
  href?: string;
  onSelect?: () => void;
  group?: string;
};
```

Behavior:

- If no items are passed, render a compact disabled "Search unavailable" affordance
  or omit it based on route needs.
- If items are passed, use an accessible combobox/listbox pattern or an existing
  accessible primitive. Do not hand-roll incomplete ARIA.
- `Cmd/Ctrl+K` is optional only if implemented globally and discoverable by the
  component; otherwise do not show a keyboard hint.
- Mobile may render as an icon button that opens a sheet with the same items.

Acceptance:

- no dead search box;
- keyboard focus, escape, arrow navigation, and enter selection work if menu is
  interactive;
- route files pass data; component does not know app route internals beyond
  generic item labels/hrefs.

### SystemHealthBand

Purpose: first-row operational summary for admin overview/status surfaces. It
answers "is anything down, degraded, queued, or blocked?"

Brand primitives:

- `.vf-health-band`
- `.vf-health-band-main`
- `.vf-health-band-grid`
- `.vf-health-metric`
- `.vf-health-metric-value`
- `.vf-health-metric-label`

React props:

```ts
type HealthTone = "ok" | "warn" | "bad" | "info" | "neutral";
type SystemHealthBandProps = {
  tone: HealthTone;
  title: string;
  detail?: string;
  metrics: Array<{ label: string; value: string; detail?: string; tone?: HealthTone }>;
  action?: ReactNode;
};
```

Rules:

- Show only real metrics from API/state.
- Do not show decorative uptime/latency unless those values are real.
- On mobile, collapse to title/detail plus a two-column metric grid.

### IncidentBanner

Purpose: actionable attention state for degraded services, failed jobs,
misconfiguration, or policy blockers.

Brand primitives:

- `.vf-incident-banner`
- `.vf-incident-banner-title`
- `.vf-incident-banner-body`
- `.vf-incident-banner-actions`

React props:

```ts
type IncidentBannerProps = {
  tone: "warn" | "bad" | "info";
  title: string;
  detail?: string;
  actions?: ReactNode;
  onDismiss?: () => void;
};
```

Rules:

- "Needs attention" without a cause and action is forbidden.
- Prefer specific copy: "2 services disabled", "Embeddings backend unavailable",
  "OIDC role mapping has no matching profile".
- If no action exists, omit the action slot.

### TelemetryCard and MiniSparkline

Purpose: compact operational telemetry cards like the screenshots' status
metrics. These are data displays, not decoration.

Brand primitives:

- `.vf-telemetry-card`
- `.vf-telemetry-card-head`
- `.vf-telemetry-value`
- `.vf-telemetry-detail`
- `.vf-sparkline`

React props:

```ts
type MiniSparklinePoint = { x: number; y: number };
type TelemetryCardProps = {
  label: string;
  value: string;
  detail?: string;
  tone?: HealthTone;
  series?: MiniSparklinePoint[];
};
```

Rules:

- Render a sparkline only when there are at least three real points.
- No generated random series.
- If data is absent, render value/detail only or omit the card.
- SVG must be `aria-hidden` unless it carries meaningful text elsewhere.

### ServiceHealthRow

Purpose: service/provider/workflow row that shows what is enabled, reachable,
allowed, and routable.

Brand primitives:

- `.vf-service-row`
- `.vf-service-row-main`
- `.vf-service-row-status`
- `.vf-service-row-metrics`
- `.vf-service-row-actions`

React props:

```ts
type ServiceHealthRowProps = {
  name: string;
  description?: string;
  status: HealthTone;
  statusLabel: string;
  meta?: string[];
  metrics?: Array<{ label: string; value: string }>;
  actions?: ReactNode;
  href?: string;
};
```

Rules:

- It represents a generic service/endpoint/workflow record, not a hardcoded
  provider.
- On mobile, actions wrap below metadata; the title remains single-column.
- It must work inside a list and inside a right-context panel.

### AppAccessRow

Purpose: app/key/profile/user access row for Apps & Access surfaces.

Brand primitives:

- `.vf-access-row`
- `.vf-access-row-identity`
- `.vf-access-row-badges`
- `.vf-access-row-usage`
- `.vf-access-row-actions`

React props:

```ts
type AppAccessRowProps = {
  name: string;
  owner?: string;
  profile?: string;
  keyCount?: number;
  lastUsedLabel?: string;
  status?: HealthTone;
  statusLabel?: string;
  actions?: ReactNode;
  href?: string;
};
```

Rules:

- "Never used" should render as an idle state only when usage data genuinely
  says no usage, not when tracking is unavailable.
- If usage tracking is unavailable, say "Usage unavailable" in muted metadata.
- Secret values are never row content.

### RightContextPanel

Purpose: selected-object context panel for desktop, rendered as a sheet on
mobile. It gives details/actions without forcing route navigation.

Brand primitives:

- `.vf-context-panel`
- `.vf-context-panel-head`
- `.vf-context-panel-section`
- `.vf-context-panel-actions`

React props:

```ts
type RightContextPanelProps = {
  title: string;
  subtitle?: string;
  status?: ReactNode;
  actions?: ReactNode;
  sections: Array<{ title?: string; content: ReactNode }>;
};
```

Rules:

- Do not show an empty panel. If nothing is selected, show an intentional
  `EmptyOperationalState` with the next useful action.
- Mobile uses `Sheet` or a full-width section, not a squeezed side column.

### ActivityFeedRow

Purpose: audit/recent activity rows with timestamp, actor, object, result, and
optional latency/code.

Brand primitives:

- `.vf-activity-feed`
- `.vf-activity-row`
- `.vf-activity-row-mark`
- `.vf-activity-row-meta`
- `.vf-activity-row-stat`

React props:

```ts
type ActivityFeedRowProps = {
  title: string;
  detail?: string;
  actor?: string;
  time?: ReactNode;
  tone?: HealthTone;
  stat?: ReactNode;
  href?: string;
};
```

Rules:

- Use human-readable phrases as primary text.
- Raw JSON belongs behind details/disclosure, not the row.
- Works for audit log and overview recent activity.

### CopyField

Purpose: one-line copyable values such as base URL, model name, curl one-liner,
or public endpoint.

Brand primitives:

- `.vf-copy-field`
- `.vf-copy-field-label`
- `.vf-copy-field-value`
- `.vf-copy-field-button`

React props:

```ts
type CopyFieldProps = {
  label?: string;
  value: string;
  copyValue?: string;
  description?: string;
  monospace?: boolean;
};
```

Rules:

- Copy state must not change layout or overflow the button. The button width is
  stable for "Copy" and "Copied".
- Long values truncate visually but copy the full value.
- On mobile, label/value/action stack only when necessary.

### KeyRevealBlock

Purpose: one-time API key display and masked-key management.

Brand primitives:

- `.vf-key-reveal`
- `.vf-key-reveal-value`
- `.vf-key-reveal-actions`
- `.vf-key-fingerprint`

React props:

```ts
type KeyRevealBlockProps = {
  secret?: string;
  fingerprint?: string;
  label?: string;
  canCopy?: boolean;
  onCopy?: () => void;
};
```

Rules:

- When `secret` exists, show copy and explicit one-time language.
- When only fingerprint exists, never imply the full secret is recoverable.
- Avoid modal-only key management; list rows can link into context panel.

### FilterToolbar

Purpose: reusable filter/search/sort toolbar for tables/lists.

Brand primitives:

- `.vf-filter-toolbar`
- `.vf-filter-group`
- `.vf-filter-chip`

React props:

```ts
type FilterToolbarProps = {
  search?: ReactNode;
  filters?: ReactNode;
  sort?: ReactNode;
  actions?: ReactNode;
};
```

Rules:

- Wrap cleanly on mobile; no horizontal scroll toolbar.
- If there are no filters, omit the filter group entirely.
- Keep controls real; no disabled-looking placeholders unless they explain why.

### StatusDotLabel

Purpose: reusable small status indicator for rows, nav, health, and filters.

Brand primitives:

- existing `.vf-status-dot`
- `.vf-status-label`

React props:

```ts
type StatusDotLabelProps = {
  tone: HealthTone;
  label: string;
  pulse?: boolean;
};
```

Rules:

- Color must not be the only signal; label text is required.
- Pulse only for live/reachable state and must respect reduced motion.

### MobileTopRail and MobileActionDock

Purpose: mobile-first shell/navigation and primary action placement.

Brand primitives:

- existing `.vf-mobile-tabbar`
- `.vf-mobile-top-rail`
- `.vf-mobile-action-dock`

Rules:

- Bottom nav remains primary app navigation on phone.
- Top rail shows brand/context and one account/menu affordance, not a desktop
  topbar squeezed down.
- Action dock is for route-primary actions only; do not duplicate every desktop
  button.
- Nothing in the dock may cover focused form fields or the tabbar.

### EmptyOperationalState

Purpose: honest empty/loading/error state for operational surfaces.

Brand primitives:

- `.vf-empty-operational`
- `.vf-empty-operational-title`
- `.vf-empty-operational-body`
- `.vf-empty-operational-actions`

React props:

```ts
type EmptyOperationalStateProps = {
  title: string;
  detail?: string;
  action?: ReactNode;
  tone?: "neutral" | "info" | "warn" | "bad";
};
```

Rules:

- No generic "Nothing here" if the next step is knowable.
- Empty sections should be hidden unless the absence itself is important.
- Loading skeletons should reserve stable dimensions.

### SectionActionHeader

Purpose: compact reusable section heading with optional kicker, count, and
action. Replaces page-local header rows and horizontal-rule clutter.

Brand primitives:

- existing `.vf-section`
- existing `.vf-section-head`
- `.vf-section-body`

React props:

```ts
type SectionActionHeaderProps = {
  title: string;
  kicker?: string;
  count?: string | number;
  description?: string;
  action?: ReactNode;
};
```

Rules:

- Do not repeat page headings.
- Use description only when it changes user action.
- Count is secondary metadata, never the headline.

### QuickStartChecklist

Purpose: only for first-run or incomplete setup, not for daily admin overview.

Brand primitives:

- `.vf-checklist`
- `.vf-checklist-item`
- `.vf-checklist-item-state`

React props:

```ts
type QuickStartChecklistProps = {
  items: Array<{
    id: string;
    label: string;
    detail?: string;
    complete: boolean;
    action?: ReactNode;
  }>;
};
```

Rules:

- Hide the whole checklist when all items are complete unless the route is a
  setup route.
- Each incomplete item needs a real action or a specific reason it cannot be
  completed in UI.
- No marketing onboarding copy.

## Implementation Batches

### Batch A: Brand Primitives

Package sources:

- `@vivid-fish/brand/src/tokens.css`
- `@vivid-fish/brand/src/theme.css`
- `@vivid-fish/brand/src/components.css`
- `@vivid-fish/brand/README.md`

These are package-relative source locations. The dependency's checkout
location is deployment-specific.

Deliver:

- CSS classes named in this spec.
- Any new tokens required for repeated, semantic visual decisions.
- No React code in the brand package unless the package already has a React
  entry point.
- README section explaining the layer contract and "no duplicated tokens" rule.

### Batch B: React Wrappers

Files:

- `web/src/components/ui/surface.tsx`
- new small component files only if `surface.tsx` becomes hard to read
- `web/src/components/ui/index.ts`

Deliver:

- Typed wrappers for the component contracts above.
- Stable copy button behavior in `CopyField`.
- No internal fetching.
- Existing wrappers reused where possible instead of renamed duplicates.

### Batch C: Route Adoption

Files:

- `web/src/app/page.tsx`
- `web/src/app/admin/layout.tsx`
- `web/src/app/admin/page.tsx`
- `web/src/app/admin/apps/page.tsx`
- `web/src/app/admin/services/page.tsx`
- `web/src/app/admin/profiles/page.tsx`
- `web/src/app/admin/users/page.tsx`
- `web/src/app/admin/audit/page.tsx`
- `web/src/app/admin/settings/page.tsx`

Deliver:

- Overview/status use `SystemHealthBand`, `IncidentBanner`, `TelemetryCard`,
  `ActivityFeedRow`, and `EmptyOperationalState`.
- Apps/users/profiles use `AppAccessRow`, `FilterToolbar`, `RightContextPanel`,
  `KeyRevealBlock`, and `CopyField` where applicable.
- Services use `ServiceHealthRow`, `StatusDotLabel`, `RightContextPanel`, and
  no service-specific wizard language.
- Audit uses `ActivityFeedRow` and compact filters.
- `/ui/` entry surface is simplified and visually aligned; no marketing hero.
- Empty sections are hidden unless operationally meaningful.

### Batch D: UX Gate

No implementation ownership. Review only.

Deliver:

- Desktop and mobile screenshot comparison against
  `docs/design/assets/reference-ui/*.png`.
- Overflow check at 390x844 and 1440x1000.
- Accessibility sanity check for target sizes, focus states, keyboard search,
  and copy/reveal controls.
- Report any remaining gaps as concrete route/component defects, not vibes.

## Verification

Required before reporting done:

- `cd web && npm run check`
- `./venv/bin/python -m pytest -q`
- authenticated Darshana desktop/mobile screenshots for all required routes
- visual inspection of desktop and mobile contact sheets
- grep affected files for duplicated hardcoded visual values:
  - hex colors in `web/src`
  - arbitrary `shadow-[...]`, `rounded-[...]`, `border-[#...]`, `bg-[#...]`
  - repeated page-local row/panel classes that should be brand primitives

## Success Criteria

- The UI looks like one product across `/ui/` and every admin route.
- Route code is smaller or more declarative where visual structure was repeated.
- New visual language is in `@vivid-fish/brand`, not copied into pages.
- Components are flexible across desktop/mobile without page-specific hacks.
- Product complexity remains honest: no fake metrics, no dead controls, no
  provider-specific UI assumptions disguised as "helpful" shortcuts.
