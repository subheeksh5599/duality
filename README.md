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

**Submission.** Bounty track only, under **Best KeeperHub Feature**: the entry is a
mergeable contribution to the KeeperHub repository, not an integration built around it. The
work submitted is the upstream merges listed in
[section 8](#8-how-it-uses-keeperhub), headed by
[#2457](https://github.com/KeeperHub/keeperhub/pull/2457). The integration in this
repository is what produced that fix, and it is here as the evidence for it. This repository
is not entered in the main track.

**Status.** Live on Base Sepolia: every release and every refusal here is a real transaction
with a hash in [`artifacts/`](artifacts/). The integration exposed a gap in the execution
rail's error decoding - filed, fixed and merged upstream
([#2457](https://github.com/KeeperHub/keeperhub/pull/2457), with
[#2472](https://github.com/KeeperHub/keeperhub/pull/2472) documenting the field). That pair
is part of nineteen merges in September, eleven of them functional. What is not built is in
[Limitations](docs/LIMITATIONS.md) rather than implied away.

---

## Table of contents

[one command](#-see-it-in-one-command) · [the gap](#the-gap-in-the-standard) ·
[1 primitive](#1-the-primitive) · [2 predicate](#2-the-release-predicate) ·
[3 the gate](#3-the-gate-is-not-advisory) · [4 invalidation classes](#4-the-three-invalidation-classes) ·
[5 architecture](#5-architecture) · [6 safety](#6-safety-claim-to-mechanism) · [7 Base](#7-how-it-uses-base) ·
[8 KeeperHub](#8-how-it-uses-keeperhub) · [9 ACP lane](#9-the-acp-lane) ·
[10 decisions](#10-engineering-decisions-and-the-hard-problems) · [11 real vs pending](#11-real-vs-pending) ·
[12 tests](#12-tests) · [13 surfaces](#13-the-web-surfaces) · [14 run](#14-run-locally) ·
[15 config](#15-configuration) · [16 deploy](#16-deploy) · [17 layout](#17-project-layout) ·
[18 stack](#18-tech-stack) · [19 roadmap](#19-roadmap)
---

## ▶ See it in one command

```bash
.venv/bin/python scripts/prove_invalidation_classes.py
```

Real output against the deployed contracts:

```text
[1/4] SUPERSEDED: a newer observation lands after the approval
  createJob / fund 1 USDC / commit v1 / approve v1       25514dc09c0188070f35b47266c00ee28e8f4648b2f8524370bf9ea605659788
                                                   (all four hashes are in artifacts/)
  commit evidence v2 (NOT approved)              OK   53c98da30fa2f21e88cb5fd1ff9f64b07792fca8e894a7dc63c7683b4f24715f
   KeeperHub simulate -> HTTP 400 wouldRevert=True
   reason: ReleaseBlocked(9, 0x455f535550455253454445440000000000000000000000000000000000000000)
   -> recovery: re-approve the current version and release through KeeperHub
  approve v2                                     OK   3beb89c15e2bbea33e761e19dd88818590753f2c8a1458a70c3ac33fec59fabf
   release -> HTTP 202 status=completed usdc 3.60 -> 4.60

RESULT: PASS - every class blocked on live chain, each named by the predicate
(the four classes and their codes are in section 4)
```

The hexadecimal reasons are ASCII: `0x455f5354414c45` is `E_STALE`.

## The gap in the standard

Read from `ERC8183.sol` and the standard's documentation, not assumed.

**ERC-8183 provides** three roles (client, provider, evaluator), escrow, an evaluator
attestation that releases funds, and a job deadline (`expiredAt`) that refunds the client
through `claimRefund` after an `EVALUATION_GRACE_PERIOD` of one hour.

**ERC-8183 does not provide** any concept of an approval becoming invalid while the job is
still inside its deadline. Its model is approve once, then a clock.

An approval is a statement about a moment. Money moves later. Between those two moments the
quote expires, a newer observation of the same subject lands, or a provider's mandate is
revoked. Nothing in the lifecycle notices. That interval is the entire product.

## 1. The primitive

A **release predicate** evaluated at release time, written once in Solidity and read from
exactly two places.

```solidity
// contracts/src/EvidenceRegistry.sol
function isReleasable(uint256 jobId, uint64 nowTs) public view returns (bool ok, bytes32 reason)
```

- the **gate hook** calls it inside `complete()`, where a false result reverts the release
- the **evaluator service** calls the same function over `eth_call` to make its decision

Because the service's decision is the chain's function, the two cannot drift. There is no
second implementation of the rule to keep in sync.

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

Clauses 3 and 4 stop a stale approval from releasing money: an approval binds one evidence id,
and that id must still be the current version. Clause 1 is enforced by ERC-8183's own
`complete()` and clause 9 by an oracle this deployment does not ship, so the clauses the
contract reads are 2, 3, 4, 5, 6, 7 and 10; clause 8 is recorded and not yet enforced, as
section 11 says. Clause order is part of the semantics - the first failure is the reason
reported, which is why a job both superseded and disqualified answers `E_SUPERSEDED`.

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

`complete()` is the evaluator's call, and the evaluator's call is what moves the money. An
ERC-8183 job stores a per-job hook, and the core calls `beforeAction(job.hook, COMPLETE,
data)` **before** it changes the job status and before it pays:

```solidity
_beforeHook(job.hook, jobId, this.complete.selector, data);   // <- the gate
job.status = JobStatus.Completed;                            // and only then the payment
```

A revert inside the hook rolls the release back, so the evaluator cannot release against
invalid evidence, even deliberately - and the gate can never trap funds, because `claimRefund`
is deliberately not hookable, and neither is `expiredAt`.

## 4. The three invalidation classes

Each was induced on live chain, and each refusal was reported by KeeperHub's own simulation.

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
client / provider / evaluator
   |
   v
ERC-8183 core (upstream)       stores a per-job hook; escrow, roles, lifecycle
   | beforeAction(COMPLETE, data)
   v
DualityGateHook                reverts unless the predicate holds; can block a
   | isReleasable(jobId, now)   release, never a refund
   v
EvidenceRegistry               versions, qualification, the predicate itself
   ^
   | the same question, by eth_call
evaluator service              approve/check/reconcile, then simulate -> broadcast
   |
   v
KeeperHub                      signs and submits complete()
```

| component | role | what it can and cannot do |
|---|---|---|
| `EvidenceRegistry` | versions, qualification, the predicate | written only by its committer; never rewrites history |
| `DualityGateHook` | the veto on the release | can block a release; never a refund |
| ERC-8183 core | escrow, roles, lifecycle | upstream reference implementation, unmodified |
| evaluator service | observes, commits, decides, reconciles | its decision is the chain's function, not a parallel one |
| KeeperHub | submits the release transaction | executes; cannot make the gate pass |

Evidence is append-only: a new observation is a new version. After version 2 exists the
approval still points at version 1, which is why the refusal is `E_SUPERSEDED` rather than
silence.

## 6. Safety: claim to mechanism

| claim | mechanism | where it is proven |
|---|---|---|
| the evaluator cannot release invalid evidence | `beforeAction` reverts before the status change and the payment | `DualityGate.t.sol`, live txs |
| the gate cannot trap funds | `claimRefund` is not hookable | `test_gateCannotBlockRefund` |
| money cannot move twice | core status check plus a settlement flag | `test_releaseCannotHappenTwice`, live tx `9afb1dce` |
| an old approval cannot be replayed | clause 4 rejects a newer current version for a bound `evidenceId` | `test_superseded_blocksRelease` |
| one deliverable cannot serve two jobs | the hook binds the deliverable hash to the job at submit | `DeliverableReused`, observed during reruns |
| the service and the chain cannot disagree | the service calls the on-chain predicate by `eth_call` | `service/duality_service.py` |
| a transport failure cannot be read as a verdict | reads rotate across the published endpoints on a 429, a timeout or any transport failure | `scripts/keeperhub_release.py` |
| the surface never shows a verdict the chain has moved past | every JSON read is sent `no-store` | `service/duality_service.py` |
| every state change is recorded | append-only JSONL log, each record referencing its predecessor | `artifacts/events.jsonl` |

## 7. How it uses Base

Base Sepolia, chain `84532`, because the gate is a contract and the escrow is real; USDC
`0x036CbD53842c5426634e7929541eC2318f3dCf7e` is the payment token, allowlisted on the core;
deployment cost `0.0001068 ETH`.

| | address |
|---|---|
| ERC-8183 core (proxy) | [`0x86b951233E131d2dc0931aeB70ebc9Da17e998f6`](https://sepolia.basescan.org/address/0x86b951233E131d2dc0931aeB70ebc9Da17e998f6) |
| EvidenceRegistry | [`0x1EcAbF0650664cB16A416fFd558a4836e0f7bCC3`](https://sepolia.basescan.org/address/0x1EcAbF0650664cB16A416fFd558a4836e0f7bCC3) |
| DualityGateHook | [`0xF5ac445c06b8a7acf94Fc5Aa6C9eeB9A437da438`](https://sepolia.basescan.org/address/0xF5ac445c06b8a7acf94Fc5Aa6C9eeB9A437da438) |

## 8. How it uses KeeperHub

KeeperHub is the execution rail, not a wrapper. The evaluator on the live jobs is
KeeperHub's own wallet, so the release transaction is signed and submitted by KeeperHub
rather than by this repository.

| step | call |
|---|---|
| dry run, and read `wouldRevert` | `POST /api/execute/contract-call` with `simulate: true` |
| broadcast, only if the gate allows | the same call, with an `Idempotency-Key` and no `simulate` |
| poll to terminal, keep the proof | `GET /api/execute/{executionId}/status` |

```text
KeeperHub SIMULATES a stale release   HTTP 400  wouldRevert=true  (signer 0x1776d4d751d97c85845bf54e6ce364cec62d4bbf)
     decoded 0x5192a3c5 = ReleaseBlocked(uint256,bytes32), jobId 7, reason E_STALE
KeeperHub BROADCASTS it               executionId qyn8k10j5mv6529c5cjtu  status completed
     tx 0x6248089e689d4dc65e721cf03399a95eaa991b8797b2ed4d3b992d89eec120e8
```

A refusal arrives in one of two shapes, both recorded with the side that said it:
`release_held via=keeperhub wouldRevert=true`, where the error is the gate's and the rail
names it; or `release_held via=predicate wouldRevert=false railSimulationClean=true` when the
rail's view has not caught up, where nothing is broadcast.

The rail is asked to simulate *before* this service acts on its own verdict: the rail's
answer is the evidence that a refusal is the gate's and not this process's opinion. When its
view lags a write the service can already read, the service refuses anyway and broadcasts
nothing - one clean simulation is not authority to move money, a path on which this log once
recorded a release that never happened. Both rows are in `artifacts/events.jsonl`.

### What the integration produced upstream

Every one below landed in September. They are split, because a reader weighing this work
should not have to count documentation commits as features.

**Eleven functional changes.**

| PR | merged | lines | what it changed |
|---|---|---|---|
| [#2515](https://github.com/KeeperHub/keeperhub/pull/2515) | 2026-09-18 | `+1156/-51` | the editor and manual runs stay off a phone, at every width |
| [#2543](https://github.com/KeeperHub/keeperhub/pull/2543) | 2026-09-18 | `+78/-2` | an operator inside a quoted operand is read as a value, not as syntax |
| [#2477](https://github.com/KeeperHub/keeperhub/pull/2477) | 2026-09-17 | `+20/-3` | a long step label wraps on a phone |
| [#2404](https://github.com/KeeperHub/keeperhub/pull/2404) | 2026-09-16 | `+1176/-12` | declared Manual input is collected before a run |
| [#2302](https://github.com/KeeperHub/keeperhub/pull/2302) | 2026-09-15 | `+632/-102` | monitoring usable on a phone |
| [#2457](https://github.com/KeeperHub/keeperhub/pull/2457) | 2026-09-15 | `+875/-19` | a revert raised in a callee is decoded from extra error ABIs |
| [#2387](https://github.com/KeeperHub/keeperhub/pull/2387) | 2026-09-15 | `+1671/-0` | approvals are checked against the Revoke.cash exploit list |
| [#2386](https://github.com/KeeperHub/keeperhub/pull/2386) | 2026-09-10 | `+152/-33` | a sponsored send is never reported pre-broadcast |
| [#2362](https://github.com/KeeperHub/keeperhub/pull/2362) | 2026-09-09 | `+288/-3` | `requiredPlan` is disclosed on action schemas |
| [#2297](https://github.com/KeeperHub/keeperhub/pull/2297) | 2026-09-09 | `+354/-94` | `kh_` keys accepted on session-only analytics routes |
| [#2300](https://github.com/KeeperHub/keeperhub/pull/2300) | 2026-09-04 | `+886/-22` | a per-chain token bucket replaces a fixed dispatch jitter |

**#2457** came straight out of this integration: it makes a revert raised inside a callee
decodable, which is the failure this project hit first, and its documentation counterpart is
**#2472** below. The rest came from the same reading of the surface.

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

One correction belongs above that split: **#2387** merged first, and a later implementation of
the same issue (#2417) took the registry slot in `plugins/web3/index.ts`, so its step file is in
the tree and tested but unreachable through the registry - listed as a change that merged,
not counted as a live feature.

## 9. The ACP lane

Every other lane here settles against evidence about a **deliverable**; this one settles
against evidence about a **counterparty**. The provider side of the job is an agent the
registry knows, and the escrow pays that agent's own wallet rather than the operator that
submitted on its behalf. It is an existing registration, reused: the identity is read from
configuration, this project registers no agent and holds no platform secret, and the lane
refuses to run without one configured rather than substituting an address of its own.

Live on Base Sepolia, every hash read out of `artifacts/acp-provider-job.json`, which the run
writes itself (so the block is generated, never retyped):

```text
  createJob (evaluator = KeeperHub)   OK   f68c8565f620309a76985d35c1f085261c21808946794258a77567f0f7aba461
   jobId 31, and payoutReceiver is aimed at the agent's own wallet before any funding
  setPayoutReceiver(agent wallet)     OK   f5483a0663f0ebfed8b86cbab54497cf363f9c6c5455ba858c0244b9b2625593
  revoke qualification (refusal beat) OK   1ab5021320b335e9e6c66f5990447245cb001fa2b6d47bf706d0240e6d14f400
   KeeperHub simulate -> HTTP 400 wouldRevert=true
   ReleaseBlocked(31, 0x455f4449535155414c4946494544000000000000000000000000000000000000)
   (ASCII: 0x455f4449535155414c4946494544 is E_DISQUALIFIED)
  reinstate qualification             OK   82535dd2f431d77271b2dff904fc98c513cf372362ba6c8176f4a098b3035a98
   release -> execution hhj5r6n0q113qqtzde8pc, sponsored true, agent USDC 29.06 -> 30.06
```

What the chain holds, so none of it has to be taken on this repository's word: that
`payoutReceiver`, read back from the core in the same artifact; `subject` and `provenanceHash`,
which commit two published files; and `scripts/acp_provider_job.py --verify`, which re-hashes
both and compares them against the deployed registry.

Three things this lane states rather than hides. **The operator submits and the agent is
paid** - the registered agent holds no key this repository can use, and the gate decides
whether the agent may be paid, not who typed the deliverable. **The refusal came from the
counterparty's standing, not the deliverable's freshness** - clauses 6 and 7 are the
qualification pair. And **it is named by KeeperHub only because this service appends the hook's
errors to the target's ABI**: the merged `errorAbis` field
([KeeperHub#2430](https://github.com/KeeperHub/keeperhub/issues/2430)) is accepted and ignored
by the hosted API, which `scripts/errorabis_probe.py` measures.

## 10. Engineering decisions and the hard problems

- **The predicate lives on-chain and is called off-chain.** A service that decides and a
  contract that enforces guarantees eventual drift; calling the contract from the service
  removes the class of bug rather than testing for it.
- **The hook, not a policy check.** A gate in the service would be advisory: anyone holding
  the evaluator key could bypass it. Attaching it to the job's hook makes the bypass require
  changing the contract.
- **Staleness cannot be written, only waited for.** An early "expire the evidence" control
  committed a version with an already-lapsed window, and clause 4 fires before clause 5, so it
  reported `E_SUPERSEDED`. The control now observes a one second window, approves it, and lets
  it lapse.
- **Known limits of the hooks.** `maxSkew` and `isReleasableAt` are implemented so an
  off-chain decision taken far from chain time can be refused.

## 11. Real vs pending

| | status |
|---|---|
| deployment, gate, verdicts, tests, surfaces | **real** - addresses, tx hashes and output above; 10 contract and 30 service tests; https://duality-lilac.vercel.app |
| KeeperHub executes the release, its simulation reporting the refusal | **real**, execution `qyn8k10j5mv6529c5cjtu`, and in the demo |
| `isReleasableAt` / `maxSkew` in the decision path | **real**: a clock past the declared bound is refused with `E_CONDITION` |
| an ACP-registered agent paid by the escrow, and the binding to it | **real**: job 31, `payoutReceiver` is the agent's wallet, and `--verify` re-derives both commitments |
| the merged `errorAbis` field, consumed | attached on every call; **the hosted API accepts and ignores it**, so the refusal is decoded via the gate's errors in `abi` |
| clause 8 of the predicate (provenance) | **not enforced**: the hash is stored so the envelope stays auditable, but no branch reads it |
| the agent signing for itself, or its own inference endpoint completing a job | **not possible / not exercised**: the registry issues no key this repository can use, and that endpoint answered HTTP 402 when the lane was built |
| the job's ERC-8004 agent-id slot | **0**: the registry issues an opaque id, so the identity is committed in the evidence envelope instead |
| source verification on Basescan, and mainnet | **pending** / **not attempted**, chain 84532 only |

## 12. Tests
```bash
cd contracts && forge test --match-path 'test/DualityGate.t.sol'
```

```text
Ran 10 tests for test/DualityGate.t.sol:DualityGateTest
Suite result: ok. 10 passed; 0 failed; 0 skipped
```

They cover the boundary being inclusive, each of the four invalidation classes, a release that
cannot happen twice, an unapproved job, and `test_gateCannotBlockRefund` - the one that
asserts the gate cannot trap a client's funds.

These run against the upstream ERC-8183 core through a UUPS proxy with a whitelisted hook and
a real mock USDC, not a stub. `test_gateCannotBlockRefund` matters most: it asserts the gate
cannot trap a client's funds. The service suite reads the deployment rather than a mock -
`.venv/bin/python -m pytest` gives `30 passed in 79.74s`:

| file | tests | what it holds |
|---|---|---|
| `tests/test_predicate_live.py` | 11 | the predicate answers as documented, and the clock bound fires one second past `maxSkew`, not at it |
| `tests/test_http_surface.py` | 9 | the reads, the decision on `/jobs/{id}`, the asset allowlist (traversal requests sent by hand: `urllib` normalises `/../` away) |
| `tests/test_reason_codes.py` | 3 | every registry reason constant must have wording and a decision in the service |
| `tests/test_acp_binding.py` | 7 | the ACP artifacts re-derive to the registry's values, and the escrow paid the wallet the binding names |

Two found real defects while being written: `E_HASH_MISMATCH` and `E_INVALIDATED` were both
returnable by the registry and neither was named in the service, so either would have reached a
reader as "unrecognised reason code".

## 13. The web surfaces
Both are served by the same process, read only from the endpoints in section 5, and have no
build step, no bundler and no mock data.

| route | file | what it is |
|---|---|---|
| `/` | `service/web/index.html` | the landing page |
| `/dashboard` | `service/web/dashboard.html` | the control surface: pick a job, read the approval and the live predicate, then act |
| `/duality.css` | `service/web/duality.css` | the design system both pages share, with the self-hosted Geist faces |

Counters are folded from `artifacts/events.jsonl` rather than process memory, so they survive a
restart and agree with the audit log, and assets are served through an allowlist rather than a
path join, so a crafted path cannot leave the directory.

### Two deployment modes, one build

On load the dashboard asks for `/health`: answered with JSON, the service is driving and all
eight actions are live; unanswered, it is the static build and only `check` is offered.

| | served by | can it act |
|---|---|---|
| local, `service/duality_service.py` | the service | yes, all eight actions |
| static, `https://duality-lilac.vercel.app` | Vercel | `check` only |

The static build holds no key, so it cannot sign, and it says so rather than offering buttons
that fail. Reads go through `ethers.FallbackProvider` across four public endpoints with a
retry, because a public RPC throttled mid-burst returns a failure that looks exactly like a
contract revert: both are `CALL_EXCEPTION`, and the throttled one carries no revert data.

## 14. Run locally

```bash
git clone https://github.com/subheeksh5599/duality && cd duality

# contracts: lib/ is gitignored, so install the two upstream dependencies first
cd contracts && forge install erc-8183/base-contracts --no-git
forge install OpenZeppelin/openzeppelin-contracts --no-git && forge build && forge test

cd .. && python3 -m venv .venv && .venv/bin/pip install web3 pytest
cp .env.example .env && $EDITOR .env && export DUALITY_ENV=.env

# the proofs (errorabis_probe measures what the hosted API does with the merged field)
.venv/bin/python scripts/prove_invalidation_classes.py
.venv/bin/python scripts/keeperhub_release.py
.venv/bin/python scripts/onchain_e2e.py
.venv/bin/python scripts/errorabis_probe.py

# the ACP lane: a job whose escrow pays an agent from the registry
.venv/bin/python scripts/acp_provider_job.py            # --verify re-derives the binding
                                                        # --resume <jobId> continues one

# the service suite, then a job for the control surface to act on
.venv/bin/python -m pytest && .venv/bin/python scripts/demo_prepare.py

# the service and its control surface, at / and /dashboard
.venv/bin/python service/duality_service.py --port 8787
```

## 15. Configuration

Every script reads `$DUALITY_ENV`, else `./.env`, else the process environment; nothing reads a
path outside the project.

| variable | purpose |
|---|---|
| `RPC_URL` | EVM JSON-RPC endpoint for the target network |
| `ADDRESS`, `PRIVATE_KEY` | admin, qualifier and committer on the deployment |
| `BUYER`/`BUYER_KEY`, `PROVIDER`/`PROVIDER_KEY`, `JUDGE`/`JUDGE_KEY` | the client that funds escrow, the provider that delivers and is paid, and the evaluator the direct scripts use |
| `KH_API_KEY` | KeeperHub direct-execution key, taken from the environment |
| `ACP_AGENT_ID`, `ACP_AGENT_WALLET`, `ACP_AGENT_REGISTRY` | the registered agent the ACP lane pays, its wallet (which the escrow's `payoutReceiver` is pointed at) and where the registration resolves; required by that lane and nothing else |
| `ACP_AGENT_TOKEN_ID`, `ACP_JOB_USDC` | optional: a numeric agent id if the registry issues one, and the lane's escrow size (1 by default) |

Three distinct addresses are required: the core rejects a job whose client, provider and
evaluator are not distinct. No key is in this repository.

## 16. Deploy

```bash
cd contracts
export DEPLOYER_KEY=... ADMIN_ADDRESS=... TREASURY_ADDRESS=...
export COMMITTER_ADDRESS=... QUALIFIER_ADDRESS=...
forge script script/Deploy.s.sol:Deploy --rpc-url $RPC_URL --broadcast -vv
```

The script whitelists the hook and allowlists the payment token in the same run, because
`createJob` refuses a job whose hook is not whitelisted.

## 17. Project layout

`contracts/` the predicate, the hook, 10 tests, the deploy script · `service/` the service and
`web/` for both surfaces · `scripts/` the live proofs, the ACP lane, the probe · `tests/` 30
tests · `docs/` PROTOCOL-SPEC, ARCHITECTURE, PRIOR-ART, LIMITATIONS · `artifacts/` run records ·
`deployments/base-sepolia.json` the addresses.

## 18. Tech stack

| layer | |
|---|---|
| contracts | Solidity 0.8.28, Foundry, upstream ERC-8183 and OpenZeppelin |
| service | Python 3, stdlib HTTP server, web3.py |
| control surface | hand-written HTML and JS, no build step, served by the service |
| network | Base Sepolia (84532), USDC escrow |
| execution rail | KeeperHub direct execution API |

## 19. Roadmap

- a scheduler, so a lapsed window holds without being asked
- enforce clause 8 (provenance) rather than recording it unread, which needs a registry
  redeploy (the deployed one is not a proxy)
- a signing counterparty for the ACP lane, and source verification on Basescan

## License

MIT. The files under `contracts/src/vendor/` are the upstream ERC-8183 hook interfaces, unmodified
and under their own MIT licence.