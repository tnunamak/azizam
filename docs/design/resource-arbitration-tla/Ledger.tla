------------------------------ MODULE Ledger ------------------------------
(*
 * Model of the resource ledger and external-effect protocol in
 * FOUNDATION.md sections 3.1, 3.3, 4.1-4.5. Model only: no product code.
 *
 * Resources: one stock (StockCap) and one window budget (WinLimit units per
 * epoch, epochs replenish up to MaxEpoch).
 * Each attempt claims StockNeed units of stock and WinNeed units of window
 * and performs exactly one external effect under one idempotency key.
 *
 * Mutant (Mutant constant) selects a deliberately broken variant:
 *   "timeoutSettles"     Timeout releases the holding (the broken variant
 *                        the task asks for).
 *   "staleReportRefund"  An old-epoch window report restores current credit.
 * Fenced = FALSE models an adapter with no fence. See README.md.
 *)
EXTENDS Naturals, FiniteSets

CONSTANTS
  Attempts,   \* e.g. {"a1", "a2"}
  MaxGen,     \* executor generations, e.g. 2
  MaxEpoch,   \* window epochs, e.g. 2
  WinLimit,   \* window budget per epoch, in units, e.g. 1
  StockCap,   \* stock capacity, in units, e.g. 2
  Fenced,     \* TRUE: the adapter enforces a monotonic fence at takeover
  Mutant      \* "none" for the real protocol

ASSUME MaxGen \in Nat /\ MaxGen >= 1
ASSUME MaxEpoch \in Nat /\ MaxEpoch >= 1
ASSUME Fenced \in BOOLEAN
ASSUME Mutant \in {"none", "timeoutSettles", "staleReportRefund"}

Gens   == 1..MaxGen
Epochs == 1..MaxEpoch

OpStates  == {"planned", "reserved", "intent", "dispatched", "unknown",
              "verified", "failed", "settled", "cancelled"}
EngStates == {"none", "tomb", "running", "done", "failed"}

\* Demand per attempt (the claim). a1 is large, so the two attempts contend.
StockNeed == [a \in Attempts |-> IF a = "a1" THEN 2 ELSE 1]
WinNeed   == [a \in Attempts |-> 1]

MsgSpace == [att : Attempts, gen : Gens]
EvSpace  == [att : Attempts, st : {"done", "failed", "absent"}]

