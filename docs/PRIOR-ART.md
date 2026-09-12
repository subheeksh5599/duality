# Prior art, and the exact boundary

Honest comparison. The claim is narrow on purpose: the release-time validity
boundary. Everything else on this page is credited to whoever built it first.

| System | What it does | DUALITY's delta | Not claimed |
|---|---|---|---|
| **ERC-8183 / ACP evaluator + escrow** | three roles, escrow, evaluator attestation, `expiredAt` -> `claimRefund` | ERC-8183 approves once and expires the JOB. It has no concept of an approval becoming invalid while still inside the job deadline. DUALITY gates the release call on evidence validity. | escrow, evaluator role, job lifecycle, expiry, refund |
| **ERC-8183 `expiredAt`** | job deadline refunds the client | time expires the job; DUALITY expires the EVIDENCE. Different clock, different object, different failure. | job-level expiry |
| **UMA optimistic oracle** | bonded claim/assertion with dispute window | UMA decides whether a claim is TRUE. DUALITY decides whether an approved deliverable is still the thing that may be paid. Deterministic, no bond, no dispute round. | dispute resolution, bonds, oracle design |
| **Kleros** | decentralized arbitration | adjudicates disputes between parties | arbitration, juries |
| **reqkeeper** | invoice-bound settlement; byte-compares calldata; refuses replay | binds payment to an identity and refuses replay. Does not re-evaluate validity at release. | replay refusal, calldata binding |
| **lucid-settlement** | x402 success does not authorize payout | separates payment receipt from payout authority. DUALITY separates evaluation from release authority. | receipt/payout separation |
| **EQLTY (ETHGlobal Lisbon)** | freshness and provenance as blocking gates for trading | applies freshness to a trade decision at decision time. DUALITY applies it to settlement at release time. | freshness as a correctness gate |
| **ColdProof (Lisbon)** | physical evidence + immutable logistics record | settles from an immutable log; DUALITY settles only if the log entry is still current | immutable logs, physical evidence |
| **Zodiac Roles Modifier** | per-action authorization with typed conditions, live in production | authorization conditions evaluated at action time, static between actions. No evidence version, no freshness bound, no supersession. | live conditional authorization |
| **ERC-7710 caveats** | delegation with scoped caveat enforcers | caps and scope the delegation. No notion of the evidence behind the action going stale. | delegated authority, caveats |
| **Lit Protocol** | signing gated on live HTTP + TEE | gates a signature on a live fact. DUALITY gates a settlement on the validity of the evidence that justified the approval. | live-condition signing |
| **squidlor/virtuals-acp** | sells freshness attestation as a service | reports freshness. DUALITY refuses to move money when it fails. Reporting is not enforcement. | freshness attestation as a service |
| **clawplaza/erc8183-reference** | production ERC-8183, 20k+ agents, Evaluator guide | production evaluator patterns. Still approve-once. | evaluator design patterns, production escrow |
| **AWS IAM / Google Zanzibar** | policy engines, relationship authorization | evaluate at request time against policy state. No evidence object, no observation-time qualification. | policy evaluation |

## The one sentence

No prior system was found that refuses to release funds because the evidence
behind an approval has expired, been superseded, or lost its qualification
between approval and payment. DUALITY makes that its entire product.
