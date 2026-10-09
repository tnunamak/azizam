# Redesign audit, 2026-10-09

Scope: `design/full-surface` at 542c95a against `main` at 88caf44. Read-only on both branches. All runs used throwaway worktrees under `~/.tmp/redesign-audit/` (now removed) and ports 18477 to 18517. Nothing touched port 5000 or the live gateway.

## Bottom line

1. **The "73 Playwright regressions" are gone.** The designer fixed them in D1.1/D1.2 follow-up commits (`03aba73`, `feaa0d8`, `e7d041b`, `5c4a352`, `542c95a`). On the 612-test `playwright.config.ts` suite the branch fails 15 and main fails 16. **All 15 branch failures also fail on main** (matched by spec file and title; checked with a rerun on both). The one test that fails on main only (`owner-alert: global owner alert: down`) passes on the branch.
2. **Two real gaps remain on the branch that the main-config suite does not show:**
   - The separate live-data suite has 1 branch-only failure (`live-data-scoping.spec.ts:19`). It is a stale locator for a deliberate change: the Library filters moved into a "Filters" popover, and "Owner" became "Person". The filter is still there.
   - The hermetic Python suite has 1 branch-only failure: `PRODUCT.md:47` mentions "Vivid Fish", and the legacy-name guard only allows that under `docs/design/`. The file is at the repo root.
3. **Merge with main is textually clean** (one auto-merge in `web/src/app/status/member-status.spec.tsx`, no conflicts). The merged tree is also clean on every check I ran: `tsc` 0 errors, vitest 521/521 plus all 11 per-folder configs, e2e identical to the branch (15 failures, the same 15), hermetic Python 5703 pass with the same single `PRODUCT.md` failure.
4. **D1.2 is mostly done.** Items 1 (audio), 2, 4 (as a document), 5 and 6 landed. Item 3 is partial: the household identity part was not started. Video posters were already working before D1.2, but they sit below the fold in the default fixture. No D1.2 report, no per-item status and no round-3 judging exist, and the contact-sheet captures are older than the last UI commits.

## Test commands

Setup, per worktree: `git worktree add --detach ~/.tmp/redesign-audit/{branch,main} <sha>`, then `npm ci --prefer-offline` and `npm run build` in `web/` (the gateway serves the static export in `web/out`).

Browser: `@playwright/test` 1.62.1 needs chromium build 1234. The host had only 1243, so I ran `npx playwright install chromium` once. That adds `~/.cache/ms-playwright/chromium_headless_shell-1234` and `chromium-1234`, which I left in place.

E2E (what `npm run test:e2e` runs: main config, then live-data config):

```sh
cd <worktree>/web
UI_E2E_PORT=<18477|18487> UI_E2E_PYTHON=/home/tnunamak/applications/azizam/venv/bin/python \
  UI_E2E_STATE_ROOT=~/.tmp/redesign-audit/state-<name> \
  PLAYWRIGHT_JSON_OUTPUT_NAME=<result>.json \
  npx playwright test --config playwright.config.ts --reporter=json,line     # 612 tests, workers=1, no retries
UI_E2E_PORT=<free port> UI_E2E_PYTHON=... npx playwright test --config playwright.live-data.config.ts   # 4 tests
```

The harness `scripts/ui/serve.py` starts an isolated gateway and fake engine on PORT and PORT+1 from the worktree's code (`PYTHONPATH=<worktree>`). I used the production checkout's `venv/bin/python` read-only as the interpreter; the worktrees have no venv. Run the main and branch suites one after the other: engine tests bind fixed ports 18690 to 18710.

Python: `scripts/run-hermetic-pytest.sh -q` in a clean, committed worktree (it archives HEAD, builds a venv from the lock file, and runs under bwrap with no network).

Frontend: `cd web && npx tsc --noEmit -p . && npx vitest run --config vitest.config.ts`, plus `npx vitest run --config <each src/**/vitest.config.ts>` (11 configs). I ran these on the merged tree only. It contains every branch file, so a branch-only pass is implied except where the merge itself matters.

Merge: `git merge-tree --write-tree 542c95a 88caf44` (exit 0), then a throwaway `git merge --no-commit` plus a detached commit in a third worktree for the checks above.