VARIABLES
  opState,      \* [Attempts -> OpStates]: durable operation state
  opGen,        \* [Attempts -> 0..MaxGen]: generation that reserved the op
  opEpoch,      \* [Attempts -> 0..MaxEpoch]: window epoch the grant charged
  basis,        \* [Attempts -> {"none","evidence","operator"}]: audit of what settled it
  barrier,      \* Attempts \cup {"none"}: drain barrier held during reserve
  owner,        \* current executor generation (durable, ledger's record)
  execAlive,    \* executor process alive; a crash loses its volatile state
  engFence,     \* adapter fence: lowest generation the engine still admits
  eng,          \* [Attempts -> EngStates]: engine ground truth for each key
  inflight,     \* SUBSET MsgSpace: dispatch messages; the network may redeliver them
  evidence,     \* SUBSET EvSpace: holder-scoped engine evidence (set = duplicates, reorder)
  wreports,     \* SUBSET Epochs: aggregate window observations, never settle anything
  epoch,        \* current window epoch
  winUsed,      \* [Epochs -> 0..WinLimit]: window units charged in each epoch, net of cancels
  grantedW,     \* ghost: window units ever granted in each epoch
  cancelledW,   \* ghost: window units returned by pre-dispatch cancel in each epoch
  sends,        \* ghost: [Attempts -> Nat] dispatch messages sent per key
  effects,      \* ghost: [Attempts -> Nat] effects started per key
  staleAccept   \* ghost: an effect was started by a message from an older generation

vars == <<opState, opGen, opEpoch, basis, barrier, owner, execAlive, engFence,
          eng, inflight, evidence, wreports, epoch, winUsed, grantedW,
          cancelledW, sends, effects, staleAccept>>

\* ---- Balance equations (FOUNDATION 4.2) -------------------------------

RECURSIVE SumNeed(_)
SumNeed(S) ==
  IF S = {} THEN 0
  ELSE LET a == CHOOSE x \in S : TRUE
       IN StockNeed[a] + SumNeed(S \ {a})

\* Holdings that count against stock: reserved and not yet settled or cancelled.
HeldSet == {a \in Attempts : opState[a] \in
              {"reserved", "intent", "dispatched", "unknown", "verified", "failed"}}

StockHeld == SumNeed(HeldSet)

\* Holder state, derived from operation state (mapping is ours; see README).
HolderState(a) ==
  IF opState[a] \in {"settled", "cancelled"} THEN "settled"
  ELSE IF opState[a] \in {"verified", "failed"} THEN "reconciling"
  ELSE IF opState[a] = "unknown" THEN "degraded"
  ELSE IF opState[a] \in {"reserved", "intent", "dispatched"}
          /\ (opGen[a] < owner \/ ~execAlive) THEN "quarantined"
  ELSE IF opState[a] = "planned" THEN "none"
  ELSE "active"

\* ---- Initial state ---------------------------------------------------

Init ==
  /\ opState   = [a \in Attempts |-> "planned"]
  /\ opGen     = [a \in Attempts |-> 0]
  /\ opEpoch   = [a \in Attempts |-> 0]
  /\ basis     = [a \in Attempts |-> "none"]
  /\ barrier   = "none"
  /\ owner     = 1
  /\ execAlive = TRUE
  /\ engFence  = 1
  /\ eng       = [a \in Attempts |-> "none"]
  /\ inflight  = {}
  /\ evidence  = {}
  /\ wreports  = {}
  /\ epoch     = 1
  /\ winUsed   = [e \in Epochs |-> 0]
  /\ grantedW  = [e \in Epochs |-> 0]
  /\ cancelledW = [e \in Epochs |-> 0]
  /\ sends     = [a \in Attempts |-> 0]
  /\ effects   = [a \in Attempts |-> 0]
  /\ staleAccept = FALSE

\* ---- Ledger transactions (4.1, 4.3) -----------------------------------

\* One serializable grant: reserve stock and window in the current epoch,
\* set the drain barrier. Blocked while another barrier is held.
Grant(a) ==
  /\ execAlive
  /\ opState[a] = "planned"
  /\ barrier = "none"
  /\ StockHeld + StockNeed[a] <= StockCap
  /\ winUsed[epoch] + WinNeed[a] <= WinLimit
  /\ opState' = [opState EXCEPT ![a] = "reserved"]
  /\ opGen'   = [opGen EXCEPT ![a] = owner]
  /\ opEpoch' = [opEpoch EXCEPT ![a] = epoch]
  /\ barrier' = a
  /\ winUsed' = [winUsed EXCEPT ![epoch] = @ + WinNeed[a]]
  /\ grantedW' = [grantedW EXCEPT ![epoch] = @ + WinNeed[a]]
  /\ UNCHANGED <<basis, owner, execAlive, engFence, eng, inflight, evidence,
                 wreports, epoch, cancelledW, sends, effects, staleAccept>>

\* Separate durable write after reserve (see README, ambiguity A1).
\* Clears the barrier. Only the current generation, in the grant epoch.
RecordIntent(a) ==
  /\ execAlive
  /\ opState[a] = "reserved"
  /\ opGen[a] = owner
  /\ opEpoch[a] = epoch
  /\ barrier = a
  /\ opState' = [opState EXCEPT ![a] = "intent"]
  /\ barrier' = "none"
  /\ UNCHANGED <<opGen, opEpoch, basis, owner, execAlive, engFence, eng,
                 inflight, evidence, wreports, epoch, winUsed, grantedW,
                 cancelledW, sends, effects, staleAccept>>

\* Intent -> dispatched. Sends one message. No replay: only the generation
\* that recorded the intent may send it, and only in its grant epoch.
Dispatch(a) ==
  /\ execAlive
  /\ opState[a] = "intent"
  /\ opGen[a] = owner
  /\ opEpoch[a] = epoch
  /\ opState' = [opState EXCEPT ![a] = "dispatched"]
  /\ inflight' = inflight \cup {[att |-> a, gen |-> owner]}
  /\ sends' = [sends EXCEPT ![a] = @ + 1]
  /\ UNCHANGED <<opGen, opEpoch, basis, barrier, owner, execAlive, engFence,
                 eng, evidence, wreports, epoch, winUsed, grantedW,
                 cancelledW, effects, staleAccept>>

\* Engine admits a dispatch message (may be a redelivery or stale).
\* The idempotency key is single-use: it is accepted only while eng = "none".
\* With a fence, the engine refuses generations older than engFence.
Accept(m) ==
  /\ m \in inflight
  /\ eng[m.att] = "none"
  /\ (~Fenced \/ m.gen >= engFence)
  /\ eng' = [eng EXCEPT ![m.att] = "running"]
  /\ engFence' = IF Fenced THEN m.gen ELSE engFence
  /\ effects' = [effects EXCEPT ![m.att] = @ + 1]
  /\ staleAccept' = (staleAccept \/ (m.gen < owner))
  /\ UNCHANGED <<opState, opGen, opEpoch, basis, barrier, owner, execAlive,
                 inflight, evidence, wreports, epoch, winUsed, grantedW,
                 cancelledW, sends>>

\* ---- Environment: engine and evidence (3.2, 4.4) ----------------------

FinishOk(a) ==
  /\ eng[a] = "running"
  /\ eng' = [eng EXCEPT ![a] = "done"]
  /\ UNCHANGED <<opState, opGen, opEpoch, basis, barrier, owner, execAlive,
                 engFence, inflight, evidence, wreports, epoch, winUsed,
                 grantedW, cancelledW, sends, effects, staleAccept>>

FinishFail(a) ==
  /\ eng[a] = "running"
  /\ eng' = [eng EXCEPT ![a] = "failed"]
  /\ UNCHANGED <<opState, opGen, opEpoch, basis, barrier, owner, execAlive,
                 engFence, inflight, evidence, wreports, epoch, winUsed,
                 grantedW, cancelledW, sends, effects, staleAccept>>

\* The engine emits authoritative, holder-scoped evidence about a key.
\* Emitting "absent" is the engine's answer to a query: it tombstones the key,
\* so no later dispatch of that key can run. The set of evidence models
\* duplicates and reordering.
Emit(a) ==
  \/ /\ eng[a] \in {"done", "failed"}
     /\ evidence' = evidence \cup {[att |-> a, st |-> eng[a]]}
     /\ UNCHANGED <<opState, opGen, opEpoch, basis, barrier, owner, execAlive,
                    engFence, eng, inflight, wreports, epoch, winUsed,
                    grantedW, cancelledW, sends, effects, staleAccept>>
  \/ /\ eng[a] = "none"
     /\ opState[a] \in {"reserved", "intent", "dispatched", "unknown"}
     /\ eng' = [eng EXCEPT ![a] = "tomb"]
     /\ evidence' = evidence \cup {[att |-> a, st |-> "absent"]}
     /\ UNCHANGED <<opState, opGen, opEpoch, basis, barrier, owner, execAlive,
                    engFence, inflight, wreports, epoch, winUsed, grantedW,
                    cancelledW, sends, effects, staleAccept>>

\* Aggregate window observation. Recorded, and nothing consumes it
\* in the real protocol.
EmitReport ==
  \E e \in Epochs :
    /\ e <= epoch
    /\ wreports' = wreports \cup {e}
    /\ UNCHANGED <<opState, opGen, opEpoch, basis, barrier, owner, execAlive,
                   engFence, eng, inflight, evidence, epoch, winUsed, grantedW,
                   cancelledW, sends, effects, staleAccept>>

\* Window replenishment: the next epoch begins. Charges stay in their epoch.
Replenish ==
  /\ epoch < MaxEpoch
  /\ epoch' = epoch + 1
  /\ UNCHANGED <<opState, opGen, opEpoch, basis, barrier, owner, execAlive,
                 engFence, eng, inflight, evidence, wreports, winUsed,
                 grantedW, cancelledW, sends, effects, staleAccept>>

\* ---- Ledger: evidence, settlement, cancel (4.3, 4.4) ------------------

\* Holder-scoped evidence moves intent/dispatched/unknown to verified or failed.
ApplyEvidence(a) ==
  /\ opState[a] \in {"intent", "dispatched", "unknown"}
  /\ \E ev \in evidence :
       /\ ev.att = a
       /\ opState' = [opState EXCEPT ![a] =
            IF ev.st = "failed" THEN "failed" ELSE "verified"]
  /\ basis' = [basis EXCEPT ![a] = "evidence"]
  /\ UNCHANGED <<opGen, opEpoch, barrier, owner, execAlive, engFence, eng,
                 inflight, evidence, wreports, epoch, winUsed, grantedW,
                 cancelledW, sends, effects, staleAccept>>

