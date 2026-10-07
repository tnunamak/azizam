# azizam-ux-states-20261007

Source-only cross-page state inventory with ten ranked exact-copy/behavior fixes, three delights and strengths. Main risks: silent provider subquery failures, ambiguous resubmission, stale receipts and silent preview retry. Empty diff; no tests.

Cross-page state review
Base: 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba

Verdict: Most pages have loading and empty-state components, but recovery is inconsistent. “Try again” can mean reload metadata, refresh a preview, or submit another real request. Missing information sometimes becomes an empty choice list. These source-derived risks undermine calm, honest family use.

Inventory
Paths are under web/src/app/. L = loading; E = empty; F = failure. “Unknown” means the UI should acknowledge missing attribution rather than blame the user.

| Page | L / E / F inventory | Judgment: happened / responsibility / next action |
|---|---|---|
| Root, page.tsx:20–38 | Named skeleton; sign-in teaching state; account retry | Clear task/action; failure cause unknown |
| Home, home/page.tsx:930–1021 | Home/recent skeletons; sign-in state; zero recent items hidden; separate retries | Good compartmentalization; generic check failure misdirects |
| Apps, apps/page.tsx:294–328 | Named skeletons; first-app/sign-in teaching states; retry | Clear entry action; no retry-busy feedback |
| Models, models/models-client.tsx:650–667,891–1001 | Tall-card skeleton; no-access and filter-empty branches; retry | Good filter recovery; no-model explanation conflates setup/access |
| Workflows, workflows/page.tsx:350–365,666–687,804–879 | Skeleton; no workflows/filter matches/recent jobs; scoped retry | Teaching mostly good; status reasons buried |
| Try, try/page.tsx:1536–1564,1629–1644,1830–1852 | Key progress/catalog notices; bare “Run…” empty; result skeleton/error | Request progress lacks context; retry resubmits |
| Library, library/page.tsx:1035–1148 | Named grid skeleton; first-use/filter empty; initial/retained/pagination errors | Strong list recovery; preview retry can fail silently |
| Usage, usage/page.tsx:243–300 | Skeleton; period/filter empty; retry | Teaching/action clear; refresh failure replaces known data |
| Activity, activity/page.tsx:242–244,302–395 | People/audit loading; teaching/filter empty; initial/refresh/page retries | Good scope separation; some retries lack busy state |
| Activity Requests, activity/requests.tsx:174–209,403–437 | Retained-data refresh/pagination handling; receipt drawer | Refresh distinction good; selected receipt can remain stale |
| Status, status/status-client.tsx:430–539,543–599 | Checking text; no engines; unknown/partial/errors | Honest uncertainty; missing checks and recovery insufficiently prominent |
| Engines, engines/page.tsx:600–678,778–794 | Skeleton fallback; none configured/in-use; registry/live-status errors | Separate failures good; hidden-engines empty CTA mismatches instruction |
| Services, services/page.tsx:1 | Re-exports Engines | Same states |
| Providers, providers/page.tsx:2027–2097 | Named skeleton; uninstalled/none; raw list error | Catalog/plan failures can masquerade as emptiness |
| Showcase, showcase/page.tsx:174–200 | Named skeleton; teaching empty + Try; retry | Good distinction from live availability; blames connection without evidence |
| Gallery, gallery/page.tsx:4–13 | Static relocation + destination | Clear; no asynchronous states needed |
| Design, design/page.tsx:137–189 | Explicitly non-live component examples | Preserve demonstration labeling |
| Not found, not-found.tsx:8–15 | Moved-page explanation + Go home | Clear recovery |

Ten ranked fixes, with exact copy

1. P1 — Providers: failed choices look empty.
providers/page.tsx:1904–1913,2027–2034,2073–2097.
“As an owner, I see no plans/providers to choose and assume none exist.” Silent dependency failure violates honest defaults. Track each query’s error, retain existing values, and block only dependent edits. Show “Access plans couldn’t be loaded. Your saved settings are unchanged.” Action: “Reload access plans.” For catalog: “Provider choices couldn’t be loaded.” / “Reload provider choices.”

2. P1 — Try: generic recovery can repeat paid work.
try/page.tsx:1836–1840.
“As a member, I press Try again expecting to check my result, but it submits the form again.” Recovery must describe its consequence. For an unconfirmed outcome: “We couldn’t confirm the result. The request may have run.” Offer receipt/outcome checking when supported. Label deliberate resubmission “Run again”; show “This sends another request and may incur another provider charge” when paid.