## Results

| Run | Pass | Fail | Flaky |
|---|---|---|---|
| e2e `playwright.config.ts`, **main** 88caf44 | 596 | 16 | 0 |
| e2e `playwright.config.ts`, **branch** 542c95a | 597 | 15 | 0 |
| e2e `playwright.config.ts`, **merged tree** | 597 | 15 (the same 15) | 0 |
| e2e live-data config, main | 4 | 0 | 0 |
| e2e live-data config, branch | 3 | 1 | 0 |
| hermetic pytest, main | 5704 | 0 | |
| hermetic pytest, branch | 5628 | 1 | |
| hermetic pytest, merged tree | 5703 | 1 (same test) | |
| tsc, vitest main config + 11 configs, merged tree | 521 + 11 configs, all pass | 0 | |

Flake check: I reran the four failing spec files (`library`, `status-engines-services`, `usage`, `owner-alert`) once on main and on the branch. Same failures, in the same places, on both. The live-data failure failed twice. Nothing is flaky. The earlier triage note of "1 flaky on main" did not repeat.

Hermetic counts differ because main has the backend lane's tests (honesty-data, drift checker) that the branch lacks.

## Failure classification

"Both" means it fails on main and on the branch. Evidence is the first failing assertion.

| Spec (test) | Branch | Main | Class | Evidence |
|---|---|---|---|---|
| `status-engines-services.spec.ts:402` engines error, 4 variants (light/dark, 1440/390) | fail | fail | **Both, pre-existing, stale test** | Expects text "Engines unavailable". No `src/app` page renders it. Main commit `4229fd6` ("stop blaming engines ... without evidence") removed it. Only `engine-gate.e2e.ts` and the spec still use it. |
| `status-engines-services.spec.ts:402` services error, 4 variants | fail | fail | Both, pre-existing, stale test | Same assertion and cause. |
| `status-engines-services.spec.ts:637` services compatibility route is owner gated | fail | fail | Both, pre-existing | The owner-gate sentence is still in `engines/page.tsx:782`, so the route or redirect behaviour changed. The cause was not isolated. |
| `usage.spec.ts:218` usage labels unavailable remote spend honestly | fail | fail | Both, pre-existing, stale test | Test expects "No paid requests". Page now shows "Unavailable" (`37d6045`: "an unknown amount never reads as zero or no charge"). The code is correct; change the assertion to "Unavailable". |
| `library.spec.ts:93` seeded, 4 variants | fail | fail | **Both, but the failures differ.** Pre-existing on main; on the branch it is also stale because of D1 changes | Main fails at the Download assertion (strict-mode violation, 2 "Download" links; the viewer shows two). Branch fails earlier at `getByText("A clean product render")`: tiles carry no visible text by design (DESIGN principle 8; the tile has an aria-label). I patched the branch spec locally to get past that (not kept): next it fails on `input[placeholder="Search prompts"]` (now "Search your creations", D1.2 item 2), then on `getByRole("link", {name: "Download"})` (not found as a link). Three layers of stale assertions; the real product check (opening a creation, Download href) has not run on the branch. |
| `library.spec.ts:242` (main: `:232`) filters dates and everyone, pages forward | fail | fail | **Both, pre-existing; branch adds a masking stale step** | Main: `getByRole("link", {name: "Open in ComfyUI"})` is gone (renamed "Download ComfyUI", main commit `b3a60eb`). Branch: times out earlier at `getByLabel("Date")` and `getByLabel("Owner")`: Date, Model and App now live in the "Filters" popover and Owner became the "Person" combobox. Intended change; test not updated. |
| `owner-alert.spec.ts:11` global owner alert: down | **pass** | fail | Fixed on the branch | Main still finds "Disk is full" where the test expects it gone. Branch commit `5c4a352` changed the test (the disk check reads the banner page) and `6a3309a`/`1c6f039` changed the alert. |
| `live-data-scoping.spec.ts:19` owner sees keyless LAN and service app media and usage (live-data config) | **fail (2 of 2 runs)** | pass | **Branch-only: intended change, test not updated** | Times out at `getByRole("combobox", {name: "Source"})`. The App/Source filter still exists (`library/page.tsx:1144`, owner view only) but is inside the "Filters" popover; the test also uses "Owner" where the UI says "Person". Commits `2ca3aa9` (one grid, person filter) and `b915323`/`44b2304`. Behaviour preserved; fix is the spec. |
| `PRODUCT.md:47` via `tests/test_legacy_name_boundary.py::test_every_tracked_legacy_token_has_an_explicit_exception` (hermetic) | **fail** | pass | **Branch-only: real regression against a repo guard** | `b19d2ac` added `PRODUCT.md` at the repo root with "Vivid Fish". The guard exempts `docs/design/`, not the root. The design brief calls the file `docs/design/PRODUCT.md`. Moving it fixes it. |

