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

### Calibration (2026-10-08)

Before judging any new design, three fresh Opus 5.5 judges compared today's UI blind against the benchmarks (73 pairs). What that showed:
- The win/tie rate is confounded. Judges preferred whichever screen did more of Azizam's exact job, so a weak or off-job benchmark lost no matter how well it was designed. Today's UI reached 45% with one judge and 32% with another.
- The rubric separated the two sides. Today's UI trailed the benchmarks on craft (6.8 vs 8.0 and 6.2 vs 7.8) and on coherence.
- "AI made this?" separated them strongly. One judge flagged 41 of 73 screens of today's UI.
- Judges disagree. A pooled score hides a lenient judge, so every test must pass for each judge on their own.

The tests below incorporate these lessons.

### T-1 Blind pairwise on design quality (primary)

For each surface and width, pair the Azizam render with each benchmark screen for the same job (`benchmarks/BOARD.md`). Show each pair in random order and unlabelled. The judge answers: **"Assume both screens had the same job and equally good data. Which is the better-designed piece of software?"** The answer may be a tie.

- Judge the design, not the scope. A screen does not win because the other one shows a different job or less data.
- Panel: at least 3 independent Opus 5.5 judges, each in a fresh context.
- **Pass:** for **each** judge on their own, Azizam is preferred or tied in at least 50% of pairs, and no surface falls below 33%.

### T-2 Rubric, relative to the benchmarks

Each judge scores both screens in every pair from 0 to 10 on five criteria: coherence, specificity, craft, clarity at a glance, and honesty. The definitions are in `judging/JUDGE.md`; clarity includes the attention cost of every element.

- **Pass:** for **each** judge, Azizam's mean is at or above the benchmarks' mean on **every** criterion, both across the product and on every surface.
- For screens a judge says cannot be compared to a benchmark, the judge also answers: "Would this hold up next to Linear's or Stripe's logged-in app?" The answer must be yes.

### T-3 "AI made this?" and "Which one is ours?"

- **Pass:** no judge flags any Azizam screen as "AI made this".
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