\* Settlement: verified or failed becomes settled. Stock returns to the pool.
\* Window charges stay in their epoch (completion never refunds).
Settle(a) ==
  /\ opState[a] \in {"verified", "failed"}
  /\ opState' = [opState EXCEPT ![a] = "settled"]
  /\ UNCHANGED <<opGen, opEpoch, basis, barrier, owner, execAlive, engFence,
                 eng, inflight, evidence, wreports, epoch, winUsed, grantedW,
                 cancelledW, sends, effects, staleAccept>>

\* Ledger-local release of a reservation that has no intent. No external
\* effect exists, so this is not a settlement. The window charge is undone in
\* the epoch it was granted.
Cancel(a) ==
  /\ opState[a] = "reserved"
  /\ opState' = [opState EXCEPT ![a] = "cancelled"]
  /\ winUsed' = [winUsed EXCEPT ![opEpoch[a]] = @ - WinNeed[a]]
  /\ cancelledW' = [cancelledW EXCEPT ![opEpoch[a]] = @ + WinNeed[a]]
  /\ barrier' = IF barrier = a THEN "none" ELSE barrier
  /\ UNCHANGED <<opGen, opEpoch, basis, owner, execAlive, engFence, eng,
                 inflight, evidence, wreports, epoch, grantedW, sends,
                 effects, staleAccept>>

