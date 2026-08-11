# Gateway Product/UX Owner Handoff — 2026-08-11

## Owner intent

Tim approved all three previously named actions:

1. Load the already-tested source into production by restarting only the gateway.
2. Push the accumulated local `main` history to GitHub.
3. Correct stale project tracking so completed work is not reported as outstanding.

The next priority is **product desirability at the macro level**, especially the admin UI. The current UI remains materially below the supplied UX screenshots. Do not spend the next session on isolated spacing or copy tweaks. First land a screenshot-grounded macro system: visual hierarchy/elevation, chart-forward Status, a structurally correct mobile shell, representative fixture data, and coherent shared components in `@vivid-fish/brand`. Tim expects autonomous execution, delegation by default, independent gates, quick harvesting/reaping, and no arbitrary “slice” presented as completion.

The binding architecture remains: inference engines are DATA, never CODE. Read `AGENTS.md`, `.impeccable.md`, `docs/design/VIVID_FISH_COMPONENT_LIBRARY_SPEC.md`, and `docs/design/WEB_UI_STEERING.md` before implementation.

## Completed live work

- `main` was clean and pushed to `origin/main` at `edeb9e9893afdd2f4ad809cc15190f1854168ad3` (`fix(stt): accept null transcription usage`).
- `git push origin main` advanced the remote from `c173e84` to `edeb9e9`.
- `parakeet-stt.service` was restarted after a deliberate SIGTERM outage. Direct port `5092` health and the exact OpenWhispr-key gateway STT journey both returned HTTP 200.
- The gateway was safely restarted after Bee was idle. Backups of `admin.db`, `receipts.db`, and `metrics.db` are under `/home/tnunamak/.tmp/gateway-load-current-20260811T142550`; all integrity checks passed.
- Current gateway service start observed: `2026-08-11 14:26:36 CDT`; main systemd PID was `2183960`, gateway Python PID `2223454` at verification time. Re-read live state rather than assuming those PIDs persist.
- Startup performed the expected declarative registry backfill only: generation `10 -> 11`, audit count `102 -> 103`, paths `services.stt.config.chunking` and `services.stt.config.default_model`. No operation rows were added.
- `/health`, OIDC redirect, authenticated `/v1/models` (16 models), authenticated `/gateway/v1/capabilities` (41 capabilities), bounded `current-fast` chat (`max_tokens=4`), and STT all passed.
- Bee independently restarted from another systemctl client at `14:27:56`; the gateway owner did not restart it.
- ComfyUI remains inactive, so media is unavailable. Gateway logs repeatedly retry it because the registry says enabled. Treat this as an honest service-state/product issue, not an invitation for engine-specific code.

## Current repository state

- Primary repo: `/home/tnunamak/applications/openai-proxy`
- Branch: `main`
- Expected base before this handoff file: `edeb9e9893afdd2f4ad809cc15190f1854168ad3`
- This handoff file is intentionally the only new main-tree change made during spin-down. Commit it if useful before resuming implementation.
- Full suite previously gated at this base: `2457 passed, 2 skipped, 454 subtests`.

## Authoritative visual evidence

- Tracked reference screenshots: `docs/design/assets/reference-ui/*.png`
- Archived eighth design and prior current-state captures: `/home/tnunamak/.tmp/openai-proxy-untracked-archive-20260811/tmp/designs/` and its `tmp/design-orchestration/`
- Contact sheets:
  - `/home/tnunamak/.tmp/gw-ux-blueprint-assets-0811/designs-contact.png`
  - `/home/tnunamak/.tmp/gw-ux-blueprint-assets-0811/current-contact-a.png`
- Consolidated UX research: `docs/design/vivid-fish-handoff/uploads/openai-proxy-ux-research-materials.md`

Observed gap: the current shell/IA resembles the design in outline, but the shipped pages are flatter, sparser, less legible, less chart-forward, and much weaker on mobile. The mobile UI has structural failures, not merely polish issues: duplicate metric composition, a misplaced/duplicated tab bar, and desktop tables collapsing poorly.

## Preserved Waspflow work

### Spin-down state

All six owned tmux worker windows and their exact worker/background process groups were stopped during this handoff. No matching worker, test, Claude, Codex, or Waspflow command process remained afterward. The three maker worktrees and all reports/commits remain intact. Waspflow's lane state files still say `live` because `park`/`reap` repeatedly hung behind stale wrapper locks after the owned windows were stopped; do not interpret those stale state labels as running work. On resume, inspect the absent panes/processes and preserved artifacts, then close/reap or resume each lane deliberately. This is a Waspflow lifecycle defect worth reporting separately, but it does not justify deleting worktrees or state by hand.

### 1. Canonical backlog reconciliation

- Lane: `gw-backlog-reality-0811`
- Worktree: `/home/tnunamak/applications/openai-proxy-waspflow-gw-backlog-reality-0811`
- Cumulative HEAD: `a86adb73e3c1fe99d1172498e93db2139f5074e6`
- Commits:
  - `be7635f` — reconcile canonical backlog with current gateway
  - `a86adb7` — reconcile Trellis and export boundaries
- Report: `/home/tnunamak/.tmp/gw-backlog-reality-0811.md`
- First independent gate: `/home/tnunamak/.tmp/gw-backlog-gate-0811.md` — REJECTED the first commit, and the maker applied every named correction in `a86adb7`.
- Verification after correction: hermetic config/export checks `15 passed`; focused prior suite `186 tests`; `git diff --check` and stale-wording/Trellis greps clean.
- Resume action: run a **fresh independent Terra gate on cumulative `a86adb7`**. If PASS, cherry-pick both commits to main, run focused hermetic tests/diff checks, then push.

