# Azizam feedback audit, 2026-10-12

Question from Tim (2026-10-12 12:12Z): "Can you confirm that all my feedback was addressed and extrapolated…?"
Short answer: **No.** Most concrete requests are done and live. Several principle-level items are only half extrapolated, and a few are open.

## Method and limits

- Sources: `convo` ledger (synced; Claude and Codex Azizam sessions; Gemini/Qwen/Pi/dsh held none for Azizam). Haiku 5.5 subagents (9 batches) pulled Tim-typed messages into `~/.tmp/audit/out/batch*.md`. I also parsed the 21 Claude jsonl files directly for the T3 orchestrator thread, plus `ux-findings/` (ORCHESTRATOR_TODO, tim-notes-2026-10-09, DIRECTION) and `memory/*.md`.
- Tim's own messages are mostly in 4 sessions: orchestrator thread (93f33e1d / 1f333961), media thread (293a2b44), a GPU/engines thread (00d07f8c) and a design thread (2c39f446). Messages relayed by "the lead" or "the dot" are marked [relayed].
- Not exhaustive: feedback from before 2026-10-07 lives only as quotes in `docs/design/PRINCIPLES.md` (P1-P10 dates June-Oct). The Haiku pass dropped some image-only messages. I judged "world-class" and "wow" by evidence, not taste.
- Repo checks run against `origin/main` a71e3e5c. Deployed = release 21, main 1054f3f7. `git diff 1054f3f7 origin/main` is tests, `docs/design/TEST_STRATEGY.md` and `web/src/components/ui/command.tsx` only, so repo claims hold for live. Live probes at 12:2xZ: `/` 200, `/ui/home` 404, `/showcase/docs/` 200, `/health` healthy.
- Status key: DONE = on main and in 1054f3f7. PARTIAL. NOT DONE. WAITING = Tim decides. SUPERSEDED.

## A. Concrete requests and complaints

