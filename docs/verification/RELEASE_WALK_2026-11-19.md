# Release walk, 2026-11-19: a fresh install, following only the shipped docs

This file closes release-bar item 10. It records one live walk of a fresh
install by a new owner, who follows the shipped docs and nothing else. For each
step it lists what the doc says, what was done, what the screen or command
showed, a verdict, and the time taken. It does not change any doc.

- **Code under test:** `main` at `1185596da` (train 96).
- **Docs followed:** `README.md` (Quickstart steps 1-6 and "Share with family or
  friends"), `docs/guides/OPERATOR.md` ("Start a new gateway", "Add local
  engines", "Manage people and access plans", "Manage apps and keys"),
  `docs/FIRST_RUN_SETUP.md` ("Connect an engine by where it runs") and
  `docs/guides/FOR_MEMBERS.md`.
- **Gateway:** `docker compose -p azizam-walk up -d`, published at
  `http://127.0.0.1:5100` (`AZIZAM_PORT=5100`). It was a clean install: new
  volumes, no `.env`.
- **Engine:** a throwaway Ollama container with the model `qwen2.5:0.5b` (CPU).
- **Browser:** headless Chromium, driven by a Playwright script. The owner and
  the member each used a separate clean browser context. Both accounts used
  `example.com` addresses and generated passwords. Keys are never written here.
- **Timing:** the times are wall-clock, measured by the agent. A person who reads
  the docs as they go needs more time. The times show where the walk waited,
  not how fast a person types.

## Result

- 18 steps: **9 PASS, 9 DRIFT**.
- A new owner who follows the docs literally **does not get a chat reply**. The
  address the docs give for an engine on the same computer (S10) passes the
  Add engine check, then every chat request returns `500`. The gateway container
  can resolve `host.docker.internal`. The chat container in the same Compose
  project cannot.
- With one changed engine address, everything after it worked. The owner got a
  chat reply and a receipt, sent an invite, and a member accepted it and got a
  chat reply with their own key.
- Walk time: about 15 minutes from the first command to the member's first chat
  reply. About 6 of those minutes were spent on the two engine-address failures
  (S1, S10).

## Step table

| # | Step | Verdict | Time |
| --- | --- | --- | --- |
| S1 | Engine: start Ollama, use the docs' address | DRIFT | 64 s to start, then 1 min to see the check fail |
| S2 | `docker compose up -d` | DRIFT | 2 s to fail, 11 s on retry |
| S3 | `curl -i /health` | PASS | within the 11 s above |
| S4 | Create the owner account (README) | PASS | 10 s |
| S5 | Owner account and first key (`FIRST_RUN_SETUP.md`) | DRIFT | no extra time |
| S6 | Apps & keys: New app | DRIFT | 8 s |
| S7 | Apps & keys: Create key from the app menu | DRIFT | 4 s |
| S8 | Gateway, Engines, Add engine dialog | PASS | 2 min for the whole dialog, with one failed first try |
| S9 | `curl .../openai/v1/models` | PASS | 0.4 s |
| S10 | Chat `curl` with the docs' engine address | DRIFT | 5.1 s to a `500` |
| S11 | Chat `curl` after the address change | PASS | 1.4 s |
| S12 | Usage shows the request record | DRIFT | 20 s |
| S13 | Receipt names the app and the model | PASS | 12 s |
| S14 | Create an access plan | PASS | 10 s |
| S15 | People, Invite someone | DRIFT | 68 s |
| S16 | Member opens the link and accepts | PASS | 13 s |
| S17 | Member connects an app from Home, "Gateway check" | DRIFT | 1 min |
| S18 | Member's first chat with their own key | PASS | 0.3 s, plus 10 s to find it in Usage |

## Steps in detail

### S1. Start an engine and give Gateway the address the docs give: DRIFT

- **Doc says.** `docs/FIRST_RUN_SETUP.md:202-203`: "Inside the gateway
  container, `127.0.0.1` means the container itself. Use the name
  `host.docker.internal` instead." Lines 205-206: "Address:
  `http://host.docker.internal:8080/v1`. For Ollama, use
  `http://host.docker.internal:11434`." Line 208: "Listener: start the engine so
  it accepts connections on all interfaces, not only loopback." Lines 236-239
  give a reachability check to run from the gateway container. `README.md:49`
  says: "For Ollama, choose **Server with its own model list**. Type the address
  with no `/v1`." The docs have no layout for an engine that runs in a Docker
  container.
- **Did.** Started Ollama with `docker run -d -p 127.0.0.1:11500:11434
  ollama/ollama` and pulled `qwen2.5:0.5b`. The image pull took 47 s and the
  container plus model took 17 s. Then I ran the doc's reachability check from
  the gateway container against `http://host.docker.internal:11500/v1/models`.
- **Showed.** `urllib.error.URLError: <urlopen error [Errno 111] Connection
  refused>`. Inside the container the name resolved to the host's Docker bridge
  address. A port that Docker publishes on host `127.0.0.1` does not listen on
  that address. In the Add engine dialog, the address `http://host.docker.internal:11500`
  gave "Server not reachable ... Nothing answered at host.docker.internal:11500
  within 5 seconds". The dialog's hint says that, in a container, "localhost"
  means the container. It does not name `host.docker.internal` or the listener.
- **Verdict.** DRIFT. The doc covers an engine started on the host, such as
  `OLLAMA_HOST=0.0.0.0:11434`. It does not cover an engine run with
  `docker run -p`, which is the usual way to run Ollama in Docker. The doc's
  "listener" advice does not map to a `-p` flag.
- **What worked.** `FIRST_RUN_SETUP.md:65-67` says to "start it on `0.0.0.0`
  for local setup or use another host address the container can reach".
  I took the second option and published the engine on the host's Docker bridge
  address, which `host.docker.internal` resolves to:
  `-p <docker0-address>:11500:11434`. The reachability check then printed `200`.
  I avoided `0.0.0.0` so that the engine stayed off the network.

### S2. `docker compose up -d`: DRIFT

- **Doc says.** `README.md:27-33`: "Start the gateway. Open a terminal in this
  repository's folder and run: `docker compose up -d`. The first start builds
  the gateway image. This can take several minutes." Line 41: "If you set
  `AZIZAM_PORT`, use that port in every URL here."
- **Did.** `AZIZAM_PORT=5100 docker compose -p azizam-walk up -d`.
- **Showed.** First try failed after 2 s: `failed to create network
  azizam-walk_default: ... invalid pool request: Pool overlaps with other one on
  this address space`. Another Docker network on this host already used
  `172.30.0.0/16`, which contains Compose's default `172.30.251.0/24`. The
  second try used `AZIZAM_DOCKER_SUBNET` set to another free /24 and
  `AZIZAM_DOCKER_GATEWAY` set to its first address. It started in 3 s.
- **Verdict.** DRIFT. The overlap is host-specific, but the docs name only
  `AZIZAM_DOCKER_SUBNET` (`FIRST_RUN_SETUP.md:223` and `:230`: "Use your own
  subnet if you changed `AZIZAM_DOCKER_SUBNET`"). They never name
  `AZIZAM_DOCKER_GATEWAY` or what to do about an overlap. `docker-compose.yml`
  defaults the gateway to the first address of the default subnet. I checked with `docker network create`
  that a /24 elsewhere with that default gateway fails with "no matching subnet
  for gateway". A new owner who sets only the subnet hits a second error.
- **Not measured.** The first try built the image from a layer cache that other
  builds on this host had filled. The "several minutes" cold build time is
  unmeasured here.

### S3. Health check: PASS

- **Doc says.** `README.md:35-41`: "`curl -i http://127.0.0.1:5000/health` ...
  The first line of the reply should be `HTTP/1.1 200 OK`."
- **Did.** Polled `http://127.0.0.1:5100/health` after the command.
- **Showed.** `HTTP/1.1 200 OK`. From the second `up` command to the first 200
  took 11 s.

### S4. Create the owner account: PASS

- **Doc says.** `README.md:43`: "Open `http://127.0.0.1:5000/setup/`. Enter an
  email address, a password, and the password again." `OPERATOR.md:21`:
  "`/setup/` creates or claims the first owner account."
- **Did.** Opened `/setup/` and filled the three fields. The fields are labelled
  "Email or username", "Password" and "Confirm password". The button is "Create
  owner account".
- **Showed.** The owner was signed in and Home opened at `/`. The sidebar has the
  entries the docs name: Apps & keys, Usage, and a Gateway group with Engines
  and People. Home already listed one app, "Personal access", with 0 keys. Home
  also said "LiteLLM can't take requests right now" and "Add an engine to turn
  something on". That fits a gateway with no engine.
- **Note.** The first field says "Email or username". The doc says "an email
  address". This is a small label difference only.

### S5. `FIRST_RUN_SETUP.md` says `/setup/` mints the first key: DRIFT

- **Doc says.** `FIRST_RUN_SETUP.md:7`: "Let the app seed sane defaults and mint
  the first App + `opk_...` runtime key." Lines 28-29: "Then open
  `http://127.0.0.1:5000/setup/` to claim the first owner account and mint a
  runtime key." Line 330: "Mint the first `opk_...` runtime key from the same
  page".
- **Did.** Finished `/setup/` as in S4.
- **Showed.** `/setup/` created the owner and went to Home. It minted no key. The
  "Personal access" app had 0 keys. `README.md:45` and `OPERATOR.md:21` say to
  use Apps & keys, and that is what works.
- **Verdict.** DRIFT between two docs. The README path is right.

### S6. Apps & keys, New app: DRIFT

- **Doc says.** `README.md:45`: "Open **Apps & keys**. Choose **New app** to make
  an app and its first key ... The key starts with `opk_`. Copy it now. It is
  shown only once." `OPERATOR.md:21` says the same.
- **Did.** Opened Apps & keys and chose **New app**. The dialog asks for "App
  name", then "Acts as" with two choices ("You" and "A service"), then "Access
  plan". I typed a name and left "Acts as" alone.
- **Showed.** **Create app and key** stayed disabled. No choice in "Acts as" is
  preselected, and no text says why the button is off. After I chose "You", the
  button worked. A dialog "New key for ..." showed the key in a text box ("Copy
  the key now. It is shown only once."), with a "Set up a client" panel under it.
  The key starts with `opk_`. The panel said "No chat model available" because
  no engine existed yet.
- **Verdict.** DRIFT. The doc leaves out the "Acts as" choice, which blocks the
  button.
- **Walk note.** My first script closed the page before I saved that key. I
  created a second key (S7). This was a script mistake, not a doc finding.

### S7. Create a key from the app's menu: DRIFT

- **Doc says.** `README.md:45`: "or open an app's menu and choose **Create
  key**."
- **Did.** Opened the menu "Actions for Walk curl app".
- **Showed.** For an app with no keys, the menu item is "Create key". For an app
  that already has a key it is "Create another key". Both exist on the same
  screen as an "Add key" button on the app row. The key appeared in the same
  one-time dialog as in S6.
- **Verdict.** DRIFT, small. The doc names one label. The screen has three:
  "Create key", "Create another key" and "Add key".

### S8. Gateway, Engines, Add engine (the dialog): PASS

- **Doc says.** `README.md:47-53`: "Open **Gateway → Engines**, then click
  **Add engine**. ... For Ollama, choose **Server with its own model list**.
  Type the address with no `/v1` ... The dialog has several steps, and each has a
  **Continue** button. **Continue** sends a few harmless requests to the server.
  Nothing is saved yet. ... Tick the abilities you want, such as chat. Keep or
  rename the model names it found, then name the engine and click **Add
  engine**." Line 53: "Write down the public model name you kept".
- **Did.** Followed it with the address `http://host.docker.internal:11500` and
  the engine published on the bridge address (S1).
- **Showed.** All the labels match. There are five steps: Kind, Address and key,
  What it can do, Models, Name. The first **Continue** said "Reached the server.
  No key needed. Found 1 models." Chat was ticked ("Likely"). Step 4 offered
  `qwen2.5:0.5b` under the suggested public name `qwen2.5-0.5b`. The dialog
  ended with "Added “walk-ollama”. It is on, with Chat." and "Saved connection
  check passed". Elapsed time was 19 s on the scripted run.
- **Note.** The Engines list names the row "Chat", not "walk-ollama", and its
  status read "Unknown" after the engine was added.

### S9. The models request: PASS

- **Doc says.** `README.md:62-67`: `curl -s http://127.0.0.1:5000/openai/v1/models
  -H "Authorization: Bearer opk_YOUR_KEY_HERE"`.
- **Did.** The same request on port 5100 with the owner's key.
- **Showed.** `200` and a list of 30 models. `qwen2.5-0.5b` is one of them. The
  list also holds media workflow names, which the README does not mention.

### S10. The chat request with the docs' address: DRIFT

- **Doc says.** `README.md:69`: "Then send a message from your app." The docs give
  no chat `curl` for the owner in the README. `FIRST_RUN_SETUP.md:316-345`
  ("Short Path" step 6) gives one against `/v1/chat/completions`.
- **Did.** One chat request to `/openai/v1/chat/completions` with the owner's
  key and the model `qwen2.5-0.5b`. The engine address in the saved engine was
  `http://host.docker.internal:11500`. This is the doc's address, with the
  listener fixed as in S1.
- **Showed.** `500` after 5.1 s: `{"error": {"message": "The engine failed while
  handling the request.", "type": "api_error"...}}`. The saved connection check
  still passed. In the Compose project, only `gateway` has
  `extra_hosts: host.docker.internal:host-gateway` (`docker-compose.yml:18-19`).
  The `litellm` service runs the chat request and has no such entry. From inside
  the `litellm` container, resolving `host.docker.internal` failed with `Name or
  service not known`. The gateway's own check, which uses the gateway
  container, passes. The chat request, which uses the `litellm` container, fails.
- **Verdict.** DRIFT, and the main finding of this walk. The doc's Compose
  address for Ollama cannot work for chat, even though Add engine says it works.
  The `:8080/v1` address in `FIRST_RUN_SETUP.md:205` goes through the same
  chat service, so it should fail the same way. I did not test it.
- **Cost to find.** About 4 minutes to find and fix. The `500` message does not
  name the address or the failed name lookup. I found the cause by resolving the
  name inside the `litellm` container.

### S11. The chat request after the address change: PASS

- **Did.** On the engine page, set the address to the host's Docker bridge
  address with the same port, `http://<docker0-address>:11500`, and saved. My
  script pressed "Save and keep off" by mistake, which turned the engine off
  (requests then returned `400 model_not_found`). I turned the engine back on
  with the **Enabled** switch. A short wait later:
- **Showed.** `200` in 1.4 s with `"Hello, how are you today?"` from
  `qwen2.5-0.5b`. An IP address works in both containers because it needs no
  name lookup. `OPERATOR.md:64` and `FIRST_RUN_SETUP.md:65-67` say a host
  address "the container can reach" is the fallback. They do not say it is
  required for Compose.

### S12. Usage shows the request record: DRIFT

- **Doc says.** `README.md:69`: "Open **Usage** to see its request record."
- **Did.** Opened Usage from the sidebar.
- **Showed.** Usage opens on monthly totals: "5 requests", "Failed requests: 4
  requests, 80%", a by-task table, and a by-model table. No request record shows
  until I chose **View recent requests**. The four failures are my own `500` and
  `400` answers from S10 and S11. They show as "Failed" rows with the same app
  and model name.
- **Verdict.** DRIFT, small. The record is one click further than the doc says.

### S13. The receipt names the app and the model: PASS

- **Doc says.** `README.md:135`: "Use ... **Usage** for request details. Receipts
  can explain routing, rejected inputs and cost where the execution path records
  them." `FOR_MEMBERS.md:98-99`: "Receipts show what happened for a request, such
  as the model, time, and cost information when the gateway has it."
- **Did.** Chose **View recent requests**, then opened the newest row.
- **Showed.** "Request receipt": App **Walk curl app**; Model **Qwen2.5 0.5b**;
  Time taken 1.3 s; Spend "Local · no charge"; Outcome "Completed". The model
  shows as a display name, not the public name `qwen2.5-0.5b`. "Person" says "Not
  recorded", although the owner made the request with an owner's own key. "Where
  it ran" says to open Technical details.

### S14. Create an access plan: PASS

- **Doc says.** `README.md:139`: "Create an access plan first." `OPERATOR.md:157`
  and `:167`: "Open **Gateway**, then **People** (`/people/`)." "An invite needs
  an access plan, so create one first."
- **Did.** Gateway, People, the **Access plans** tab, **New access plan**. Named
  the plan "Walk family chat" and saved it with the defaults.
- **Showed.** The plan was saved. A plan named "Local only" already existed. The
  dialog's defaults allow every kind of work and no paid provider.

### S15. Invite someone: DRIFT

- **Doc says.** `README.md:139`: "Open **Gateway → People** and choose **Invite
  someone**. Set **Access plan** and **Link expires after**, then copy the link
  to the person." `OPERATOR.md:167-173`: choose **Invite someone**; "in that
  dialog, choose the person's **Access plan**"; "set **Email (optional)** to
  restrict who can accept it"; choose **Link expires after**.
- **Did.** Opened **Invite someone**, chose "Walk family chat" and left the
  expiry at 7 days.
- **Showed.** **Create invite link** stayed disabled. It enabled only after I
  also ticked "Allow anyone with the link to create an account and receive an
  API key". The doc calls the email "optional", but with no email the button
  needs that tick. Nothing on the screen says why the button is off. After the
  tick, "Invitation ready" showed a link of the form
  `http://127.0.0.1:5100/invite/#token=...`, "Each invitation can be accepted
  once", "Expires in 7d". The link starts with the loopback address. It opens
  only on the gateway's own computer. The README step says "copy the link to the
  person". It does not say the host must change first.
- **Verdict.** DRIFT. The docs omit a required choice, and the doc's "optional"
  email is not optional unless the tick is set.

### S16. The member opens the link and accepts: PASS

- **Doc says.** `FOR_MEMBERS.md:18-21`: "Open the invitation link. Create an
  account, or sign in with the configured identity provider. Save the API key
  when the page shows it. Continue to Home." `OPERATOR.md:195-197`: acceptance
  "assigns the chosen access plan, creates a personal App and key ... The new key
  is shown once."
- **Did.** Opened the link in a second clean browser context and filled "Your
  name", "Email", "Password", "Confirm password".
- **Showed.** The page named the plan ("Walk family chat"), then "Your account is
  ready" with the key in a text box and "Save it now; it will only be shown
  once." The button to go on is "I've saved it", not "Continue to Home". Home
  opened signed in as the member, with the app "My first app".

### S17. The member connects an app from Home, "Gateway check": DRIFT

- **Doc says.** `FOR_MEMBERS.md:30-34`: "Open `/`. Choose an app card. Create or
  select a key. Copy the base URL and key into your app. Use **Gateway check**,
  then send a message from your app." Line 52: "**Gateway check** tests the
  gateway with a real request."
- **Did.** Opened Home, chose the app card "My first app", then followed "Set up
  My first app". I searched Home, Apps & keys and Connect, with and without a
  client chosen, for "Gateway check".
- **Showed.** The app card opens Apps & keys, where the key is listed as the
  first 8 characters only. The **Set up** link opens Connect, which shows the
  address `http://127.0.0.1:5100/openai/v1`, the key hint ("the secret you saved
  when you created it") and the model `qwen2.5-0.5b`. No control and no text
  anywhere says "Gateway check". The nearest is the header button "Gateway
  status", which opened a panel that read "Gateway: Reachable, checked just now".
  Home also has a chat box with a **Send** button. FOR_MEMBERS never mentions it.
- **Verdict.** DRIFT. A member who follows step 5 looks for a control that does
  not exist. The terms file defines the label (`web/src/lib/terms.ts:102`) but
  no screen uses it.

### S18. The member's first chat, with their own key: PASS

- **Doc says.** `FOR_MEMBERS.md:34`: "send a message from your app." Line 94:
  "Open `/usage/` to see your usage when measurement data exists."
- **Did.** One `curl` to `/openai/v1/chat/completions` with the member's key and
  the model from Connect. Then I opened Usage as the member.
- **Showed.** `200` in 0.3 s: `"Hello! How may I help you?"`. The member's Usage
  showed "1 request", "0 failed", and the receipt named the app "My first app"
  and the model "Qwen2.5 0.5b". The member saw only their own request, not the
  owner's five.

## Things seen that are not doc drift

- **Status after a chat that works.** After S11 the Status page still said "LiteLLM
  is not accepting connections", "5 of 5 capabilities need attention" and "Down
  since about 1:05 PM". The gateway could reach LiteLLM, and chat worked.
  **Refresh** did not clear it. This is a product defect, not a doc one.
- **"Person: Not recorded"** showed on the receipts for both the owner's key (S13)
  and the member's key (S18).
- **Failed requests count as usage.** My `500` and `400` answers show in Usage as
  failed chat requests (S12).
- **Disk warning.** Status also said "System disk (/) is almost full (92%)". That
  is a property of the test host.

## Ranked DRIFT items and the smallest doc change for each

1. **Compose and Ollama: the doc's address passes the check, then chat returns
   `500` (S10).** Edit `FIRST_RUN_SETUP.md:202-207` and `OPERATOR.md:61-64`. For
   Compose, tell the owner to use the host's Docker bridge address (the
   `docker0` address from `ip addr show docker0`) in **Add engine** instead of
   `host.docker.internal`, and say why: the chat service in Compose cannot
   resolve that name. (The product fix is one `extra_hosts` line on the `litellm`
   service in `docker-compose.yml`. After that fix, `host.docker.internal` would
   work and this item would close.)
2. **No layout for an engine run with `docker run -p` (S1).** Add one bullet to
   `FIRST_RUN_SETUP.md` under "Connect an engine by where it runs": "Engine in a
   Docker container on the same computer: publish it on the host's Docker bridge
   address, for example `-p <docker0-address>:11434:11434`, not on `127.0.0.1`.
   Then use that address in **Add engine**." Say that a `127.0.0.1` publish
   answers "Connection refused" from the gateway container.
3. **The invite dialog needs an email or the "Allow anyone" tick (S15).** Add one
   sentence to `README.md:139` and after `OPERATOR.md:172`: "Type the person's
   email, or tick **Allow anyone with the link to create an account and receive
   an API key**. **Create invite link** stays off until you do one of the two."
   Add: "Replace `127.0.0.1` in the link with the gateway's public address before
   you send it."
4. **"Gateway check" does not exist for a member (S17).** Edit
   `FOR_MEMBERS.md:34` and `:52`. Replace step 5 with "Open **Set up** on the app
   card (Connect), copy the address, key and model, then send a message from
   your app". Mention the Send box on Home as the quickest first chat. Delete or
   fix the sentence at line 52. Or add the control to the screen.
5. **Compose subnet overlap needs two variables (S2).** Add to `README.md:41` or
   `FIRST_RUN_SETUP.md:222-230`: "If Docker says `Pool overlaps with other one on
   this address space`, set both `AZIZAM_DOCKER_SUBNET` and
   `AZIZAM_DOCKER_GATEWAY` (the first address in that subnet) to unused values."
6. **`FIRST_RUN_SETUP.md` says `/setup/` mints a key (S5).** Change lines 7, 28-29
   and 330 to say that `/setup/` creates the owner and the key comes from **Apps
   & keys** (`README.md:45`).
7. **New app needs the "Acts as" choice; "Create key" is "Create another key"
   (S6, S7).** Add to `README.md:45` and `OPERATOR.md:21`: "Choose who the app
   acts as (**You** or **A service**). **Create app and key** stays off until you
   do." Write the menu item as "**Create key** (**Create another key** if the
   app has one)".
8. **Usage opens on totals (S12).** Change `README.md:69` to "Open **Usage**,
   choose **View recent requests**, then open the newest row to see its
   receipt."
9. **Small label differences (S4, S16).** "Email or username" for "an email
   address" (`README.md:43`). "I've saved it" for "Continue to Home"
   (`FOR_MEMBERS.md:21`).
