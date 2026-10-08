# Azizam voice and glossary

Applies to every word in the product UI. Marketing pages (azizam.dev) follow it too, with room for one headline.

## Voice in five rules

1. **Say what is true, then what to do.** Every state sentence names the fact, then at most one next action. "Images are paused. Ask Sam to turn them back on." Not "Images are currently experiencing degraded availability."
2. **Spend attention like money.** One fact appears once per screen. No sentence repeats a heading. Cut any word that does not change what the reader does. Owner, 2026-10-04: "this burns my attention by being repetitive".
3. **Plain words for people, exact words for builders.** Member surfaces never show internal terms (service record, binding, reconcile, profile, alias of alias). Builder surfaces show exact model names, request IDs and HTTP status, in monospace, only where they are copied or matched.
4. **Name the layer when something fails.** Your app, your key, the gateway, an engine or provider, a model, a workflow. If the evidence does not identify the layer, say so: "We couldn't tell where this stopped." Never blame the reader's connection or key without evidence.
5. **No marketing voice, no measuring-app voice.** No slogans, no exclamation marks, no "Oops". No "not measured yet" or "observation unavailable" phrasing where a person expects an answer; say "We haven't checked yet" or "Last checked 2 minutes ago".

## Money words

- A recorded charge: "USD 0.42". A charge under one cent: "Less than USD 0.01".
- A local request with routing evidence: "Local · no provider charge".
- Missing amount: "Cost not recorded" (never "Free", never blank, never "0").
- Before a paid run: "This uses [model], a paid model. About USD 0.02."

## Glossary (one name per concept)

| Use | For | Do not use |
|---|---|---|
| **Gateway** | This Azizam instance | server, proxy, backend (in UI) |
| **Engine** | A local AI service on the owner's hardware (ComfyUI, llama.cpp, a TTS server) | service, service record, backend, binding |
| **Provider** | A remote AI service the owner pays for (OpenRouter, OpenAI) | remote engine, upstream |
| **Model** | An AI model a request runs on | backend, route |
| **Model name** | The exact string to paste into an app | alias (for members), model id |
| **Alias** | Builder surfaces only: another accepted model name | - |
| **Capability** | What you can do: Chat, Images, Video, Voice, Transcription, Music, Sound effects, Embeddings | intent, resource class, kind |
| **Workflow** | A configured multi-step process with inputs and outputs | pipeline, graph, binding |
| **App** | Something that connects to the gateway (Open WebUI, a phone app, Daisy) | client, integration, consumer |
| **Key** | The secret an app uses | token, credential, API key (except in the "API key" field an app asks for) |
| **Access plan** | What a person or app may use and spend | profile, policy, plan (alone) |
| **Person** | Anyone with an account | user, account, principal |
| **Owner** / **Admin** / **Member** | Roles | operator (in UI), superuser |
| **Request** | One call an app makes | job (except long media jobs), execution |
| **Receipt** | The record of one request: who, what, outcome, cost, time | log, audit row, trace |
| **Creation** | One generated image, video or audio file in Library | asset, media item, artifact |
| **Spend** | Money recorded against providers | cost (for totals), billing |
| **Cap** | A spend limit | quota, budget (in UI) |

## Words that never appear in member surfaces

service record, binding, reconcile, principal, profile, resource class, intent, observation, partially checked, upstream, payload, JSON (except under "Technical details").
