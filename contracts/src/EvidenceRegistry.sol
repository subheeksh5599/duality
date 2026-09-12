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
    bytes32 public constant E_DISQUALIFIED    = "E_DISQUALIFIED";
    bytes32 public constant E_QUAL_OBSERVATION= "E_QUAL_OBSERVATION";
    bytes32 public constant E_PROVENANCE      = "E_PROVENANCE";
    bytes32 public constant E_SUBJECT         = "E_SUBJECT";
    bytes32 public constant E_CONDITION       = "E_CONDITION";
    bytes32 public constant E_ALREADY_SETTLED = "E_ALREADY_SETTLED";
    bytes32 public constant E_HASH_MISMATCH   = "E_HASH_MISMATCH";
    bytes32 public constant E_INVALIDATED     = "E_INVALIDATED";

    address public committer;    // the evaluator service key
    address public qualifier;    // qualification controller
    address public gateHook;     // the release gate permitted to record settlement
    uint64  public maxSkew;      // toleranted |evaluatorNow - block.timestamp|

    mapping(bytes32 => Evidence) private _evidence;
    mapping(uint256 => mapping(bytes32 => uint64))  public currentVersion;   // jobId,subject -> version
    mapping(uint256 => mapping(bytes32 => mapping(uint64 => bytes32))) public versionEvidence;
    mapping(uint256 => Approval) public approval;
    mapping(address => Qualification) public qualification;
    mapping(uint256 => bool) public settled;

    event EvidenceCommitted(bytes32 indexed evidenceId, uint256 indexed jobId, address indexed provider,
                            uint64 version, uint64 observedAt, uint64 freshnessBound,
                            QualStatus qualificationAtObservation, bytes32 contentHash);
    event EvidenceApproved(uint256 indexed jobId, bytes32 indexed evidenceId, bytes32 evaluationId);
    event EvidenceInvalidated(bytes32 indexed evidenceId, EvidenceStatus status, bytes32 reason);
    event QualificationSet(address indexed provider, QualStatus status, uint64 revision);
    event ReleaseChecked(uint256 indexed jobId, bytes32 indexed evidenceId, bool ok, bytes32 reason, uint64 at);
    event Settled(uint256 indexed jobId, bytes32 indexed evidenceId, uint64 at);

    error NotCommitter();
    error NotQualifier();
    error BadVersion();
    error UnknownEvidence();
    error JobMismatch();
    error AlreadyInvalidated();
    error AlreadySettled();

    modifier onlyCommitter() { if (msg.sender != committer) revert NotCommitter(); _; }
    modifier onlyCommitterOrHook() { if (msg.sender != committer && msg.sender != gateHook) revert NotCommitter(); _; }
    modifier onlyQualifier() { if (msg.sender != qualifier) revert NotQualifier(); _; }

    constructor(address committer_, address qualifier_, uint64 maxSkew_) {
        committer = committer_;
        qualifier = qualifier_;
        maxSkew = maxSkew_;
    }

    // ------------------------------------------------------------------ writes

    /// @notice Append a new evidence observation. Never rewrites history: a new
    ///         observation is a new version, and the prior version stays recorded.
    function commit(Evidence calldata e) external onlyCommitter {
        if (e.evidenceId != evidenceIdFor(e.jobId, e.provider, e.contentHash, e.version)) revert JobMismatch();
        if (e.version != currentVersion[e.jobId][e.subject] + 1) revert BadVersion();
        _evidence[e.evidenceId] = e;
        _evidence[e.evidenceId].status = EvidenceStatus.CURRENT;
        _evidence[e.evidenceId].invalidationReason = OK;
        currentVersion[e.jobId][e.subject] = e.version;
        versionEvidence[e.jobId][e.subject][e.version] = e.evidenceId;
        emit EvidenceCommitted(e.evidenceId, e.jobId, e.provider, e.version, e.observedAt,
                               e.freshnessBound, e.qualificationAtObservation, e.contentHash);
    }

    /// @notice Bind an approval to one exact evidence version. Clause 3 of the
    ///         predicate reads this, so a stale approval cannot drift to newer evidence.
    function approve(uint256 jobId, bytes32 evidenceId, bytes32 evaluationId) external onlyCommitter {
        Evidence storage e = _evidence[evidenceId];
        if (e.evidenceId == bytes32(0)) revert UnknownEvidence();
        if (e.jobId != jobId) revert JobMismatch();
        approval[jobId] = Approval(evidenceId, evaluationId, true);
        emit EvidenceApproved(jobId, evidenceId, evaluationId);
    }

    /// @notice One-way invalidation. Sets status and reason once, then refuses.
    function invalidate(bytes32 evidenceId, EvidenceStatus status, bytes32 reason) external onlyCommitter {
        Evidence storage e = _evidence[evidenceId];
        if (e.evidenceId == bytes32(0)) revert UnknownEvidence();
        if (e.status != EvidenceStatus.CURRENT) revert AlreadyInvalidated();
        e.status = status;
        e.invalidationReason = reason;
        emit EvidenceInvalidated(evidenceId, status, reason);
    }

    function setQualification(address provider, QualStatus status) external onlyQualifier {
        Qualification storage q = qualification[provider];
        q.status = status;
        q.revision += 1;
        q.updatedAt = uint64(block.timestamp);
        emit QualificationSet(provider, status, q.revision);
    }

    function setMaxSkew(uint64 v) external onlyQualifier { maxSkew = v; }

    function setGateHook(address h) external onlyQualifier { gateHook = h; }

    function markSettled(uint256 jobId) external onlyCommitterOrHook {
        if (settled[jobId]) revert AlreadySettled();
        settled[jobId] = true;
        emit Settled(jobId, approval[jobId].evidenceId, uint64(block.timestamp));
    }

    // ------------------------------------------------------------------- reads

    function evidenceIdFor(uint256 jobId, address provider, bytes32 contentHash, uint64 version)
        public pure returns (bytes32)
    { return keccak256(abi.encode(jobId, provider, contentHash, version)); }

    function getEvidence(bytes32 evidenceId) external view returns (Evidence memory) { return _evidence[evidenceId]; }

    /// @notice THE PREDICATE. Clauses 2,3,4,5,6,7,8,10 of the specification.
    ///         Clause 1 (ACP approval) is enforced by ERC-8183 itself: complete()
