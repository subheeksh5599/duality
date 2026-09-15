#!/usr/bin/env python3
"""Open one job for the control surface to act on.

The recorded demo is clicks on a page: nothing in it should need a shell. So the
job the control surface drives is opened here, before recording, and left in the
state the first click expects - funded, submitted, qualification set, and one
observation approved with a day of freshness.

The job pays the ACP agent's wallet when it is configured, so the release the
recording ends on is the same counterparty the ACP lane settles against.

Usage:
    python scripts/demo_prepare.py [env-file]
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import keeperhub_release as K  # noqa: E402
from eth_utils import keccak  # noqa: E402
from web3 import Web3  # noqa: E402

# A day, not an hour: the job is opened before a recording session, and a first click
# that reads E_STALE because the session started late is a trap, not a finding.
FRESHNESS_BOUND = 86400
BUDGET_USDC = 1


def main() -> int:
    env = K.load_env(sys.argv[1] if len(sys.argv) > 1 else None)
    ch = K.Chain(env)

    recipient = (env.get("ACP_AGENT_WALLET") or "").strip()
    agent_id = (env.get("ACP_AGENT_ID") or "").strip()

    print("1. open the job, with KeeperHub's wallet as the evaluator")
    exp = int(time.time()) + 7200
    rc, _ = ch.send(ch.core.functions.createJob(
        ch.addr["provider"], ch.addr["evaluator"], exp,
        "DUALITY: time-sensitive counterparty check", ch.dep["gateHook"], 0),
        "client", "createJob")
    jid = None
    for log in rc["logs"]:
        try:
            jid = int(ch.core.events.JobCreated().process_log(log)["args"]["jobId"])
        except Exception:  # noqa: BLE001
            continue
    if not jid:
        raise SystemExit("job id not found in the receipt")
    print(f"   jobId {jid}")

    print("2. price it, point the payout, and fund it")
    if recipient:
        ch.send(ch.core.functions.setPayoutReceiver(jid, Web3.to_checksum_address(recipient)),
                "provider", "setPayoutReceiver(agent wallet)")
    else:
        print("   no ACP_AGENT_WALLET configured, so the operator is paid directly")
    ch.send(ch.core.functions.setBudget(jid, ch.usdc.address, BUDGET_USDC * 1_000_000, b""),
            "provider", f"setBudget {BUDGET_USDC} USDC")
    ch.send(ch.core.functions.fund(jid, ch.usdc.address, BUDGET_USDC * 1_000_000, b""),
            "client", f"fund {BUDGET_USDC} USDC")

    print("3. qualify the provider, and have it deliver")
    ch.send(ch.reg.functions.setQualification(ch.addr["provider"], 1), "deployer",
            "setQualification(QUALIFIED)")
    ch.send(ch.core.functions.submit(jid, keccak(text=f"deliverable-{jid}-{int(time.time())}"), b""),
            "provider", "submit deliverable")

    print("4. one observation, committed and approved with a day of freshness")
    observed_at = int(time.time())
    content = keccak(text=f"observation-{jid}-v1")
    eid = keccak(ch.w3.codec.encode(["uint256", "address", "bytes32", "uint64"],
                 [jid, ch.addr["provider"], content, 1]))
    ch.send(ch.reg.functions.commit(K._ev(eid, jid, 1, content, FRESHNESS_BOUND, observed_at, ch)),
            "deployer", "commit evidence v1")
    ch.send(ch.reg.functions.approve(jid, eid, keccak(text=f"evaluation-{jid}")), "deployer", "approve v1")

    ok, reason = ch.reg.functions.isReleasable(jid, int(time.time())).call()
    code = bytes(reason).rstrip(b"\x00").decode(errors="replace") or "OK"
    out = {"jobId": jid, "payoutReceiver": recipient or ch.addr["provider"],
           "agentId": agent_id or None, "budgetUsdc": BUDGET_USDC,
           "firstClickShouldRead": code, "evidenceId": "0x" + eid.hex()}
    json.dump(out, open(os.path.join(K.ROOT, "artifacts", "demo-job.json"), "w",
                        encoding="utf-8"), indent=2, sort_keys=True)

    print(f"\n   the predicate now answers: {code}")
    print(f"   job {jid} is ready. Open http://127.0.0.1:8787/dashboard and pick job {jid}.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