Counts: 15 fail on both, 0 e2e-main-config fail on the branch only, 1 fixed on the branch, 2 branch-only failures in the other suites (live-data, hermetic).

None of the 73 earlier regressions remain. I did not re-derive which of the 73 commits fixed which, only that none fail now.

## D1.2 status

Brief: `~/Documents/azizam/briefs/d1.2-fixes.md`. D1.2 commits are the ones from `b915323` (07:49) to `542c95a` (10:32) on 10-08. The designer's contact sheet is `docs/design/D1-CONTACT-SHEET.md`. Its final captures are `~/.tmp/azizam-redesign/d1-8/`.

| # | Item | Status | Evidence |
|---|---|---|---|
| 1 | Media tiles that identify themselves | **Done for audio. Video was already acceptable and was left alone; partial against the "above the fold" complaint.** | Audio: `b915323` (`components/product/audio-cover.tsx`, `lib/audio-peaks.ts`) gives each clip a cover with the maker's tint, kind ("Voice"), the clip's words, a real waveform and its length. `4df1de0` does the same for the viewer. `ee9b0cc` adds `web/tests/components/audio-cover.spec.tsx` (peaks drawn only when measured). Capture `default_member_library_populated_light_1440.png` shows it works. Video: tiles show the poster with play pill and length (viewed in the capture). The contact sheet says "unchanged: posters load". The fixture has one video, grouped under "Week of Oct 12" below the fold, so a judge's viewport shot still shows only images and audio. The brief's "find out why posters are hidden" was answered only as "below the fold". DESIGN backend ask 8 (posters listed as separate assets) is still open. |
| 2 | One search | **Done** | `b915323`: sidebar entry is now "Go to…" (opens the command palette); the Library page search ("Search your creations") is the only search box. Updated `shell.spec.ts`, `app-shell.*` tests. DESIGN.md principle in the Shell section records it. Shell e2e passes. |
| 3 | Phone Home that is not a generic list | **Partial** | Done: one 56 px row grammar, same heights, no filler subtitles (reason on the right: state or "Used 39m ago") (`45feacc`, `31f4662`); "Connect an app" is a header button on phones that jumps to the panel (`b915323`, `home/page.tsx:1273`; panel is in the right column on desktop). Not done: **household identity.** Home's header is the title "Home", a verdict line and a button (`home/page.tsx:1273-1310`). There is no household or owner name and no person marks on Home beyond the avatar in the top bar. The 390 px capture still reads as icon, title, chevron rows in one card. The brief's "bring back the household identity ... without the template tile grid" has no commit. `owner_name` now exists on main but the branch does not use it. |
| 4 | Craft pass with benchmarks, and a craft diff | **Done as a written pass; not independently verified** | Craft diff for Home, Status, Library plus coherence notes is in `D1-CONTACT-SHEET.md` (`31f4662`, `ee9b0cc`). It records measured sizes and changes (row 56 px, section heads 16 px, stroke 1.75, status card layout, one 896 px column, legend under the uptime bar, library header spacing). Caveats: the `d1-8` captures were written at 08:16, before `31f4662` (task columns), `d6b2ee4`, `4df1de0` and `07b69f1`; the "final D1.2 build" gate results therefore describe an earlier build. The sheet itself admits owner Status reads 83 words against a 60-word budget. No judge has seen D1.2. |
| 5 | Coherence across the three surfaces | **Mostly done** | One list-row anatomy shared by Home tasks and Status capability lists; one header rhythm (PageHeader); lucide stroke 1.75; shared empty, loading and error components (`45feacc`, `status-ui.tsx`). Library's header rhythm changed to match (`b915323`). Claimed in the sheet; I did not measure it. |
| 6 | Keep what is winning | **Done** | Status problem card, state vocabulary, freshness rules and honest copy kept; vitest 521 plus 11 configs pass on the merged tree, including the honesty tests (`capability-state.spec.ts`, `status-signal-defects.spec.tsx`, `member-status.spec.tsx`). `6a3309a` keeps provider errors visible when folded. |

