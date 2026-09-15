#!/usr/bin/env python3
"""The ACP lane: a gated job whose beneficiary is an agent from the ACP registry.

The quote-shaped jobs in this repository settle against evidence about a
deliverable. This lane settles against evidence about a COUNTERPARTY: the
provider side of the job is an agent that exists on the ACP registry, and the
escrow pays that agent's own wallet rather than the operator that submitted on
its behalf.

What the chain holds, and what an outside reader can check without trusting
this script:

  * the job's ``payoutReceiver`` is the agent's wallet, read back from the core
  * the evidence ``subject`` is keccak(canonical(agent binding))
  * the evidence ``provenanceHash`` is keccak(canonical(observation envelope))
  * the predicate is unchanged: qualification, freshness and supersession are
    the same clauses that gate every other job here

Both published artifacts are therefore re-derivable. ``--verify`` re-hashes them
and compares the result against the deployed registry, so the claim is not
"the script says so" but "the chain agrees with the file".

Env: ACP_AGENT_ID, ACP_AGENT_WALLET, ACP_AGENT_REGISTRY, ACP_JOB_USDC (optional)

Usage:
    python scripts/acp_provider_job.py [env-file]
    python scripts/acp_provider_job.py --verify
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

BINDING_OUT = os.path.join(K.ROOT, "artifacts", "acp-agent-binding.json")
JOB_OUT = os.path.join(K.ROOT, "artifacts", "acp-provider-job.json")
PROOF_OUT = os.path.join(K.ROOT, "artifacts", "events.jsonl")
FRESHNESS_BOUND = 3600


def canonical(payload: dict) -> bytes:
    """One canonical form for every published record, so a reader can re-derive it."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def binding_for(env: dict) -> dict:
    agent_id = (env.get("ACP_AGENT_ID") or "").strip()
    wallet = (env.get("ACP_AGENT_WALLET") or "").strip()
    registry = (env.get("ACP_AGENT_REGISTRY") or "").strip()
    missing = [k for k, v in (("ACP_AGENT_ID", agent_id), ("ACP_AGENT_WALLET", wallet),
                              ("ACP_AGENT_REGISTRY", registry)) if not v]
    if missing:
        raise SystemExit(f"missing {', '.join(missing)}: the ACP lane is about a named "
                         "counterparty, so the lane refuses to run without one")
    return {"agentId": agent_id, "agentWallet": wallet.lower(),
            "registry": registry, "chainId": int(env.get("CHAIN_ID") or 84532),
            "network": K.NETWORK}


def usdc_of(ch, who: str) -> float:
    raw = ch.usdc.functions.balanceOf(Web3.to_checksum_address(who)).call()
    return raw / 1e6


RELEASE_BLOCKED_SELECTOR = "5192a3c5"


def decode_release_blocked(text: str) -> dict | None:
    """Decode the gate's own error out of a simulation that could not.

    A caller that holds only the core's ABI cannot name an error raised inside the
    job's hook, so the refusal arrives as raw hex. Decoding it here is what turns
    "execution reverted" into "the gate refused job 21 with E_DISQUALIFIED".
    """
    idx = (text or "").find(RELEASE_BLOCKED_SELECTOR)
    if idx < 0:
        return None
    blob = text[idx + len(RELEASE_BLOCKED_SELECTOR):]
    hexish = "".join(c for c in blob[:128] if c in "0123456789abcdef")
    if len(hexish) < 128:
        return None
    job_id = int(hexish[:64], 16)
    reason = bytes.fromhex(hexish[64:128]).rstrip(b"\x00").decode(errors="replace")
    return {"error": "ReleaseBlocked(uint256,bytes32)", "jobId": job_id, "reason": reason}