| # | Date | Quote | Ask | Status | Evidence |
|---|---|---|---|---|---|
| 1 | 10-10 | "Please deploy as you go occasionally so I can see" | Incremental deploys | DONE | Releases r10-r21; r21 live at 1054f3f7 (ORCHESTRATOR_TODO "Release 21"); memory `deploy-incrementally` |
| 2 | 10-10, 10-11, 10-12 (x8) | "Will you use dot lanes…", "Remember to fully utilize dots", "Max out dots they are idle", "Status? Idle dots", "…confirm you are fully utilizing dots" | Keep dots busy, dot first | **NOT DONE** (today) | `dot-worker status --explain` 12:21Z: claimed 1/6, queued 1, util24h 31%, idle_while_queued24h 29%, util7d 26%. Memory `dot-first-lane-routing` records the rule, but the queue still empties. Tim has asked 8 times |
| 3 | 10-11 | "do we need /ui or can it move to /?" ; "Still takes me to /ui/home" | UI at root | DONE | 6ca0d220, 2807a776, 3fd4e8ed; r20 deployed 03:25Z; live `/` 200, `/ui/home` 404 |
| 4 | 10-11 | "Links already sent out… must keep working through permanent redirects < False this is unreleased" | No compat redirects | DONE | `/ui/home` returns 404 by design; memory `azizam-unreleased-no-compat` |
| 5 | 10-11 | "Speech voice: becomes a picker… But how? Isn't the UI fully dynamic…" | Voice picker from model data | DONE | `web/src/app/try/voice-picker`, `try/page.tsx:1220,2505`; voices come from API `speech_voices`; 0580331a, e7957739; r21 |
| 6 | 10-11 | "the legacy plans hidden… I don't want legacy anything so why do we still have them vs mapping old keys" | Remove legacy plans | **NOT DONE** | Live `admin.db` profiles: Legacy simon, daisy, librechat, cap, open-webui; `settings/page.tsx:99-101,902-970` hides them instead of removing; migration preview needs Tim's approval (see section C) |
| 7 | 10-10 | "getting rid of any trace of anything legacy in the app and in my deployment" | Zero legacy | **NOT DONE** | 60+ code hits: `gateway/category_policy.py:256-257,306,327-332,350-372,423`; `web/src/app/categories/editor.tsx:15,105-108,322`; `proxy.py` 33 hits (e.g. 716, 5680-6253, 24991); `gateway/ownership.py`; `gateway/transforms.py:572` "backward-compat"; ORCHESTRATOR_TODO lists the remaining legacy names. Some commits did strip some (f7628ad8, 2eee2bca, 89de02ce) |
| 8 | 10-11 | "I don't want the default to be anything local… is None as an access plan supported by divan?" | Deny by default; no "None" plan | **NOT DONE** | `settings/page.tsx:954` "None — built-in local access", `:986`, `:1060`; live: 7 enabled apps have `default_profile_id` NULL (OpenWhispr, Try x2, SillyTavern, Paid provider proof…). Design `design/access-plans` 545726a7 (v4, "remove the Permit") is not on main |
| 9 | 10-10/11 | "categories, like nsfw… How dynamic… without baking knowledge into azizam?" ; "merge category rules into access plans" ; "I require the full design and grounded research" | Data-defined categories in plans; research before build | PARTIAL | Research in divan (owner-defined dimensions entry, red-team entries). Categories page and editor shipped (a5de85ef, `docs/design/CATEGORIES_AND_PLANS.md`). The merge into plans is design-only (branch `design/access-plans`, not in main). Tim's go/no-go is pending |
| 10 | 10-11 | "Explain owners ask for everyone's Library and Usage…" ; "Should it be possible to configure the server to not allow owners to see user data?" ; "Just follow best practices… like immich" | Owner visibility policy by prior art | PARTIAL | Research `research-results/azizam-research-owner-visibility-20261011.md`; design v3 on `design/owner-visibility` 782d763f, not in main. Live code still broadens the owner Library to everyone: `web/src/app/library/page.tsx:240-243` (r18 security review P3) |
| 11 | 10-11 | "Don't keep this prompt… How does this relate to incognito" | Private-mode framing from prior art | PARTIAL | Maker-only prompt memory shipped (e40db092, 0359532e, `PROMPT_MEMORY.md`); private-mode research (16 parts) recovered; the copy fix waits in UX_BATCH1 r3 (not on main) |
| 12 | 10-11 | "Isn't the usual thing to have a self service link?" (forgot password) | Self-service reset | **NOT DONE** | `web/src/app/sign-in/page.tsx:249` still says "Ask the person who invited you for a reset link". Depends on mail |
| 13 | 10-11 | "the mail system is fully research backed designed and implemented?" ; "are there no brainer libraries to outsource…" | Mail design with library choice | PARTIAL | Design in `design/connect-root-mail` aaab1723 (red-teamed), not in main; no mail code. Whether to build mail at all is an open decision |
| 14 | 10-11 | "Invite someone… what are the options and specifically what is the default" | Explain/fix invite plan choice | PARTIAL | Folded into items 6/8; the invite page still offers "None" (settings default). Invite sign-in bug from 10-09 (#18 below) fixed |
| 15 | 10-11 | "Why are there different model names? … is it confusing how models are mixed with model aliases" ; "Picture is ready… Open result seems persistent" ; "in Try… loading state for models" ; "playback text weirdly placed"; "quality decisions is down" | Naming and state hygiene in Try | PARTIAL | WP-5/9/13 Try work in r21 (fix/wp-try in 1054f3f7). "One name per thing" design (UX_BATCH1) is not on main; voice naming (alloy/af_heart mix) revision pending |
| 16 | 10-11 | "Also the UI doesn't reflect that there is an mcp etc?" | Surface MCP/headless in UI | **NOT DONE** | No `mcp` string in `web/src/app/*` or `web/src/components` outside tests; design `connect-root-mail` not on main |
| 17 | 10-11 | "the ui shouldn't be the only surface… mcp… cli… rest api… DRY" | Headless-first | PARTIAL | REST exists; MCP exists with 3 data-plane tools (`gateway/mcp_server.py`); `HEADLESS_AND_RESOURCES.md` on main. **No CLI** (CLI spec v2 is a draft in research-results); no owner/admin MCP tools |
| 18 | 10-11 | "measured and consumable… resource usage and assignments… competing for the same resources" | Resource visibility for users/owners | **NOT DONE** | Design draft only (`azizam-design-release-c-capacity…`, consolidation into LIVE_CAPACITY.md pending). No feature on main |
| 19 | 10-12 | "There is no good reason to have an hour long suite ever" ; 10-10 "It just seems pretty slow" | Fast gate, fast releases | PARTIAL | `ci-local` ~8 min (b49f3987), xdist (e1afe3d1), `test-affected` (2293b363); main gate 6:55 (1892cd0c). But r21 gate took 2134 s wall (fresh-native ~25 min). Test overhaul slices are landing (a71e3e5c…) but not finished. Target <10 min not held |
| 20 | 10-10 | "get the CI fixed… no good reason why the repo should have racked up so much usage" | CI repair, no waste | DONE | 25a7e248, 960b039f: hosted CI manual only, `ci-local` signs off |
| 21 | 10-10 | "throw any idle dot lanes at optimizing performance? Make it blisteringly fast" ; 10-07 "gateway may not be performance optimized" | Performance work | PARTIAL | Static-serve patch (72% saving, `gateway/ui_static.py`), throughput work; no continuing benchmark/hill-climb on main that shows a target met |
| 22 | 10-12 | "Put world class docs on the product site" | Docs on the product site | DONE | 3d9dd645, 121fe409, e35d2439 (r20.1); live `/showcase/docs/` 200. "World class" is my unverified judgment; ORCHESTRATOR_TODO lists 3 docs fixes + de-personalisation still unreleased |
| 23 | 10-11 | "if receipts are valuable data maybe we shouldn't burn them" (kept 14 days) | Keep receipts | DONE | 1112fbdb; owner-chosen retention `gateway/receipt_store.py:53,591` (default keeps all) |
| 24 | 10-11 | "You can delete all qwen 3.6 models but not 3.8" ; "delete the drummer one" | Delete models | DONE | `/media/windows/AI Models/LLMs/GGUF` now holds only Qwen3.8 files, MTP, decider models |
| 25 | 10-11 | "Would I get better performance using strata… flash next is better"; "how many resources flash-next will use" | Evaluate | WAITING | Estimates given (Q2 ~62 GB RAM, ~17-19 GB VRAM for experts); a real test needs the 27B stopped on GPU1 |
| 26 | 10-11 | "login screen sucks… Default access plan / No default plan… Receipts kept 14 days" | Settings/defaults copy to design bar | PARTIAL | 08c13b32 "Settings Defaults states the plan once". The "No default plan" and "None" options remain (items 6, 8) |
| 27 | 10-10 | "Hmm can we unlock by using gh signoff or ci locally" | Local CI | DONE | 25a7e248 |
| 28 | 10-10 | Lead: verified P1 showcase key-forwarding fix | Merge | DONE | a2b0e2f1 "keep the API key in page memory" |
| 29 | 10-10 | "Now evaluate how very useful defaults can be bundled… a registry in git" | Bundled defaults | PARTIAL | `registry/`, `first-run.yml`, seeded names (2eee2bca). Dynamic/automatic registry updates not shown; dot-patch for seed names rejected (additive v2 pending) |
| 30 | 10-10 | "The red-teams didn't re-check the morning entry's claims about Qwen, Civitai, Together, Cloudflare or Portkey" | Verify those claims | PARTIAL | Verification entries exist in divan (categories-v3 "1096 verification rows… corrected… Cloudflare DLP"); I did not trace each of the 5 named claims |
| 31 | 10-10 | "Btw it's not necessarily household" | Broader target users | PARTIAL | Memory `azizam-target-users`, designs follow. Code still has 61 "household" hits in `gateway`/`proxy.py` (many are fine surface wording) |
| 32 | 10-10 | Video/VFX: "cinematic VFX imagine big budget film studios… until >95% confidence I will be amazed"; "Way too short"; "something totally different now that is not still" | Make a wow video | WAITING | Versions delivered (night-sky, 10 s with score); Tim has not confirmed the last ones. Not product code |
| 33 | 10-09 | Notes 1-10 (LTX I2V image input, tab clipping, Home "Unknown", Face Likeness wording, Usage phone overflow, raw paths, invite key, "Failed to fetch", Try redesign) | Bug and UX batch | DONE (1-10), PARTIAL (11) | Fix branches fix/wp-try, fix/wp-7-18, fix/home-unknown (WP-8 b6fd7e52), invite fix in 1054f3f7; `web/src/lib/model-name.ts` hides paths. Note 11 (gateway-composed workflows never health-checked) was a TODO "after batch2"; not found in git log |
| 34 | 10-09 | "I tried to invite… got an api key. Then I couldn't log in" | Invite flow | DONE | 4e0a5d7b and invite commits; Tim 10-11: "I did sign in with keycloak all good" |
| 35 | 10-09 | "challenge you to reflect on whether this hits the target (stripe linear vercel plaid)" | Hold the bar | PARTIAL | PRINCIPLES.md P1-P22; craft-system; sweeps. Tim's own 10-11 complaints (items 15, 26) show the bar was not yet met on the live build |
| 36 | 10-08 | "I want opus 5.5 designing the full product surface and opus and dot lanes redteaming" | Process | DONE | Memory `design-decisions-are-mine`, red-team reports in `research-results/` |
| 37 | 10-08 | GPU memory orchestration: "does this significantly expand responsibilities… go wide and deep on prior art in divan" | Research before scope | DONE (research), PARTIAL (build) | divan entries; `gateway/resource_admission.py`; broader orchestration not adopted. Overlaps item 18 |
| 38 | 10-07 [relayed] | "when /v1/audio/speech omits response_format, return MP3" | OpenAI parity | DONE | `proxy.py:4385,4399` |
| 39 | 10-07 [relayed] | "custom voices and generated media must be owned by the principal, NOT the API key hash" | Ownership | PARTIAL | `gateway/ownership.py:82-101` still accepts `key_hash` as an owner key next to the principal (legacy read path) |
| 40 | 10-07 [relayed] | "make OpenAI's Codex CLI work with Azizam" | Codex CLI compatibility | DONE | 5375b217, 1292eda6 (Codex e2e) |
| 41 | 10-07 [relayed] | "I want everything renamed" | Rename | PARTIAL (unverified) | I could not tell from the transcript what the new name was; no leftover check done. Treat as unverified |
| 42 | 10-11 | "You had better use cheap subagents appropriately as you are expensive" | Cost routing | DONE | Memory notes (2026-10-12 lead quota steer); this audit used Haiku for extraction |
| 43 | 10-10/11 | "Am I blocking?" / "Don't introduce optional product decisions as blockers" | Keep shipping | DONE | Defaults adopted per prior art (follow-prior-art memory) |
| 44 | 10-12 | "Everything deployed for my testing?" | Deployed? | DONE | r21 1054f3f7, smoke 12/12 (ORCHESTRATOR_TODO); `/health` healthy |

## B. Stated principles: extrapolation and remaining violations

| Principle (Tim's words) | Extrapolated to | Still violated (file:line on main) |
|---|---|---|
| **Engines, models, providers, workflows are data, not code** (P12; "the app is not supposed to have specific knowledge of any model or workflow or provider") | UI: `web/src/**` has no model, voice or provider names outside tests, one comment (`lib/model-name.ts:43`). Engine boundary phases 1-2 (01fa88e9, fix/engine-p2-runtime), `ENGINE_KNOWLEDGE_BOUNDARY.md`, `SERVICE_HARDCODING_AUDIT.md`, voice lists from engine data (e7957739, 0580331a) | **Not fully true in the gateway.** `gateway/video_extend.py:80,193-198,238-282,317` (whole LTX-2.5 graph, weights file names, class name `ltx25_chain`); `gateway/video_extend_h3.py:58` (Qwen3-VL text encoder file); `proxy.py:782` default `flux2-klein-4b`; `proxy.py:816` default `qwen-image-2.1-edit`; `gateway/identity_scene.py:39` Qwen-Image slots; `gateway/models_catalog.py:752` hard-coded voice `alloy`; `gateway/transforms.py:572-594` Qwen3 `/no_think`; `gateway/decision_provider_execution.py`, `providers.py:775-958`, `provider_catalog.py:20` TypeSafe-specific adapters (a protocol adapter, arguably fine, but named); `proxy.py:10155` hard-coded `ai.vivid.fish`/`openai.vivid.fish` trusted hosts. Backlog also lists `firehose_render.py`, `proxy_config.yml`, `safe-restart` personal defaults. So "fully extensible, just data" is **not confirmed** |
| **Nothing legacy** (P11) | Hygiene lane, `retire_legacy_names.py`, comments cleanup | Items 6-7 above; also live data (5 Legacy plans) |
| **Deny by default / secure by default** (P13) | Category policy fails closed (`category_policy.py`) | Item 8: "None — built-in local access" still the fallback (`settings/page.tsx:954,986,1060`); 7 live apps without a plan |
| **One name per thing** (P16) | Copy audits, WP-11 Engines plain wording | Model vs alias vs workflow names still mixed in Try/Models; design pending |
| **Follow prior art (Immich etc.)** (memory `follow-prior-art-defaults`) | Owner-visibility, access-plans, mail and private-mode all went through divan research and red-team | Defaults not built: owner Library defaults to everyone (`library/page.tsx:240-243`) |
| **Headless first, one core** (P21) | REST, MCP (3 tools), `HEADLESS_AND_RESOURCES.md` | No CLI; no owner/admin MCP tools; UI never mentions MCP (item 16) |
| **Fast releases** | ci-local ~8 min, affected tests, per-branch deploys | Item 19: r21 gate 2134 s; hosted CI removed but local gate varies under load |
| **Unreleased: no compat** | UI paths, redirects dropped | Compat code remains: `ownership.py` key_hash path, `transforms.py:572` "kept for backward-compat", `proxy.py` legacy fallbacks |
| **Extrapolate from feedback** (P22: "I've given you a lot of feedback but it's not exhaustive") | P11-P22 written with checks; a visual sweep against P1-P22 | Sweep findings are only partly fixed; this audit finds P11, P12, P13, P21 still broken |
| **Keep valuable data** (P19) | Receipt retention, keep-data repair | None found; open question in DATA_RETENTION on hourly telemetry cap |
| **Dots first, keep them busy** | Memory rules, submit loops, 24h cap removed | Item 2: 31% 24h utilization |
| **Research, design and red-team before building** (P20) | Designs for owner visibility, access plans, connect/root/mail, capacity | These designs are on branches only; none merged to main |
| **Target users beyond households** | Memory and designs | 61 "household" hits in Python |

## C. Waiting on Tim

1. Go/no-go on owner visibility v3 (`design/owner-visibility` 782d763f) and the policy defaults it adopts from Immich.
2. Go/no-go on access plans v4 (`design/access-plans` 545726a7), which removes "None" and merges categories into plans.
3. Approve the legacy-plan migration preview (map each Legacy plan and 7 plan-less apps to modern plans; apply with backup). This unblocks items 6-8.
4. Approve disabling the remaining test apps and say whether the "Paid provider proof" Default key was a test.
5. Rotate the OIDC client secret (partial value printed in a lane transcript; admin backup holds the plaintext).
6. Build mail at all (item 13)? And approve the connect/root/mail design.
7. Allow a Flash-Next test that stops the 27B on GPU1 (item 25).
8. Say whether the latest VFX video is good enough (item 32).
9. Tell household members the app moved from `/ui` to `/`.
10. Decide on the open DATA_RETENTION questions (hourly telemetry cap, replay window).

## D. Counts (section A, 44 rows)

- DONE 17: rows 1, 3, 4, 5, 20, 22, 23, 24, 27, 28, 34, 36, 38, 40, 42, 43, 44.
- PARTIAL 18: rows 9, 10, 11, 13, 14, 15, 17, 19, 21, 26, 29, 30, 31, 33, 35, 37, 39, 41.
- NOT DONE 7: rows 2, 6, 7, 8, 12, 16, 18.
- WAITING ON TIM 2 rows (25, 32), plus the decisions in section C that gate PARTIAL rows 9, 10, 13.
- SUPERSEDED 0.
