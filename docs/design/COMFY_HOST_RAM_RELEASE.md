# Releasing idle host RAM held by the graph engine (proposal, no code)

Status: proposal. The admission in `gateway/host_memory_admission.py` ships first; this note says what comes next.

## Why

The graph engine keeps the model files of its last prompt staged in host RAM after the prompt ends. On 2026-11-16 and 11-17 a
load of 14 to 35 GB began while the host had 12 to 19 GB free, and earlyoom and the kernel OOM killer then killed unrelated
work. Admission stops the next burst from starting. It does not give back memory the engine already holds, so the host stays
short for everything else (suites, browsers, agents) until the engine's models are freed.

## Prior art


- Idle timers come from billing, not from performance (claim A3, Shahrad et al.). A household host owns its memory, so a
  timer frees memory nobody wanted.
- FaasCache (A4) keeps everything warm while there is room and evicts by priority only when something needs the space: about
  3x less cold-start cost than a TTL.
- Torpor (A6) keeps idle models in host RAM and evicts light models first, then least recently used among heavy ones.
- Prism (A6) evicts a model that is idle and only when GPU memory is tight for other models.
- OS, browser and GPU memory managers keep things resident until pressure, then evict by class, then by recency or reload cost.

## Proposal

**Signal.** Pressure, not a timer. Primary: `/proc/pressure/memory` `some avg10` above a threshold (the host's own rule is 20)
for two reads. Cross-check: MemAvailable below the admission margin. PSI measures stall, which is what hurts people; MemAvailable
measures room. Either alone is wrong: MemAvailable can sit low with a large reclaimable cache, and PSI rises only after the stall.
A release triggers when PSI is high or when a waiter in the admission queue cannot be admitted.

**What gets freed.** The engine's own `POST /free` with `{"unload_models": true, "free_memory": true}`, sent only when its queue
is empty (the gateway already does this after an out-of-memory refusal: `_free_graph_models_if_idle`). It frees all models;
the engine has no per-model free. So the choice is whether to free, not what. After a successful free the gateway calls
`HostMemoryAdmission.forget_all()` (already done in `GraphSession.free`), so the next prompt waits for room again.
If a finer tool is wanted later, the engine would need to expose a per-model unload; do not build one in the gateway.

**Order.** Class, then recency or reload cost:
1. Never free while the engine's queue or a prompt the gateway sent is running.
2. If more than one engine record shares the host, free the one whose models are idle longest first. With one engine this is moot.
3. Among the models of one engine, the free is all-or-nothing, so order does not apply inside it; the proxy for reload cost is
   staged GB (large models cost more to reload and free more): free when the idle models hold at least what the waiter needs.

**Interaction with admission.** Admission waits; release makes the wait end sooner. A waiting person's request is the trigger:
when the head of the queue has waited `poll_s * N` and the engine is idle, the gateway sends one `/free`, calls `forget_all()`,
and re-reads MemAvailable. A background request never triggers a free for itself (it yields to people). The reload cost lands
on the next prompt that needs those models, which is the cost the divan entry accepts for pressure-driven eviction.
No periodic timer; no free while memory is plentiful.

**Not proposed.** Changing the engine's service unit, its OOM score or earlyoom flags; a fixed idle timeout (the host owns its
memory, so a timer buys nothing, per A3/A4); freeing during a prompt.

**Unproven.** Whether `/free` returns the staged RAM to the host promptly (it should, measure MemAvailable before and after on
an idle engine); the PSI threshold; whether the head-of-queue trigger thrashes when two people alternate models. Measure each
before building.
