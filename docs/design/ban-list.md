# Ban list (v1, 2026-10-07)

Named patterns, not adjectives. Every render adds to this list. A screen that contains a banned pattern fails the deterministic gate unless the exception is recorded here with a reason.

Run the mechanical part with `~/.claude/skills/impeccable/scripts/impeccable detect <path-or-url>` plus the Azizam lints.

## A. Default looks: banned unless the owner chooses one at T1, with the reason recorded

1. Warm cream or sand background with a serif display face and an earthy accent (the current Azizam look; the owner's "sand is claudy").
2. Near-black background with a single acid or neon accent.
3. Broadsheet: hairline rules everywhere, zero radius, newspaper type.
4. SaaS card kit: identical rounded cards, one radius, the same grey shadow, gradient washes.
5. Generic dark SaaS dashboard: navy-black, blue-violet gradient glow, KPI tiles with sparklines on every card, used without real data behind them.

## B. Template chrome: always banned

6. Tracked-caps eyebrow labels above headings.
7. "A · B · C" metadata strings as decoration.
8. "WORD — fragment" labels.
9. `→` appended to every link.
10. Monospace for small labels that are not copied or matched (monospace is for model names, request IDs, URLs and code).
11. Italic accent words inside headings.
12. `01 / 02 / 03` numbering unless the content is a real sequence (setup steps are a real sequence).
13. Pill-shaped primary buttons.
14. Coloured left borders on cards as a status signal.
15. A slogan headline inside the product (owner, 2026-06-25).

## C. Azizam-specific (from the 2026-10-07 screenshots and audits)

16. A status hero that leads with an unclassified state ("partially checked") instead of a verdict.
17. The same large numeral style for counts and for words ("No paid requests" set as a number).
18. Chips or pills that look selectable but do nothing, or show a capability as available while a banner says it is disabled.
19. Navigation with duplicate icons for different destinations (Usage and Activity, Models and Providers).
20. A fact repeated on one screen (heading plus sentence plus badge saying the same thing; "Gateway default / Gateway default").
21. More than one tab or segment pattern in the product (underline tabs, unselected text tabs, pill segments, check chips).
22. Tabs that duplicate sections already visible below them (Settings).
23. Empty states that repeat the button label ("Run chat" above a Run button).
24. Measuring-instrument language for people ("not measured", "observation unavailable").
25. A connection or test "success" shown without an explicit successful result.
26. A blank cost cell where the amount is unknown.
27. Page content that ends in the top third of a 1440 px screen with no composition below (use a narrower measure or a real secondary region).

## D. Process bans

28. "Make it better", "more premium", "less AI-looking" as instructions.
29. A recommended-fonts list (it becomes the next default).
30. Judging from light-theme desktop captures only.
31. Fixed or sticky elements left fixed in full-page captures.
