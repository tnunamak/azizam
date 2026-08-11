# devspecs feedback

## 2026-06-24 — LiteLLM rearchitecture memo

- Task: `ds task quick "produce LiteLLM rearchitecture decision memo"`.
- Useful: quickly identified a bounded target and relevant source files (`gateway/litellm_substrate.py`, `gateway/backends/llm_execution.py`, `gateway/admin_api.py`, related tests).
- Rough edge: it created `devspecs/tasks/...` in the repo root, not `.devspecs/`; that directory is currently untracked, so the "gitignored globally" expectation did not hold for this command/output shape.
- Net: useful for orientation, but the unexpected untracked `devspecs/` artifact is a handoff/cleanup risk.

## 2026-06-24 — LiteLLM local lifecycle target

- Task: `ds task quick "migrate LLM access control to LiteLLM ownership boundary"`.
- Useful: framed the code slice and checkpointed the final promoted result after tests.
- Rough edge: indexing was noisy and the task pack missed the prior decision memo path on first pass. The work still required manual owner synthesis.
- Process note: five waspflow Claude lanes (`ltm-keys`, `ltm-db`, `ltm-life`, `ltm-media`, `ltm-ui`) all reaped as `recovered` with no substantive report or diff. For this task, broad fan-out wasted coordination time; a narrower implementation prompt after the owner identified the exact seam would likely have been better.

## 2026-07-15 — official llama.cpp wedge triage

- Task: `ds task quick "triage reproducible official llama.cpp large-context generation wedge on Bee"`.
- Rough edge: automatic indexing emitted only a progress line and did not yield a usable task id, packed context, or prompt before the incident work had to continue. The shell process remained around longer than expected, so it was not useful as a fast triage boundary.
- Net: `ds tldr` was useful for choosing the incident-shaped workflow; for a live engine incident, `ds task quick` needs a bounded readiness/timeout signal so it cannot compete with the diagnostic window.
