# devspecs feedback

## 2026-10-03 — People and built-in accounts lane

- Useful: `ds tldr` explained the slice workflow, and `ds recent` returned recent repository topics.
- Rough edge: creating the three-slice account task printed `Task index preflight: waiting for another index update` without an observed task ID or usable task pack. `ds task status` required an ID; `ds task list` returned no output. I continued from the explicit brief and recorded verification manually. A bounded wait and a recoverable task ID would help when another index update is active.

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

## 2026-08-11 — bounded admin UX contract

- Useful: `ds tldr`, `recent`, and `map` quickly confirmed the Gateway, admin UI, and identity boundaries.
- Rough edge: `ds task` hit `SQLITE_BUSY` during auto-indexing after creating an untracked `devspecs/` task directory. The task pack was not usable and the generated directory had to be moved to trash. A failed task creation should clean up its own repository artifact.

## 2026-08-11 — P0 Gate 2 correction

- Useful: `ds tldr` and `ds recent` identified the immediately preceding P0 contract commit and its source boundary without conflating it with unrelated active work.
- Rough edge: the bounded `ds task` run took about 29 seconds, printed only indexing progress, and left an untracked `devspecs/tasks/…` directory without returning a usable task ID or prompt. This is expensive ceremony for a confirmed hotfix and leaves cleanup work to the caller.

## 2026-08-11 — P0 Gate 3 correction

- Useful: `ds tldr` and `ds recent` gave the expected task workflow and confirmed there was no relevant prior Gateway UX task.
- Rough edge: this bounded task took about 28 seconds to produce a task pack, but its primary-file list missed all three actual target files (`gateway/admin_api.py`, `web/src/app/admin/layout.tsx`, and `web/src/lib/admin-links.ts`) and instead elevated stale, unrelated sources. It also left `devspecs/` untracked in the worktree, so manual review and cleanup remained necessary.

## 2026-08-11 — Audit and Settings final gate

- Useful: `ds tldr` made the intended bounded-hotfix workflow clear.
- Rough edge: `ds task --quick` spent about 29 seconds indexing and created an untracked `devspecs/tasks/...` pack, but returned no task identifier or usable prompt. The implementation still needed manual source/test orientation, and the generated artifact required cleanup.

## 2026-10-03 — first-run automation lane

- Useful: `ds tldr` and `ds recent` returned the workflow and recent repository boundaries.
- Rough edge: `ds task "Prove first-run setup from a fresh instance" --slice "Implement isolated API journey and CI" --slice "Verify native and Compose first-run plus hermetic suite" ran for more than six minutes without a task ID or usable output. I terminated that task process and continued from the explicit brief and source files. A bounded timeout or progress signal would make this usable during implementation.

## 2026-10-03 — Status and Engines UI lane

- Useful: `ds tldr` and `ds recent` returned workflow guidance and the shell foundation boundary.
- Rough edge: task creation waited on another index writer without returning a task ID. I canceled it. Retrying with `--no-refresh --index=false` also returned no task output after more than a minute; I terminated that process and continued from the complete lane brief. These flags did not provide a bounded path around the index wait.
## 2026-10-03 — decision implementation worktree

- `ds tldr` and `ds recent` succeeded. Recent returned source/test/doc groups
  for five earlier changes and gave useful file pointers.
- `ds find 'stable aliases capabilities registry lifecycle'` produced no
  result over several polls. A subsequent sliced `ds task` reported
  `Task index preflight: waiting for another index update` and did not advance.
- Stopped only those two task-owned CLI processes after the wait. Continued
  repository orientation through direct source reads. No DevSpecs state was
  deleted and no other indexing processes were terminated.
- A bounded timeout with the indexing owner's status would make this failure
  easier to diagnose in a worktree shared by concurrent implementation lanes.