Other D1.2-era commits: `07b69f1` (link colour for primary text, a11y), `e7d041b`, `feaa0d8`, `03aba73`, `5c4a352`, `542c95a` (e2e specs follow the redesign), `b0e3a71` (owner with nothing set up gets "Add an engine").

Backend fields from main that the branch does not yet use: `owner_name`, `plan_spend`, `refusal {code, limit}`, evidence `last_attempt`/`last_success` and check-time semantics, 503-as-unavailable. The generated types (`web/src/lib/product-api-types.ts`) already include them after the merge.

## Merge conflicts

- `git merge-tree --write-tree 542c95a 88caf44`: exit 0, no conflicts. Merge base `94f3c85`; main has 15 commits the branch lacks.
- One auto-merge: `web/src/app/status/member-status.spec.tsx`.
- Merged tree: `tsc` clean, vitest all green, e2e same 15 failures as the branch (no new failures), hermetic Python the same one `PRODUCT.md` failure. A merge does not lose or add behaviour that the suites can see. Visual or runtime interaction between the new backend fields and the redesigned Home and Status was not looked at.

## Found after the runs: D1.2 is already being continued elsewhere

At cleanup, `git worktree list` showed a locked agent worktree on branch `design/d12-continue` (HEAD 8870894, `.claude/worktrees/agent-a57bf4c242b6a7aa4`). It is 8 commits ahead of 542c95a (542c95a is its ancestor). I did not test it and its commits are not in the numbers above. Titles, oldest first: `9b4eba5` video tile without a poster asks Safari for its first frame; `1995d88` audio covers on deep water in the maker's hue; `1a21030` each Home task row leads with what this person last made with it; `6198c0d` phone Library filters in two lines; `e10f992` craft pass on phone callouts, health legend, audio scrubber; `1fd1660` Home loading skeleton matches the page; `3a65385` audio tile play control clickable again; `8870894` docs: "poster root cause". That is likely the cut-off designer's continuation, and it touches items 1, 3 and 4. Decide whether to restart from it before assigning anything above. Run the same two suites on it first; its spec updates would also change the stale-spec list. `main` has also moved from 88caf44 to 42c6a32 (TTS and lifecycle commits, no web changes seen in the log titles); my merge result is for 88caf44.

## Environment notes for the lead

- Another agent is active: processes from `~/applications/azizam-wt-redesign` (state `~/.tmp/d12c/state/`, port 18477 and 18478) and one under `~/applications/azizam/.claude/worktrees/agent-a57bf4c242b6a7aa4` (18492/18493) were listening while I ran. I did not touch them. Use ports above 18497 or check `ss -ltn` before starting a suite.
- The main checkout `data/` has two untracked files (`.litellm.config.yaml.*.tmp`, `litellm.pid.runtime-hash`) and `.playwright-mcp/`. I did not create them.
- Results and logs kept at `~/.tmp/redesign-audit-results/` (JSON for every run).

## Recommended next steps

### (a) Design-judgement work (needs screenshots and visual iteration)

