# DUALITY

**Approved is not releasable.**

DUALITY is an ERC-8183 release gate. ACP and ERC-8183 prove the work at
evaluation time. DUALITY proves the approval is still valid at the instant the
money moves, and refuses the release when it is not.

Live on Base Sepolia. Real USDC moved. Real releases blocked.

---

## The result, in one table

Every row is a real transaction on Base Sepolia (chain 84532). Nothing here is a
simulation.

| what happened | how it ended | evidence |
|---|---|---|
| a job is approved, then the freshness window lapses, then the evaluator calls `complete()` | **BLOCKED**, money held | [tx](https://sepolia.basescan.org/tx/e493ef893a9004922b9999a2005cacb70f93c7a1d55a4c45643fc802913da0ec) |
| the same job, after a fresh observation is approved | **RELEASED**, provider paid exactly 1 USDC | [tx](https://sepolia.basescan.org/tx/4b2a020f288720366ab8dad9a33056195e8b820847f6cb4095fbef11a7c4c05d) |
| a third `complete()` on the settled job | **BLOCKED**, no double settlement | [tx](https://sepolia.basescan.org/tx/9afb1dcedff79dd9853117e9a821ef9816527b3ad066f6d62f1b011ec3620b94) |
| the release, executed by **KeeperHub**, not by our script | completed, gas sponsored | [tx](https://sepolia.basescan.org/tx/0x6248089e689d4dc65e721cf03399a95eaa991b8797b2ed4d3b992d89eec120e8), execution `qyn8k10j5mv6529c5cjtu` |
| KeeperHub's own simulation, asked to release stale evidence | `wouldRevert: true`, `ReleaseBlocked(jobId, E_STALE)` | `artifacts/keeperhub-release.json` |

## The three invalidation classes, each proven on live chain

Checklist requirement: all three must work in the actual demo. All four cases
below were blocked by the gate and named by the predicate, and each refusal was
reported by KeeperHub's own simulation.

| class | what changed after approval | decision |
|---|---|---|
| **SUPERSEDED** | a newer observation of the same subject landed | `E_SUPERSEDED` |
| **DISQUALIFIED** | the provider lost its qualification | `E_DISQUALIFIED` |
| **STALE** | the freshness window lapsed | `E_STALE` |
| **NOT_APPROVED** | nothing was ever bound to the job | `E_NOT_APPROVED` |

The SUPERSEDED job was then carried through reconciliation to a real release
(execution `05d269jk3yx0xpllgsf6l`), so the artefact shows both the refusal and
the recovery.

Raw records: `artifacts/invalidation-classes.json`, `artifacts/onchain-e2e.json`,
`artifacts/keeperhub-release.json`.

---

## What ERC-8183 does, and what it does not

Read from `ERC8183.sol` and the standard's own docs, not assumed.

**It does:** three roles (client, provider, evaluator), escrow, an evaluator
attestation that releases funds, and a job deadline (`expiredAt`) that refunds
the client through `claimRefund` after an `EVALUATION_GRACE_PERIOD` of one hour.

**It does not:** have any concept of an approval becoming invalid while the job
is still inside its deadline. Its model is approve once, then a clock.

That gap is the product. An approval is a statement about a moment. Money moves
later. Between those two moments the quote expires, a newer observation lands, a
provider's mandate is revoked.

## The primitive

The thing DUALITY adds is a **release predicate** evaluated at release time,
written once in Solidity and read from exactly two places:

```
RELEASE(job, ev, now)  iff
   1  job state is approved
   2  ev.jobId == job.id
   3  ev.evidenceId == the evidence the approval bound
   4  ev.version == the current version for (job, subject)
   5  now <= ev.observedAt + ev.freshnessBound          inclusive at the boundary
   6  qualification at observation == QUALIFIED
   7  qualification now == QUALIFIED
   8  provenance hash matches the committed one
   9  job conditions hold                                (external oracle, optional)
  10  the job has not already settled
```

Clauses 3 and 4 together are what stop a stale approval from releasing money: an
approval binds one evidence id, and that id must still be the current version.

Because the off-chain decision calls the same on-chain function through
`eth_call`, the decision a service makes and the decision the chain enforces
cannot drift. There is no second implementation.

## The gate is not advisory

`complete()` is the evaluator's call, and the evaluator's call is what moves the
money. An ERC-8183 job stores a per-job hook, and the core calls
`beforeAction(job.hook, COMPLETE, data)` **before** it changes the job status and
before it pays, so a revert rolls the whole release back.

The consequence is worth stating plainly: the evaluator cannot release against
invalid evidence, even if the evaluator wants to. And because `claimRefund` is
deliberately not hookable, this gate can never trap a client's funds.

## Architecture

```
  client          provider           evaluator                 DUALITY
    |                 |                  |                        |
    | createJob(..., hook=DUALITY)      |                        |
    |                 | setBudget (the provider prices)         |
    | fund USDC       |                  |                        |
    |                 | submit(deliverable)                      |
    |                 |                  |                  observe + commit
    |                 |                  |                  approve evidence
    |                 |        complete()|                        |
    |                 |                  |--> core --> hook --> predicate
    |                 |                  |              RELEASE | HOLD
    |                 |                  |                        |
    |                 |                  |            reconciliation: new version
    |                 |                  |                        |
    |                 |        complete()|--> core --> hook --> RELEASED
```

Components, and the trust boundary of each:
