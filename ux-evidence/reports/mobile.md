# azizam-ux-mobile-20261007

Completed bounded source-only 390 px mobile audit: eight ranked findings, three delight opportunities, preservation notes; six listed files only, verified empty diff, no checks or rendered validation.

390 px phone layout audit — source only

Evidence and limits
Reviewed only the six listed files at base 3a5a61be109f79dfd634708ecfb9e1bbae79d0ba, with read windows of at most 200 lines. No imports, excluded paths, stylesheets, or UI primitives opened. No rendering, browser/device testing, user validation, network requests to the app, edits, or checks. Breakpoint conclusions follow the declared sm/md/lg branches; actual breakpoint values, inherited target sizes, and computed sheet dimensions remain unverified. A 390 px width alone does not establish viewport height: “below first screen” below denotes content-order risk, not a measured fold. All paths below are in web/src/.

Ranked findings

1. P1 — Usage hides cost precisely on phones.
app/usage/page.tsx:695–701; 567–619.
“As a household member, I can see whether a request completed, but its cost disappears on my phone; comparing models requires sideways scrolling.” Receipt cost is explicitly hidden below sm; the model table has a 420 px minimum width, wider than the entire target viewport. This weakens the promise of honest, easily understood spending.
Change: put “Cost: [amount]” alongside time beneath each receipt summary on phones; use “Cost not recorded” for null. Below sm render each model as a two-line row: capability/model, then “Requests: [n] · Cost: [amount or Not recorded]”. Keep the desktop table.

2. P2 — The zero-key app card allocates too little room to the app name.
app/apps/page.tsx:336–385.
“As a phone user, I must distinguish my app in a narrow strip between its icon and two actions.” A single flex row contains a 40 px icon, three gaps, text, Create key, and an overflow button inside p-5; long names must wrap heavily or controls may squeeze, depending on unread button styles. This makes a recovery task look fragile.
Change: use a two-row mobile grid: icon/name and overflow in row one; full-width “Create key” in row two when needed. Retain the current horizontal layout at sm. Keep app names wrapping, never ellipsized beyond recognition.

3. P2 — Home keeps four rows of choices above the next task.
app/home/page.tsx:353–400, 477–524; 1036–1043.
“As a member who has chosen my app, I still scroll past seven choices and connection fields to reach Create key or Check connection.” The two-column phone chooser remains expanded after selection; the action row follows URL, model, key selection, and explanatory content. This spends attention on a decision already made and also pushes capabilities/recent items farther down.
Change: after selection collapse to “Connecting [app]” with a 44 px “Change app” control. Show numbered steps: “1. Copy address”, “2. Save key”, “3. Check connection”; place each action beside its relevant step. Preserve all seven choices inside Change app.

4. P2 — Library download follows an unbounded title, preview, and repeated prompt.
app/library/page.tsx:1150–1200; 480–493.
“As a member opening a portrait image or long prompt, I may have to scroll substantially just to download it.” The entire prompt becomes the sheet title, appears again before actions, and the large preview has no explicit viewport-height cap here. The content order can displace the primary action beyond the first screen.
Change: title “Image”, “Video”, or “Audio”, with a two-line prompt excerpt; cap the preview at 40dvh with object-contain. Put Download directly below the header or in a safe-area-aware sticky footer; move the full prompt into “Prompt” disclosure with Copy prompt. Do not duplicate the full text above Download.

5. P2 — Try's result and recovery arrive after the entire mobile form.
app/try/page.tsx:1594–1601, 1656–1805, 1808–1851.
“As a phone user running a multi-input workflow or decision, I scroll through every input to Run, then farther to find success or failure.” The two-column layout starts only at lg; the form precedes results, with no result focus/scroll handoff visible. A failed run can feel like nothing happened, conflicting with clear failure ownership.
Change: on submit expose “Running [task]…” beside Run; on completion show an adjacent “Result ready — View result” or “Request failed — View details” control that moves focus to the result heading. For lengthy forms use a sticky Run footer above the phone-navigation height, with scroll padding so it never covers fields. Keep errors linked to the existing receipt when available.

6. P2 — Important destinations lose their current-page marker on phones.
components/shell/app-shell.tsx:1009–1049, 1072–1096.
“As a member in Library, Try, or Usage, none of my visible bottom tabs tells me where I am.” Phone tabs expose Home, Apps & keys, Status, and More; More has expanded state but no active-destination treatment. Search is below the entire navigation list inside the sheet. Navigation must help household members stay oriented.
Change: when the route is inside More, label it “More · Library” (or current destination) and give it a visible selected state plus an accessible current-location description. Put Search immediately beneath the sheet header, before navigation. Keep the four destinations stable rather than reordering them based on activity.

7. P2 — Later Try capabilities have no explicit phone discovery cue.
app/try/page.tsx:1569–1584.
“As a new phone user, I may not notice Decisions, Embeddings, and Workflow beyond the horizontally scrolling tabs.” The list is min-w-max, inside a scrolling wrapper, with no explicit overflow cue in this file.
Change: below sm replace the long strip with a labeled full-width “Task” select containing all seven choices, or show a persistent “More tasks” affordance and scroll the active tab into view. Preserve desktop tabs.

8. P3 — Touch-target guarantees are inconsistent in local markup.
app/try/page.tsx:1555–1563; app/apps/page.tsx:618–621; app/usage/page.tsx:813–816.
“As a phone user, the small Revoke and disclosure labels demand more precise taps than nearby 44 px controls.” These elements lack the explicit minimum targets already used elsewhere; actual computed sizes need validation.
Change: add inline-flex min-h-11 items-center and suitable horizontal padding to Revoke, Connection examples, and Technical details. Preserve separation around destructive actions.

Three delight opportunities
- Home: after a successful check show a compact “Gateway + key checked” receipt with measured latency and a clear “Now test in [app]” next step, keeping the existing caveat that app functionality is not verified.
- Library: preserve scroll position and the selected card when closing a preview, so opening several creations feels like browsing a personal album.
- Usage: a compact phone “What happened?” receipt card showing outcome, source, time, and measured/unknown cost together, before technical details.

Already good; keep
- Shell bottom navigation accounts for the device safe area; main content reserves pb-28. More sheet explicitly caps at 90dvh and scrolls, with focus restoration.
- Home app choices, capability pills, receipt links, and audio buttons explicitly use 44 px minimums.
- Library uses a one-column phone gallery, hides its full filter stack behind a 44 px disclosure, wraps actions, and retains loaded media after refresh failures.
- Usage already has a screen-reader chart table, responsive chart container, wrap-capable filter row, and readable request outcomes.
- Apps' one-time-key dialog explicitly caps at calc(100dvh - 2rem) and scrolls. Other dialog/sheet heights depend partly on unread primitives and cannot be certified.
- Try uses min-w-0 form/result columns, contained code scrolling, and progressive disclosure for JSON and code.

Final state: owner notes checked; no cancellation or revocation reported. Verified diff_bytes=0; empty-diff SHA-256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855. No checks run.
