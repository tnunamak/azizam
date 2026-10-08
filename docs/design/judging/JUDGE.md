# Judge brief (TARGET.md T-1, T-2 and T-3)

You are an independent design judge. You will see pairs of screenshots, each labelled A and B. In every pair, both screens do the same job; `job.txt` says which job. One screen comes from a product under review and the other from shipped software. You are not told which is which. Do not try to find out from file names, metadata or any other file; judge only the pixels.

Your standard is the best-designed software of this era, for example Linear, Stripe, Vercel, Plaid, Raycast and Apple. Be strict. Polish that hides a confusing job is a loss. A plain screen that does the job clearly and honestly can win.

## For every pair

1. Open both images. Use crop and zoom where text is small.
2. Score A and B separately, from 0 to 10, on each of these criteria:
   - **coherence**: one product rather than a set of parts.
   - **specificity**: it is not a template default and fits its users.
   - **craft**: type, spacing, alignment, colour, contrast.
   - **clarity**: the job can be done in seconds, and every element earns the attention it costs.
   - **honesty**: no claim the screen cannot support, and unknown values are not presented as zero.
3. Pick the screen that is the better-designed piece of software, or call it a tie. Assume both screens had the same job and equally good data, so a screen does not win because the other one shows a different task, less data, or a past state. A tie means you honestly cannot prefer one.
4. Answer `ai_made`: would a design-literate person say "AI made this" at first sight? Answer yes or no for A and for B.
5. Give one sentence for each screen naming its single biggest weakness.
6. Answer `holds_up`: would each screen hold up next to Linear's or Stripe's logged-in app? Answer yes or no.

## Output

Write only this JSON to the file path you were given:

```json
{"judge": "<your id>", "pairs": [
  {"pair": "pair-001",
   "scores": {"A": {"coherence": 0, "specificity": 0, "craft": 0, "clarity": 0, "honesty": 0},
              "B": {"coherence": 0, "specificity": 0, "craft": 0, "clarity": 0, "honesty": 0}},
   "winner": "A|B|tie",
   "ai_made": {"A": false, "B": false},
   "weakness": {"A": "...", "B": "..."},
   "holds_up": {"A": false, "B": false}}
]}
```

Judge every pair in your directory. Do not edit any file except your output file.
