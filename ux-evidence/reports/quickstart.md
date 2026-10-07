# azizam-ux-quickstart-20261007

Bounded quickstart audit: six ranked findings, three delight opportunities, and keep list. Setup-doc continuation denied; no retry. Empty diff verified, no runtime validation.

First-run quickstart audit — bounded review

Coverage and limits
Reviewed README.md 1–118; docker-compose.yml 1–64; docs/FIRST_RUN_SETUP.md 1–200 of 316 at base 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba. The continuation read beginning at setup-doc line 201 was denied for exceeding authorized read scope. Stopped immediately; no retry or alternate access. The remaining setup states and troubleshooting may address gaps below, so absence claims apply only to the reviewed quickstart path. No installation, runtime requests, checks, shell commands, or changes were performed. Workspace receipt confirms an empty diff (0 bytes) and no checks. No cancellation or owner notes were present at the final state check.

Documented path and boundaries
README 20–36 requires Docker with Compose, a repository checkout, and an already reachable OpenAI-compatible chat engine. From the repository root, the command is “docker compose up -d”; no .env is required. This builds the gateway and starts a pinned LiteLLM sidecar; it does not download a model. Gateway host binding is 127.0.0.1:5000 by default, overridable with AZIZAM_PORT; the sidecar uses internal port 4000 without a published host port (compose 3–30, 46–52). Persistent state is the shared gateway-data volume.

The owner opens http://127.0.0.1:5000/setup, creates an account and App key, registers an engine/model mapping, and tests a request. README specifies Home’s Gateway check and then a client message. Setup doc 117–131 supplies curl against /v1/chat/completions with public model my-llama and a placeholder App key; README’s client base is /openai/v1.

Time to first successful request is not measured by these files. README’s five minutes explicitly starts after containers are ready and excludes build/model-download time. Neither container startup nor the gateway /health check proves model execution is available. Setup doc 174–181 describes a test that expects unauthenticated 401 and authenticated success; those are documentation claims, not results obtained here. Exact install-failure messages are not established in the reviewed quickstart sections.

Ranked findings

1. P1 — Engine reachability instructions can expose the wrong service.
Surface: engine connection; docs/FIRST_RUN_SETUP.md:54–58; README.md:34.
“As a new self-hoster, I follow ‘start it on 0.0.0.0’ to make Docker connect, without realizing other devices may now reach my inference server.”
This undermines confidence in sharing a family service. README mentions firewall restrictions, but the detailed setup recipe drops that condition.
Change the setup paragraph to: “The gateway container must be able to reach your engine. Prefer an address restricted to the Docker network. If you bind the engine to 0.0.0.0, restrict its port with your firewall before starting it; this may otherwise expose the engine to other devices. Do not publish the engine directly to the internet.” Keep reachability and exposure in the same step.

2. P1 — Two canonical instructions disagree on where the first key appears.
Surface: first owner journey; README.md:30; docs/FIRST_RUN_SETUP.md:24–25, 114–116.
“I finish creating my owner account and look for the key on /setup because the detailed guide says it is on the same page. The README instead sends me to Home → Create key for.”
This can strand a first-time owner at the first successful API request. It is a documentation contradiction; actual UI behavior was outside scope.
Proposed canonical copy, following README’s task-oriented path: “Create your owner account, continue to Home, choose your app, then select Create key for [app]. Copy the App key now; it is shown once.” Confirm that behavior before publishing, then use identical instructions in both guides.

3. P1 — Startup has no explicit readiness or failure-localization checkpoint.
Surface: boot-to-request; README.md:24–32; docker-compose.yml:21–27, 35–46.
“I run the one command, then cannot tell whether a browser or chat failure means the gateway is still starting, the sidecar is waiting, or my engine is unreachable.”
The gateway health check tests only /health. LiteLLM’s wrapper waits silently for a nonempty config, and its container has no health check. ‘Running’ is therefore an inadequate first-success signal.
Add a checkpoint immediately after startup: “Check startup: docker compose ps. If the setup page will not open, inspect docker compose logs --tail=80 gateway. A healthy gateway means its HTTP service responds; it does not prove your model works. After adding an engine, run Gateway check.”
Add a compact troubleshooting block: “Page unavailable → gateway/host port; engine connection fails → container-to-engine URL and firewall; gateway check succeeds but your app fails → client base URL, App key, model.” Obtain and document exact observed messages in a separately authorized install test rather than inventing error strings. Add a clear sidecar waiting-for-config log and an execution-readiness signal.

4. P2 — Compose prerequisites and port/network collisions are hidden.
Surface: install prerequisites; README.md:22; docker-compose.yml:6–9, 58–64.
“I have Docker Compose, but an optional env-file setting or a pre-existing network stops the advertised one-command install before I can open anything.”
The compose file uses optional env_file syntax and a fixed default subnet; the quickstart specifies neither a tested Compose version nor a recovery route.
Add “Tested with Docker Compose [verified version]; check yours with docker compose version.” Explain that port 5000 and subnet 172.30.251.0/24 must be available. Provide a port override example: “AZIZAM_PORT=5001 docker compose up -d”, followed by the matching /setup URL. For subnet overlap, explain that AZIZAM_DOCKER_SUBNET and AZIZAM_DOCKER_GATEWAY must be changed together to an unused matching network; do not suggest a universal replacement subnet.

5. P2 — The success test changes URL conventions and hides the success criterion.
Surface: first API request; README.md:32; docs/FIRST_RUN_SETUP.md:123–131, 178–181.
“I configure /openai/v1, then the verification example uses /v1, and I do not know what output proves setup worked.”
Both may be supported; the issue is unnecessary uncertainty. Use /openai/v1/chat/completions consistently in the first request example and say: “Success: HTTP 200 with the assistant reply in choices[0].message.content. If Gateway check works but this request fails, compare your key, base URL, and public model name.” Include status-bearing output instead of curl -s alone.

6. P2 — The shortest route is interrupted by implementation details.
Surface: setup guide; docs/FIRST_RUN_SETUP.md:60–111, 133–187.
“I am trying to send Hello, but I pass storage overrides, dependency-lock provenance, a second install method, and admin API automation.”
Keep one numbered Compose route through a successful request. Move native installation, storage overrides, dependency provenance, and automation beneath explicitly labeled optional sections after that milestone. Rename the headline to “Quickstart: about five minutes after containers are ready” to preserve the timing caveat at scanning speed.

Three delight opportunities
- A four-step first-run status strip: “Gateway ready → Engine reachable → Model replied → App connected.” Each failure shows the responsible boundary and one next action.
- A connection card that substitutes the actual port, public model, and chosen app into a copyable recipe, with a clear reminder that another device cannot use this computer’s localhost.
- A first-success receipt: “Your model replied through Azizam,” showing gateway/model/client checks separately and linking to request details. Show unknown cost honestly rather than implying free execution.

Keep
The loopback-only gateway default; no mandatory .env; explicit model-download exclusion; pinned sidecar digest; durable named volume; public-to-upstream model mapping example; one-time-key warning; real Gateway check followed by an app test; and the README’s explicit compatibility and paid-fallback caveats. These already demonstrate honest boundaries rather than inflated promises.
