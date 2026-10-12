# FK lane: state at handoff (2026-10-12, quota stop)

Worktrees: test branch ~/.tmp/azizam-fk (test/fk-core), fixes ~/.tmp/azizam-fk-fix (fix/fk-upstream-faults).
Focused runs: scripts/run-hermetic-pytest.sh -q -p no:randomly <files> (runner archives HEAD: commit first).
Mutant harness: ~/Documents/azizam/test-overhaul/lanes/mutant.py (copy of ~/.tmp/fk-tools/mutant.py);
results so far: lanes/mutants-batch1.txt, lanes/mutants-s2.txt (all listed mutants killed by new tests;
old tests survive MK1-4, MS1, MF1, MF2, M14 — see files). Inventory: lanes/fk-inventory.md.

## Done (all committed here as wip commits; squash before landing)
- tests/support/fake_upstream/: scenario JSON format + loader/validator, protocols.json (openai-compatible,
  comfyui, litellm-admin), app (matcher, ordered/repeat/optional exchanges, 599 + violation latch, request log),
  StreamingASGITransport + HostRouter (patches httpx.AsyncHTTPTransport only), h11 loopback server, VirtualClock,
  fake_upstreams fixture with fail-closed teardown. tests/support/gateway.py (real admin store + registry,
  gateway_client, ResponseTap for the frontends' loopback hop).
- Engine guard scans the package (tests/test_engine_knowledge_boundary.py + self-test).
- tests/test_fake_upstream.py core self-tests; tests/test_upstream_faults_openai_compatible.py (9);
  tests/test_upstream_faults_protocol_frontends.py (17, anthropic+ollama frontends over the openai-compatible upstream).
- Migrations: test_home_api correlation test, test_dispatch_admission backend (golden byte-identical).
- S2 rewrites done: test_litellm_delegated_auth.py, test_protocol_forward_auth.py, test_litellm_key_endpoints.py.
- tests/test_fake_ratchet.py (reads [ratchets] ad_hoc_upstream_fake_files from tests/budgets.toml; count before 29 on
  origin/main, now 27-28 — recount with the test's own function). budgets.toml comes from T0a (not merged yet).
- Bugs (xfail strict on this branch, fixed on fix/fk-upstream-faults, one commit each, each verified green per commit):
  FK-B1 think-repair per wire chunk; FK-B2 upstream drop mid-stream ends cleanly; FK-B3 LiteLLM timeout body
  {"detail":..}; FK-B4 frontends decode per chunk (U+FFFD); FK-B5 frontends report end_turn/done after a drop;
  FK-B6 frontends drop Retry-After. Dot B1 (paid SSE waits for EOF after [DONE], provider_execution.py:702) verified by
  reading, no test yet.

## Remains
1. Wait for T0a on main; rebase; add tests/budgets.toml [ratchets] ad_hoc_upstream_fake_files = <count> and raise
   pytest ceiling by the FK matrix integer; add [[xfail]] entries in tests/suites.toml for every FK-B* xfail.
2. Squash into 2 batches (core+openai+faults+2 migrations+ratchet; frontends+S2), ONE ci-local gate each via
   ~/.tmp/tso-tools/heavy, ff-push to main. Push fix/fk-upstream-faults rebased on the landed tests; list it for release.
3. Not started: comfyui / typesafe-systemone / decisions FK apps + fault cases; tests/compat/harness/fake_backends move;
   more migrations (test_image_polling, engine_p2_differential, speech_tier_helpers, decision_* MockTransport files);
   paid-path (provider_execution) fault test for dot B1; mutants for frontend fault tests on the fix branch.
4. Write ~/Documents/azizam/test-overhaul/lanes/fk-REPORT.md (protocol x scenario table, counts, S2 ledger, bugs, SHAs).
