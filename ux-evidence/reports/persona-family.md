# azizam-ux-persona-family-20261007

Source-only family-member phone walkthrough: first ten minutes, six ranked journey findings, three delights and strengths. Main risks: phone setup dead ends, unclear failure boundaries, missing price states and technical first-use language. Empty diff; no tests or user validation.

Household member: first ten minutes on a phone
Base: 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba

This is a source-derived walkthrough, not observed behaviour or timed usability testing. Assume a family invitation has been accepted and Home opens; invitation/sign-in implementation is deliberately outside scope. The member wants to ask a question, make a picture, and know whether either costs money.

00:00–01:00 — Home. “My family said this was ready. I see Chat and Images, but do those colours mean I can use them?” Connect an app offers a direction, yet readiness lacks visible plain-language states. I look for my familiar phone app.

01:00–03:00 — Home to Apps. If it is not listed, Other leads to Python. I wonder whether I chose the wrong product. Apps offers New app and App recipe; I may think I must install or build something. Choosing a named client still leaves generic connection fields and code examples. Switching between browser and app means remembering which field belongs where. This is the likeliest abandonment point: “Can you set it up for me?”

03:00–04:00 — Member guide. I seek reassurance but meet endpoint, key, access plan and a list mixing chat apps with developer tools. Its explanation that Gateway check does not verify my app is useful. I still need the actual app-specific steps; the guide largely repeats the handoff.

04:00–06:00 — Try. I discover an apparent way to use AI without another app. The first explanation concerns a Try key and Revoke. The horizontally scrolling choices include Embeddings and Workflow; Chat has no help sentence. “Is this a chat app, or am I testing something?” Model name, Prompt and Run ask me to translate the task. If I make an image, the real preview is reassuring.

06:00–07:00 — Models. “Pick a model name” asks a question I do not know how to answer. Capability grouping and price information help, but copying a model name is not the same as doing something. I open details and meet names, providers, inputs/outputs and example requests.

07:00–09:00 — Library. A creation appearing here makes the product feel tangible. On my phone, search is behind Filter; a long prompt can occupy both title and body before saving controls. If Preview unavailable appears, I cannot tell whether I lost the file or should wait.

09:00–10:00 — Usage. I check whether my experiment cost my family money. The monthly overview helps, but individual prices are hidden in the narrow list, and an unrecorded receipt cost disappears. If a request failed, Technical details still asks me to diagnose it. I finish with something useful only if the happy path works; otherwise the family operator remains the interface.

Ranked findings

1. P1 — Phone setup loses the member before first success.
Home: web/src/app/home/page.tsx:365–368, 513–515; Apps: web/src/app/apps/page.tsx:485–500, 606–660; guide: docs/guides/FOR_MEMBERS.md:27–34.
“I choose Other and get Python; choosing a named app still leaves me to find its settings.” This blocks the core family task and spends attention on integration knowledge.
Change: Other opens “Which app are you connecting?” with “Phone app” and “Another OpenAI-compatible app”; separate developer tools. Render maintained, version-verified numbered steps with exact destination field labels. Primary CTA: “Connect an app”; secondary: “Try a sample here.” Add “Keep this page open while you switch to your app.” Do not claim a connection worked until an actual attributable request is observed.

2. P1 — No consistent answer to “Can I use it, and what failed?”
Home: web/src/app/home/page.tsx:653–671, 989–992, 1022–1042; Usage: web/src/app/usage/page.tsx:945–973; Library: web/src/app/library/page.tsx:211–236, 451–475.
“I cannot distinguish unavailable AI, a broken app setting, and a missing preview. I ask my relative to interpret the page.” Confidence requires the product to explain its boundaries.
Change: show “Chat · Ready” and a factual reason for other states. Name checks “Check gateway with this key” with “This does not test your app.” Failed receipts show “Where it stopped,” “What happened,” and one evidence-backed action. When unknown, say “The receipt does not identify where this failed.” Preview failures say “We couldn’t load this preview. Your file may still be available,” with independent Retry preview and Download actions; expose verified unavailability reasons.

3. P1 — Missing costs interrupt trust at the moment of success or failure.
Try: web/src/app/try/page.tsx:2041–2058; Usage: web/src/app/usage/page.tsx:672–701, 780–783.
“I made something, but no Cost field tells me whether it was free. On my phone I cannot scan the receipt prices either.” A family-shared service must not turn missing measurement into apparent zero.
Change: always show “Provider charge” in results and receipts: measured amount, “Local · no provider charge” only with recorded local evidence, otherwise “Cost not recorded · this does not mean free.” Use mobile receipt cards retaining outcome and charge rather than hiding price.

4. P2 — Try introduces mechanics before purpose.
web/src/app/try/page.tsx:1532–1619, 1814–1848; defaults:146–162.
“I see Revoke, Model name and Run before I understand what I am trying.” This contradicts calm onboarding.
Change: header answer “Try one request here before connecting an app.” Chat helper: “Ask one question. For an ongoing conversation, use your connected app.” Default prompt: “Suggest a quick dinner with rice and vegetables.” Use “Send question,” “Generate image,” and “Read aloud.” Keep capability options available, with developer-oriented ones under “More capabilities.” Move key-management wording to secondary details; preserve its behaviour.

5. P2 — Models gives me an identifier rather than a next step.
web/src/app/models/models-client.tsx:587–645, 744–792, 887.
“I can copy a model name, but I do not know where to paste it.” A catalog should support the member’s task without becoming a required lesson.
Change: primary row action “Try this model,” prefilling capability/model without submitting; secondary “Use in an app” opens the existing setup guide with that choice. Keep copyable identifiers and example requests in Details. Label a recommendation only when explicitly marked by the catalog.

6. P2 — Finding and keeping a creation is harder on the phone.
web/src/app/library/page.tsx:976–1006, 1154–1181, 1209–1212; guide: docs/guides/FOR_MEMBERS.md:82–89.
“Search is hidden, and a long repeated prompt pushes saving further away.” The emotional payoff should be the creation.
Change: keep Search prompts and media-kind chips visible; collapse advanced filters only. Show the prompt once, expandable, and place Download and Copy prompt immediately below the preview. Add a brief member-guide “Make your first thing” section linking Try → Library → Usage, with no claim every result is stored.

Three delight opportunities

• A resumable “Choose app → Add settings → Send your first message” guide remembers only non-sensitive progress across phone app switching.
• An earned “Your first message arrived” moment names the app and links its real receipt; copying settings never triggers success.
• “Need help?” previews a copyable, plain-language summary of the selected app, last verified check and request ID, excluding keys and prompt contents by default.

Keep

Real previews and manual audio playback; honest Gateway check scope in the guide; capability grouping and explicit price filters; code and technical details behind disclosures; copy feedback and manual-copy recovery; retry/empty states, receipt links, and the plain monthly overview. Preserve broad client choice while making one safe path easy.

Evidence and limits

Directly read PRODUCT_STRATEGY, FOR_MEMBERS, Models route/client and bounded Try source. Reused completed azizam-ux-home/apps/library/usage-activity-20261007 reports for cited surface findings; no duplicate audits. No excluded reads, browser, runtime, live requests, user validation, patches or checks. Workspace verified diff_bytes=0 and checks=[]. Immediately before completion, cancellation/authority flags were clear and owner_notes empty.