def main() -> int:
    if "--verify" in sys.argv:
        return verify()
    env = K.load_env(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else None)
    ch = K.Chain(env)
    binding = binding_for(env)
    agent = Web3.to_checksum_address(binding["agentWallet"])
    subject = keccak(canonical(binding))
    amount = int(float(env.get("ACP_JOB_USDC") or 1) * 1e6)

    proof: dict = {"network": K.NETWORK, "core": ch.dep["core"], "registry": ch.dep["registry"],
                   "gateHook": ch.dep["gateHook"], "agentId": binding["agentId"],
                   "agentWallet": agent, "subject": "0x" + subject.hex(),
                   "escrowUsdc": amount / 1e6}

    json.dump(binding, open(BINDING_OUT, "w", encoding="utf-8"), indent=2, sort_keys=True)
    print(f"binding written to {BINDING_OUT}")
    print(f"  agent {binding['agentId']} at {agent}")

    print("\n0. what the chain already knows about the counterparty")
    before = usdc_of(ch, agent)
    print(f"   agent USDC before: {before:.2f}")
    proof["agentUsdcBefore"] = before

    print("\n1. the client opens a job whose evaluator is KeeperHub's wallet")
    exp = int(time.time()) + 7200
    token_id = int(env["ACP_AGENT_TOKEN_ID"]) if (env.get("ACP_AGENT_TOKEN_ID") or "").strip() else 0
    resume = None
    for i, a in enumerate(sys.argv):
        if a == "--resume" and i + 1 < len(sys.argv):
            resume = int(sys.argv[i + 1])
    if resume:
        # A lane that stops mid-flight leaves real money in escrow. Resuming is the
        # difference between recovering that job and stranding it.
        jid = resume
        job = ch.core.functions.getJob(jid).call()
        if job[12].lower() != agent.lower():
            raise SystemExit(f"job {jid} pays {job[12]}, not the agent: refusing to resume")
        if job[1] not in (1, 2):
            raise SystemExit(f"job {jid} is in status {job[1]}: nothing to resume")
        proof["jobId"] = jid
        proof["resumed"] = True
        print(f"   resuming job {jid}, status {job[1]} (escrow already funded)")
    else:
        rc, _ = ch.send(ch.core.functions.createJob(
            ch.addr["provider"], ch.addr["evaluator"], exp,
            f"ACP counterparty check: {binding['agentId']}", ch.dep["gateHook"], token_id),
            "client", "createJob (evaluator = KeeperHub)")
        jid = None
        for log in rc["logs"]:
            try:
                jid = int(ch.core.events.JobCreated().process_log(log)["args"]["jobId"])
            except Exception:  # noqa: BLE001
                continue
        if not jid:
            raise SystemExit("job id not found in the receipt")
        proof["jobId"] = jid
        print(f"   jobId {jid}  providerAgentId {token_id}"
              + ("  (the registry issues an opaque id, so the identity is committed in evidence)"
                 if token_id == 0 else ""))

    if (not resume) or ch.core.functions.getJob(jid).call()[1] == 1:
        print("\n2. the operator submits on the agent's behalf, and points the payout at the agent")
        if not resume:
            ch.send(ch.core.functions.setPayoutReceiver(jid, agent), "provider",
                    "setPayoutReceiver(agent wallet)")
            ch.send(ch.core.functions.setBudget(jid, ch.usdc.address, amount, b""), "provider",
                    f"setBudget {amount / 1e6:.2f} USDC")
            ch.send(ch.core.functions.fund(jid, ch.usdc.address, amount, b""), "client",
                    f"fund {amount / 1e6:.2f} USDC")
        ch.send(ch.reg.functions.setQualification(ch.addr["provider"], 1), "deployer",
                "setQualification(operator, QUALIFIED)")

    observed_at = int(time.time())
    deliverable = canonical({"kind": "acp-counterparty-observation",
                             "agentId": binding["agentId"],
                             "agentWallet": agent,
                             "agentUsdcAtObservation": f"{before:.6f}",
                             "observedAt": observed_at})
    ch.send(ch.core.functions.submit(jid, keccak(deliverable), b""), "provider", "submit deliverable")

    envelope = {"kind": "acp-counterparty-observation", "binding": binding,
                "job": {"id": jid, "core": ch.dep["core"], "chainId": ch.dep["chainId"]},
                "observation": {"observedAt": observed_at, "freshnessBound": FRESHNESS_BOUND,
                                "agentUsdc": f"{before:.6f}",
                                "deliverableHash": "0x" + keccak(deliverable).hex()},
                "settlement": {"requested": f"{amount / 1e6:.6f}", "token": ch.dep["usdc"],
                               "payoutReceiver": agent}}
    provenance = keccak(canonical(envelope))
    proof["provenanceHash"] = "0x" + provenance.hex()
    proof["envelope"] = envelope

    print("\n3. evidence binds the counterparty, not just the deliverable")
    eid = keccak(ch.w3.codec.encode(["uint256", "address", "bytes32", "uint64"],
                 [jid, ch.addr["provider"], keccak(deliverable), 1]))
    ch.send(ch.reg.functions.commit(K._ev(eid, jid, 1, keccak(deliverable), FRESHNESS_BOUND,
            observed_at, ch, subject=subject, evidence_type=keccak(text="acp-counterparty"),
            evaluation=keccak(canonical({"jobId": jid, "agentId": binding["agentId"],
                                          "observedAt": observed_at})), provenance=provenance)),
            "deployer", "commit evidence (agent-bound subject)")
    ch.send(ch.reg.functions.approve(jid, eid, keccak(text=f"evaluation-{jid}")), "deployer",
            "approve")
    proof["evidenceId"] = "0x" + eid.hex()
    print(f"   subject  0x{subject.hex()[:24]}...  provenance 0x{provenance.hex()[:24]}...")

    print("\n4. KeeperHub is asked to release while the counterparty is not qualified")
    ch.send(ch.reg.functions.setQualification(ch.addr["provider"], 3), "deployer",
            "revoke qualification (the refusal beat)")
    zero = keccak(text="acp-release")
    st, sim = K.kh(env, "POST", "/api/execute/contract-call",
                   {"contractAddress": ch.dep["core"], "network": K.NETWORK,
                    "abi": json.dumps(K.Chain.abi("ERC8183.sol", "ERC8183")),
                    "functionName": "complete", "functionArgs": json.dumps([str(jid), "0x" + zero.hex(), "0x"]),
                    "simulate": True}, idem=f"duality-acp-hold-{jid}")
    print(f"   HTTP {st}  wouldRevert={sim.get('wouldRevert')}  reason={str(sim.get('revertReason'))[:90]}")
    decoded = decode_release_blocked(str(sim.get("revertReason")))
    if decoded:
        print(f"   decoded by us: {decoded['error']} jobId {decoded['jobId']} reason {decoded['reason']}")
    proof["keeperHubHoldSimulation"] = {"httpStatus": st, "wouldRevert": sim.get("wouldRevert"),
                                        "revertReason": sim.get("revertReason"), "signer": sim.get("from"),
                                        "decodedByUs": decoded,
                                        "decodedByKeeperHub": False if decoded else None}
    proof["predicateWhileDisqualified"] = ch.reg.functions.isReleasable(jid, int(time.time())).call()[1].rstrip(b"\x00").decode()

    print("\n5. the counterparty is reinstated, and the same call is asked again")
    ch.send(ch.reg.functions.setQualification(ch.addr["provider"], 1), "deployer", "reinstate qualification")
    after_reinstate = ch.reg.functions.isReleasable(jid, int(time.time())).call()[1].rstrip(b"\x00").decode() or "OK"
    # A simulate right after a write can be answered from a node whose view has not
    # caught up, and that reads exactly like a refusal: success=false, wouldRevert=true.
    # It is not one, so the honest record is the sequence of attempts plus what our own
    # read of the same predicate said, and the broadcast is the authority.
    attempts = []
    converged = None
    for attempt in range(1, 7):
        st2, sim_ok = K.kh(env, "POST", "/api/execute/contract-call",
                           {"contractAddress": ch.dep["core"], "network": K.NETWORK,
                            "abi": json.dumps(K.Chain.abi("ERC8183.sol", "ERC8183")),
                            "functionName": "complete",
                            "functionArgs": json.dumps([str(jid), "0x" + zero.hex(), "0x"]),
                            "simulate": True}, idem=f"duality-acp-ok-{jid}-{attempt}")
        attempts.append({"attempt": attempt, "httpStatus": st2, "wouldRevert": sim_ok.get("wouldRevert")})
        print(f"   attempt {attempt}: HTTP {st2}  wouldRevert={sim_ok.get('wouldRevert')}")
        if sim_ok.get("wouldRevert") is False:
            converged = attempt
            break
        time.sleep(5)
    proof["keeperHubCleanSimulation"] = {"attempts": attempts, "convergedOnAttempt": converged,
                                        "predicateAfterReinstate": after_reinstate,
                                        "note": "a simulate answered from a lagging node reads as a "
                                                "refusal; the predicate read and the broadcast disagree "
                                                "with it and are what this record goes by"}

    print("\n6. KeeperHub broadcasts, and the agent's wallet is what gets paid")
    st3, sent = K.kh(env, "POST", "/api/execute/contract-call",
                     {"contractAddress": ch.dep["core"], "network": K.NETWORK,
                      "abi": json.dumps(K.Chain.abi("ERC8183.sol", "ERC8183")),
                      "functionName": "complete", "functionArgs": json.dumps([str(jid), "0x" + zero.hex(), "0x"])},
                     idem=f"duality-acp-release-{jid}")
    exid = sent.get("executionId")
    status = {}
    for _ in range(30):
        if not exid:
            break
        _s, status = K.kh(env, "GET", f"/api/execute/{exid}/status")
        if status.get("status") in ("completed", "failed"):
            break
        time.sleep(3)
    proof["keeperHubRelease"] = {"httpStatus": st3, **{k: (status or sent).get(k) for k in
                                 ("executionId", "status", "transactionHash", "transactionLink", "sponsored")}}
    print(f"   executionId={exid}  status={(status or sent).get('status')}"
          f"  tx={(status or sent).get('transactionHash')}")

    print("\n7. read back from the chain, not from this script's own bookkeeping")
    time.sleep(3)
    job = ch.core.functions.getJob(jid).call()
    ev = ch.reg.functions.getEvidence(eid).call()
    after = usdc_of(ch, agent)
    readback = {"jobStatus": job[1], "payoutReceiver": job[12], "providerAgentId": job[9],
                "jobProvider": job[2], "jobEvaluator": job[4],
                "evidenceSubject": "0x" + bytes(ev[3]).hex(),
                "evidenceProvenanceHash": "0x" + bytes(ev[6]).hex(),
                "evidenceProvider": ev[10], "agentUsdcAfter": after}
    proof["readback"] = readback
    print(f"   payoutReceiver on chain: {readback['payoutReceiver']}")
    print(f"   agent USDC {before:.2f} -> {after:.2f}")
    proof["agentUsdcAfter"] = after

    json.dump(proof, open(JOB_OUT, "w", encoding="utf-8"), indent=2, sort_keys=True)
    print(f"\nwrote {JOB_OUT}")

    ok = (proof["keeperHubHoldSimulation"]["wouldRevert"] is True
          and readback["payoutReceiver"].lower() == agent.lower()
          and abs(after - before - amount / 1e6) < 1e-9)
    print("RESULT:", "PASS" if ok else "REVIEW",
          "- held while the counterparty was not qualified, then paid the agent's own wallet")
    return 0 if ok else 1


def verify() -> int:
    """Re-derive every commitment from the published files and compare to the chain."""
    env = K.load_env()
    ch = K.Chain(env)
    binding = json.load(open(BINDING_OUT, encoding="utf-8"))
    proof = json.load(open(JOB_OUT, encoding="utf-8"))
    jid = proof["jobId"]
    eid = bytes.fromhex(proof["evidenceId"][2:])
    derived_subject = keccak(canonical(binding))
    derived_provenance = keccak(canonical(proof["envelope"]))
    job = ch.core.functions.getJob(jid).call()
    ev = ch.reg.functions.getEvidence(eid).call()
    checks = {
        "subject re-derives from the binding file": derived_subject == bytes(ev[3]),
        "provenance re-derives from the envelope file": derived_provenance == bytes(ev[6]),
        "published subject matches the run record": derived_subject.hex() == proof["subject"][2:],
        "payoutReceiver on chain is the agent": job[12].lower() == binding["agentWallet"].lower(),
        "job is settled": bool(ch.reg.functions.settled(jid).call()),
    }
    for label, passed in checks.items():
        print(f"  {'OK  ' if passed else 'FAIL'} {label}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
