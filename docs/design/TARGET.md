# Azizam design target

Owner: the Azizam design director (Opus 5.5). Status: v1, 2026-10-08.

## The claim we must earn

Tim's bar: "an AI proxy instance I can confidently share with family members that is not a toy", designed "on par with Stripe, Vercel, Linear, Plaid": in the same league as the best-designed software of this era, across the **whole product surface**, not three hero screens.

"In league with" cannot be measured directly. This file turns it into tests that someone other than the designer can run and repeat. Each test says what passes. A surface ships only when all of them pass.

## Scope: the full product surface

Every surface, in every state, at 390 and 1440 px (plus 768 for the shell), in light and dark:

Home · Status · Library · Try · Models · Workflows · Apps and keys · Usage · Activity and receipts · Engines · Providers · People, invites and access plans · Settings · Sign-in, invite acceptance and first-run setup · Command palette · Navigation shell.

States per surface: populated (the household seed), empty, loading, error, degraded (one capability down), permission-denied (member vs owner), and the household scenarios: running, budget-exhausted, provider-down, stale-health, new-member.

## Roles

| Role | Who | May | May not |
|---|---|---|---|
| Designer | Opus 5.5 lanes, high effort | Design and build on the real code | Judge its own surface |
| Director | This thread (Opus 5.5) | Brief, triage, decide, run the gates | Grade its own design decisions alone |
| Judges | Fresh-context Opus 5.5 agents | Score renders against this file | See the designer's reasoning; edit |
| Red team | Fresh-context Opus 5.5 agents, and dot lanes | Attack the product | Fix what they find |

A judge sees only renders, the benchmark images, the job each screen does, and this file. Self-preference is a known risk when the same model family designs and judges, so the primary test (T-1) is relative to shipped benchmarks and run blind.

## Tests

### T-1 Blind pairwise against best-in-class (primary)

For each surface and width, pair the Azizam render with the benchmark screen for the same job (`benchmarks/BOARD.md`). Present each pair in random order, unlabelled, with product branding cropped where possible. The judge says which screen is better designed **for this job**, or that they tie, and gives the reason in one sentence per rubric criterion.

- Panel: at least 3 independent Opus 5.5 judges, each with a fresh context.
- **Pass:** across the product, Azizam is preferred or tied in at least 50% of pairs, and no surface is below 33%.
- If a judge recognises the benchmark, that biases the result against Azizam. That makes the test conservative, which is acceptable.

### T-2 Rubric score, calibrated on the benchmarks

The judges score the benchmark screens first, then Azizam, from 0 to 10 on each criterion:

1. **Coherence:** one product, not a set of parts.
2. **Specificity:** could an unrelated product use it unchanged? Is anything a template default?
3. **Craft:** type hierarchy, spacing rhythm, alignment, colour, contrast and motion.
4. **Clarity at a glance:** can the job be done in seconds? This includes the cost in attention of every element (`vision-from-logs.md`: user value = design value and delight − cognitive cost).
5. **Honesty:** every claim on screen is supported by data. Unknown never looks like zero, and readiness never looks like a guess.

- **Pass:** on every surface, Azizam's mean is at or above the benchmark median for that surface, on each criterion. Scoring relative to the benchmark removes most judge drift.

### T-3 "AI made this?" and "Which one is ours?"

- Each judge is asked whether a design-literate person would say "AI made this" at first sight. **Pass:** no judge says yes.
- Each Azizam screen is shuffled among three benchmark screens. **Pass:** no judge picks Azizam out because it looks generic or templated. Being picked out for its household identity is fine.

### T-4 Hard gates (automated, pass or fail)

- No console errors or failed requests. No horizontal scroll and no clipped text at any width.
- WCAG AA contrast in both themes, visible focus, touch targets of at least 44 px, and `prefers-reduced-motion` honoured.
- No values outside the design tokens: zero token-drift findings.
- No fact or sentence repeated on one screen, word budgets per element, and at most one primary action per view (`lint.py`).
- Every state listed in Scope has a screenshot, and the captures are deterministic.
- The ban list (`ban-list.md`) and the Impeccable detector are clean, apart from documented exceptions.
- `slopgate` is clean on all product copy, and it follows `VOICE.md` and the glossary.

### T-5 Journeys, with step budgets set by the benchmarks

Each journey runs on a fresh install and on the lived-in household. Its step budget is no more than the benchmark product's equivalent (for example, Stripe's create-and-reveal API key).

1. **Member:** accept an invite on a phone, reach Home, create a key, connect a named client, test the connection and see a first result.
2. **Member:** find something the household made last week and play or download it.
3. **Owner, day 1:** connect an engine and see which capabilities turned on.
4. **Owner, day 30:** find who spent the most this month, and why.
5. **Builder:** find the endpoint, the model aliases, and the receipt for a failed request.

The dot walks each journey through the DOM and accessibility tree; an Opus judge walks it through screenshots. **Pass:** within budget, with no dead end and no unexplained jargon.

### T-6 Diagnosability

Cause each failure and capture what a member and an owner see: gateway unreachable, bad or revoked key, engine unavailable, workflow failure, spend cap reached, and provider timeout.

**Pass:** the screen names the layer at fault (gateway, key or person, model or engine, workflow, provider) only when the data supports it, gives the next action in plain words, and shows no raw JSON or internal IDs.

### T-7 Red team

Each round has these parts:
- **Opus 5.5 red-team agents, each with one adversarial brief:**
  - "Find what makes this feel second-rate next to Linear or Stripe."
  - "You are a 60-year-old family member on a phone."
  - "You are a skeptical designer reviewing this for an X thread."
  - "You are the owner at 11 pm, when video is broken."
  - "Find every place the UI claims something the data does not support."
- **Dot lanes:** accessibility tree and keyboard walk-throughs, copy and voice, state coverage, and text-only journey walk-throughs.

Every finding is triaged as P1 (blocks shipping), P2 or P3. **Pass:** two consecutive rounds with no new P1.

## Stop rule

Ship when T-1 to T-7 all pass. If three rounds in a row bring no gain on T-1 or T-2, stop, keep the best round, and report the gap honestly with the evidence. Do not keep polishing and do not claim a pass.

## What Tim sees

- At direction: one recommended direction, with its T-1 and T-2 results against the benchmarks.
- At the end: the full contact sheet and every test result.

Tim never tests, and he never decides UI details.

## Known limits of the benchmark set (v1)

Most benchmarks are public pages, docs screenshots or App Store imagery. They are not the logged-in product surfaces that made Linear and Stripe famous, because capturing those needs an account. Seven jobs have no public best-in-class pixels, so Azizam sets the bar itself there (`benchmarks/NOTES.md`). This makes T-1 easier than the real bar on some surfaces. Judges must therefore also answer, for every screen: "Would this hold up next to Linear's or Stripe's logged-in app?" Mobbin is the planned source for the logged-in screens once its MCP connector is authorised.
