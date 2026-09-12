# Limitations, trust model, and threat model

Written to be read adversarially. If something here weakens the project's claim,
it is because it is true.

## 1. DUALITY bounds validity, not truth

This is the first thing to understand and the easiest to overstate.

The predicate answers: **is the evidence behind this approval still valid right
now.** It does not answer: **is the evidence correct.** If the observation
process lies at the moment of observation, every clause passes and the money
moves. DUALITY catches evidence that has become *invalid*: expired, superseded,
or disqualified. It cannot catch evidence that was *wrong when it was taken*.

Anyone who reads a release refusal as a correctness proof has read it wrong.

## 2. Trust assumptions, stated rather than hidden

| assumption | in this deployment | what it means |
|---|---|---|
| the evaluator on the live jobs is KeeperHub's wallet `0x1776D4D7...` | because KeeperHub is the execution rail | KeeperHub controls the release call. The gate bounds it, so it cannot release invalid evidence, but it is a party to the release rather than a neutral relay |
| the deployer key `0x087e...A1dA` is admin, qualifier and committer | one key for the testnet demo | whoever holds it can set qualification and commit evidence, which is enough to make a release pass. In production these are three roles and the qualifier should not be the committer |
| the evaluator's clock | our process, with a 120 second skew tolerance recorded on the registry | off-chain freshness decisions use our clock; the hook uses `block.timestamp`. `isReleasableAt` refuses if the two differ by more than the tolerance |
| the qualification registry's controller | the qualifier key | qualification is asserted, not proven. DUALITY does not define who may qualify a provider |

## 3. What is off-chain

Evidence bodies, provenance envelopes, the observation clock, and the
reconciliation loop are off-chain. What is on-chain is the commitment: content
hash, provenance hash, version, observation time, freshness bound, and the
qualification status recorded at observation.

That means a judge of this system can verify that the evidence at release is the
evidence that was approved, and that its declared window has not lapsed. They
cannot verify the evidence's contents from the chain alone.

## 4. Race conditions that are real

- **The simulate window is not atomic.** We observed this on our own harness. Our
  RPC confirmed a revocation while the RPC behind a simulation had not yet, and
  the simulation reported the release as clean for evidence that was already
  invalid. The contract is correct and the harness was wrong; the lesson is that
  a pre-flight result is a statement about a block, and a release is a statement
  about a later one. The gate closing that gap is the argument for a gate at all.
- **Concurrent releases.** Two `complete()` calls in flight settle once because
  the core refuses the second on status. The registry's `settled` flag is a
  second guard behind it. Both were exercised.
- **Inclusive expiry boundaries** are defined: valid at exactly
  `observedAt + freshnessBound`, stale one second later.

## 5. Threat model

| threat | prevention | detection | recovery | residual risk |
|---|---|---|---|---|
| malicious evaluator releases invalid evidence | the hook reverts before the status change and before payment | `ReleaseChecked` event with `ok=false` and the reason code | reconciliation, or the client's `claimRefund` after the deadline | none within the predicate; the evaluator can still refuse to release |
| malicious provider submits a stale deliverable | evidence version and observation time are committed, not claimed | clause 4 and 5 on the next release attempt | the provider re-observes and a new version is approved | a provider that never re-observes strands the job until the deadline refunds it |
| replay of old signed evidence | an approval binds one `evidenceId`; a newer version makes clause 4 fail | `E_SUPERSEDED` | reconciliation commits the current version | none identified |
| evidence substitution (new evidence passed as old) | `evidenceId` is derived from job, provider, content hash and version | the derived id will not match the approved id | none needed; the attempt fails | none identified |
| one deliverable reused across jobs | the hook binds deliverable hash to job at submit | `DeliverableReused` | a distinct deliverable | none identified; this fired on its own during our reruns |
| duplicate release | core status check plus the registry's `settled` flag | the second call reverts | none needed | none identified |
| RPC failure mid-sequence | scripts retry gas estimation; the hook is the authority, so a failure to read is a failure to release | attempt count in the run log | re-run; a job can still be refunded through `claimRefund` | a persistent outage stalls releases until the deadline, which is the safe direction |
| KeeperHub failure | the gate does not depend on KeeperHub; it depends on the chain | execution `status: failed` with an error | retry with a new idempotency key, or call the core directly | none beyond losing the audit record |
| compromised backend | the backend cannot make the predicate pass; it can only stop releases | a mismatch between the predicate's answer and the hook's revert | re-run the predicate; the chain is the arbiter | a compromised backend can stall releases and commit false observations, which is a real harm and is why the committer role is a named trust assumption above |
| malicious reconciliation | reconciliation can only commit a new version and re-run the same predicate | the new version and its observation time are on-chain | none needed | it cannot edit the original evidence |
| oracle or data-source manipulation | out of scope: clause 9 delegates to an optional oracle and we did not ship one | n/a | n/a | the predicate cannot catch a lying source, see section 1 |

