# azizam-ux-models-workflows-try-20261007

Completed static Models/Workflows/Try discovery audit: 7 ranked findings, 3 delight opportunities, preserved strengths; empty diff and no runtime claims.

Models / Workflows / Try: static discovery audit

Verdict: the source supports a promising catalog, but the Models-to-first-result journey is not yet a coherent one-minute path. This is a heuristic assessment, not a measured task-completion result.

Evidence: docs/PRODUCT_STRATEGY.md:9–30,54–64,110–138 and the complete scoped UI files below at base 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba. No browser, user study, live requests, checks, shell, or edits. Restricted paths were not read. File abbreviations below are exact: Models = web/src/app/models/models-client.tsx; Workflows = web/src/app/workflows/page.tsx; Try = web/src/app/try/page.tsx.

Ranked findings

1. P1 — Catalog availability disappears before Run.
Try:494–546,557–598,1808–1824,2181–2232; Workflows:328–345,411–422.
“As a household member, I can choose a model and press Run without learning whether it is available. A workflow can show a warning but still offer the same Run in Try action.”
Try’s model/workflow normalization drops availability; Run has no availability guard. Unverified model fallbacks remain runnable. Workflow reasons are buried inside Technical details. This undermines honest health and makes avoidable failures look arbitrary.
Change: carry availability and reason into every Try option. Put “Unavailable · [plain reason]” immediately above the action; disable execution only when unavailability is known, with an appropriate recovery action. Preserve explicit builder attempts for unknown states behind “Try anyway”; say “Availability not checked. This request may fail.” Keep Starting/Busy distinct from Unavailable. Rename catalog navigation to “Open in Try” so navigation does not imply execution.

2. P1 — Failed tests discard the evidence needed to locate the problem.
Try:245–270,1300–1337,1835–1840.
“As a builder, I get a message or HTTP code and Retry, but no failed-request receipt showing where the attempt stopped.”
publicRequest throws before returning response context; receipt verification only happens after success. The result panel replaces the result with a generic error component. This directly conflicts with the owner’s requirement to distinguish gateway, model, app, and workflow failures.
Change: preserve request ID, HTTP status, request target, elapsed time, and safe structured failure details. Show “The request failed” followed by “Reported by: [verified layer]”; when unknown, explicitly say “The failing layer could not be identified.” Offer “View failure receipt” when verified, “Copy diagnostic summary,” and “Retry this request” using the original submitted input. Never infer blame from an HTTP status alone.

3. P2 — Models ends at copying a name, rather than trying it.
Models:379–432,564–643,690–830,1067–1078; Try:778–783,862–885.
“As a new member, I can copy a name or open Details, but I must find Try separately and select the same thing again.”
Models has no direct Try action in its full UI; Try already accepts model/alias and capability query parameters. This consumes the attention the catalog should save.
Change: add “Try this” alongside “Copy model name” on each supported row and at the top of Details. Deep-link the exact ID and supported capability, without running automatically. Header: “Choose what you want to make, then try a model.” For unsupported capabilities, offer the applicable workflow or example request with honest copy, rather than sending users to Chat.

4. P2 — Location and no-charge information vanish from the decision point.
Models:238–249,580–604,1059–1065; Try:528–538,734–741,2223–2228.
“As a family member, a blank cost field gives me no explicit reassurance, and a model’s friendly name does not tell me where my prompt goes.”
Models only renders a cost label for paid rows; Try stores locality but renders only the name and paid price in its selector. The strategy explicitly promises local/remote and direct money language.
Change: show a persistent selection summary: “Local · no provider charge” or “Remote · [provider] · Paid · [rate and unit].” For unknown data, say “Location not reported” or “Price unavailable.” Keep this summary directly above Run, visible with the menu closed. Do not auto-select a paid recommendation when an explicit paid choice has not been made; offer it visibly as an option.

5. P2 — “Model name” sometimes shows a title instead of the name to paste.
Try:513–531,1614–1627,2181–2232; Models:744–759; Workflows:424–435,474–495.
“As a builder, the field labeled Model name can contain a friendly label; the exact ID appears only when labels collide. For a workflow, I am still asked for a Model name.”
That breaks continuity between catalog, Try, and external app setup.
Change: label the workflow field “Workflow.” Always display the friendly title plus a subordinate, selectable exact identifier. After selection expose “Copy model name” or “Copy workflow ID.” In Models Details label one canonical selector “Use this name in your app”; place aliases under “Other accepted names,” keeping backend resolution advanced. Do not imply every workflow identifier belongs in an OpenAI model field.

6. P2 — Workflow cards conceal the requirements and waiting time that determine the choice.
Workflows:163–171,229–251,256–326,390–400,447–452.
“As a phone user, I see input icons and sometimes Needs an image, but cannot quickly tell which inputs I must supply, what I will receive, or how long it typically takes.”
Text/audio requirements are not explicitly called out, output is in Details, timing is inside Technical details, and jargon-only descriptions can leave purpose blank. The subtitle promises selection by purpose and inputs.
Change: give each card three compact lines: a plain purpose, “Needs: [required input types] → Produces: [output],” and “Typically [time]” only when known. Use “Requirements not reported” where necessary. Require authored plain-language purpose text; avoid a blank description or guessed promise.

7. P2 — Try does not clearly explain its first action.
Try:143–159,1524–1528,1808–1824,1850–1854.
“As a first-time visitor, I see Try, several capability tabs, and Run, but no sentence explaining that this makes a real request.”
The chat help is empty, and sample text such as “smoke test” is operator vocabulary. This weakens calm, household-friendly onboarding.
Change: header answer “Send one small request and see the result, time, and receipt.” Use “Send test request” for Chat and capability-specific verbs elsewhere. Provide ordinary examples, such as “Suggest one easy lunch,” while keeping editable defaults and never executing on arrival.

Three delight opportunities

• A selected-model handoff strip: “You chose [name] in Models,” with the exact ID and the same capability icon. Preserve this through Try and its receipt.
• Workflow requirement previews: show one compact text→image or image→video example labeled “Example,” with a “Use example inputs” action that fills fields but does not run.
• An evidence-backed completion card: “[selected capability] worked · [time] · [actual cost],” followed by “Use this exact request in your app.” Limit the claim to this request; never declare the entire gateway healthy.

Keep these strengths

• Models groups by capability, progressively reveals alternatives, and never invents a recommendation from an unmarked catalog (Models:564–570).
• Model-copy failures provide a selectable manual-copy field (Models:416–430).
• Workflows already has an exact deep link into Try, phone-specific input summaries, and separately recoverable recent-job loading (Workflows:339–341,503–552,650–690).
• Try has editable examples, structured workflow inputs with Advanced JSON, real result media, and verified receipt links rather than fabricated proof (Try:292–338,1667–1717,1900–1967).

Delivery limits: source-derived behavior and conditional risks only; rendered layout, deployment routing, actual catalog data, and runtime outcomes remain unverified. Bridge workspace result confirms diff_bytes=0 and no check receipts. Final assignment read found no cancellation, revoked authority, or owner notes.