3. P1 — Receipt outcome can remain stale.
usage/page.tsx:173–179,293–307; activity/requests.tsx:174–183,204–209,403–437.
“As a builder, my open receipt still looks unfinished after the list updates.” Store selected ID and reconcile refreshed data. Show “Last checked [time]” and “Check latest outcome”; on failure, “Couldn’t update this receipt. Showing the last recorded outcome.” Accurate currentness is fundamental to trust.

4. P1 — Library preview retry swallows failure.
library/page.tsx:450–475.
“As a member, I retry a missing picture and get no explanation.” The rejected download-link request is discarded. Preserve the preview and display “Preview couldn’t be loaded. We haven’t confirmed whether the original is available.” Action: “Retry preview”; pending: “Reloading preview…” Do not infer deletion or guarantee download availability.

5. P2 — Home check assigns blame prematurely.
home/page.tsx:315–332,537–565.
“As a member, a failed check tells me to inspect my key even when the check never finished.” Replace generic blame with “We couldn’t complete the gateway check. Your key may still be valid.” Action: “Check gateway again.” Preserve verified failure categories; state beside success, “Gateway responded. Next, send a message in your app.”

6. P2 — Try’s running state does not say what is happening.
try/page.tsx:1813–1821,1830–1835,1849–1852.
“As a phone user, I see gray blocks and a Run label while waiting.” Show “Waiting for the model response · [elapsed]” and button “Running…”. Use “Queued” only with evidence. Empty copy: “Your result will appear here. Review the model and prompt, then choose Run.” Keep layout stable; do not invent a percentage.

7. P2 — Providers exposes raw failures at the wrong level.
providers/page.tsx:2073–2093,2141–2160.
“As an owner, a technical message at the page bottom leaves me unsure which change failed.” Put mutation feedback beside its control; retain inputs. Default: “We couldn’t confirm this change. Refresh provider settings before trying again.” Action: “Refresh provider settings.” Use “Nothing changed” only when confirmed. Move raw messages under Technical details.

8. P2 — Status names uncertainty without identifying the gap.
status/status-client.tsx:482–521,543–599.
“As an owner, ‘partially checked’ does not tell me what remains unknown.” Show observed components: “Status partly checked · provider availability unknown”; place “Refresh status” beside it, with “Last checked [time].” Never infer a component outage from a failed status read.

9. P2 — Engines’ empty action contradicts its explanation.
engines/page.tsx:661–678.
“As an owner, I’m told to show existing engines, but the only empty-state button adds another.” When configured entries are hidden: “Your engines are hidden by this view.” Action: “Show engines not in use,” toggling the existing filter. Reserve “Add engine” for genuinely empty configuration.

10. P2 — Refresh failures erase useful context inconsistently.
usage/page.tsx:243–289; home/page.tsx:998–1021; apps/page.tsx:294–310.
“As a returning user, a failed refresh replaces the information I was reading.” When same-scope cached data exists, retain it and show “Couldn’t refresh. Showing information from [time].” Action: “Refresh”; pending: “Refreshing…”. Never retain one person’s data under another person’s filter. Pass retrying through consistently.

Three delight opportunities
• A consistent recovery card: what failed, what remains usable, and one next action; an optional copyable help summary without request bodies or private values.
• Resumable state: keep filters, draft inputs and scroll position through reload/retry so recovery does not feel like starting over.
• Earned recovery acknowledgment: “Library refreshed · 3 new items” or “Gateway responding again,” based on observed changes, without celebratory interruption.

Keep
Named loading regions; Library and Activity’s retained-data/pagination distinction; filter-specific empty states; Status refusing “All systems normal” on incomplete evidence; Apps’ partial-creation explanation; Showcase’s explicit historical-versus-current distinction; shared ErrorState’s busy control and secondary technical details.

Evidence/limits
Direct permitted-source reads plus same-revision Models/Workflows, Status and completed Home/Apps/Usage peer evidence. Page-level inventory, not exhaustive runtime branch coverage. No excluded paths, shell, live calls, browser, patches, tests or checks. workspace_result verified empty diff (0 bytes), checks=[]. Pre-finish get: no cancellation, revocation or owner notes.