## 6. What cannot be deterministically validated

Outcomes that are not a fact about a chain or a signed reading. A judgement about
whether a document is good, a rating, a negotiation result. For those the
evaluator is a human or a model, and DUALITY can only hold the evidence's
validity window around that judgement. The default position of this project is
that an LLM is never the final authority for moving money.

## 7. Not built yet

Stated plainly, because a README that implies otherwise is the more damaging
error:

- **No structured logging.** Every request gets a correlation id, the response
  carries it, and every state change is appended to the audit log, but the log is
  a JSONL file rather than a logging pipeline with levels and shipping. The
  counters are a dict behind `GET /health`, not a metrics endpoint.
- **No scheduler.** The service reacts to requests. Nothing watches evidence
  windows and holds or reconciles on its own, so a job whose window lapses sits
  in `E_STALE` until someone asks.
- **No ACP adapter.** The jobs are ERC-8183 on Base Sepolia. The ACP agent
  identity exists but is not wired into this flow, so the ACP terminology in the
  README describes the standard the gate is built on, not a live ACP job.
- **Not verified on mainnet.** Chain 84532 only.
- **Contracts are not verified on Basescan.** The addresses are live and readable
  but the source is not published there.

## 8. Operational limits of the service

Measured, not estimated.

- **List views are slow on a public RPC.** `GET /jobs` makes several round trips
  per job. Sequentially that was 98 seconds for 20 jobs, which made the control
  surface look broken when it was only slow. It is now parallel and cached
  (20 second TTL, warmed at startup): 17.5 seconds cold, instant warm. A
  production deployment needs batched reads or an index, not more concurrency.
- **A landed transaction can be reported as a failure.** We did this to
  ourselves: a mutation succeeded on chain (tx `322d5c4a...`) while the service
  returned HTTP 500 because of a parameter collision in its own event writer.
  The chain was right and the report was wrong. This is the exact failure DUALITY
  exists to prevent, committed by DUALITY's own service, and the fix was to
  surface the cause in the response rather than swallow it.
- **The service is subject to the staleness it guards against.** A read taken
  immediately after a write can reflect the previous state. We saw it three times:
  twice comparing our RPC against the sponsor's, and once inside our own
  reconciliation, where a successful reconciliation incremented
  `reconciliation_succeeded` by zero and reported `before: E_STALE, after: OK` in
  the same record. The gate was right every time and the reader was wrong, which
  is the argument for the product stated by its own bugs.
  The reconciliation path now waits until its writes are readable (the approval
  binding the current version) before it believes the predicate's answer. The
  wider risk is not eliminated: any read on this service can still reflect a
  state one block behind.
- **Reconciliation cannot fix a revoked provider.** By design: it re-observes the
  minimum fact, and if that fact is the provider's qualification, the operator has
  to restore it. The correct terminal state is then a refund.
- **No persistence.** Decisions and reconciliation records live in memory and in
  the JSONL log. A restart loses the in-memory counters, which is why the log,
  not the process, is the record.

## 9. What would make this fail review

Naming it before a reviewer does:

- claiming trustlessness. Nothing here is trustless. The gate is enforceable, and
  the roles around it are named above.
- claiming the gate validates outcomes. It validates evidence against a window
  and a qualification state.
- claiming production readiness. It is a testnet deployment with a single-key
  role model and one hour of operational history.
