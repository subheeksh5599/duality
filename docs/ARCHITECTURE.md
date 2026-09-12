# Architecture

## The one idea

An approval is a statement about a moment. Money moves later. Everything here
exists to make the gap between those two moments checkable.

## Components

```
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

## Why the predicate lives on-chain and is called off-chain

The service does not reimplement the release rule. It calls
`EvidenceRegistry.isReleasable(jobId, now)` over `eth_call`, which is the same
function the hook runs inside `complete()`. Two consequences:

- the service's decision and the chain's enforcement cannot disagree, because
  they are one function;
- a judge can reproduce either one without trusting the other.

## Evidence lifecycle

```
observe ---------> commit(version n)                 append-only, version = current + 1
evaluate --------> approve(evidenceId)               binds one exact version
mutate ----------> commit(version n+1)               the old record is untouched
                   or setQualification(REVOKED)
release ---------> complete() -> hook -> predicate   RELEASE | HOLD
reconcile -------> observe + approve, then the same predicate again
```

No code path rewrites a stored evidence record. `invalidate` sets status and
reason once and refuses a second call. This is what makes lineage auditable: the
approval still points at version 1 after version 2 exists, which is why the
refusal is `E_SUPERSEDED` rather than silence.

## Clause order is part of the semantics

The predicate checks clauses in a fixed order, and the first failure is the
reason you see. A job that is both superseded and disqualified reports
`E_SUPERSEDED`, because clause 4 precedes clause 7. Observed, not theorised: the
control surface's three mutation buttons each produce their class only when the
conditions before them are clear.

## The service

`service/duality_service.py` is a real HTTP service on the deployed contracts.

| endpoint | effect |
|---|---|
| `GET /health` | counters and chain identity |
| `GET /jobs`, `GET /jobs/{id}` | job, evidence window, the live predicate verdict |
| `GET /events` | the append-only audit log, each entry referencing its predecessor |
| `POST /jobs/{id}/check` | run the predicate, write a decision record |
| `POST /jobs/{id}/observe`, `/approve` | commit a version, bind an approval |
| `POST /jobs/{id}/invalidate{,-stale,-disqualify}` | the mutation control, one class each |
| `POST /jobs/{id}/reconcile` | re-observe the minimum fact and re-run the predicate |
| `POST /jobs/{id}/release` | simulate through KeeperHub, broadcast only if the gate allows |

`service/ui.html` is served by the same process at `/`. It reads only from these
endpoints, so every number and state it shows comes from the chain. It has no
build step and no mock data.

## Audit log

`artifacts/events.jsonl`, one JSON object per line, never rewritten. Each record
carries an id, a timestamp, a correlation id, a `prev` pointing at the previous
record's id, and the fields of the event. That is a hash-free chain, enough to
prove ordering and to reconstruct a settlement end to end.

## Trust boundaries

- the gate is enforced by the chain, so the evaluator cannot bypass it;
- refunds are enforced by the core's own path, which is deliberately not
  hookable, so the gate can never trap funds;
- KeeperHub executes and cannot make the gate pass;
- the committer, qualifier and admin roles are named in LIMITATIONS, and in this
  deployment they are one key, which is a testnet decision and not a design claim.