\* Timeout is a failure detector only: intent/dispatched -> unknown.
TimeoutOk(a) ==
  /\ opState[a] \in {"intent", "dispatched"}
  /\ opState' = [opState EXCEPT ![a] = "unknown"]
  /\ UNCHANGED <<opGen, opEpoch, basis, barrier, owner, execAlive, engFence,
                 eng, inflight, evidence, wreports, epoch, winUsed, grantedW,
                 cancelledW, sends, effects, staleAccept>>

\* MUTANT: a timeout releases the holding with no evidence.
TimeoutBad(a) ==
  /\ opState[a] \in {"intent", "dispatched"}
  /\ opState' = [opState EXCEPT ![a] = "settled"]
  /\ basis' = [basis EXCEPT ![a] = "none"]
  /\ UNCHANGED <<opGen, opEpoch, barrier, owner, execAlive, engFence, eng,
                 inflight, evidence, wreports, epoch, winUsed, grantedW,
                 cancelledW, sends, effects, staleAccept>>

Timeout(a) == IF Mutant = "timeoutSettles" THEN TimeoutBad(a) ELSE TimeoutOk(a)

\* Audited operator reconciliation (4.4). It is allowed only when the engine
\* is not running. It fences the key (none -> tomb) so a late dispatch cannot
\* run after the holding is released (see README, ambiguity A6).
OperatorReconcile(a) ==
  /\ opState[a] \in {"intent", "dispatched", "unknown"}
  /\ eng[a] \in {"none", "tomb", "done", "failed"}
  /\ eng' = IF eng[a] = "none" THEN [eng EXCEPT ![a] = "tomb"] ELSE eng
  /\ opState' = [opState EXCEPT ![a] = "verified"]
  /\ basis' = [basis EXCEPT ![a] = "operator"]
  /\ UNCHANGED <<opGen, opEpoch, barrier, owner, execAlive, engFence,
                 inflight, evidence, wreports, epoch, winUsed, grantedW,
                 cancelledW, sends, effects, staleAccept>>

\* ---- Executor: crash, restart with new generation ---------------------

\* Crash loses volatile executor state. Durable ledger state and in-flight
\* messages survive.
Crash ==
  /\ execAlive
  /\ execAlive' = FALSE
  /\ UNCHANGED <<opState, opGen, opEpoch, basis, barrier, owner, engFence,
                 eng, inflight, evidence, wreports, epoch, winUsed, grantedW,
                 cancelledW, sends, effects, staleAccept>>

\* Restart takes ownership at a new generation. With a fence, the engine's
\* fence is raised in the same step (ambiguity A4).
Restart ==
  /\ ~execAlive
  /\ owner < MaxGen
  /\ owner' = owner + 1
  /\ execAlive' = TRUE
  /\ engFence' = IF Fenced THEN owner + 1 ELSE engFence
  /\ UNCHANGED <<opState, opGen, opEpoch, basis, barrier, eng, inflight,
                 evidence, wreports, epoch, winUsed, grantedW, cancelledW,
                 sends, effects, staleAccept>>

\* ---- Mutant only: an old-epoch report restores current credit ----------

ApplyStaleRefund ==
  /\ \E e \in wreports : e < epoch
  /\ winUsed[epoch] > 0
  /\ winUsed' = [winUsed EXCEPT ![epoch] = @ - 1]
  /\ UNCHANGED <<opState, opGen, opEpoch, basis, barrier, owner, execAlive,
                 engFence, eng, inflight, evidence, wreports, epoch, grantedW,
                 cancelledW, sends, effects, staleAccept>>

