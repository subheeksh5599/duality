#!/usr/bin/env python3
"""Prove all three invalidation classes on live chain, through KeeperHub.

Checklist section 15 says all three must work in the actual demo. This runs one
job per class, each one approved and then invalidated in a different way, and
records what KeeperHub's own simulation says when the release is attempted.

  SUPERSEDED     a newer observation of the same subject lands after approval
  DISQUALIFIED   the provider loses its qualification after approval
  STALE          the freshness window lapses after approval
  NOT_APPROVED   no approval was ever bound to the job

The SUPERSEDED job is then carried all the way through reconciliation to a real
release, so the artefact shows both the refusal and the recovery.

Usage:  python scripts/prove_invalidation_classes.py [env-file]
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from eth_utils import keccak  # noqa: E402
from web3 import Web3  # noqa: E402

import keeperhub_release as K  # noqa: E402

ZERO32 = b"\x00" * 32


def evidence_id(ch, jid: int, content: bytes, version: int) -> bytes:
    return keccak(ch.w3.codec.encode(["uint256", "address", "bytes32", "uint64"],
                                     [jid, ch.addr["provider"], content, version]))


def open_job(ch, label: str) -> int:
    exp = int(time.time()) + 7200
    rc, _ = ch.send(ch.core.functions.createJob(ch.addr["provider"], ch.addr["evaluator"], exp,
                    f"DUALITY invalidation proof: {label}", ch.dep["gateHook"], 1), "client", "createJob")
    for log in rc["logs"]:
        try:
            return int(ch.core.events.JobCreated().process_log(log)["args"]["jobId"])
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("no job id")


def fund_and_deliver(ch, jid: int) -> None:
    ch.send(ch.core.functions.setBudget(jid, ch.usdc.address, 1_000_000, b""), "provider", "  setBudget 1 USDC")
    ch.send(ch.core.functions.fund(jid, ch.usdc.address, 1_000_000, b""), "client", "  fund 1 USDC")
    ch.send(ch.core.functions.submit(jid, keccak(text=f"deliverable-{jid}"), b""), "provider", "  submit deliverable")


def commit_and_approve(ch, jid: int, version: int, bound: int, label: str) -> bytes:
    content = keccak(text=f"quote-{jid}-v{version}")
    eid = evidence_id(ch, jid, content, version)
    ch.send(ch.reg.functions.commit(K._ev(eid, jid, version, content, bound, int(time.time()), ch)),
            "deployer", f"  commit evidence v{version}")
    ch.send(ch.reg.functions.approve(jid, eid, keccak(text=f"evaluation-{jid}-{version}")),
            "deployer", f"  approve v{version}")
    return eid


def settle(ch, jid: int, expect_ok: bool, tries: int = 20, pause: float = 2.0) -> bool:
    """Wait until our own view of the predicate reflects the change, then pause.

    Without this the proof is flaky for a real reason: a public node can confirm
    a transaction while the sponsor's node has not yet, and a stale read then
    records a clean simulation for an approval that is already invalid.
    """
    for _ in range(tries):
        ok, _reason = ch.reg.functions.isReleasable(jid, int(time.time())).call()
        if ok == expect_ok:
            time.sleep(3.0)
            return True
        time.sleep(pause)
    return False


class Prover:
    def __init__(self, env, ch, core_abi, merged_abi):
        self.env, self.ch, self.core_abi, self.merged_abi = env, ch, core_abi, merged_abi
        self.results = {}

    def attempt(self, jid: int, label: str, merged: bool = True) -> dict:
        """Ask KeeperHub to simulate the release and record what it says."""
        st, res = K.kh(self.env, "POST", "/api/execute/contract-call", {
            "contractAddress": self.ch.dep["core"], "network": K.NETWORK,
            "abi": json.dumps(self.merged_abi if merged else self.core_abi),
            "functionName": "complete",
            "functionArgs": json.dumps([str(jid), "0x" + keccak(text="approved").hex(), "0x"]),
            "simulate": True}, idem=f"duality-class-{label}-{jid}")
        pok, preason = self.ch.reg.functions.isReleasable(jid, int(time.time())).call()
        predicate_code = bytes(preason).rstrip(b"\x00").decode(errors="replace")
        reason = str(res.get("revertReason"))
        decoded = reason if "unknown custom error" not in reason else None
        import re as _re
        m = _re.search(r"ReleaseBlocked\((\d+), 0x([0-9a-fA-F]+)\)", reason)
        if m:
            raw = bytes.fromhex(m.group(2)).rstrip(b"\x00")
            decoded = f"ReleaseBlocked(jobId={m.group(1)}, reason={raw.decode(errors='replace')})"
        self.results[label] = {"jobId": jid, "httpStatus": st, "wouldRevert": res.get("wouldRevert"),
                               "predicateOk": pok, "predicateReason": predicate_code,
                               "revertReason": reason[:240], "decoded": decoded,
                               "reasonLegibleViaKeeperHub": bool(decoded),
                               "signer": res.get("from")}
        print(f"   KeeperHub simulate -> HTTP {st} wouldRevert={res.get('wouldRevert')}")
        print(f"   our predicate      -> ok={pok} reason={predicate_code or '(none)'}")
        print(f"   reason: {str(decoded or reason)[:150]}")
        return self.results[label]


def main() -> int:
    env = K.load_env(sys.argv[1] if len(sys.argv) > 1 else None)
    ch = K.Chain(env)
    core_abi = json.load(open(os.path.join(K.ART, "ERC8183.sol", "ERC8183.json"), encoding="utf-8"))["abi"]
    hook_abi = json.load(open(os.path.join(K.ART, "DualityGateHook.sol", "DualityGateHook.json"), encoding="utf-8"))["abi"]
    merged = core_abi + [x for x in hook_abi if x.get("type") == "error"]
    p = Prover(env, ch, core_abi, merged)

    if ch.w3.eth.get_balance(K.KH_WALLET) < Web3.to_wei(0.0005, "ether"):
        ch.transfer(K.KH_WALLET, 0.003, "top up KeeperHub wallet")
    ch.send(ch.reg.functions.setQualification(ch.addr["provider"], 1), "deployer", "setQualification(QUALIFIED)")

    # ---------------------------------------------------------------- SUPERSEDED
    print("\n[1/4] SUPERSEDED: a newer observation lands after the approval")
    j1 = open_job(ch, "superseded"); fund_and_deliver(ch, j1)
    commit_and_approve(ch, j1, 1, 3600, "v1")
    same_content = keccak(text=f"quote-{j1}-v2")
    e2 = evidence_id(ch, j1, same_content, 2)
    ch.send(ch.reg.functions.commit(K._ev(e2, j1, 2, same_content, 3600, int(time.time()), ch)),
            "deployer", "  commit evidence v2 (NOT approved)")
    print("   settle:", settle(ch, j1, expect_ok=False))
    p.attempt(j1, "SUPERSEDED")
    print("   -> recovery: re-approve the current version and release through KeeperHub")
    ch.send(ch.reg.functions.approve(j1, e2, keccak(text="evaluation-recovered")), "deployer", "  approve v2")
    before = ch.usdc.functions.balanceOf(ch.addr["provider"]).call() / 1e6
    st, sent = K.kh(env, "POST", "/api/execute/contract-call", {
        "contractAddress": ch.dep["core"], "network": K.NETWORK, "abi": json.dumps(core_abi),
        "functionName": "complete",
        "functionArgs": json.dumps([str(j1), "0x" + keccak(text="approved").hex(), "0x"])},
        idem=f"duality-class-release-{j1}")
    after = ch.usdc.functions.balanceOf(ch.addr["provider"]).call() / 1e6
    print(f"   release -> HTTP {st} status={sent.get('status')} usdc {before:.2f} -> {after:.2f}")
    p.results["SUPERSEDED_recovered_release"] = {"httpStatus": st, **{k: sent.get(k) for k in
        ("executionId", "status", "transactionHash", "transactionLink")}}

    # -------------------------------------------------------------- DISQUALIFIED
    print("\n[2/4] DISQUALIFIED: the provider loses qualification after the approval")
    j2 = open_job(ch, "disqualified"); fund_and_deliver(ch, j2)
    commit_and_approve(ch, j2, 1, 3600, "v1")
    ch.send(ch.reg.functions.setQualification(ch.addr["provider"], 3), "deployer", "  revoke provider qualification")
    print("   settle:", settle(ch, j2, expect_ok=False))
    p.attempt(j2, "DISQUALIFIED")
    ch.send(ch.reg.functions.setQualification(ch.addr["provider"], 1), "deployer", "  restore qualification")
    print("   settle:", settle(ch, j2, expect_ok=True))
    p.attempt(j2, "DISQUALIFIED_then_restored")

    # ---------------------------------------------------------------------- STALE
    print("\n[3/4] STALE: the freshness window lapses after the approval")
    j3 = open_job(ch, "stale"); fund_and_deliver(ch, j3)
    commit_and_approve(ch, j3, 1, 5, "v1 with a 5s bound")
    time.sleep(11)
    print("   settle:", settle(ch, j3, expect_ok=False))
    p.attempt(j3, "STALE")

    # --------------------------------------------------------------- NOT_APPROVED
    print("\n[4/4] NOT_APPROVED: nothing was ever bound to the job")
    j4 = open_job(ch, "not-approved"); fund_and_deliver(ch, j4)
    print("   settle:", settle(ch, j4, expect_ok=False))
    p.attempt(j4, "NOT_APPROVED")

    out = os.path.join(K.ROOT, "artifacts", "invalidation-classes.json")
    json.dump({"network": K.NETWORK, "core": ch.dep["core"], "gateHook": ch.dep["gateHook"],
               "keeperHubWallet": K.KH_WALLET, "classes": p.results},
              open(out, "w", encoding="utf-8"), indent=2)
    print(f"\nwrote {out}")

    want = {"SUPERSEDED": "E_SUPERSEDED", "DISQUALIFIED": "E_DISQUALIFIED",
            "STALE": "E_STALE", "NOT_APPROVED": "E_NOT_APPROVED"}
    print("\n   class          invariant: money blocked   predicate code   reason legible")
    ok = True
    for label, code in want.items():
        r = p.results.get(label, {})
        blocked = r.get("wouldRevert") is True and r.get("predicateOk") is False
        named = r.get("predicateReason") == code
        legible = bool(r.get("reasonLegibleViaKeeperHub"))
        print(f"   {label:<14} {'YES' if blocked else 'NO ':<26} {code:<16} {'yes' if legible else 'no (transient)'}")
        ok = ok and blocked and named
    print("\nRESULT:", "PASS - every class blocked on live chain, each named by the predicate" if ok else "REVIEW")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
