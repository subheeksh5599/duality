// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title DUALITY EvidenceRegistry
/// @notice The one authoritative implementation of the release predicate.
///
///         It is written once, in Solidity, and read from exactly two places:
///           - the gate hook, at release time, where a false result reverts
///           - the evaluator service, off-chain, via eth_call, for the same answer
///         Because the off-chain decision IS this function, the two cannot
///         disagree. There is no second implementation to drift.
///
///         ERC-8183 proves the work at evaluation time. This contract holds the
///         commitments that make an approval checkable at release time.
contract EvidenceRegistry {
    enum EvidenceStatus { CURRENT, STALE, SUPERSEDED, DISQUALIFIED, DISPUTED, UNRECOVERABLE }
    enum QualStatus     { UNKNOWN, QUALIFIED, PROBATION, REVOKED }

    struct Evidence {
        bytes32 evidenceId;                  // keccak(jobId, provider, contentHash, version)
        uint256 jobId;
        bytes32 evaluationId;                // the ACP evaluation this was approved under
        bytes32 subject;                     // what the evidence is about
        bytes32 evidenceType;                // e.g. keccak256("quote")
        bytes32 contentHash;                 // SHA-256 of the deliverable bytes
        bytes32 provenanceHash;              // hash of the provenance envelope
        uint64  version;                     // monotonic per (jobId, subject)
        uint64  observedAt;                  // unix seconds, the freshness clock origin
        uint64  freshnessBound;              // validity window length in seconds
        address provider;
        QualStatus qualificationAtObservation;
        uint64  qualificationRevision;
        EvidenceStatus status;
        bytes32 invalidationReason;          // set at most once
    }

    struct Approval {
        bytes32 evidenceId;
        bytes32 evaluationId;
        bool    exists;
    }

    struct Qualification {
        QualStatus status;
        uint64     revision;
        uint64     updatedAt;
    }

    // ---- reason codes (short literals so they read in logs and explorers) ----
    bytes32 public constant OK                = bytes32(0);
    bytes32 public constant E_NOT_APPROVED    = "E_NOT_APPROVED";
    bytes32 public constant E_STALE           = "E_STALE";
    bytes32 public constant E_SUPERSEDED      = "E_SUPERSEDED";