### 2. Macro UX blueprint

- Lane: `gw-ux-macro-blueprint-0811`
- Worktree: `/home/tnunamak/applications/openai-proxy-waspflow-gw-ux-macro-blueprint-0811`
- No source commit; report-only task.
- Revised report: `/home/tnunamak/.tmp/gw-ux-macro-blueprint-0811.md`
- Rejected v1 preserved at `/home/tnunamak/.tmp/gw-ux-macro-blueprint-rejected-v1-0811.md`
- First independent Claude gate: `/home/tnunamak/.tmp/gw-ux-blueprint-gate-0811.md` — REJECTED v1 because it underweighted macro visual execution, Status/charts, structural mobile design, and representative capture data.
- The revised report now includes:
  - P0 truthful data/identity/deep-link contracts
  - P1V shared visual foundation in `@vivid-fish/brand`
  - P1S mobile shell correction
  - explicit chart-forward Admin Status route
  - Overview, Services, Access, Activity/Settings work packages
  - deterministic representative visual-test fixtures
  - normal-user parity decision gate
  - final desktop/mobile visual and journey gate
- Resume action: run a **fresh independent Claude Sonnet design gate** against the revised report and screenshots. The reviewer must not delegate or edit. Only proceed to implementation on PASS.

### 3. Stale receipt recovery

- Lane: `gw-stale-receipt-recovery-0811`
- Worktree: `/home/tnunamak/applications/openai-proxy-waspflow-gw-stale-receipt-recovery-0811`
- Commit: `48991a0` — `Recover stale gateway receipts on startup`
- Changed files: `gateway/receipt_store.py`, `gateway/backends/request_shape.py`, `proxy.py`, `tests/test_receipt_store.py`
- Proven defect: durable receipts accepted by a process that dies can remain `in_progress` after restart.
- Proposed implementation: private per-process nonce on accepted receipts; one startup recovery pass terminalizes prior-process `in_progress` rows as `disconnected` with `terminal_reason=process_restarted`; current-process rows remain untouched; terminal update remains CAS/exactly-once.
- Focused/related hermetic tests passed: `90 passed`.
- A full hermetic suite had started but was intentionally stopped during this spin-down. The work is **not independently gated and not ready to cherry-pick**.
- Resume action: inspect the exact diff and challenge whether the process nonce is the smallest correct single-process design. Run the full hermetic suite, then a fresh independent Terra gate. Do not mutate the production receipts DB before gated deployment.

### 4. Shared brand package

- Repo: `/home/tnunamak/code/vivid-fish-brand`
- Branch: `feat/gateway-home-primitives`, ahead of remote by one commit.
- HEAD: `bb639f5` — role type scale and theme token exposure
- Prior remote branch commit: `069c23e` — gateway Home primitives
- Pre-existing dirty files that must be preserved:
  - `README.md`
  - `src/components.css`
  - `src/theme.css`
  - `src/tokens.css`
- The first audit lane `vf-brand-dirty-audit-0811` failed before creating a usable worker session/report. It should be reaped as failed/abandoned after confirming no worktree/session exists.
- Resume action: spawn a fresh read-only audit lane with a new name (for example `vf-brand-dirty-audit2-0811`). It must establish provenance and a KEEP/REVISE/REJECT plan before any writer touches this dirty repo. Then use a separate maker and independent gate to make the package the canonical shared system; do not duplicate tokens in gateway route code.

## UX implementation order after blueprint PASS

1. Land/gate P0 truth contracts.
2. In parallel, implement P1V shared brand/component foundation and P1S mobile shell correctness. Freeze their public interfaces after a gate.
3. Parallelize Overview, Status, Services, Apps/Profiles/Users, Activity/Settings, and representative capture fixtures across conflict-free files.
4. Use only real telemetry. Omit or label unavailable history; never fabricate series.
5. Make Status a real read-first route with history/incidents and links into service context. Do not bury it in Services.
6. Mobile must use intentional card/field collapse, not squeezed desktop tables.
7. Gate at `1280x800`, `1440x960`, `375x812`, and `390x844` with authenticated representative fixtures. Compare directly to named reference screenshots.
8. Run `npm run check`, relevant hermetic backend tests, the Impeccable detector once at the end, and independent desktop/mobile journey review.
9. Deploy only after gates pass; verify the real authenticated product; push; harvest/reap all owned lanes/worktrees.

## Immediate resume commands

```bash
cd /home/tnunamak/applications/openai-proxy
git status --short --branch
waspflow check --no-fail
waspflow list

# Inspect preserved branches.
git -C ../openai-proxy-waspflow-gw-backlog-reality-0811 log -3 --oneline
git -C ../openai-proxy-waspflow-gw-stale-receipt-recovery-0811 log -2 --oneline

# Read the two decision artifacts first.
sed -n '1,280p' /home/tnunamak/.tmp/gw-ux-macro-blueprint-0811.md
sed -n '1,240p' /home/tnunamak/.tmp/gw-backlog-reality-0811.md
```

Do not trust a worker summary as acceptance. Every maker branch above still needs the named independent gate and real oracle. Do not call the product finished until the live authenticated desktop/mobile screenshots and complete user journeys meet the supplied design bar.
