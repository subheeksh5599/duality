// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {ERC8183} from "@erc8183/ERC8183.sol";
import {MockUSDC} from "@erc8183/mocks/MockUSDC.sol";
import {EvidenceRegistry} from "../src/EvidenceRegistry.sol";
import {DualityGateHook} from "../src/DualityGateHook.sol";

/// @title DUALITY gate, against the real ERC-8183 core
/// @notice Every case here is a sentence from the submission:
///         a valid release succeeds / a stale approval is refused /
///         a superseded approval is refused / a revoked provider is refused /
///         the boundary is inclusive / a release cannot happen twice /
///         and the gate can never block a refund.
contract DualityGateTest is Test {
    ERC8183 core;
    MockUSDC usdc;
    EvidenceRegistry reg;
    DualityGateHook hook;

    address treasury  = makeAddr("treasury");
    address admin     = makeAddr("admin");
    address client    = makeAddr("client");
    address provider  = makeAddr("provider");
    address evaluator = makeAddr("evaluator");

    uint256 constant BUDGET  = 10_000_000; // 10 USDC, 6 decimals
    uint64  constant FRESH   = 60;         // seconds
    bytes32 constant SUBJECT = keccak256("duality/quote/subject");

    function setUp() public {
        ERC8183 impl = new ERC8183();
        core = ERC8183(address(new ERC1967Proxy(
            address(impl),
            abi.encodeCall(ERC8183.initialize, (treasury, admin))
        )));
        usdc = new MockUSDC();

        // committer and qualifier are this test, standing in for the evaluator service
        reg  = new EvidenceRegistry(address(this), address(this), 120);
        hook = new DualityGateHook(address(core), address(reg));
        reg.setGateHook(address(hook));
        reg.setQualification(provider, EvidenceRegistry.QualStatus.QUALIFIED);

        vm.startPrank(admin);
        core.setHookWhitelist(address(hook), true);
        core.setPaymentTokenAllowed(address(usdc), true);
        core.setEvaluatorFee(0);
        vm.stopPrank();

        usdc.mint(client, 1_000_000_000);
        vm.prank(client);
        usdc.approve(address(core), type(uint256).max);
    }

    // ------------------------------------------------------------------ helpers

    function _commit(uint256 jobId, uint64 version, EvidenceRegistry.QualStatus q, uint64 bound)
        internal returns (bytes32 evidenceId)
    {
        bytes32 contentHash = keccak256(abi.encode("content", version));
        evidenceId = reg.evidenceIdFor(jobId, provider, contentHash, version);
        reg.commit(EvidenceRegistry.Evidence({
            evidenceId: evidenceId,
            jobId: jobId,
            evaluationId: keccak256("evaluation-1"),
            subject: SUBJECT,
            evidenceType: keccak256("quote"),
            contentHash: contentHash,
            provenanceHash: keccak256("provenance-1"),
            version: version,
            observedAt: uint64(block.timestamp),
            freshnessBound: bound,
            provider: provider,
            qualificationAtObservation: q,
            qualificationRevision: 1,
            status: EvidenceRegistry.EvidenceStatus.CURRENT,
            invalidationReason: bytes32(0)
        }));
    }

    /// @dev The full real flow: createJob -> setBudget -> fund -> submit -> approve.
    function _openJob() internal returns (uint256 jobId, bytes32 evidenceId) {
        vm.prank(client);
        jobId = core.createJob(
            provider, evaluator, uint48(block.timestamp + 1 hours),
            "time-sensitive quote", address(hook), 1
        );
        vm.prank(provider);
        core.setBudget(jobId, address(usdc), BUDGET, "");
        vm.prank(client);
        core.fund(jobId, address(usdc), BUDGET, "");
        vm.prank(provider);
        core.submit(jobId, keccak256(abi.encode("quote-v1")), "");

        evidenceId = _commit(jobId, 1, EvidenceRegistry.QualStatus.QUALIFIED, FRESH);
        reg.approve(jobId, evidenceId, keccak256("evaluation-1"));
    }

    function _release(uint256 jobId) internal {
        vm.prank(evaluator);
        core.complete(jobId, keccak256("approved"), "");
    }

    // ------------------------------------------------------- the happy path

    function test_validRelease_succeeds() public {
        (uint256 jobId,) = _openJob();
        uint256 before = usdc.balanceOf(provider);

        (bool ok, bytes32 reason) = reg.isReleasable(jobId, uint64(block.timestamp));
        assertTrue(ok, "predicate should pass");
        assertEq(reason, reg.OK());

        _release(jobId);

        assertEq(usdc.balanceOf(provider) - before, BUDGET, "provider paid in full");
        assertEq(uint8(core.getJob(jobId).status), uint8(ERC8183.JobStatus.Completed));
    }

    function test_boundaryIsInclusive() public {
        (uint256 jobId,) = _openJob();
        uint64 observedAt = reg.getEvidence(reg.approvedEvidence(jobId)).observedAt;

        vm.warp(observedAt + FRESH);          // exactly at expiry: still valid
        (bool okAtBoundary,) = reg.isReleasable(jobId, uint64(block.timestamp));
        assertTrue(okAtBoundary, "boundary inclusive");
        _release(jobId);
    }

    // ------------------------------------------------- the three invalidations

    function test_stale_blocksRelease() public {
        (uint256 jobId,) = _openJob();
        uint256 before = usdc.balanceOf(provider);
        uint64 observedAt = reg.getEvidence(reg.approvedEvidence(jobId)).observedAt;

        vm.warp(observedAt + FRESH + 1);      // one second past expiry

        vm.expectRevert(abi.encodeWithSelector(
            DualityGateHook.ReleaseBlocked.selector, jobId, reg.E_STALE()
        ));
        _release(jobId);

        assertEq(usdc.balanceOf(provider), before, "no money moved");
        assertEq(uint8(core.getJob(jobId).status), uint8(ERC8183.JobStatus.Submitted));
    }

    function test_superseded_blocksRelease() public {
        (uint256 jobId,) = _openJob();
        uint256 before = usdc.balanceOf(provider);

        // a newer observation of the same subject lands before release
        _commit(jobId, 2, EvidenceRegistry.QualStatus.QUALIFIED, FRESH);
        assertEq(reg.currentVersion(jobId, SUBJECT), 2);

        vm.expectRevert(abi.encodeWithSelector(
            DualityGateHook.ReleaseBlocked.selector, jobId, reg.E_SUPERSEDED()
        ));
        _release(jobId);
