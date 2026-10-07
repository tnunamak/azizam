# azizam-ux-terminology-20261007

Read-only terminology evidence inventory across all twelve listed files; ten concepts, source anchors, confirmed static-site minimum counts and ambiguity flags. Exhaustive runtime/imported occurrence totals remain unverified. Empty diff; no checks.

Terminology evidence inventory
Base: 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba. Read-only; no proposed rewrites, following the task-specific instruction.

Counting convention and limits
Numbers are confirmed static naming-site minimums, not exhaustive lexical or rendered-UI totals: case and ordinary singular/plural are grouped; one term is counted once per cited source line, including accessibility text and literal setup fields. Nested phrases overlap their parent noun counts. References below are representative evidence anchors, not every occurrence. Server values, imported labels, dynamically assembled text, and generated code cannot be exhaustively enumerated from these twelve files alone. Thus the requested exhaustive occurrence census remains unverified; these are not fabricated complete counts.

Path key (relative to web/src/app/ unless stated):
H home/page.tsx; R home/recipes.ts; A apps/page.tsx; M models/models-client.tsx; T try/page.tsx; U usage/page.tsx; Q activity/requests.tsx; S status/status-view.tsx; E engines/page.tsx; P providers/page.tsx; W workflows/page.tsx. N = web/src/components/shell/app-shell.tsx.

1. API key
“key(s)” ≥77 sites (H:350; A:358; P:580).
“API key” ≥10 (H:387; R:43,53,79,81,91,93,162; P:1544,1550), including “OpenAI API key” 2 (R:91,93) and “Provider API key” 2 (P:1544,1550).
“OpenAI key” 1 (R:86); “gateway key” 3 (R:43,81,93); “app key(s)” 2 (H:448; A:214); “Try key” 6 (T:929,947,966,1098,1553,1559); “replacement key” 3 (A:274,579; P:1954); “old key(s)” 3 (A:258,274,672); “previous keys” 1 (A:678).
Collision: “key” covers member app access, temporary Try access, provider credentials, and a gateway-management identity (N:859). OpenAI-branded recipe labels sometimes request the saved gateway key, not an OpenAI-issued credential (R:93).

2. App
“app(s)” ≥48 (H:347; A:285–286,473; Q:443); “Apps & keys” ≥2 (A:285; N:194); “App recipe” 1 (A:488); “App name” 2 (A:473; N:611); “gateway app” 3 (U:265,266,711); “trusted-network clients” 1 (Q:293).
Related “Source(s)” ≥6 (U:220,231,234,810; Q:242,249); “Your apps” ≥3 (A:307,312; U:341).
Collision: app means existing software (A:324), a gateway-created key container (“Create app and key”, A:527), and a selectable usage source. “App recipe” selects setup instructions rather than the named access container. “Source” also means an external application's connection type in R:81.

3. Access plan
“access plan(s)” 10 confirmed sites (H:466; A:313; M:899; P:746,757,1028,1172,1683,1796; W:815).
Shortened “plan(s)” additionally appears at P:1031,1686–1687; “default access” 1 (H:405); “key access” 1 (H:527); unqualified “access” ≥19 sites (H:410,467,643; A:467; N:876).
Collision: access covers permitted models/key creation and owner-level administration. “Owner access required” is a different concept from the account's access plan.

4. Model name / alias
“model(s)” ≥88 sites (H:382; M:886,1037); noun “model name” ≥7 (M:406,411,422,425,703,886; T:1615); “Exact model” 2 (M:703,979); “Names you can use” 2 (M:749; S:114); “Resolves to” 1 (M:708).
“model aliases” 2 (M:1004; P:2071); “alias” 1 (P:1421); workflow “Stable aliases” 2 (W:472,486).
“Requested model” 2 (U:765; Q:446); “Served model(s)” ≥7 (U:769; Q:450; E:87,99,103,105,669), plus “Models served” (E:350).
External setup labels: “models.default” 1 (R:64), “Custom model” 2 (R:77,81), “Add model” 2 (R:89,93), “ANTHROPIC_MODEL” 1 field (R:114), lowercase “model” 4 fields (R:102,126,139,151).
Collision: model denotes a user-facing selector, exact backend model, alias, and actual served model. T:1615 labels the workflow selector “Model name” too. “Alias” can target models or workflows.

