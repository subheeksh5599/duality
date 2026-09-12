# DUALITY Protocol Specification

Version 0.1 — draft. One authoritative statement of the release predicate.
Every release path in this repository calls the same predicate. If an
implementation disagrees with this document, the document is wrong only after
it is amended here.

## 1. Thesis

**APPROVED is not RELEASABLE.**

ACP (via the ERC-8183 job lifecycle) proves the work at evaluation time. It has
no concept of whether that approval is still true when money moves. DUALITY owns
exactly that interval and nothing else.

## 2. Scope

In scope: evidence validity at the instant of release, the release gate, the
three invalidation classes, reconciliation, refund, idempotency, audit.

Out of scope, and never claimed: escrow, evaluator agents, disputes,
attestations, freshness checks in general, capability revocation, oracle
design, reputation, pricing, arbitration.

## 3. Roles

| Role | Actor | Authority |
|---|---|---|
| Client | buyer wallet | funds escrow, can claim refund after expiry |
| Provider | worker agent | delivers evidence bound to the job |
| Evaluator | DUALITY | calls complete() -> release; also the gate |
| Witness | DUALITY registries | records evidence versions and qualification state |

The evaluator is the only role that can release. The hook is what makes the
evaluator unable to release against invalid evidence. That is the point.

## 3.1 Verified lifecycle ordering (ERC-8183 reference core)

Read from `ERC8183.sol` and its own demo-flow docs, not assumed:

```
1  client     createJob(provider, evaluator, expiredAt, description, hook, agentId)
               expiredAt must satisfy: expiredAt > now + 5 minutes
               hook must be whitelisted AND pass an ERC-165 IERC8183Hook check
2  provider   setBudget(jobId, token, amount, optParams)      <- the PROVIDER prices
               the token must be allowlisted by ADMIN_ROLE
3  client     fund(jobId, expectedToken, expectedBudget, optParams)   -> Funded
4  provider   submit(jobId, keccak256(deliverable), optParams)         -> Submitted
5  evaluator  complete(jobId, reason, optParams)             <- THE RELEASE
               beforeAction(job.hook, COMPLETE, data) fires BEFORE the status
               change and BEFORE payment, so a revert rolls the whole release back
6  anyone     claimRefund(jobId)
               not hookable, so no gate can trap a client's funds
               a Submitted job refunds only after expiredAt + EVALUATION_GRACE_PERIOD
               where EVALUATION_GRACE_PERIOD = 1 hours
```

The provider naming the price matters for the job class: in a time-sensitive
quote job the approved evidence IS the provider's quote, and the client funds
against that price. The evidence version and the budget therefore become stale
together, which is exactly the failure DUALITY guards.

## 4. Objects

### 4.1 Evidence

    Evidence {
      evidenceId        bytes32   deterministic: keccak(jobId, provider, contentHash, version)
      jobId             bytes32   ACP/ERC-8183 job identifier
      evaluationId      bytes32   the ACP evaluation this evidence was approved under
      subject           bytes32   what the evidence is about (resource identity)
      evidenceType      string    e.g. "quote"
      contentHash       bytes32   SHA-256 of the deliverable bytes
      version           uint64    monotonically increasing per (jobId, subject)
      observedAt        uint64    unix seconds, source of truth for freshness
      freshnessBound    uint64    seconds; validity is [observedAt, observedAt + freshnessBound]
      provider          address
      qualificationAtObservation uint8   qualification status recorded at observedAt
      qualificationRevision      uint64  registry revision at observedAt
      provenanceHash    bytes32   hash of the provenance envelope
      status            enum      CURRENT | STALE | SUPERSEDED | DISQUALIFIED | DISPUTED | UNRECOVERABLE
      invalidationReason string   set iff invalidated; never cleared
    }

Evidence is append-only. A new observation creates a new version. No code path
mutates a stored evidence record except to set its status and reason once.

### 4.2 Qualification

    Qualification { provider address => { status: uint8, revision: uint64, updatedAt: uint64 } }

Qualification history is append-only in the audit log. This document does not
define who qualifies a provider; it defines that the release predicate reads the
status that held at observation and the status that holds now.

## 5. Freshness semantics

| Rule | Value |
|---|---|
| Clock | wall-clock unix seconds |
| Boundary | INCLUSIVE. valid iff now <= observedAt + freshnessBound |
| Exact expiry | at now == observedAt + freshnessBound the evidence is still valid; one second later it is STALE |
| Maximum allowed observation age | freshnessBound is per-job, set at job creation, immutable after funding |
| Timestamp source | the evaluator's clock, logged; the hook reads block.timestamp and refuses if it disagrees by more than the configured skew |
| Clock skew tolerance | 120 seconds, configurable, recorded per deployment |
| Supersession | a strictly greater version for the same (jobId, subject) supersedes all lower versions |

