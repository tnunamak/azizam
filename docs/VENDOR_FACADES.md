# Vendor facades: which adapters to extend, build, or skip

Status: decision document, 2026-10-02. No facade code exists on this branch.
Branch `vendor-facades`, base `159928d`. Full evidence is in the research entry
`ai/research/local-ai-gateway/` of the dotfiles repo (see "Evidence" at the end).

Scope change. This document first covered only the ElevenLabs facade. Tim asked
for a wider view: which adapters should the gateway extend (OpenAI, Anthropic,
Ollama) or create (new vendor shapes)? ElevenLabs is now one candidate among
many. Work on the ElevenLabs build stopped before any code was written.

## 1. Decision

Rule: build only when a named client or tool needs it and local models can honour
most of it. A facade must pull its weight in users, not in API coverage.

### Ranked list

| Rank | Item | Verdict | Effort | Reason |
|---|---|---|---|---|
| 1 | Rerank route: `POST /v1/rerank`, `/rerank`, `/v2/rerank` (Cohere/Jina shape; also accept TEI `texts` and Voyage `top_k`; return `results` and `data`) | BUILD (extends the OpenAI surface) | 1-2 d, plus an ops request to register a `llama-server --rerank` engine | One shape serves Open WebUI, Dify, RAGFlow, Continue, LlamaIndex, Vercel AI SDK, langchain-cohere. llama.cpp already speaks it. The gateway has no reranker today. |
| 2 | Gateway MCP server: 3 meta-tools (`capabilities`, `run`, `job`), streamable HTTP, bearer app key; refresh the skills catalog | BUILD (not a vendor facade) | 1-2 d after typed manifests land (discovery worktree); +2-3 d if OAuth is needed for claude.ai or ChatGPT | Claude Code, Cursor, Codex and Daisy (Pi loads `pi-mcp-adapter`) gain every media workflow at about 200 standing tokens. Official vendor MCP servers are hosted and cannot be pointed at us. |
| 3 | A1111 `/sdapi/v1` thin subset (txt2img, img2img, options, samplers, schedulers, sd-models, progress, interrupt, upscalers) mounted at host root | BUILD thin | 2-3 d | SillyTavern (Tim's :8000) reaches a local image server only through `/sdapi/v1` or ComfyUI. Its `openai` image source is hard-coded to api.openai.com. It adds no capability. It is a translator over the existing image routes. |
| 4 | Small extends: Anthropic `cache_control` passthrough, `cache_*_tokens: 0` in usage, `anthropic-beta` tolerance, SSE `ping` events; OpenAI `GET /v1/models/{id}`; embeddings task-prefix aliases (Cohere `input_type`, Gemini `taskType` onto nomic prefixes), base64 and `dimensions` conformance | EXTEND | about 2 d in total | Cheap. Claude Code and the Agent SDK send these. Coordinate with the `-proto` worktree, which owns Anthropic, Ollama and Responses conformance. |
| 5 | Client recipes for SillyTavern, OpenWhispr, Daisy, ComfyUI; `wyoming_openai` bridge for Home Assistant | DOC | 0.5 d | Only SillyTavern is in the compatibility matrix, as "unverified". |
| 6 | Conditional builds (need a named client first): ElevenLabs thin subset, Runway, MiniMax (MCP and CLI users), Replicate predictions, Gemini `generateContent`, OpenAI Batch + Files + Uploads (and Anthropic Message Batches), Conversations + items, Vector stores, Moderations | DEFER | ElevenLabs 3-5 d; Runway 3-4 d; MiniMax 2-3 d; Replicate 3-4 d; Gemini 5-8 d; Batch/Files 3-4 d; Conversations 1-2 d; Vector stores 4-6 d; Moderations needs a local classifier | Each passes some criteria, but no Tim tool needs it and the community shows no ask. See sections 3 to 5. |
| 7 | Skip list: OpenAI Realtime WebSocket (8-15 d, revisit on trigger), fal queue, Vertex, Bedrock, Mistral, Cohere chat, Kobold-native, Azure (alias shim only on demand), Kling, Luma, Pika, Stability, BFL, Ideogram, Cartesia, Deepgram, AssemblyAI, Gladia, Speechmatics, Azure Speech, Google Cloud speech, Suno, Udio, Wyoming (recipe only), ComfyUI native facade, Ollama blobs/create/web-search | SKIP | none | Fails S1 (no base-URL override), S5 (local models cannot honour it), or has no client. See section 3. |

### Top 3

1. Rerank route. Best score on every criterion, 1-2 days.
2. Gateway MCP server plus skills refresh. Best value for Tim's own agents.
   Wait for the typed manifests so the tools can be generated from them.
3. A1111 `/sdapi/v1` thin subset. The only change that makes SillyTavern's
   image generation reach the gateway.

If only one: MCP for Tim's agents; rerank for third-party RAG users.

### What not to do, and why

- Do not build a vendor facade because the vendor is famous. The community
  evidence for vendor-shaped local servers (ElevenLabs, fal, Replicate,
  Runway, Kling, Veo, Luma, Suno) is a negative result. People ask for the
  OpenAI and Anthropic shapes, rerank, ComfyUI control and model swapping.
- Do not build OpenAI Realtime now. It needs about 11 client events and about
  46 server events, plus server VAD, barge-in handling and a live-microphone
  STT hop that does not exist. Pipecat and LiveKit already work against the
  gateway's HTTP STT, chat and TTS routes. OpenWhispr's realtime path is
  hard-coded to api.openai.com.
- Do not build Bedrock, Vertex or Azure shapes. They add routing or signing
  cost and no capability. Claude Code on Bedrock is better served by the
  existing Anthropic adapter.
- Do not build fal. The Python client forces `https://` and has a fixed upload
  host. The JS client needs a proxy header. Every model has its own schema.
- Do not build Suno or Udio. Suno has no official API (a partner intake form was
  announced on 2026-07-01, no launch date). Udio's help center says there is no
  public API.
- Do not accept and ignore output-changing parameters. Section 6 sets the rule.
- Do not mount a facade under a path prefix when its client drops the prefix
  (SillyTavern's image code, the BFL and Gladia SDKs). Use host root or a
  dedicated port for those.

### Triggers that would change the ranking

- A named client for a deferred item appears (for example a Pipecat or LiveKit
  agent that needs the ElevenLabs shape, or a Runway SDK user).
- The `-video` worktree ships extend, and a Runway or Google SDK user asks for it.
- Open WebUI or LibreChat turn their reranker on (raises rerank value).
- A local speech-to-speech model appears (raises Realtime S5).
- Home Assistant core adds a base-URL option for OpenAI or ElevenLabs.
- Suno publishes a public API with a stable shape.

## 2. Method

Each candidate gets five scores, 0 to 2, with evidence.

| Score | Question |
|---|---|
| S1 | Does the official SDK or tool allow a base-URL override? Verified in SDK source. |
| S2 | Does the vendor vocabulary expose capabilities the gateway has and the current adapters cannot express? |
| S3 | Ecosystem reach: SDKs, MCP servers, agent skills, apps, RAG frameworks, and tools Tim runs. |
| S4 | Fidelity cost and maintenance (higher means cheaper): API size, churn, streaming or WebSocket needs, auth. |
| S5 | Local fit: can local models honour it? |

Admission rule for a new facade: S1 at least 1, S5 at least 1, and a named
client (an app, SDK user, MCP server or skill that would run against it).
The named-client gate is new. Without it, the prior corpus entry and this
research both show that facades wait for a client that never comes.

Sources of evidence: SDK source read in `~/.tmp/facades-sdks` (packages
downloaded with `uv` and `npm pack`, never `/tmp`), installed apps on Tim's
machine (read-only), about 330 redlib searches over r/LocalLLaMA,
r/StableDiffusion, r/SillyTavernAI, r/comfyui, r/homeassistant and related
subs, and GitHub README and star data. Six workers gathered it. The
orchestrator spot-checked the base-URL quotes marked "checked" below.

## 3. Candidates by modality

Scores are S1/S2/S3/S4/S5. A range means the evidence supports both ends.

### 3.1 Existing adapters: extend

| Gap | S1 | Reach | Backing | Effort | Verdict |
|---|---|---|---|---|---|
| Anthropic cache/beta/ping polish | 2 | Claude Code, Agent SDK, Cline | existing translator | 0.5 d | extend |
| OpenAI `GET /v1/models/{id}` | 2 | OpenAI SDK users | catalog | 0.25 d | extend |
| Embeddings prefix aliases, base64, `dimensions` | 2 | RAG tools | nomic via LiteLLM; `dimensions` already truncates and renormalizes (`proxy.py` near line 21088) | 1 d | extend |
| OpenAI Batch, Files, Uploads; Anthropic Message Batches | 2 | LiteLLM batch users, eval harnesses | media-jobs queue, asset store, receipts | 3-4 d | defer; no Tim tool uses them, community negative |
| Conversations + items | 2 | Responses clients | `responses_store.py` | 1-2 d | defer to the `-proto` worktree |
| Vector stores, `file_search` | 2 | Responses `file_search` users | qdrant (:6333 is OpenWhispr's private sidecar, not shared), nomic | 4-6 d | defer |
| Moderations | 2 | few | needs a Llama Guard class model; none installed | 1.5 d + model | defer |
| Realtime WebSocket | 2 | Pipecat, LiveKit, Agents SDK | STT + chat + TTS; no live-mic STT hop | 8-15 d | skip now |
| Responses over WebSocket | 2 | Codex CLI | translator | 2-3 d | optional |
| Ollama `blobs`, `create`, `keep_alive` | 2 | Open WebUI model upload | gateway owns no model files | n/a | skip |
| Ollama web search and fetch | 0 | none | SDK hard-codes ollama.com | n/a | skip |
| Assistants/Threads, fine-tuning, evals, containers | n/a | none | no local analogue | n/a | skip |

Current state taken from the `-proto` worktree docs (read-only, not re-run):
Anthropic supports messages, streaming, tools, thinking, images, models and an
estimated `count_tokens`; Batches, Files, documents and server tools are
unsupported; `cache_control` is ignored. Ollama supports chat, generate, embed,
tags, show, ps and version; `pull`, `push`, `create`, `copy`, `delete` and
`blobs` return 501. `docs/KNOWN_WORK.md:216` defers Files, Batches, Realtime,
Assistants and fine-tuning.

What Claude Code and the Agent SDK call: `POST /v1/messages?beta=true`, with
`count_tokens` and `GET /v1/models` optional. They do not use Files or Batches.
Model discovery keeps only ids that contain "claude" or "anthropic", so a
`default` alias is hidden; name discovery aliases `claude-local-*`.

### 3.2 LLM vendors, embeddings and rerank

| Candidate | S1 | S2 | S3 | S4 | S5 | Verdict | Why |
|---|---|---|---|---|---|---|---|
| Rerank (`/rerank` family) | 2 | 2 | 2 | 2 | 2 | BUILD | See section 1. |
| Gemini `generateContent` | 2 | 1 | 2 | 1 | 1 | defer | `GOOGLE_GEMINI_BASE_URL` works (checked). Gemini CLI and the AI SDK honour it. Most clients also speak OpenAI. Large schema; Live, Files and grounding stay unsupported. |
| Vertex shape | 1 | 0 | 1 | 0 | 1 | skip | Adds only a project and location path prefix. Alias it if Gemini is built. |
| Mistral | 2 | 1 | 1 | 1 | 1 | skip | Vibe CLI takes `api_style="openai"`. No local OCR. |
| Cohere chat and embed | 2 | 1 | 1 | 2 | 1 | skip | Rerank is folded into the rerank route. SillyTavern hard-codes `api.cohere.ai`. |
| Bedrock Converse | 1 | 0 | 1 | 0 | 0 | skip | AWS eventstream framing. Claude Code docs say gateways cannot route Claude Code to non-Claude models. |
| Azure OpenAI path shape | 2 | 0 | 1 | 2 | 2 | alias on demand | Pure routing; check for collisions with `/openai/`. |
| OpenRouter extensions | 2 | 1 | 1 | 2 | 1 | tolerate fields | Not source-verified. |
| KoboldAI native API | 1 | 0 | 0 | 2 | 1 | skip | Tim's SillyTavern uses llama.cpp and a custom OpenAI source. |

Rerank facts. llama-server in the builds `llama-mtp`, `llama-turboquant`,
`koboldcpp` and `llamacpp-official-b11337` registers `POST /rerank`,
`/reranking`, `/v1/rerank`, `/v1/reranking`. It must start with `--rerank`
(env `LLAMA_ARG_RERANKING`), an embedding model and `--pooling rank`. It accepts
the Jina/Cohere body (`documents`) and the TEI body (`texts`). The `--rerank`
flag is confirmed by `llama-server --help` on b11337 (checked). The
`docs/design/ENGINE_REGISTRY_DESIGN.md:96` engine registry already lists
`rerank`. Both 3090s sit at about 19.5 and 22.8 GB used, so only a 0.6B to 4B
reranker fits on GPU; a 0.6B model can run on CPU. Candidate models:
bge-reranker-v2-m3 Q8, Qwen3-Reranker-4B. One r/LocalLLaMA benchmark
(2026-08-09) found bge-reranker-v2-m3 slightly lowered MRR on a translation
memory task, so Tim needs his own eval before choosing.

Consumers that take a rerank base URL: Open WebUI, Dify, RAGFlow, Continue,
LlamaIndex, Vercel AI SDK, langchain-cohere, LibreChat web search (Jina URL).
Consumers that do not: AnythingLLM (native ONNX), Khoj, `langchain_community`
JinaRerank (hard-coded URL), Perplexica, SillyTavern.
Open WebUI and LibreChat on Tim's vivid-fish host have the reranker off today.

### 3.3 Image

| Candidate | S1 | S2 | S3 | S4 | S5 | Verdict | Why |
|---|---|---|---|---|---|---|---|
| A1111/Forge/SD.Next `/sdapi/v1` | 2 | 1 | 2 | 1 | 1-2 | BUILD thin | Section 1. Open WebUI and LibreChat also call it, but both also accept an OpenAI Images URL. SillyTavern does not. |
| ComfyUI native API | 2 | 1 | 1 | 0 | 1 | skip | The payload is a node graph. |
| BFL (`api.bfl.ai`) | 1 | 0-1 | 1 | 2 | 1 | skip | Python SDK uses `urljoin` and drops path prefixes. MCP is hosted. |
| Ideogram | 0 | 0 | 0 | 2 | 0 | skip | No official SDK. |
| Recraft | 2 | 0 | 0 | 2 | 0 | skip | Its docs say the OpenAI SDK works with a custom base URL, so the Images route serves it. |
| Stability | 0 | 1 | 0 | 1 | 1 | skip | No official REST SDK. `stability-sdk` is the legacy gRPC client. |
| Leonardo, Midjourney | 1 / 0 | 0-1 / 0 | 0 | n/a | 1 | skip | Midjourney has no official API. |

A1111 mapping notes. SillyTavern sends prompt, negative prompt, sampler,
scheduler, steps, cfg scale, size, `restore_faces`, `enable_hr` with hires
parameters, denoising strength and seed. The gateway already binds
negative prompt, seed, sampler, scheduler, steps, guidance, denoise, mask and
ControlNet through `extensions.com.vividfish.workflow`. It cannot do hires fix,
`restore_faces`, ADetailer, LoRA prompt tags or embeddings. The facade must name
unsupported fields in a header and reject output-changing ones.
SillyTavern replaces `url.pathname` (`src/endpoints/stable-diffusion.js:34`,
checked), so the routes must live at host root. stable-diffusion.cpp's server
is prior art: it serves OpenAI routes, a nine-route `/sdapi/v1` subset and a
native API on one port. A1111 upstream is dormant (last push 2026-03-02), so
the dialect is stable and cheap to maintain.

### 3.4 Video

| Candidate | S1 | S2 | S3 | S4 | S5 | Verdict |
|---|---|---|---|---|---|---|
| Runway | 2 | 2 | 1 | 1 | 2 | defer; first choice if a video facade is forced |
| Google genai (Veo) | 2 | 1-2 | 2 | 1 | 1-2 | defer; widest reach |
| Replicate predictions | 2 | 1-2 | 1-2 | 1-2 | 1 | defer; optional generic envelope |
| fal queue | 1 | 2 | 1 | 1 | 1 | skip |
| Luma | 2 | 1 | 1 | 1 | 1 | skip |
| Kling | 1 | 1 | 1 | 0-1 | 1 | skip |
| MiniMax / Hailuo | 2 | 1-2 | 2 | 1 | 1-2 | conditional (see 3.5) |
| Higgsfield | 2 | 1 | 1 | 1 | 1 | skip |
| Pika | 0 | 0-1 | 0 | 1 | 0 | skip |

See section 7 for the video recommendation.

### 3.5 Speech, audio and music

| Candidate | S1 | S2 | S3 | S4 | S5 | Verdict | Why |
|---|---|---|---|---|---|---|---|
| ElevenLabs | 1 | 2 | 1 | 1 | 2 | defer | Python and JS SDKs take `base_url` only (no env var). The official local MCP server is archived and cannot be pointed at a custom host (it accepts only a fixed `ELEVENLABS_API_RESIDENCY` map). ElevenLabs skills hard-code the host. The n8n node, Home Assistant core and SillyTavern hard-code it. Open WebUI, LibreChat, Pipecat and LiveKit accept a base URL but also speak OpenAI TTS. Real gain: SDK users, plus voice design, sound generation, music, `with-timestamps` and Scribe diarization, which OpenAI cannot express. |
| MiniMax | 2 | 1-2 | 2 | 1 | 1-2 | conditional | `minimax-mcp` needs `MINIMAX_API_HOST`; the MiniMax CLI reads `MINIMAX_BASE_URL`. About nine routes (TTS, voice clone, voice design, t2v, i2v, t2i) match gateway capabilities. It is the one official local MCP server we could serve without writing our own. |
| Cartesia | 2 | 0-1 | 1 | 1 | 1 | skip | `CARTESIA_BASE_URL` plus a separate `websocket_base_url`. Low marginal value. |
| Deepgram | 2 | 1 | 2 | 1 REST / 0 live | 1 | skip REST, defer live WS | The live WebSocket is the real gap. WhisperLiveKit's adapter is 1,136 lines. Open WebUI hard-codes `api.deepgram.com`. |
| AssemblyAI | 2 | 0-1 | 2 | 1 | 0-1 | skip | Mostly audio-intelligence features the gateway lacks. |
| Gladia, Speechmatics, Azure Speech, Google Cloud TTS/STT | 1-2 | 0-1 | 1 | 0-1 | 1 | skip | Prefix dropped (Gladia), SSML plus proprietary WebSocket (Azure). |
| Suno | 0 | 1 | 0 | n/a | 1 | skip | No official API. |
| Udio | n/a | n/a | n/a | n/a | n/a | skip | No public API. |
| Mureka, Beatoven, Loudly | 1 / 1 / ? | 1 / 0-1 / ? | 0-1 | 1 | 1 | skip | Small ecosystems. |
| Wyoming (Home Assistant) | 1 | 1 | 1 | 1 | 2 | skip | The `wyoming_openai` bridge (218 stars) already fronts OpenAI-shaped STT and TTS. Write a recipe. |

### 3.6 Cross-cutting interfaces

| Candidate | S1 | S2 | S3 | S4 | S5 | Verdict |
|---|---|---|---|---|---|---|
| MCP server, 3-4 meta-tools | 2 | 2 | 2 | 1 | 2 | BUILD |
| Skills and `llms.txt` (already built) | 2 | 2 | 2 | 2 | 2 | extend: the catalog lists only `openai-proxy-gateway`; `autotile-edge-stitch` is deliberately unlisted |
| OpenAI Realtime WebSocket | 2 | 1 | 2 | 0 | 1 | skip now |
| Replicate predictions as generic job envelope | 2 | 2 | 1 | 1-2 | 1 | defer |
| fal queue as generic job envelope | 1 | 2 | 1 | 1 | 1 | skip |
| `/v1/models` metadata richness | 2 | 1 | 1 | 2 | 2 | extend, small |

MCP facts. The MCP spec revision 2026-07-28 has no initialize handshake or
session and uses JSON Schema 2020-12 for `outputSchema`. The official media MCP
servers (fal, Replicate, Runway, ElevenLabs) are all hosted and expose generic
search, schema and run tools, not one tool per model. Daisy loads
`npm:pi-mcp-adapter` (`~/applications/daisy/.pi/settings.json:15`, checked). Its README claims one proxy tool of about 200 tokens with lazy
server start, so the Pi author's 13.7k-token MCP cost figure in the earlier
corpus entry does not apply to Daisy. Suggested tools: `capabilities` (search),
`run` (workflow id and inputs, returns a job id or asset link), `job` (status,
wait, cancel), generated from `/gateway/v1/capabilities` and the workflow
manifests.

## 4. SDK base-URL evidence

"Checked" means the orchestrator re-read the line. Other rows come from worker
notes with path and line; they were not re-read.

| Tool | Version | Override | Evidence | Caveat |
|---|---|---|---|---|
| runwayml (py) | 5.20.1 | `base_url` or `RUNWAYML_BASE_URL` | `runwayml/_client.py:153` (checked) | Path prefix kept. npm 4.20.1 `client.js:70` also reads the env var. Uploads use a server-supplied `uploadUrl`. |
| google-genai (py) | 2.27.0 | `http_options.base_url`, `GOOGLE_GEMINI_BASE_URL`, `GOOGLE_VERTEX_BASE_URL` | `google/genai/_base_url.py:48-50` (checked) | Live WebSocket host derives from the base URL (`_api_client.py:1297`). npm 2.26.0 reads the same variables. |
| Gemini CLI core | 0.62.0 | `GOOGLE_GEMINI_BASE_URL` | `contentGenerator.js:46` | Plain http only for localhost; LAN hosts need https. |
| replicate (py) | 1.0.7 | `base_url` or `REPLICATE_BASE_URL` | `replicate/client.py:381` (checked) | The local `replicate-mcp` 0.9.0 should inherit it; inferred, not run. |
| fal-client (py) | 1.0.3 | `FAL_RUN_HOST`, `FAL_QUEUE_RUN_HOST` (host only) | `fal_client/auth.py:85`; `client.py:83-84` (checked) | Scheme forced to `https://`. `REST_URL` and `CDN_URL` hard-coded, so `upload_file` cannot move. JS needs `proxyUrl` plus an `x-fal-target-url` header. |
| elevenlabs (py, npm) | 2.70.0 | `base_url` argument only | `base_client.py:92` | Paths are relative (`v1/text-to-speech/...`), so `base_url=<gw>/elevenlabs` yields `/elevenlabs/v1/...`. The CLI has `ELEVENLABS_BASE_URL`. `elevenlabs-mcp` 0.12.2 cannot be pointed. |
| cartesia (py) | 4.2.0 | `CARTESIA_BASE_URL`, `websocket_base_url` | `_client.py:127-130` | Local MCP not run. |
| lumaai (py) | 1.21.0 | `LUMAAI_BASE_URL` | `lumaai/_client.py:81-84` | Prefix kept in a smoke test. |
| blackforest (py) | 0.1.4a1 | `base_url`, no env var | `blackforest/client.py:43` | `urljoin` drops path prefixes. |
| deepgram-sdk (py) | 7.12.0 | `DeepgramClientEnvironment(base, production, agent, agent_rest)` | `deepgram/environment.py` | No env var. Prefix kept. |
| assemblyai (py) | 1.6.1 | `base_url`; streaming via `api_host` | `assemblyai/types.py:137` | |
| gladiaio-sdk | 2.1.0 | `GLADIA_API_URL` | `client_options.py:112` | Prefix dropped. |
| minimax-mcp | 0.0.19 | `MINIMAX_API_HOST` (required) | `minimax_mcp/server.py:36-43` | CLI: `MINIMAX_BASE_URL` (`src/config/loader.ts:86`). |
| mistralai (py) | 3.0.0 | `server_url` | `client/sdkconfiguration.py:51-59` | Vibe CLI: `api_base`, `api_style="openai"`. |
| cohere (py) | 5.21.1 | `base_url` | `base_client.py:113` | |
| openai (py) | 3.24.0 / 2.54.0 (two workers) | `OPENAI_BASE_URL`, `base_url`, `websocket_base_url` | `_client.py:306`; `resources/realtime/realtime.py:803-812` | The two workers reported different versions; unresolved. Realtime URL appends `/realtime`. |
| anthropic (py) | 1.11.0 | `ANTHROPIC_BASE_URL` | `_client.py:234` | TS 0.131.0 `client.js:71`. |
| ollama (py) | 0.6.3 | `OLLAMA_HOST` | `_client.py:117` | `web_search` and `web_fetch` hard-code ollama.com. |
| Pipecat Realtime | 1.12.0 | `base_url` | `services/openai/realtime/llm.py:240` | |
| LiveKit Realtime | n/a | `base_url` or `OPENAI_BASE_URL` | `_realtime/openai.py:540` | |
| SillyTavern 1.18.0 | local | image `openai` source hard-coded | `src/endpoints/openai.js:630` (checked) | STT, video and TTS `openai` providers also hard-coded; only its `openai-compatible` TTS takes a URL. |

## 5. Community and local-stack evidence

Demand, strongest first (from about 330 redlib searches; keyword search is
imperfect, so "not found" means those queries on those subs only):

1. OpenAI chat, models, embeddings: strong.
2. OpenAI audio speech and transcriptions: strong. Complaint: "it doesn't
   actually stream".
3. Anthropic Messages (`ANTHROPIC_BASE_URL`): strong. llama.cpp, llama-swap,
   Lemonade and LocalAI serve it.
4. ComfyUI native API plus MCP: strong supply, split stance.
5. Model-swap layer behind OpenAI and Anthropic dialects (llama-swap pattern,
   450 points): strong. llama-swap also serves rerank and `/sdapi/v1`.
6. Wyoming: strong inside Home Assistant, but it is a TCP protocol.
7. Ollama native: medium.
8. Jina/Cohere-style rerank: medium. Open WebUI expects it.
9. Responses, Realtime, A1111, OpenAI Images: medium to weak.
10. ElevenLabs shape: weak. Supply only (LocalAI, small bridges). ElevenLabs'
    free-tier TTS API access for Home Assistant ends 2026-12-31 and users
    answered by praising self-hosting, not by asking for a clone.
11. Gemini on a local server, Suno, fal, Replicate, Runway, Kling, Veo, Luma,
    Batch, Files, Vector stores, Moderations: negative.

Nearest competitors by dialect: LocalAI (49.4k stars: OpenAI, Anthropic,
ElevenLabs, Realtime, rerank, Responses), llama-swap (5.8k: OpenAI, Anthropic,
rerank, `/sdapi/v1`, ComfyUI), Lemonade (5.8k: OpenAI, Anthropic, Ollama).
Nobody serves a Runway, Replicate or fal shape locally.

What Tim runs (read-only inventory):

| Tool | Talks to today | Would gain from |
|---|---|---|
| SillyTavern 1.18.0 (:8000) | llama.cpp direct (:5051); custom OpenAI source at llm.vivid.fish; ComfyUI direct (:7890); user `tim` uses the gateway for TTS, but his `openai` image source cannot reach it | `/sdapi/v1` |
| OpenWhispr 1.7.6 | STT and cleanup LLM at openai.vivid.fish (batch only); its Realtime, Deepgram and AssemblyAI paths are hard-coded | nothing new now |
| Daisy / Pi | gateway via `openai-completions` plus helper scripts; loads `pi-mcp-adapter` | MCP server |
| Claude Code | Anthropic directly; no `ANTHROPIC_BASE_URL` set | small Anthropic polish |
| Open WebUI v0.11.3, LibreChat (host vivid-fish) | gateway via OpenAI shapes; rerank off | rerank |
| ComfyUI | driven by the gateway | none |

## 6. Architecture for any facade we do build

Each facade is a thin adapter on the shared capability core.

- No business logic in the adapter. It maps auth headers, translates request and
  response, shapes errors and frames streams.
- Dispatch in process to the existing core routes (`/v1/audio/speech`,
  `/v1/audio/transcriptions`, `/v1/audio/generate`, `/v1/audio/voices`,
  `/v1/images/*`, `/v1/videos`). The core handlers already authenticate, apply
  profile policy, write receipts and manage resources, so the facade inherits
  them. Not prototyped; the pattern follows `gateway/voices_api.py`, which takes
  its dependencies by injection and registers routes straight on `app`.
- Auth. `get_client_api_key_from_headers` (`proxy.py:1992`) already accepts
  `Authorization: Bearer`, `x-api-key` and `api-key`. A vendor header such as
  `xi-api-key` is mapped to a bearer key inside the adapter.
- Mount points. Use `/<vendor>/...` when the SDK keeps the prefix (checked for
  Runway, Google, Luma, Deepgram, Replicate). Use host root or a dedicated port
  when it does not (SillyTavern image code, BFL, Gladia).
- Errors. Use the vendor's own error body and status codes, so the vendor SDK
  raises its normal exception types.
- Unsupported features. Return a vendor-shaped error. Never return success.
  - Tuning knobs that do not change the contract may be ignored, and the
    response lists them in `x-gateway-ignored-params`.
  - A parameter that changes what the response contains or promises must be
    rejected. Examples: entity redaction, webhooks, C2PA signing, fine-tune ids,
    composition plans, hires fix on an image route that cannot do it.
- Fidelity tests. Each facade ships tests that run the vendor's official SDK
  against a hermetic gateway instance, plus response validation against a pinned
  vendor OpenAPI (kept as a trimmed subset in `tests/fixtures/`, with sha256 and
  fetch date). Track SDK releases; drop a facade that nobody uses.
- Discovery. List each facade in `/.well-known/ai-gateway` and `llms.txt` with
  its support tier from `docs/API_TARGETS.md`.

Measured facts that bear on a TTS or STT adapter (live gateway on :5000,
2026-10-02, one request each):

- `POST /v1/audio/speech` passes `speed` through (4.28 s of audio at 1.0, 2.68 s
  at 1.6). `seed` is accepted and has no visible effect.
- Core `pcm` output is 24 kHz signed 16-bit mono. A vendor `output_format`
  (mp3, opus, ulaw, wav at other rates) therefore needs an ffmpeg transcode,
  which `gateway/speech_stream.py` already does for the OpenAI formats.
- `whisper-1` with `response_format=verbose_json` and word granularity returns
  word start and end times. It reports the language as a name ("english"), not
  an ISO code.
- A request with `model=stt-diarize` and `response_format=diarized_json`
  returned `404 {"detail":"Not Found"}` from the live gateway. The base commit
  contains the diarize wiring, so the running process may predate it. Not
  investigated here.

## 7. Video facade recommendation

Recommendation: build none now. If Tim wants one, build Runway first.

Why none now. The community evidence for any video-vendor shape is a negative
result, no tool Tim runs speaks one, and the gateway's native `POST /v1/videos`
shape already works for Daisy. The `-video` worktree is still adding extend.
The most useful video investment is exposing every video operation through the
MCP server and skill, generated from the manifests.

Why Runway first. It is the only vendor with one uniform async envelope
(`POST` returns an id, `GET /v1/tasks/{id}` polls) that can carry every gateway
video operation, and `runwayml` honours `RUNWAYML_BASE_URL` (checked).

| Gateway capability | Runway route | Fit |
|---|---|---|
| Generate (text to video) | `text_to_video` | direct |
| Image to video, first and last frame | `image_to_video` with `promptImage[{uri, position}]` | direct |
| Remix (Bernini-R) | `video_to_video` (aleph2) | direct |
| Extend (coming) | `video_to_video` with `mode: "extend"` | direct |
| Character replace (SCAIL) | `character_performance` (act_two) | partial |
| Upscale | `video_upscale` | direct |
| Interpolation (RIFE) | `enhance_frame_rate` | exact |

Model names are free strings, so local models can sit under any name.
Weak point: ecosystem. The official Runway MCP server is archived and its
replacement is hosted-only (`https://mcp.runwayml.com/mcp`). The Runway skill
honours `RUNWAY_SKILLS_BASE_URL` in one script, while another helper hard-codes
the host (which one `rw-generate-video` uses is unconfirmed). Real users are
developers with the `runwayml` SDK.

Why Google genai is second. It has the widest reach: `GOOGLE_GEMINI_BASE_URL`
works in the Python and JS SDKs, Gemini CLI and the AI SDK, and it brings
Imagen, TTS and embeddings with it. Its Veo vocabulary covers generate, image to
video, `lastFrame`, `referenceImages` and extend (`source.video`). It cannot
express remix, character replace, upscale or interpolation. Gemini's own docs
now steer new work to Gemini Omni Flash rather than Veo. Pick Google if reach
matters more than capability.

Why not fal: `https://` is forced, upload hosts cannot move, and each endpoint
has its own schema. Replicate is an optional generic envelope
(`REPLICATE_BASE_URL` works), but each model slug needs its own input schema.
Luma, Kling, Pika, Higgsfield are skipped (see 3.4).

## 8. If an ElevenLabs facade is built later (design sketch, not built)

The earlier plan, kept so the work is not lost. Mount `/elevenlabs/v1/...`;
the SDK takes `base_url=<gw>/elevenlabs`.

| ElevenLabs feature | Maps to | Status |
|---|---|---|
| `text-to-speech/{voice_id}` and `/stream` | `/v1/audio/speech` with `pcm`, ffmpeg to `output_format`; `voice_settings.speed` becomes `speed` | works |
| `with-timestamps` | STT word times, spread over characters | works, approximate; flag in a header |
| `voices` list and get, `v2/voices` | built-in names plus custom voices | works |
| `voices/add` (IVC) and delete | `/v1/audio/voices` | works |
| `text-to-voice/design` and create-from-preview | prompt voices (`type: prompt`) | works with preview cleanup |
| `models` | ElevenLabs model ids mapped to gateway aliases | works |
| `sound-generation` | `/v1/audio/generate` `sfx` | works |
| `music` | `/v1/audio/generate` `music` or `song` (lyrics) | works; sections and plans unsupported |
| `speech-to-text` (Scribe) | `/v1/audio/transcriptions`; `stt-diarize` for speakers | works; no audio events, ISO code mapped from name |
| Speech to speech, audio isolation, forced alignment | no local engine | vendor-shaped error |
| Dubbing, agents (137 paths), Studio, pronunciation dictionaries, PVC | none | vendor-shaped error |
| WebSocket TTS and Scribe realtime | none | vendor-shaped error |

Ignorable with header: stability, similarity boost, style, speaker boost,
`seed`, `previous_text`, `next_text`, request ids, `apply_text_normalization`,
`enable_logging`, `optimize_streaming_latency`. Reject: entity detection and
redaction, `webhook`, `cloud_storage_url`, `source_url`, `finetune_id`,
`sign_with_c2pa`, `composition_plan`.

## 9. Confidence, disagreements, unverified

Confidence. High on the SDK base-URL facts for the rows marked checked, and on
the gateway's current route gaps. Medium on community demand: redlib search is
keyword-based and returned noise for some queries. Medium on effort figures:
none was prototyped. Low on Realtime effort and on any claim marked "from worker
notes".

Disagreements between sources.

- The prior corpus entry says Pi has no MCP. Daisy loads `pi-mcp-adapter`.
  The newer evidence wins.
- The prior entry says the ElevenLabs MCP server is deprecated in favour of a
  hosted one. Confirmed: archived 2026-08-20, no custom host.
- Pika: one worker found a README saying `pika.me/dev` is discontinued; the video
  corpus entry cites `dev.pika.art`. Not reconciled.
- The `openai` Python SDK version differs between two workers (3.24.0 and 2.54.0).
- Arena ranks Gemini Omni first for video, Artificial Analysis ranks Wan 3.0
  first (from the video entry); neither bears on the facade choice.

Not verified.

- No facade was run against a live gateway. No official MCP server was pointed
  at the gateway (the ElevenLabs one cannot be).
- Home Assistant's AI and voice configuration, n8n, and Open WebUI's DB-stored
  image, audio and rerank settings were not inspected.
- Whether LiteLLM batches, files, realtime and vector stores work with local
  backends.
- Whether the gateway streams `partial_images`, or supports `logprobs`, `n` and
  audio chat output.
- Codex CLI behaviour when WebSocket Responses is unsupported; Gemini CLI beyond
  the base-URL line.
- Rerank quality on Tim's data; GPU headroom for a reranker.

Side findings for the owner.

- The gateway's LiteLLM requirement is `litellm[proxy]>=1.77.0` (unpinned). The
  live venv has 1.86.2. A community thread (2026-03-24, 389 points) reports
  PyPI versions 1.82.7 and 1.82.8 were compromised. The installed version is
  newer, but pin and hash it.
- One worker's read of `~/applications/daisy/.pi/agent/telegram.json` printed
  the Daisy Telegram bot token into that worker's transcript. It was not saved
  to any file. Consider rotating the token.

## Evidence

- Corpus entry: `~/code/dotfiles/ai/research/local-ai-gateway/` (see
  `INDEX.md`), with every worker's notes, quotes and negative results.
- Working files: `~/.tmp/media-refresh/facades/` (`notes/`, `scoring-evidence.md`,
  `gateway-routes.txt`, `elevenlabs-openapi.json`).
- Prior entries: `audio-generation/a-local-media-gateway-should-fix-its-native-discovery-...`
  and `video-generation/long-video-api-should-be-explicit-extend-primitive-...`.