5. Engine
“engine(s)” ≥40 sites (E:269,587; N:323); “Configured engine” 1 (M:256); “Engine binding” 2 (W:440,443); “workflow engine” 1 (W:815); “service config” 1 (E:373); “service record” 1 (E:657).
“Engine or provider” occurs in two fact/table labels (M:715,1039) and two failure-label definitions (U:908; Q:69).
Collision: the same engine becomes a service in advanced editing and missing-selection errors.

6. Provider
“provider(s)” ≥67 sites (P:1418,2046); “Remote provider(s)” 5 (M:256,263; P:370,1420,2047); “OpenAI-compatible provider” 1 (A:614); “paid routes” 1 (P:2047); “Provider ID” 1 (P:768).
Collision: provider means a configured remote paid service, but A:614 means a provider/connection option inside the user's external app.

7. Receipt / request
“request(s)” ≥82 sites (U:376,462; Q:214); “receipt(s)” ≥32 (U:650; T:2030); “request receipt(s)” 4 (H:568; U:737; Q:291,397); “Request ID” 5 labeling/copy sites (U:795,803; Q:441,470; P:1158).
“job(s)” ≥7 (T:1979,1994; W:658,659,667,673,680), including “Recent jobs” 5.
Collision: request is an operation, receipt its recorded evidence, but request/receipt labels navigate the same history. W:658–680 calls stored media outputs “jobs”; T:1979 describes a currently processing job. S:233 explicitly distinguishes request from receipt.

8. Capability
“capability” ≥9 sites (T:1580,1582; U:463,518,586; S:89,97; W:436,863); “capabilities” 4 (S:141,207; E:355,373); “tasks” 1 (H:639); “What you can use” 1 (H:634).
Explicit taxonomy declarations: Chat 3 (H:80; M:95; T:161), Images 2 (H:85; M:96) versus Image 1 (T:167); Video 2 (H:90; M:97); Music 2 (H:105; M:98); Sound effects 2 (H:110; M:99); Speech 3 (H:100; M:100; T:173); Audio 2 (H:135; M:101); Transcription 2 (M:102; T:179); Decisions 2 (M:103; T:185); Embeddings 2 (M:104; T:191); Reranking 1 (M:105). These declaration counts exclude imported labels and narrative mentions.
Collision: tasks/capabilities/models substitute for each other on availability surfaces; “Workflow(s)” is also a capability category (W:101; T:197).

9. Workflow
“workflow(s)” ≥30 sites (W:792; T:197); “Workflow manifest” 1 (T:594); “workflow ID” 1 (W:430); “Machine-readable workflow” 1 (W:466). “Workflow” labels both the configured process and its identifier (W:424–427); “Stable aliases” names alternate selectors.

10. Owner / member / person
“owner(s)” ≥13 sites (H:467,538; P:787; N:876), including “gateway owner(s)” 5 and “owner account” 2 (E:778; N:879); “admins” 2 (E:778; N:877).
“person” 7 (U:332,1082; Q:219,442; N:682,859,863); “People” 3 (N:602,665,860); “Everyone” 1 (U:340); “Your usage” 1 (U:336); self-scope “Your apps” 1 (U:341). No confirmed literal member-facing label “member”; internal role values are not UI evidence.
Collision: Person's self-filter reads “Your apps,” blending identity with source.

Closing note: preserve request-versus-receipt evidence, requested-versus-served model distinctions, and external applications' exact field names; reconcile the collisions before replacing terminology.

Evidence: all twelve listed files read in ≤200-line chunks; no imports followed. Prior copy reports were context only; owner copy remains partial with Services unread. No patches, shell, checks, model APIs or live calls. workspace_result: diff_bytes=0, checks=[]. Final get: active authority, no cancellation or owner notes.
