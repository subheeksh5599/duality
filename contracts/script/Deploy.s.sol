// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {Script, console} from "forge-std/Script.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {ERC8183} from "@erc8183/ERC8183.sol";
import {EvidenceRegistry} from "../src/EvidenceRegistry.sol";
import {DualityGateHook} from "../src/DualityGateHook.sol";

/// @title DUALITY deploy, Base Sepolia (84532)
/// @notice Deploys the ERC-8183 core, the evidence registry and the gate hook,
///         then whitelists the hook and allowlists USDC, because the core
///         refuses a job whose hook is not whitelisted.
///
/// Env: DEPLOYER_KEY, ADMIN_ADDRESS, TREASURY_ADDRESS,
///      COMMITTER_ADDRESS, QUALIFIER_ADDRESS, USDC_ADDRESS (optional)
contract Deploy is Script {
    address constant USDC_BASE_SEPOLIA = 0x036CbD53842c5426634e7929541eC2318f3dCF7e;

    function run() external {
        uint256 pk        = vm.envUint("DEPLOYER_KEY");
        address admin     = vm.envAddress("ADMIN_ADDRESS");
        address treasury  = vm.envAddress("TREASURY_ADDRESS");
        address committer = vm.envAddress("COMMITTER_ADDRESS");
        address qualifier = vm.envAddress("QUALIFIER_ADDRESS");
        address usdc      = vm.envOr("USDC_ADDRESS", USDC_BASE_SEPOLIA);

        vm.startBroadcast(pk);

        ERC8183 impl = new ERC8183();
        ERC8183 core = ERC8183(address(new ERC1967Proxy(
            address(impl),
            abi.encodeCall(ERC8183.initialize, (treasury, admin))
        )));

        EvidenceRegistry reg = new EvidenceRegistry(committer, qualifier, 120);
        DualityGateHook hook = new DualityGateHook(address(core), address(reg));

        // wiring the core requires
        reg.setGateHook(address(hook));
        core.setHookWhitelist(address(hook), true);
        core.setPaymentTokenAllowed(usdc, true);
        core.setEvaluatorFee(0);

        vm.stopBroadcast();

        console.log("CHAIN_ID", block.chainid);
        console.log("USDC", usdc);
        console.log("ERC8183_IMPL", address(impl));
        console.log("ERC8183_CORE", address(core));
        console.log("EVIDENCE_REGISTRY", address(reg));
        console.log("DUALITY_GATE_HOOK", address(hook));
        console.log("ADMIN", admin);
        console.log("COMMITTER", committer);
        console.log("QUALIFIER", qualifier);

        // prove the wiring took, in the same run
        require(core.whitelistedHooks(address(hook)), "hook not whitelisted");
        require(reg.gateHook() == address(hook), "gate hook not set on registry");
    }
}
