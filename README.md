<div align="center">

# DUALITY

### Approved is not releasable.

An ERC-8183 release gate. The standard proves the work at evaluation time. DUALITY proves that the approval is still valid at the instant the money moves, and refuses the release when it is not.

[![Network](https://img.shields.io/badge/network-Base%20Sepolia-0052FF?labelColor=0f1420)](https://sepolia.basescan.org/address/0x86b951233E131d2dc0931aeB70ebc9Da17e998f6)
[![Contract tests](https://img.shields.io/badge/contract%20tests-10%20passing-2ecc71?labelColor=0f1420)](contracts/test/DualityGate.t.sol)
[![Predicate](https://img.shields.io/badge/predicate-on--chain-14151a?labelColor=0f1420)](docs/PROTOCOL-SPEC.md)
[![License](https://img.shields.io/badge/license-MIT-yellow?labelColor=0f1420)](LICENSE)

**[ERC-8183](https://eips.ethereum.org/EIPS/eip-8183)** &nbsp;·&nbsp;
**[Live core](https://sepolia.basescan.org/address/0x86b951233E131d2dc0931aeB70ebc9Da17e998f6)** &nbsp;·&nbsp;
**[Live gate](https://sepolia.basescan.org/address/0xF5ac445c06b8a7acf94Fc5Aa6C9eeB9A437da438)** &nbsp;·&nbsp;
**[Demo](docs/demo.mp4)** &nbsp;·&nbsp;
**[Evidence](artifacts/)** &nbsp;·&nbsp;
**[Protocol spec](docs/PROTOCOL-SPEC.md)** &nbsp;·&nbsp;
**[Limitations](docs/LIMITATIONS.md)**

</div>

**Status.** Live on Base Sepolia: every release and every refusal in this repository is a
real transaction with a hash in [`artifacts/`](artifacts/). Building the integration exposed
a gap in the execution rail's error decoding, which is filed, fixed and merged upstream -
[#2457](https://github.com/KeeperHub/keeperhub/pull/2457), with
[#2472](https://github.com/KeeperHub/keeperhub/pull/2472) documenting the field that carries
it. That pair is part of seventeen merges in September, nine of them functional changes.
What is not built yet is listed in [Limitations](docs/LIMITATIONS.md) rather than implied
away.

---

## Table of contents

- [▶ See it in one command](#-see-it-in-one-command)
- [The gap in the standard](#the-gap-in-the-standard)
- [1. The primitive](#1-the-primitive)
- [2. The release predicate](#2-the-release-predicate)
- [3. The gate is not advisory](#3-the-gate-is-not-advisory)
- [4. The three invalidation classes](#4-the-three-invalidation-classes)
- [5. Architecture](#5-architecture)
- [6. Safety: claim to mechanism](#6-safety-claim-to-mechanism)
- [7. How it uses Base](#7-how-it-uses-base)
- [8. How it uses KeeperHub](#8-how-it-uses-keeperhub)
- [9. The ACP lane](#9-the-acp-lane)
- [10. Engineering decisions and the hard problems](#10-engineering-decisions-and-the-hard-problems)
- [11. Real vs pending](#11-real-vs-pending)
- [12. Tests](#12-tests)
- [13. The web surfaces](#13-the-web-surfaces)
- [14. Run locally](#14-run-locally)
- [15. Configuration](#15-configuration)
- [16. Deploy](#16-deploy)
- [17. Project layout](#17-project-layout)
- [18. Tech stack](#18-tech-stack)
- [19. Prior art](#19-prior-art)
- [20. Roadmap](#20-roadmap)
- [License](#license)

---

## ▶ See it in one command

```bash
.venv/bin/python scripts/prove_invalidation_classes.py
```

Real output from a run against the deployed contracts:

```text
[1/4] SUPERSEDED: a newer observation lands after the approval
  createJob                                      OK   25514dc09c0188070f35b47266c00ee28e8f4648b2f8524370bf9ea605659788
  setBudget 1 USDC                               OK   24d97506bb4b18ea45eb28153913dec01cf1bdba305d7644aaac038db0504170
  fund 1 USDC                                    OK   2abc169a33235d037dee098c6c059af57985efdaf083e088d09c33cf44510864
  submit deliverable                             OK   0215e6115679faa294da5e75cb3fce1a190dca2661307bec122ed8f2601ca005
  commit evidence v1                             OK   5a465123be1137afb978c0d6acb394235344bc95690090f876ebdf5e98f05ca7
  approve v1                                     OK   56b75b5860c2bc6f80f58fcf1a6e78eb3f92a050657a9ab9a31a6be0f2eca775
  commit evidence v2 (NOT approved)              OK   53c98da30fa2f21e88cb5fd1ff9f64b07792fca8e894a7dc63c7683b4f24715f
   KeeperHub simulate -> HTTP 400 wouldRevert=True
   reason: ReleaseBlocked(9, 0x455f535550455253454445440000000000000000000000000000000000000000)
   -> recovery: re-approve the current version and release through KeeperHub
  approve v2                                     OK   3beb89c15e2bbea33e761e19dd88818590753f2c8a1458a70c3ac33fec59fabf
   release -> HTTP 202 status=completed usdc 3.60 -> 4.60

   class          invariant: money blocked   predicate code   reason legible
   SUPERSEDED     YES                        E_SUPERSEDED     yes
   DISQUALIFIED   YES                        E_DISQUALIFIED   yes
   STALE          YES                        E_STALE          yes
   NOT_APPROVED   YES                        E_NOT_APPROVED   yes

RESULT: PASS - every class blocked on live chain, each named by the predicate
```

The hexadecimal reasons are the ASCII codes: `0x455f5354414c45` is `E_STALE`.

## The gap in the standard

Read from `ERC8183.sol` and the standard's own documentation, not assumed.

**ERC-8183 provides** three roles (client, provider, evaluator), escrow, an evaluator attestation that releases funds, and a job deadline (`expiredAt`) that refunds the client through `claimRefund` after an `EVALUATION_GRACE_PERIOD` of one hour.

**ERC-8183 does not provide** any concept of an approval becoming invalid while the job is still inside its deadline. Its model is approve once, then a clock.

An approval is a statement about a moment. Money moves later. Between those two moments the quote expires, a newer observation of the same subject lands, or a provider's mandate is revoked. Nothing in the lifecycle notices. That interval is the entire product.

## 1. The primitive

A **release predicate** evaluated at release time, written once in Solidity and read from exactly two places.

```solidity
// contracts/src/EvidenceRegistry.sol
function isReleasable(uint256 jobId, uint64 nowTs) public view returns (bool ok, bytes32 reason)
```

- the **gate hook** calls it inside `complete()`, where a false result reverts the whole release
- the **evaluator service** calls the same function over `eth_call` to make its decision

Because the service's decision *is* the chain's function, the two cannot drift. There is no second implementation of the rule to keep in sync.

## 2. The release predicate

```text
RELEASE(job, ev, now)  iff
   1  job state is approved
   2  ev.jobId == job.id
   3  ev.evidenceId == the evidence the approval bound
   4  ev.version == the current version for (job, subject)
   5  now <= ev.observedAt + ev.freshnessBound            inclusive at the boundary
   6  qualification at observation == QUALIFIED
   7  qualification now == QUALIFIED
   8  provenance hash matches the committed one         NOT ENFORCED, see section 11
   9  job conditions hold                                  external oracle, optional
  10  the job has not already settled
```

Clauses 3 and 4 together are what stop a stale approval from releasing money: an approval binds one evidence id, and that id must still be the current version.

Clause 1 is enforced by ERC-8183's own `complete()`; clause 9 is delegated to an
oracle this deployment does not ship. The clauses the contract actually reads are
2, 3, 4, 5, 6, 7 and 10 - so clause 8 is a commitment this project records and does
not yet enforce, and section 11 says so in the same words.

Clause order is part of the semantics. The first failure is the reason reported, which is why a job that is both superseded and disqualified answers `E_SUPERSEDED`.

| reason code | decision | meaning |
|---|---|---|
| `OK` | `RELEASE` | valid at release time |
| `E_STALE` | `HOLD` | past the freshness bound |
| `E_SUPERSEDED` | `RECONCILIATION_REQUIRED` | a newer observation exists |
| `E_DISQUALIFIED` | `HOLD` | the provider is no longer qualified |
| `E_QUAL_OBSERVATION` | `HOLD` | not qualified when the reading was taken |
| `E_NOT_APPROVED` | `HOLD` | no approval bound to the job |
| `E_PROVENANCE` | `HOLD` | provenance commitment mismatch |
| `E_ALREADY_SETTLED` | `SETTLED` | already paid |
| `E_CONDITION` | `HOLD` | the checker's clock is outside the declared skew bound, so no decision is taken |
| `E_HASH_MISMATCH` | `HOLD` | the deliverable does not match the committed content hash |
| `E_INVALIDATED` | `HOLD` | the evidence was invalidated: disputed or unrecoverable |

## 3. The gate is not advisory

`complete()` is the evaluator's call, and the evaluator's call is what moves the money. An ERC-8183 job stores a per-job hook, and the core calls `beforeAction(job.hook, COMPLETE, data)` **before** it changes the job status and before it pays:

```solidity
if (actor != job.evaluator) revert Unauthorized();
bytes memory data = abi.encode(actor, reason, optParams);
_beforeHook(job.hook, jobId, this.complete.selector, data);   // <- the gate
job.status = JobStatus.Completed;
```

So a revert inside the hook rolls the whole release back. Two consequences worth stating plainly:

- **the evaluator cannot release against invalid evidence, even deliberately.**
- **the gate can never trap funds**, because `claimRefund` is deliberately not hookable, and neither is `expiredAt`.

## 4. The three invalidation classes

Each was induced on live chain against the deployed contracts, and each refusal was reported by KeeperHub's own simulation.

| class | what changed after approval | job | decision | record |
|---|---|---|---|---|
| **SUPERSEDED** | a newer observation of the same subject landed | 17 | `E_SUPERSEDED` | `artifacts/invalidation-classes.json` |
| **DISQUALIFIED** | the provider's qualification was revoked | 18 | `E_DISQUALIFIED` | same |
| **STALE** | the freshness window lapsed | 19 | `E_STALE` | same |
| **NOT_APPROVED** | nothing was ever bound to the job | 20 | `E_NOT_APPROVED` | same |

The refused release, then the recovery, on job 19:

```text
POST /jobs/19/release     -> release_held   via keeperhub  wouldRevert=true   E_STALE
POST /jobs/19/reconcile   -> reconciled     E_STALE -> OK   replacementVersion 2
POST /jobs/19/release     -> released       execution nnue54k2f9ajk245oplga
                             tx 0x6de7c403d8a74712ade266be26ddb464a8e02f9027d22c957c4ee9ba53d8ff48
GET  /jobs/19             -> Completed, settled, decision SETTLED
```

## 5. Architecture

```text
                    +-------------------------------+
   client  -------->|  ERC-8183 core (unmodified)   |-------> provider
   provider ------->|  escrow, roles, job lifecycle |
   evaluator ------>|  stores a per-job hook        |
                    +---------------+---------------+
                                    | beforeAction(COMPLETE, data)
                                    v
                    +-------------------------------+
                    |      DualityGateHook          |
                    |  reverts unless the predicate |
                    |  holds at block.timestamp     |
                    +---------------+---------------+
                                    | isReleasable(jobId, now)
                                    v
                    +-------------------------------+
                    |      EvidenceRegistry         |
                    |  versions, qualification,     |
                    |  the predicate itself         |
                    +-------------------------------+
                                    ^
                                    | the same question, by eth_call
                    +-------------------------------+
                    |     evaluator service         |
                    |  observe / approve / check /  |
                    |  reconcile / release          |
                    +---------------+---------------+
                                    | simulate, then broadcast
                                    v
                    +-------------------------------+
                    |          KeeperHub            |
                    |  signs and submits complete() |
                    +-------------------------------+
```

| component | role | what it can and cannot do |
|---|---|---|
| `EvidenceRegistry` | holds versions, qualification and the predicate | written only by its committer; never rewrites history |
| `DualityGateHook` | the veto on the release | can block a release; can never block a refund |
| ERC-8183 core | escrow and roles | the upstream reference implementation, unmodified |
| evaluator service | observes, commits, decides, reconciles | its decision is the chain's function, not a parallel one |
| KeeperHub | submits the release transaction | executes; cannot make the gate pass |

Evidence is append-only. A new observation is a new version, and no code path edits a stored record other than to set its status and reason once. That is what makes lineage auditable: after version 2 exists, the approval still points at version 1, which is why the refusal is `E_SUPERSEDED` rather than silence.

## 6. Safety: claim to mechanism

| claim | mechanism | where it is proven |
|---|---|---|
| the evaluator cannot release invalid evidence | hook `beforeAction` reverts before the status change and the payment | `DualityGate.t.sol`, live txs |
| the gate cannot trap funds | `claimRefund` is not hookable | `test_gateCannotBlockRefund` |
| money cannot move twice | core status check, plus a settlement flag in the registry | `test_releaseCannotHappenTwice`, live tx `9afb1dce` |
| an old approval cannot be replayed | the approval binds one `evidenceId`; clause 4 rejects a newer current version | `test_superseded_blocksRelease` |
| one deliverable cannot serve two jobs | the hook binds the deliverable hash to the job at submit | `DeliverableReused`, observed during reruns |
| the service and the chain cannot disagree | the service calls the on-chain predicate by `eth_call` | `service/duality_service.py` |
| a transport failure cannot be read as a verdict | reads rotate across the deployment's published endpoints on a 429, a timeout, or any transport failure | `scripts/keeperhub_release.py` |
| the surface never shows a verdict the chain has moved past | every JSON read is sent `no-store`; the browser cannot serve a cached decision | `service/duality_service.py` |
| every state change is recorded | append-only JSONL audit log, each record referencing its predecessor | `artifacts/events.jsonl` |

## 7. How it uses Base

- Base Sepolia, chain `84532`, because the gate is a contract and the escrow is real.
- USDC `0x036CbD53842c5426634e7929541eC2318f3dCf7e` is the payment token, allowlisted on the core.
- Every release and every refusal in this repository is a real transaction with a hash in `artifacts/`.
- Deployment cost `0.0001068 ETH`.

| | address |
|---|---|
| ERC-8183 implementation | [`0x8661E6bc2854d1ecAc0DB3f4467f4B64327a8e71`](https://sepolia.basescan.org/address/0x8661E6bc2854d1ecAc0DB3f4467f4B64327a8e71) |
| ERC-8183 core (proxy) | [`0x86b951233E131d2dc0931aeB70ebc9Da17e998f6`](https://sepolia.basescan.org/address/0x86b951233E131d2dc0931aeB70ebc9Da17e998f6) |
| EvidenceRegistry | [`0x1EcAbF0650664cB16A416fFd558a4836e0f7bCC3`](https://sepolia.basescan.org/address/0x1EcAbF0650664cB16A416fFd558a4836e0f7bCC3) |
| DualityGateHook | [`0xF5ac445c06b8a7acf94Fc5Aa6C9eeB9A437da438`](https://sepolia.basescan.org/address/0xF5ac445c06b8a7acf94Fc5Aa6C9eeB9A437da438) |

## 8. How it uses KeeperHub

KeeperHub is the execution rail, not a wrapper. The evaluator on the live jobs is KeeperHub's own wallet, so the release transaction is signed and submitted by KeeperHub rather than by this repository.

| step | call |
|---|---|
| dry run, and read `wouldRevert` | `POST /api/execute/contract-call` with `simulate: true` |
| broadcast, only if the gate allows | the same call, with an `Idempotency-Key` and no `simulate` |
| poll to terminal, keep the proof | `GET /api/execute/{executionId}/status` |

```text
KeeperHub SIMULATES while the approval is stale
    HTTP 400   success=false   wouldRevert=true
    signer     0x1776d4d751d97c85845bf54e6ce364cec62d4bbf
    decoded    0x5192a3c5 = ReleaseBlocked(uint256,bytes32), jobId 7, reason E_STALE

KeeperHub BROADCASTS the release
    executionId qyn8k10j5mv6529c5cjtu
    status      completed
    sponsored   true
    tx          0x6248089e689d4dc65e721cf03399a95eaa991b8797b2ed4d3b992d89eec120e8
```

A refusal arrives in one of two shapes, and both are recorded with the side that said it.

```text
the rail agrees      release_held  via=keeperhub  wouldRevert=true   E_SUPERSEDED
                     the error is the gate's, named by the rail
the rail has not     release_held  via=predicate  wouldRevert=false  E_SUPERSEDED
caught up yet        railSimulationClean=true - nothing broadcast
```

The rail is asked to simulate *before* this service acts on its own verdict, because the rail's answer is the evidence that a refusal is the gate's and not this process's opinion. When its view has not caught up with a write the service can already read, the service refuses anyway, records which side said what, and broadcasts nothing. One clean simulation is not authority to move money: on that path this log once recorded a release that never happened - no execution, no transaction, no payment - while the money stayed put. Both rows are in `artifacts/events.jsonl`.

This is worth dwelling on: KeeperHub's documented safe-first-write sequence is simulate, check `wouldRevert`, then broadcast. That is the same shape as DUALITY's thesis one layer down, and the gate is what makes `wouldRevert` informative rather than decorative, because the predicate behind it can fail after the approval it was made against.

One gap found while building this is filed upstream as **[KeeperHub#2430](https://github.com/KeeperHub/keeperhub/issues/2430)**: a revert raised inside a callee contract cannot be decoded, because the API accepts a single `abi` field, so a hook's custom error reaches the caller as raw hex. It was accepted, the fix and its docs both merged (#2457, #2472), and the refusal in section 9 is recorded in exactly that raw form - because the hosted API does not read the new field yet. See section 9 for the measurement.

### What the integration produced upstream

Building the release gate is what exposed the gap above, and the fixes are merged rather
than filed and abandoned. Every one below landed in September. They are split, because a
reader weighing this work should not have to count documentation commits as features.

**Nine functional changes.**

| PR | merged | lines | what it changed |
|---|---|---|---|
| [#2477](https://github.com/KeeperHub/keeperhub/pull/2477) | 2026-09-17 | `+20/-3` | a long step label wraps on a phone instead of setting the table width |
| [#2404](https://github.com/KeeperHub/keeperhub/pull/2404) | 2026-09-16 | `+1176/-12` | declared Manual input is collected before an editor run |
| [#2302](https://github.com/KeeperHub/keeperhub/pull/2302) | 2026-09-15 | `+632/-102` | monitoring is reachable and readable on a phone |
| [#2457](https://github.com/KeeperHub/keeperhub/pull/2457) | 2026-09-15 | `+875/-19` | a revert raised in a callee is decoded from extra error ABIs |
| [#2387](https://github.com/KeeperHub/keeperhub/pull/2387) | 2026-09-15 | `+1671/-0` | approvals are checked against the Revoke.cash exploit list |
| [#2386](https://github.com/KeeperHub/keeperhub/pull/2386) | 2026-09-10 | `+152/-33` | a sponsored send is never reported pre-broadcast when its outcome is unknown |
| [#2362](https://github.com/KeeperHub/keeperhub/pull/2362) | 2026-09-09 | `+288/-3` | `requiredPlan` is disclosed on action schemas |
| [#2297](https://github.com/KeeperHub/keeperhub/pull/2297) | 2026-09-09 | `+354/-94` | `kh_` API keys are accepted on the session-only analytics routes |
| [#2300](https://github.com/KeeperHub/keeperhub/pull/2300) | 2026-09-04 | `+886/-22` | a per-chain token bucket replaces a fixed dispatch jitter |

**#2457** came straight out of this integration: it makes a revert raised inside a callee
decodable, which is the failure this project hit first, and its documentation counterpart is
**#2472** below. The rest came from the same reading of the surface - API keys that only worked on
session routes, a sponsored send reported before it was broadcast, a monitoring page that
was unusable on a phone.

**Eight documentation and housekeeping changes.**

| PR | merged | what it changed |
|---|---|---|
| [#2472](https://github.com/KeeperHub/keeperhub/pull/2472) | 2026-09-15 | point an undecoded revert at `errorAbis` |
| [#2446](https://github.com/KeeperHub/keeperhub/pull/2446) | 2026-09-14 | document the `failOnError` toggle on Write Contract |
| [#2409](https://github.com/KeeperHub/keeperhub/pull/2409) | 2026-09-14 | correct stale paths in messages that tell a reader where to look |
| [#2385](https://github.com/KeeperHub/keeperhub/pull/2385) | 2026-09-14 | fix stale README links and paths |
| [#2356](https://github.com/KeeperHub/keeperhub/pull/2356) | 2026-09-08 | point agent and API consumers at wallet and address discovery |
| [#2355](https://github.com/KeeperHub/keeperhub/pull/2355) | 2026-09-08 | protocol writes return a 202 `executionId` envelope |
| [#2298](https://github.com/KeeperHub/keeperhub/pull/2298) | 2026-09-08 | add an ID glossary and cross-link the onboarding guides |
| [#2301](https://github.com/KeeperHub/keeperhub/pull/2301) | 2026-09-08 | scope the simulate preflight to tools that support it |

One correction belongs above that split rather than inside it: **#2387** merged first, and a
later implementation of the same issue (#2417) took the registry slot in
`plugins/web3/index.ts`. The step file it added is still in the tree and still tested, but it
is not reachable through the plugin registry, so it is listed as a change that merged rather
than counted as a live feature.

## 9. The ACP lane

Every other lane here settles against evidence about a **deliverable**. This one settles
against evidence about a **counterparty**: the provider side of the job is an agent that
exists in the ACP registry, and the escrow pays that agent's own wallet rather than the
operator that submitted on its behalf. It is an existing registration, reused: the
agent's identity is read from configuration, this project registers no agent and holds no
platform secret, and the lane refuses to run without one configured rather than
substituting an address of its own. A counterparty this project had created would be one
this project could vouch for, which is worth less than one that arrived with a history.

```text
client     createJob(provider = operator, evaluator = KeeperHub's wallet, hook = the gate)
provider   setPayoutReceiver(agent wallet)   <- the money is aimed at the agent before funding
provider   setBudget        client   fund
provider   submit(deliverable)
             evidence subject     = keccak(canonical(agent binding))
             evidence provenance  = keccak(canonical(observation envelope))
evaluator  KeeperHub complete()  -> the gate re-reads the predicate and refuses, or pays
```

What the chain holds, so none of it has to be taken on this repository's word:

- the job's `payoutReceiver` is the agent's wallet, read back from the core in `artifacts/acp-provider-job.json`
- `subject` and `provenanceHash` are commitments to two published files - `artifacts/acp-agent-binding.json` and the envelope inside the run record
- `scripts/acp_provider_job.py --verify` re-hashes both files and compares the result against the deployed registry, so the binding is checkable without running anything that produced it

The run, live on Base Sepolia:

```text
  createJob (evaluator = KeeperHub)              OK   f68c8565f620309a76985d35c1f085261c21808946794258a77567f0f7aba461
   jobId 31, payoutReceiver aimed at the agent's own wallet before funding
  setPayoutReceiver(agent wallet)                OK   f5483a0663f0ebfed8b86cbab54497cf363f9c6c5455ba858c0244b9b2625593
  setBudget 1.00 USDC                            OK   3666fa6a196cb1534db7632e6ea0ebcaf054575cff582b7040c1f113e3d37090
  fund 1.00 USDC                                 OK   9ab0e9301612af7b52ab94472752f1be5fe31af46ae6b27076b47dbd7d9f4500
  setQualification(operator, QUALIFIED)          OK   2a20aa9e493b42e2f5558369644fbb896d4a60c542ac1362e737839ad187f5d0
  submit deliverable                             OK   0cba7215ecab227e719d75f2a8b3dc38af56c1ab9da6ac039886ed4c23a65519
  commit evidence (agent-bound subject)          OK   b46c7f0be8b233382d92f4bfd3cc7d117948ace0279b0097aa7b47df1f38ef6d
  approve                                        OK   07a92134716b08e0c2f706597fac7e31fb06168fe886ba9191ffafbe44e6fd82
  revoke qualification (the refusal beat)        OK   1ab5021320b335e9e6c66f5990447245cb001fa2b6d47bf706d0240e6d14f400
   KeeperHub simulate -> HTTP 400 wouldRevert=true  (attempt 1 of 1)
   the request carried the gate's errors, so KeeperHub names the refusal itself:
   ReleaseBlocked(31, 0x455f4449535155414c4946494544000000000000000000000000000000000000)
   (that argument is the ASCII reason code: 0x455f4449535155414c4946494544 is E_DISQUALIFIED)
  reinstate qualification                        OK   82535dd2f431d77271b2dff904fc98c513cf372362ba6c8176f4a098b3035a98
   attempt 1: HTTP 200 wouldRevert=false, predicate OK
   release -> execution hhj5r6n0q113qqtzde8pc, status completed, sponsored true
   tx 0xcebb4c5528e794a6c7fcee4d50f159910a584730be2e77f073603d828d34e544
   agent USDC 29.06 -> 30.06
```

Every hash above is read out of `artifacts/acp-provider-job.json`, which the run writes
itself, and the block is generated from that file rather than retyped - a retyped hash is
how a wrong one gets published.

Three things this lane states rather than hides:

- **the operator submits, the agent is paid.** The registered agent holds no signing key this
  repository can use, so the submission comes from an operator address while the escrow's
  `payoutReceiver` is the agent's wallet. The gate does not care who typed the deliverable: it
  decides whether the agent may be paid. An operator that submits against a disqualified
  counterparty is refused exactly like a provider that submits stale evidence.
- **the reason the release was refused came from the counterparty's standing, not the
  deliverable's freshness.** Same predicate, same clauses: 6 and 7 are the qualification pair,
  and the reason code names which one fired.
- **The refusal above is named by KeeperHub, and getting there took a measurement.** A revert
  raised inside a job's hook is not in the ABI of the call target, so it reaches a caller as
  hex. That gap is filed upstream as
  [KeeperHub#2430](https://github.com/KeeperHub/keeperhub/issues/2430), and the fix and its
  docs both merged (#2457, #2472), adding an `errorAbis` request field. This lane attaches it
  on every release call. The hosted API accepts it and ignores it: an empty document, a
  document that is not an ABI at all, and five documents (over the documented cap of four) all
  pass without the rejection the merged contract specifies, and a hook refusal stays hex with
  `errorAbis` attached. What does work is the shape this project sends - the hook's errors
  appended to the target's ABI in `abi` itself - which is why the block above reads
  `ReleaseBlocked(...)` with its arguments instead of raw hex.
  `scripts/errorabis_probe.py` measures both halves against the live API and writes
  `artifacts/errorabis-probe.json`. "The fix merged" and "the fix answers" are different
  claims, and only the second one is worth writing down.

## 10. Engineering decisions and the hard problems

**The predicate lives on-chain and is called off-chain.** The alternative, a service that decides and a contract that enforces, guarantees eventual drift. Calling the contract from the service removes the class of bug rather than testing for it.

**The hook, not a policy check.** A release gate that lives in the service would be advisory: anyone holding the evaluator key could bypass it. Attaching the gate to the job's hook makes the bypass require changing the contract.

**Staleness cannot be written, only waited for.** An early version of the service's "expire the evidence" control committed a new version with an already-lapsed window. That is a supersession, and clause 4 fires before clause 5, so it reported `E_SUPERSEDED`. Real staleness means the *approved* evidence's own window lapsing, so the control now observes a one second window, approves it, and lets it lapse.

**Known limits of the hooks.** `maxSkew` and `isReleasableAt` are implemented so an off-chain decision taken far from chain time can be refused, and nothing calls them yet. Named here rather than left as a claim.

## 11. Real vs pending

| | status |
|---|---|
| ERC-8183 core, registry and gate deployed on Base Sepolia | real, addresses above |
| 10 contract tests against the real core | real, output in section 12 |
| release blocked, then released, then blocked again | real, tx hashes in section 4 |
| all four invalidation classes refused on live chain | real, `artifacts/invalidation-classes.json` |
| KeeperHub executes the release, with its own simulation reporting the refusal | real, execution `qyn8k10j5mv6529c5cjtu` |
| evaluator service: six reads and eight signing actions over the deployed contracts | real, `service/duality_service.py`, `tests/test_http_surface.py` |
| a test suite for the service | real, 30 tests: `tests/` |
| `isReleasableAt` / `maxSkew` wired into the decision path | real: the service decides through `isReleasableAt` and reports the skew it measured; a clock further than the declared bound is refused with `E_CONDITION` |
| landing page and control surface, reading only from those endpoints | real, `service/web/` |
| a public deployment, reading the chain from the browser | real, https://duality-lilac.vercel.app |
| an ACP-registered agent paid by the escrow | real: job 31, `payoutReceiver` is the agent's own wallet, `artifacts/acp-provider-job.json` |
| the merged `errorAbis` field, consumed | attached on every release call; **the hosted API accepts and ignores it**, measured by `scripts/errorabis_probe.py` - the refusal is decoded by way of the gate's errors in `abi` instead |
| the binding between the job and that agent | real: the evidence `subject` and `provenanceHash` commit two published files, and `scripts/acp_provider_job.py --verify` re-derives both against the registry |
| clause 8 of the predicate (provenance) | **not enforced**: the hash is stored so the envelope stays auditable, but no branch reads it. The doc comment used to claim it; `docs/LIMITATIONS.md` records the gap |
| the agent signing its own submission | **not possible today**: the registry issues no key this repository can use, so an operator submits and the agent is paid (section 9) |
| a completion from the agent's own inference endpoint | **not exercised**: it answered HTTP 402 insufficient credits when this lane was built, so nothing here claims one |
| the job's ERC-8004 agent-id slot | **0**: the registry issues an opaque id rather than a token id, so the identity is committed in the evidence envelope instead |
| source verification on Basescan | **pending** |
| mainnet | **not attempted**, chain 84532 only |

## 12. Tests

```bash
cd contracts && forge test --match-path 'test/DualityGate.t.sol'
```

```text
Ran 10 tests for test/DualityGate.t.sol:DualityGateTest
[PASS] test_boundaryIsInclusive()                  gas: 770513
[PASS] test_disqualified_blocksRelease()           gas: 730321
[PASS] test_gateCannotBlockRefund()                gas: 707373
[PASS] test_invalidated_evidence_blocksRelease()   gas: 738016
[PASS] test_qualificationAtObservation_blocksRelease() gas: 714174
[PASS] test_releaseCannotHappenTwice()             gas: 767259
[PASS] test_stale_blocksRelease()                  gas: 731290
[PASS] test_superseded_blocksRelease()             gas: 956320
[PASS] test_unapprovedJob_cannotRelease()          gas: 386081
[PASS] test_validRelease_succeeds()                gas: 773949
Suite result: ok. 10 passed; 0 failed; 0 skipped
```

These run against the upstream ERC-8183 core through a UUPS proxy with a whitelisted hook and a real mock USDC, not against a stub. `test_gateCannotBlockRefund` is the one that matters most: it asserts the gate cannot trap a client's funds.

The service has its own suite, and it reads the deployment rather than a mock:

```bash
.venv/bin/python -m pytest
```

```text
30 passed in 79.74s
```

| file | tests | what it holds |
|---|---|---|
| `tests/test_predicate_live.py` | 11 | the predicate answers as documented for settled, unapproved and recorded-invalidation jobs; the clock bound fires one second past `maxSkew` and not at it |
| `tests/test_http_surface.py` | 9 | the reads, the decision on `/jobs/{id}`, and the asset allowlist (the traversal requests are sent by hand, because `urllib` normalises `/../` away and would never exercise the guard) |
| `tests/test_reason_codes.py` | 3 | every reason constant is read off the deployed registry and must have wording and a decision in the service, so a code added on-chain fails the suite until it is named |
| `tests/test_acp_binding.py` | 7 | the published ACP artifacts re-derive to the values the registry holds, and the escrow paid the wallet the binding names |

Two of these found real defects while being written: `E_HASH_MISMATCH` and `E_INVALIDATED` were both returnable by the registry and neither was named in the service, so either would have reached a reader as "unrecognised reason code". Both now have wording and a decision.

## 13. The web surfaces

Both are served by the same process, read only from the endpoints in section 5,
and have no build step, no bundler and no mock data.

| route | file | what it is |
|---|---|---|
| `/` | `service/web/index.html` | the landing page |
| `/dashboard` | `service/web/dashboard.html` | the control surface: select a job, read the approval in the left half and the live predicate in the right half, then act |
| `/duality.css` | `service/web/duality.css` | the design system both pages share, including the self-hosted Geist faces |

The dashboard's counters are folded from `artifacts/events.jsonl` rather than from
process memory, so they survive a restart and agree with the audit log. Web assets
are served through an allowlist rather than a path join, so a crafted path cannot
leave the directory.

### Two deployment modes, one build

The same files serve both. On load the dashboard asks for `/health`; if a service
answers with JSON it uses it, and if not it treats itself as a static build and
reads the contracts from the browser through `service/web/chain.js`.

| | served by | can it act |
|---|---|---|
| local, `service/duality_service.py` | the service | yes, all eight actions |
| static, `https://duality-lilac.vercel.app` | Vercel | `check` only |

The static build holds no key, so it cannot sign. It says so on the page rather
than offering buttons that fail, and the other seven actions are locked. `check`
still re-reads the predicate live, which is the operation the product is about.
Reads go through `ethers.FallbackProvider` across four public endpoints with a
retry, because a public RPC throttled mid-burst returns a failure that looks
exactly like a contract revert: both are `CALL_EXCEPTION`, and the throttled one
simply carries no revert data. The client distinguishes them and says which
happened instead of blaming the contract.

## 14. Run locally

```bash
git clone https://github.com/subheeksh5599/duality && cd duality

# contracts: lib/ is gitignored, so install the two upstream dependencies first
cd contracts
forge install erc-8183/base-contracts --no-git
forge install OpenZeppelin/openzeppelin-contracts --no-git
forge build && forge test

# python side
cd ..
python3 -m venv .venv && .venv/bin/pip install web3 pytest

# configuration comes from the environment, never from a path in this repo
cp .env.example .env && $EDITOR .env
export DUALITY_ENV=.env

# the proofs
.venv/bin/python scripts/prove_invalidation_classes.py
.venv/bin/python scripts/keeperhub_release.py
.venv/bin/python scripts/onchain_e2e.py

# what the hosted execution API does with the merged errorAbis field
.venv/bin/python scripts/errorabis_probe.py

# the ACP lane: a job whose escrow pays an agent from the ACP registry
.venv/bin/python scripts/acp_provider_job.py
.venv/bin/python scripts/acp_provider_job.py --verify   # re-derive the binding against the chain
.venv/bin/python scripts/acp_provider_job.py --resume <jobId>   # a lane that stopped mid-flight

# the service suite
.venv/bin/python -m pytest

# before driving the control surface, open a job for it to act on, so a recording
# or a walkthrough needs no shell: it prints the job id and the verdict it starts in
.venv/bin/python scripts/demo_prepare.py

# the service and its control surface
.venv/bin/python service/duality_service.py --port 8787
#   http://127.0.0.1:8787/           the landing page
#   http://127.0.0.1:8787/dashboard  the control surface, which drives the actions below
```

## 15. Configuration

Every script reads `$DUALITY_ENV`, else `./.env`, else the process environment. Nothing reads a path outside the project.

| variable | purpose |
|---|---|
| `RPC_URL` | EVM JSON-RPC endpoint for the target network |
| `ADDRESS`, `PRIVATE_KEY` | admin, qualifier and committer on the deployment |
| `BUYER`, `BUYER_KEY` | the client that funds escrow |
| `PROVIDER`, `PROVIDER_KEY` | the provider that prices, delivers and is paid |
| `JUDGE`, `JUDGE_KEY` | the evaluator, used by the direct scripts |
| `KH_API_KEY` | KeeperHub direct-execution key, taken from the environment |
| `ACP_AGENT_ID` | the registered agent the ACP lane pays. Required by that lane and nothing else |
| `ACP_AGENT_WALLET` | that agent's wallet; the lane points the escrow's `payoutReceiver` at it |
| `ACP_AGENT_REGISTRY` | where the registration resolves, recorded in the binding artifact |
| `ACP_AGENT_TOKEN_ID` | optional, if the registry issues a numeric agent id rather than an opaque one |
| `ACP_JOB_USDC` | optional escrow size for the ACP lane, 1 by default |

Three distinct addresses are required: the core rejects a job whose client, provider and evaluator are not distinct. No key is in this repository.

## 16. Deploy

```bash
cd contracts
export DEPLOYER_KEY=... ADMIN_ADDRESS=... TREASURY_ADDRESS=...
export COMMITTER_ADDRESS=... QUALIFIER_ADDRESS=...
forge script script/Deploy.s.sol:Deploy --rpc-url $RPC_URL --broadcast -vv
```

The script whitelists the hook and allowlists the payment token in the same run, because `createJob` refuses a job whose hook is not whitelisted.

## 17. Project layout

```text
contracts/
  src/EvidenceRegistry.sol        the predicate, and the evidence it reads
  src/DualityGateHook.sol         the veto attached to each job
  src/vendor/                     the upstream hook interfaces, unmodified
  test/DualityGate.t.sol          10 tests against the real core
  script/Deploy.s.sol             deploys and wires the stack
service/
  duality_service.py              the reads and the eight signing actions
  web/index.html                  the public page, served at /
  web/dashboard.html              the control surface, served at /dashboard
  web/duality.css                 the design system both pages share
  web/dashboard.js                one build, two modes: service or chain
  web/chain.js                    browser reads, for the static deployment
  web/abis.json                   the six functions the client calls
  web/chain-config.json           addresses, chain id, RPC endpoints
  web/audit-log.json              the recorded trail, for the static build
  web/vendor/ethers.umd.min.js    vendored, so the page needs no CDN
  web/fonts/                      Geist and Geist Mono, self-hosted, OFL
scripts/
  prove_invalidation_classes.py   all four classes, live
  keeperhub_release.py            the release, executed by KeeperHub
  onchain_e2e.py                  the full sequence end to end
  acp_provider_job.py             the ACP lane, and its --verify re-derivation
  demo_prepare.py                 opens a job in the state the control surface expects
  errorabis_probe.py              measures whether the hosted API reads errorAbis
tests/
  test_predicate_live.py          the predicate, read from the deployment
  test_http_surface.py            the routes, the decisions and the asset allowlist
  test_reason_codes.py            every registry reason code must be named by the service
  test_acp_binding.py             the ACP artifacts re-derived against the chain
docs/
  PROTOCOL-SPEC.md                definitions, predicate, state machine, trust
  ARCHITECTURE.md                 components, data flow, clause ordering
  PRIOR-ART.md                    what exists, and the exact boundary
  LIMITATIONS.md                  limits, trust model, threat model
artifacts/                        run records: transactions, refusals, audit log
  acp-agent-binding.json          the counterparty the ACP lane binds, and its registry id
  acp-provider-job.json           that lane's run, its envelope and the chain readback
deployments/base-sepolia.json     the live deployment
```

## 18. Tech stack

| layer | |
|---|---|
| contracts | Solidity 0.8.28, Foundry, upstream ERC-8183 and OpenZeppelin |
| service | Python 3, stdlib HTTP server, web3.py |
| control surface | hand-written HTML and JS, no build step, served by the service |
| network | Base Sepolia (84532), USDC escrow |
| execution rail | KeeperHub direct execution API |

## 19. Prior art

Escrow, evaluator agents, disputes, attestations, freshness gates for trading, capability revocation and job expiry all exist, and `docs/PRIOR-ART.md` credits each by name.

What was not found, after searching, is a system that refuses to release funds because the evidence behind an approval has expired, been superseded, or lost its qualification between approval and payment. That interval is what this project is.

## 20. Roadmap

- a scheduler, so a lapsed window holds without being asked
- enforce clause 8 (provenance) in the predicate rather than recording the commitment
  unread, which needs a registry redeploy because the deployed one is not a proxy
- source verification on Basescan, which needs an explorer API key this repository does
  not hold
- generalise the evidence adapter so a second job class needs no contract change
- give the ACP lane a signing counterparty, so the agent submits for itself instead of
  an operator submitting and the agent being paid

## License

MIT. The files under `contracts/src/vendor/` are the upstream ERC-8183 hook interfaces, unmodified and under their own MIT licence.