// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {BaseERC8183Hook} from "./vendor/BaseERC8183Hook.sol";
import {EvidenceRegistry} from "./EvidenceRegistry.sol";

/// @title DUALITY release gate
/// @notice An ERC-8183 hook attached per job at createJob. It sits on the
///         evaluator's complete() call, which is the call that moves the money.
///
///         attached per job            -> not a platform policy, a job's own gate
///         beforeAction may revert     -> the evaluator itself cannot release
///                                        against invalid evidence
///         claimRefund is NOT hookable -> our gate can never block a refund
///
///         "APPROVED" is produced by evaluation. "RELEASABLE" is produced here.
contract DualityGateHook is BaseERC8183Hook {
    EvidenceRegistry public immutable registry;

    /// @dev deliverable hash -> job that claimed it, so one deliverable cannot
    ///      be submitted against two jobs.
    mapping(bytes32 => uint256) public deliverableJob;
    mapping(bytes32 => bool)    public deliverableSeen;

    error ReleaseBlocked(uint256 jobId, bytes32 reason);
    error DeliverableReused(bytes32 deliverable, uint256 otherJob);

    event ReleaseChecked(uint256 indexed jobId, bytes32 indexed evidenceId, bool ok, bytes32 reason, uint64 at);
    event ReleaseAllowed(uint256 indexed jobId, bytes32 indexed evidenceId, uint64 at);
    event DeliverableBound(uint256 indexed jobId, bytes32 deliverable);

    constructor(address erc8183Contract_, address registry_) BaseERC8183Hook(erc8183Contract_) {
        require(registry_ != address(0), "registry required");
        registry = EvidenceRegistry(registry_);
    }

    /// @notice submit(uint256,bytes32,bytes) -> abi.encode(caller, deliverable, optParams)
    function _preSubmit(uint256 jobId, address, bytes32 deliverable, bytes memory) internal override {
        if (deliverableSeen[deliverable] && deliverableJob[deliverable] != jobId) {
            revert DeliverableReused(deliverable, deliverableJob[deliverable]);
        }
        deliverableSeen[deliverable] = true;
        deliverableJob[deliverable] = jobId;
        emit DeliverableBound(jobId, deliverable);
    }

    /// @notice complete(uint256,bytes32,bytes) -> abi.encode(caller, reason, optParams)
    /// @dev THE GATE. Re-reads the predicate at release time and reverts on failure.
    function _preComplete(uint256 jobId, address, bytes32, bytes memory) internal override {
        uint64 nowTs = uint64(block.timestamp);
        bytes32 evidenceId = registry.approvedEvidence(jobId);
        (bool ok, bytes32 reason) = registry.isReleasable(jobId, nowTs);
        emit ReleaseChecked(jobId, evidenceId, ok, reason, nowTs);
        if (!ok) revert ReleaseBlocked(jobId, reason);
        emit ReleaseAllowed(jobId, evidenceId, nowTs);
    }

    /// @notice Runs after the core has paid the provider. Records settlement so a
    ///         second release for the same job is refused by the registry too.
    function _postComplete(uint256 jobId, address, bytes32, bytes memory) internal override {
        registry.markSettled(jobId);
    }
}