\* ---- Next, fairness, specification -----------------------------------

Next ==
  \/ \E a \in Attempts :
       \/ Grant(a) \/ RecordIntent(a) \/ Dispatch(a)
       \/ FinishOk(a) \/ FinishFail(a) \/ Emit(a)
       \/ ApplyEvidence(a) \/ Settle(a) \/ Cancel(a)
       \/ Timeout(a) \/ OperatorReconcile(a)
  \/ \E m \in MsgSpace : Accept(m)
  \/ Crash \/ Restart \/ Replenish \/ EmitReport
  \/ ((Mutant = "staleReportRefund") /\ ApplyStaleRefund)

\* Fairness: evidence eventually arrives (engine finishes, emits), and the
\* ledger then acts on it. Cancel is a ledger recovery step. Timeout and
\* operator reconciliation get no fairness.
Fairness ==
  \A a \in Attempts :
    /\ WF_vars(Emit(a))
    /\ WF_vars(FinishOk(a) \/ FinishFail(a))
    /\ WF_vars(ApplyEvidence(a))
    /\ WF_vars(Settle(a))
    /\ WF_vars(Cancel(a))

Spec == Init /\ [][Next]_vars /\ Fairness

\* ---- Invariants -------------------------------------------------------

TypeOK ==
  /\ opState \in [Attempts -> OpStates]
  /\ opGen \in [Attempts -> 0..MaxGen]
  /\ opEpoch \in [Attempts -> 0..MaxEpoch]
  /\ basis \in [Attempts -> {"none", "evidence", "operator"}]
  /\ barrier \in Attempts \cup {"none"}
  /\ owner \in Gens
  /\ execAlive \in BOOLEAN
  /\ engFence \in Gens
  /\ eng \in [Attempts -> EngStates]
  /\ inflight \subseteq MsgSpace
  /\ evidence \subseteq EvSpace
  /\ wreports \subseteq Epochs
  /\ epoch \in Epochs
  /\ winUsed \in [Epochs -> 0..WinLimit]
  /\ sends \in [Attempts -> 0..1]
  /\ effects \in [Attempts -> 0..1]
  /\ staleAccept \in BOOLEAN
  /\ \A a \in Attempts : HolderState(a) \in
       {"active", "degraded", "quarantined", "reconciling", "settled", "none"}

\* Conservation: granted stock never exceeds capacity, and window credit
\* never goes negative.
NoOverGrant ==
  /\ StockHeld <= StockCap
  /\ \A e \in Epochs : winUsed[e] <= WinLimit

\* Completion never refunds the window, and an old-epoch report never
\* restores credit. Window use equals grants minus pre-dispatch cancels.
WindowNoRefund ==
  \A e \in Epochs : winUsed[e] = grantedW[e] - cancelledW[e]

\* No settlement without holder-scoped evidence or audited operator
\* reconciliation.
SettledHasEvidence ==
  \A a \in Attempts :
    /\ (opState[a] = "settled") => (basis[a] \in {"evidence", "operator"})
    /\ (basis[a] = "evidence") => (\E ev \in evidence : ev.att = a)

\* A stale generation never starts an effect after a newer one took ownership.
NoStaleEffect == ~staleAccept

\* No idempotency key is sent twice, and no key starts two effects.
NoDoubleEffect ==
  \A a \in Attempts : sends[a] <= 1 /\ effects[a] <= 1

\* A holding that is released (settled or cancelled) has no running engine
\* effect behind it.
ReleasedNoRunning ==
  \A a \in Attempts :
    (opState[a] \in {"settled", "cancelled"}) => (eng[a] # "running")

\* Action property: a timeout never settles a holding.
TimeoutNeverSettles ==
  [][\A a \in Attempts :
       (opState[a] \in {"intent", "dispatched"}) => (opState'[a] # "settled")]_vars

\* ---- Liveness (under Fairness) ----------------------------------------

\* With evidence arriving, every holding that has a dispatch path settles.
Live ==
  \A a \in Attempts :
    (opState[a] \in {"intent", "dispatched", "unknown"}) ~> (opState[a] = "settled")

\* A reservation with no intent is either recorded or released.
ReservedResolves ==
  \A a \in Attempts :
    (opState[a] = "reserved") ~> (opState[a] \in {"intent", "cancelled"})

=============================================================================
