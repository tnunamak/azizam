# BX lane: state at handoff (2026-10-12, quota stop)

Branch `test/bx-isolation`, based on `origin/ci/t0a-candidate` (T0a was NOT on main yet; rebase on main once it lands).
Brief: ~/Documents/azizam/test-overhaul/lanes/bx-brief.md + COMMON-RULES.md. Ledger: ~/Documents/azizam/test-overhaul/lanes/bx-ledger.md.
Bench scripts: ~/.tmp/bx-bench (prep.sh, measure.sh = systemd-run scope + cgroup cpu.stat, lanes-legacy.sh, lanes-shared.sh, mutants.md).

## Done (committed here, all WIP commits; nothing landed, no gate run yet)
1. `web/e2e/fixtures/isolation.ts`: `test`/`expect` with
   - auto `globalStateGuard`: snapshots 7 owner-global settings (instance, defaults, prompt-memory, receipts, sign-in, user-access-defaults, media-job-admission) before every test; restores after; fails a test that changed one without tag `@global-state` (GLOBAL_STATE_TAG), or whose change could not be restored.
   - `isolated` fixture: namespace `<run>-<lane>-<project>-w<worker>-i<n>` (annotation -> case receipts `namespace`), `name()`, `email()`, `tag()`, `member()` (invite+accept via API -> own user/app/key/signed-in context); teardown fails on queued/running media jobs of its members and on open fake-engine requests for its namespace.
2. `scripts/ui/fake_engine.py`: per-namespace request log `/fixture/requests?namespace=` (in-flight counted to last body byte), `[hold]` + `/fixture/release` for teardown proof.
3. `scripts/ui/fixture_gateway.py`: fixture-only `PUT /fixture/operator-settings/sign_in` (admin API cannot restore sign-in "both" while OIDC is off -> real leak found in settings journey).
4. Pilot: invite-signin.spec.ts migrated (namespaced email); settings.spec.ts two real journeys tagged @global-state (manual partial restore removed; fixture restores all 7).
5. Proof: `scripts/e2e/prove-isolation` + `web/e2e/isolation.proof.ts` + `web/playwright.isolation-proof.config.ts`: 4 copies x 4 workers on one server pass; teardown cases fail with the right message. Mutant: old invite-signin fails 3/4 copies. PASSED (recorded in ~/.tmp/bx-bench/mutants.md).
6. `scripts/ci/policy.py` hygiene ratchet (waitForTimeout 19, networkidle 12, force_true 4, one_shot_expect 4, bare_isVisible 7 in tests/budgets.toml [hygiene]) + 5 injection tests in tests/test_test_policy.py. merge_cases passes `namespace` (+test).
7. Shared servers: `web/playwright.config.ts` = one invocation, webServer on P (project `chromium`, parallel, UI_E2E_WORKERS, grepInvert @global-state) and P+10 (project `chromium-global-state`, workers 1). global-setup seeds every server. 33 cases tagged @global-state (PRINCIPAL/OWNER-GLOBAL per dot inventory; list in ledger). receipt-retention finds DB under UI_E2E_STATE_ROOT/<server>.
8. `scripts/ci-local`: no per-lane copies; `--browser-workers N` (default 4) replaces `--shards`; web/test-results symlinked to $run/browser-results; steps e2e-main, e2e-live-data, e2e-household run at once (ports +10/+20, +30, +40), then `e2e-tree-unchanged`. plan_shards.py, its test and shard-weights.tsv deleted. tests/test_ci_local.py updated + new write-into-build test. Focused tests green: test_ci_local, test_test_policy, test_merge_cases (43 passed). Global-state project alone: 33/33 pass (4.5 min serial).

## Measurements so far
- Baseline (origin/main f8e1a106, 10 shards, lanes-legacy.sh, base port 18300): wall 430.8 s, CPU 3365 s, 716 main cases (1 load flake people.spec:973). INVALID for live-data/household: they collided with my dev run's ports (18410/18420). Rerun baseline with base port 18500 (and never overlap port ranges).
- Dev run of shared layout (port 18400, 4 workers) was stopped at the quota stop; only live-data (31 s) and household (154 s) finished OK.

## Remaining
1. Rerun baseline (heavy, port 18500) and after-run (prep.sh on branch SHA, lanes-shared.sh, port 18700+ not overlapping) with measure.sh; record wall, CPU-s, case count, per-invocation wall, load start/end, processes (12 gateway pairs -> 4).
2. Full shared run under 2 worker counts (e.g. 2 and 4) = the "two neighbor plans" proof; fix any parallel collisions (watch Try/workflow-try PRINCIPAL cases left in the parallel pool; polish-tour invites a fixed email in beforeAll; login throttle is 60/15 min per IP on one gateway).
3. Update docs/ops/CI.md (lane/shard sections, order-seed Playwright paragraph) with the new layout + numbers; adjust tests/budgets.toml if counts change.
4. Rebase on main after T0a lands; ONE `scripts/ci-local --force --signoff --target main` via heavy per batch; land (ff-only). fixture_gateway/fake_engine are test tooling (scripts/ui) -> main.
5. Worker retune from per-worker p95 and host load; report after-state alone and with VT deletions.
6. Open items: OIDC provider config not restorable (write-only secret); queued-media-job teardown branch unproven (default fixture has no media engine); specs writing to /home/tnunamak/.tmp/* absolute paths (try, library, long-jobs, polish-tour, shared-copy-contracts, signin-shell); VT owns UI-CHECK files (settings/providers/public-surfaces: only per-test tags/imports touched here).