Freshness is NOT job expiry. The ERC-8183 `expiredAt` field is a separate
mechanism that refunds the client when the job deadline passes. DUALITY never
uses `expiredAt` as its validity clock and does not claim to have invented
expiry.

## 6. The release predicate

    RELEASE(job, ev, now)  iff

      1  job.acp_state                  == APPROVED
      2  ev.jobId                       == job.id
      3  ev.evidenceId                  == job.approvedEvidenceId
      4  ev.version                     == currentVersion(job.id, ev.subject)
      5  now                            <= ev.observedAt + ev.freshnessBound
      6  ev.qualificationAtObservation  == QUALIFIED
      7  qualificationNow(ev.provider)  == QUALIFIED
      8  ev.provenanceHash              == committedProvenance(job.id)
      9  jobConditionsHold(job.id, now)
     10  !settled(job.id)

Every one of the ten clauses is machine-checkable. Clause 3 and 4 together are
what stop a stale approval from releasing funds: approval binds an evidenceId,
and the current version must still be that evidence.

## 7. Decision

    decision ∈ { RELEASE, HOLD, RECONCILIATION_REQUIRED, REFUND }

| Condition | Decision | Reason code |
|---|---|---|
| all ten clauses hold | RELEASE | OK |
| clause 5 fails | HOLD | E_STALE |
| clause 4 fails (strictly newer exists) | RECONCILIATION_REQUIRED | E_SUPERSEDED |
| clause 6 or 7 fails | HOLD | E_DISQUALIFIED |
| clause 8 fails | HOLD | E_PROVENANCE |
| clause 2, 3 or 9 fails | HOLD | E_SUBJECT |
| clause 1 fails | HOLD | E_NOT_APPROVED |
| clause 10 fails | (no decision) | E_ALREADY_SETTLED |
| content hash mismatch on check | HOLD | E_HASH_MISMATCH |

A decision record is written for every evaluation, including RELEASE, and
records: decision, reason code, human reason, timestamp, evidenceId and version
evaluated, the ACP evaluationId, and the KeeperHub execution id if one followed.

## 8. State machine

    PENDING -> DELIVERED -> EVALUATED -> APPROVED -> RELEASE_CHECK
    RELEASE_CHECK -> RELEASED | HOLD | INVALID
    HOLD -> RECONCILIATION -> RELEASE_CHECK | REFUNDED | DISPUTED
    any -> FAILED

Rejected transitions (must revert): RELEASED -> APPROVED,
REFUNDED -> RELEASED, RELEASED -> RELEASED (double settlement),
DELIVERED -> APPROVED (skipping evaluation), any write to a stored evidence
record. Historical transitions are never deleted or rewritten.

## 9. Reconciliation

A HOLD does not terminate a settlement. It creates a reconciliation record:

    Reconciliation {
      reconciliationId, settlementId, failedClause, reasonCode,
      minimumFact, action (RE_OBSERVE | RE_QUOTE | RE_ATTEST),
      attempts, replacementEvidenceId, outcome, createdAt, closedAt
    }

Reconciliation re-observes only the minimum fact the predicate names, produces a
new evidence version, and re-runs the same predicate. It cannot mutate the
original evidence, and it cannot satisfy the predicate by editing recorded state.
If it cannot restore validity it terminates in REFUND.

## 10. Idempotency and replay resistance

- Every settlement has a deterministic settlementId.
- A second release for a settled job reverts (E_ALREADY_SETTLED).
- An evidenceId is deterministic, so the same deliverable re-submitted is the
  same evidence, not a second one.
- Signed metadata carries the version, so an old signed evidence record cannot
  be replayed as current (clause 4 rejects it).
- Duplicate KeeperHub callbacks and duplicate reconciliation requests are
  absorbed by the settlementId, not by hope.

## 11. Trust assumptions

What DUALITY trusts:
- the chain to execute reverts faithfully
- the evaluator's clock within the declared skew, for off-chain freshness checks
- the qualification registry's controller to set status honestly

What DUALITY does NOT trust:
- that an approval remains valid (that is the whole point)
- the provider's claim that its evidence is fresh
- the client's continued consent
- a previous evaluation result

What stays off-chain: evidence bodies, provenance envelopes, the observation
clock, reconciliation orchestration. Their commitments are on-chain.

What happens if the evidence source lies: the predicate still holds and money
moves. DUALITY bounds validity, not truth. Stated in LIMITATIONS.

What happens if DUALITY is offline: no release can be produced by the gate, so
nothing settles; a job can still be refunded through ERC-8183's own `expiredAt`
path, which does not depend on DUALITY.

## 12. Finality

Release is final when the transaction is included and the hook did not revert.
HOLD is not final: it is a state, not an outcome. The absence of a decision is
never treated as a release.