1. **Household identity on Home.** The only D1.2 item with no commit. Decide how a household looks on Home without a tile grid: the owner's or household's name in the header, person marks for who made recent things, or a recent-activity strip. Judge it on a 390 px viewport capture next to the Apple Home and Stripe iOS benchmarks. Use `owner_name` (merge main first).
2. **Is the phone Home still generic?** Rows are now consistent, but the 390 px capture is still a chevron list. Compare with the benchmarks again and decide if the identity work is enough or the list shape needs to change.
3. **Video presence in Library.** Give video tiles weight above the fold: sort or grouping order (the "Week of Oct 12" group follows "Last week", which looks wrong), a fixture with more than one video, and a look at whether videos need a visible cue other than the play pill.
4. **Recapture, then craft check.** Rebuild the captures from 542c95a (or the merged tree), redo the side-by-side with the benchmarks, update the craft diff, and only then run judge round 3. The `d1-8` set predates the last UI commits.
5. **Owner Status density.** 83 words against 60 on a twelve-engine outage. Decide what folds (chips legend, uptime legend) without hiding anything that carries honesty.
6. **Library tile person marks.** DESIGN principle 6 says tiles carry the maker's mark; the capture shows none on image tiles. Decide if that is a gap or the right quiet default.

### (b) Bounded code tasks (for remote workers at a pinned SHA)

"No browser" means a worker can verify it with `pytest`, `tsc` and `vitest` only. Playwright needs chromium and a local gateway, so those items are not verifiable in the sandbox; a worker can still edit the spec and run `tsc`, but a person or a browser-capable agent must run it.

| # | Task | Acceptance test | Checkable without a browser? |
|---|---|---|---|
| 1 | Move `PRODUCT.md` to `docs/design/PRODUCT.md` (or reword line 47), and update any reference | `tests/test_legacy_name_boundary.py` passes; full hermetic run: 0 failures | **Yes** (pytest) |
| 2 | Merge main into `design/full-surface` (clean) and commit the merge | `git merge-tree` empty; `tsc` 0; vitest 521 plus 11 configs pass; hermetic: only task 1's failure before task 1 | **Yes** |
| 3 | Make the frontend use the backend fields now on main: show `owner_name` where copy says "the gateway owner"; `plan_spend` in the budget card; `refusal.code` and `limit` instead of matching warning text (DESIGN backend asks 1, 9, 10); evidence `source`, `last_success` and check-time semantics (asks 2, 3, 4) | New vitest cases using fixtures from `docs/design/web-api-contract.md`; existing honesty tests unchanged and green; a test that the warning-text matcher is no longer reached when `refusal` is present | **Yes** (vitest); the look needs a browser |
| 4 | Update stale Library specs: `library.spec.ts:93` (visible text to the button name, "Search your creations", the Download link or button, strict mode) and `:242` (open the Filters popover; "Person" for "Owner"; "Download ComfyUI" for "Open in ComfyUI"); `live-data-scoping.spec.ts:19` (Filters popover, "Source", "Person") | The 5 library specs and the live-data scoping spec pass with the same behaviour asserted; `tsc` clean | **No** to run; `tsc` only in the sandbox |
| 5 | Update the 15 pre-existing stale specs: engines/services error copy (`status-engines-services.spec.ts:402`, 8 variants), `:637` owner-gated services route (find the real behaviour first), `usage.spec.ts:218` (expect "Unavailable") | Specs pass on main; do not weaken honesty assertions; write down the cause for `:637` | **No** (Playwright); the Usage case could also get a vitest equivalent that is runnable |
| 6 | Add a vitest (or pytest) guard for task 4's stale locators, so a renamed filter fails fast: a component test that renders Library for an owner and asserts the "Filters" popover contains Model, Date and (instance view) App, and that the person combobox is named "Person" | New test fails on a revert of the labels | **Yes** (vitest) |
| 7 | Library grouping order: put "Week of ..." groups in date order (the sheet shows "Week of Oct 12" after "Last week") | A unit test on the grouping function with fixed dates | **Yes** if the grouping logic is a pure function (it appears to be); the visual part is in (a)3 |
| 8 | Re-run the full e2e on the merged tree after tasks 1 to 5 and record counts | 0 failures on `playwright.config.ts` and `playwright.live-data.config.ts` | **No**, needs a browser agent |

Suggested order: 1, 2 and 6 first (small, safe, and they make the branch mergeable and green), then 3 in parallel with design items (a)1 and (a)2 because both need `owner_name`, then 4 and 5 by a browser-capable worker, then (a)4 and a judge round.
