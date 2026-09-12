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
**[Evidence](artifacts/)** &nbsp;·&nbsp;
**[Protocol spec](docs/PROTOCOL-SPEC.md)** &nbsp;·&nbsp;
**[Limitations](docs/LIMITATIONS.md)**

</div>

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
- [9. Engineering decisions and the hard problems](#9-engineering-decisions-and-the-hard-problems)
- [10. Real vs pending](#10-real-vs-pending)
- [11. Tests](#11-tests)
- [12. The web surfaces](#12-the-web-surfaces)
- [13. Run locally](#13-run-locally)
- [14. Configuration](#14-configuration)
- [15. Deploy](#15-deploy)
- [16. Project layout](#16-project-layout)
- [17. Tech stack](#17-tech-stack)
- [18. Prior art](#18-prior-art)
- [19. Roadmap](#19-roadmap)
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
   8  provenance hash matches the committed one
   9  job conditions hold                                  external oracle, optional
  10  the job has not already settled
```

Clauses 3 and 4 together are what stop a stale approval from releasing money: an approval binds one evidence id, and that id must still be the current version.

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

This is worth dwelling on: KeeperHub's documented safe-first-write sequence is simulate, check `wouldRevert`, then broadcast. That is the same shape as DUALITY's thesis one layer down, and the gate is what makes `wouldRevert` informative rather than decorative, because the predicate behind it can fail after the approval it was made against.

One gap found while building this is filed upstream as **[KeeperHub#2430](https://github.com/KeeperHub/keeperhub/issues/2430)**: a revert raised inside a callee contract cannot be decoded, because the API accepts a single `abi` field, so a hook's custom error reaches the caller as raw hex.

## 9. Engineering decisions and the hard problems

**The predicate lives on-chain and is called off-chain.** The alternative, a service that decides and a contract that enforces, guarantees eventual drift. Calling the contract from the service removes the class of bug rather than testing for it.

**The hook, not a policy check.** A release gate that lives in the service would be advisory: anyone holding the evaluator key could bypass it. Attaching the gate to the job's hook makes the bypass require changing the contract.

**Staleness cannot be written, only waited for.** An early version of the service's "expire the evidence" control committed a new version with an already-lapsed window. That is a supersession, and clause 4 fires before clause 5, so it reported `E_SUPERSEDED`. Real staleness means the *approved* evidence's own window lapsing, so the control now observes a one second window, approves it, and lets it lapse.

**Known limits of the hooks.** `maxSkew` and `isReleasableAt` are implemented so an off-chain decision taken far from chain time can be refused, and nothing calls them yet. Named here rather than left as a claim.

## 10. Real vs pending

| | status |
|---|---|
| ERC-8183 core, registry and gate deployed on Base Sepolia | real, addresses above |
| 10 contract tests against the real core | real, output in section 11 |
| release blocked, then released, then blocked again | real, tx hashes in section 4 |
| all four invalidation classes refused on live chain | real, `artifacts/invalidation-classes.json` |
| KeeperHub executes the release, with its own simulation reporting the refusal | real, execution `qyn8k10j5mv6529c5cjtu` |
| evaluator service with nine endpoints | real, `service/duality_service.py` |
| landing page and control surface, reading only from those endpoints | real, `service/web/` |
| clause 8 of the predicate (provenance) | **not enforced**: the hash is stored so the envelope stays auditable, but no branch reads it. The doc comment used to claim it; `docs/LIMITATIONS.md` records the gap |
| a live ACP job | **pending**: the jobs are ERC-8183, the standard whose escrow model ACP implements |
| source verification on Basescan | **pending** |
| mainnet | **not attempted**, chain 84532 only |
| a test suite for the service | **pending**, the contract suite is complete |
| `isReleasableAt` / `maxSkew` wired into the decision path | **pending**, see section 9 |

## 11. Tests

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

## 12. The web surfaces

Both are served by the same process, read only from the endpoints in section 5,
and have no build step, no bundler and no mock data.

| route | file | what it is |
|---|---|---|
| `/` | `service/web/landing.html` | the landing page |
| `/dashboard` | `service/web/dashboard.html` | the control surface: select a job, read the approval in the left half and the live predicate in the right half, then act |
| `/duality.css` | `service/web/duality.css` | the design system both pages share, including the self-hosted Geist faces |

The dashboard's counters are folded from `artifacts/events.jsonl` rather than from
process memory, so they survive a restart and agree with the audit log. Web assets
are served through an allowlist rather than a path join, so a crafted path cannot
leave the directory.

## 13. Run locally

```bash
git clone https://github.com/subheeksh5599/duality && cd duality

# contracts: lib/ is gitignored, so install the two upstream dependencies first
cd contracts
forge install erc-8183/base-contracts --no-git
forge install OpenZeppelin/openzeppelin-contracts --no-git
forge build && forge test

# python side
cd ..
python3 -m venv .venv && .venv/bin/pip install web3

# configuration comes from the environment, never from a path in this repo
cp .env.example .env && $EDITOR .env
export DUALITY_ENV=.env

# the proofs
.venv/bin/python scripts/prove_invalidation_classes.py
.venv/bin/python scripts/keeperhub_release.py
.venv/bin/python scripts/onchain_e2e.py

# the service and its control surface
.venv/bin/python service/duality_service.py --port 8787
#   http://127.0.0.1:8787/           the landing page
#   http://127.0.0.1:8787/dashboard  the control surface, which drives the actions below
```

## 14. Configuration

Every script reads `$DUALITY_ENV`, else `./.env`, else the process environment. Nothing reads a path outside the project.

| variable | purpose |
|---|---|
| `RPC_URL` | EVM JSON-RPC endpoint for the target network |
| `ADDRESS`, `PRIVATE_KEY` | admin, qualifier and committer on the deployment |
| `BUYER`, `BUYER_KEY` | the client that funds escrow |
| `PROVIDER`, `PROVIDER_KEY` | the provider that prices, delivers and is paid |
| `JUDGE`, `JUDGE_KEY` | the evaluator, used by the direct scripts |
| `KH_API_KEY` | KeeperHub direct-execution key, taken from the environment |

Three distinct addresses are required: the core rejects a job whose client, provider and evaluator are not distinct. No key is in this repository.

## 15. Deploy

```bash
cd contracts
export DEPLOYER_KEY=... ADMIN_ADDRESS=... TREASURY_ADDRESS=...
export COMMITTER_ADDRESS=... QUALIFIER_ADDRESS=...
forge script script/Deploy.s.sol:Deploy --rpc-url $RPC_URL --broadcast -vv
```

The script whitelists the hook and allowlists the payment token in the same run, because `createJob` refuses a job whose hook is not whitelisted.

## 16. Project layout

```text
contracts/
  src/EvidenceRegistry.sol        the predicate, and the evidence it reads
  src/DualityGateHook.sol         the veto attached to each job
  src/vendor/                     the upstream hook interfaces, unmodified
  test/DualityGate.t.sol          10 tests against the real core
  script/Deploy.s.sol             deploys and wires the stack
service/
  duality_service.py              nine endpoints over the deployed contracts
  web/landing.html                the public page, served at /
  web/dashboard.html              the control surface, served at /dashboard
  web/duality.css                 the design system both pages share
  web/dashboard.js                reads only from the service endpoints
  web/fonts/                      Geist and Geist Mono, self-hosted, OFL
scripts/
  prove_invalidation_classes.py   all four classes, live
  keeperhub_release.py            the release, executed by KeeperHub
  onchain_e2e.py                  the full sequence end to end
docs/
  PROTOCOL-SPEC.md                definitions, predicate, state machine, trust
  ARCHITECTURE.md                 components, data flow, clause ordering
  PRIOR-ART.md                    what exists, and the exact boundary
  LIMITATIONS.md                  limits, trust model, threat model
artifacts/                        run records: transactions, refusals, audit log
deployments/base-sepolia.json     the live deployment
```

## 17. Tech stack

| layer | |
|---|---|
| contracts | Solidity 0.8.28, Foundry, upstream ERC-8183 and OpenZeppelin |
| service | Python 3, stdlib HTTP server, web3.py |
| control surface | hand-written HTML and JS, no build step, served by the service |
| network | Base Sepolia (84532), USDC escrow |
| execution rail | KeeperHub direct execution API |

## 18. Prior art

Escrow, evaluator agents, disputes, attestations, freshness gates for trading, capability revocation and job expiry all exist, and `docs/PRIOR-ART.md` credits each by name.

What was not found, after searching, is a system that refuses to release funds because the evidence behind an approval has expired, been superseded, or lost its qualification between approval and payment. That interval is what this project is.

## 19. Roadmap

- wire `isReleasableAt` and `maxSkew` into the service's decision path, so the skew tolerance is enforced rather than declared
- a scheduler, so a lapsed window holds without being asked
- ACP as a live integration, not only the standard the gate is built on
- source verification and a test suite for the service
- generalise the evidence adapter so a second job class needs no contract change

## License

MIT. The files under `contracts/src/vendor/` are the upstream ERC-8183 hook interfaces, unmodified and under their own MIT licence.
