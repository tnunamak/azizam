# Vivid Fish reference UI

These PNGs are a user-provided design handoff, curated from the prior
untracked staging set on 2026-08-11. The stable filenames describe the page,
viewport, and state. The image bytes are preserved unchanged; no audit
screenshots or generated replacements belong here.

## Curated references

| Durable file | Source filename | Page/state and visual classification |
| --- | --- | --- |
| `portal-overview-mobile-welcome.png` | `158ec959-b549-4db2-b8f0-e9df9cce287a.png` | Mobile portal overview; welcome/marketing hero, app list, recent activity, health strip, and bottom navigation. |
| `portal-overview-mobile-quickstart.png` | `5ea319de-75c2-4437-8295-66475d076f9c.png` | Mobile portal overview; expanded quickstart, operational metric cards, app/activity lists, base URL, and bottom navigation. |
| `admin-overview-desktop-quickstart.png` | `567c0dcf-1a6b-40cf-8ea8-ba6ad470da18.png` | Desktop admin overview; sidebar/top rail, expanded quickstart, telemetry cards, apps, recent activity, base URL, and health. |
| `admin-overview-desktop-welcome.png` | `64307958-8aac-49b9-9c4a-2d58160a458b.png` | Desktop admin overview; fish welcome hero, horizontal four-step quickstart, app table, system health, and recent activity. |
| `admin-status-desktop-incident-table.png` | `3daf4110-cd28-4212-a52d-a7238c85f30e.png` | Desktop status surface; operational banner, metric cards, active-incident panel, service table, uptime chart, and incident list. |
| `admin-status-desktop-hero.png` | `cc38aa61-be22-4a7f-92b7-03a09f79aa32.png` | Desktop status surface; prominent status hero with fish artwork, incident summary, service table, uptime chart, and recent incidents. |
| `admin-apps-access-desktop-selected-detail.png` | `e872d0e1-dc06-46c7-a2e5-cc68644963e2.png` | Desktop Apps & Access; filtered list/table with a selected row, right context panel, summary, scopes, linked keys, and activity. |

## Excluded duplicate

`965e9223-7115-4f46-8122-334979d1c3cf.png` is not copied. It repeats the
same Apps & Access desktop selected-detail layout as
`admin-apps-access-desktop-selected-detail.png`; its shorter list and
different sample metrics are illustrative data, not a distinct visual state.

## Normative vs illustrative

Normative visual grammar is the reusable direction shown across the set:
dark, dense, high-contrast operations surfaces; deep-water backgrounds;
elevated panels with hairline dividers; restrained cyan/blue accents; green,
amber, and red status signals with text labels; compact top status/account
clusters; desktop sidebar navigation; mobile bottom navigation; operational
rows, status bands, metric cards, activity feeds, incident treatment, and
desktop right-context panels that become a mobile sheet or full-width section.

Illustrative only are screenshot copy and claims, marketing language, pricing
or enterprise positioning, names, IDs, endpoints, secrets or secret-like
values, timestamps, counts, uptime/latency/error metrics, service/provider
names, chart data, exact dimensions, pixel measurements, UUID source names,
and any control that is not backed by a current product contract. Product
requirements remain in [`../../WEB_UI_STEERING.md`](../../WEB_UI_STEERING.md)
and [`../../VIVID_FISH_COMPONENT_LIBRARY_SPEC.md`](../../VIVID_FISH_COMPONENT_LIBRARY_SPEC.md).
