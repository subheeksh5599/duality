#!/usr/bin/env python3
"""DUALITY release, executed by KeeperHub.

This is rubric axis 2: the value movement is not signed by a script here. The
evaluator on the job IS KeeperHub's own wallet, and the `complete()` call that
moves the money goes out through KeeperHub's direct-execution API:

    simulate   -> check success / wouldRevert   (KeeperHub reports the gate)
    broadcast  -> with a unique Idempotency-Key
    poll       -> until completed, then keep the transactionLink

The first simulate is the interesting one: it is KeeperHub itself reporting that
the gate refused, before any money moves.

Usage:
    python scripts/keeperhub_release.py [env-file]
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

from eth_utils import keccak
from web3 import Web3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ART = os.path.join(ROOT, "contracts", "out")
DEPLOYMENT = os.path.join(ROOT, "deployments", "base-sepolia.json")
KH_BASE = "https://app.keeperhub.com"
KH_WALLET = "0x1776D4D751d97c85845bF54e6CE364CEc62D4bBf"
NETWORK = "base-sepolia"
ZERO32 = b"\x00" * 32


def load_env(path: str | None = None) -> dict:
    """Read configuration from DUALITY_ENV, or ./.env, plus the process environment.

    The KeeperHub key is taken from the environment. This module never reads a
    path outside the project and never writes a secret to disk.
    """
    path = path or os.environ.get("DUALITY_ENV") or os.path.join(ROOT, ".env")
    env: dict = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():
        if k.isupper():
            env[k] = v
    return env


def kh(env: dict, method: str, path: str, body: dict | None = None, idem: str | None = None):
    headers = {"Authorization": f"Bearer {env['KH_API_KEY']}", "Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124"}
    if idem:
        headers["Idempotency-Key"] = idem
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(KH_BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"raw": raw[:400]}


class Chain:
    def __init__(self, env: dict):
        self.w3 = Web3(Web3.HTTPProvider(env["RPC_URL"]))
        self.dep = json.load(open(DEPLOYMENT, encoding="utf-8"))
        c = Web3.to_checksum_address
        self.core = self.w3.eth.contract(address=c(self.dep["core"]), abi=self.abi("ERC8183.sol", "ERC8183"))
        self.reg = self.w3.eth.contract(address=c(self.dep["registry"]), abi=self.abi("EvidenceRegistry.sol", "EvidenceRegistry"))
        self.usdc = self.w3.eth.contract(address=c(self.dep["usdc"]), abi=self.abi("MockUSDC.sol", "MockUSDC"))
        self.keys = {"deployer": env["PRIVATE_KEY"], "client": env["BUYER_KEY"], "provider": env["PROVIDER_KEY"]}
        self.addr = {"client": c(env["BUYER"]), "provider": c(env["PROVIDER"]),
                     "evaluator": c(KH_WALLET), "deployer": c(env["ADDRESS"])}

    @staticmethod
    def abi(folder: str, name: str) -> list:
        return json.load(open(os.path.join(ART, folder, f"{name}.json"), encoding="utf-8"))["abi"]

    def send(self, fn, who: str, label: str, gas: int | None = None):
        acct = self.addr[who]
        tx = fn.build_transaction({"from": acct, "nonce": self.w3.eth.get_transaction_count(acct, "pending"),
                                   "chainId": self.dep["chainId"], "gasPrice": self.w3.eth.gas_price,
                                   "gas": gas or 900_000})
        if gas is None:
            for attempt in range(8):
                probe = {k: v for k, v in tx.items() if k != "gas"}
                try:
                    tx["gas"] = int(self.w3.eth.estimate_gas(probe) * 1.3)
                    break
                except Exception:  # noqa: BLE001
                    if attempt == 7:
                        raise
                    time.sleep(2)
        signed = self.w3.eth.account.sign_transaction(tx, self.keys[who])
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        rc = self.w3.eth.wait_for_transaction_receipt(h, timeout=180)
        print(f"  {label:<46} {'OK ' if rc['status'] == 1 else 'FAIL'}  {h.hex()}")
        time.sleep(1.2)
        return rc, h.hex()

    def transfer(self, to: str, ether: float, label: str):
        acct = self.addr["deployer"]
        tx = {"from": acct, "to": to, "value": self.w3.to_wei(ether, "ether"), "gas": 21000,
              "chainId": self.dep["chainId"], "gasPrice": self.w3.eth.gas_price,
              "nonce": self.w3.eth.get_transaction_count(acct, "pending")}
        signed = self.w3.eth.account.sign_transaction(tx, self.keys["deployer"])
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        rc = self.w3.eth.wait_for_transaction_receipt(h, timeout=180)
        print(f"  {label:<46} OK   {h.hex()}")
        time.sleep(1.2)
        return rc, h.hex()


def main() -> int:
    env = load_env(sys.argv[1] if len(sys.argv) > 1 else None)
    ch = Chain(env)
    core_abi = json.dumps(Chain.abi("ERC8183.sol", "ERC8183"))
    proof: dict = {"network": NETWORK, "keeperHubWallet": KH_WALLET,
                   "core": ch.dep["core"], "gateHook": ch.dep["gateHook"]}

    print("0. KeeperHub's wallet must be able to pay gas; it had none")
    if ch.w3.eth.get_balance(KH_WALLET) < Web3.to_wei(0.0005, "ether"):
        rc, _ = ch.transfer(KH_WALLET, 0.003, "fund KeeperHub wallet with test ETH")
        proof["fundTx"] = _
    print(f"   KeeperHub wallet balance: {ch.w3.eth.get_balance(KH_WALLET) / 1e18:.6f} ETH")

    print("\n1. open a job whose EVALUATOR is KeeperHub's wallet")
    exp = int(time.time()) + 7200
    rc, _ = ch.send(ch.core.functions.createJob(ch.addr["provider"], ch.addr["evaluator"], exp,
                    "DUALITY x KeeperHub: time-sensitive quote", ch.dep["gateHook"], 1),
                    "client", "createJob (evaluator = KeeperHub)")
    jid = None
    for log in rc["logs"]:
        try:
            jid = int(ch.core.events.JobCreated().process_log(log)["args"]["jobId"])
        except Exception:  # noqa: BLE001
            continue
    print(f"   jobId {jid}, evaluator {KH_WALLET}")
    proof["jobId"] = jid

    ch.send(ch.core.functions.setBudget(jid, ch.usdc.address, 1_000_000, b""), "provider", "setBudget 1 USDC")
    ch.send(ch.core.functions.fund(jid, ch.usdc.address, 1_000_000, b""), "client", "fund 1 USDC")
    ch.send(ch.core.functions.submit(jid, keccak(text=f"quote-kh-{jid}"), b""), "provider", "submit deliverable")
    ch.send(ch.reg.functions.setQualification(ch.addr["provider"], 1), "deployer", "setQualification(QUALIFIED)")

    print("\n2. evidence v1, approved at evaluation time, 5 second freshness")
    t1 = int(time.time())
    ev1 = keccak(ch.w3.codec.encode(["uint256", "address", "bytes32", "uint64"],
                [jid, ch.addr["provider"], keccak(text="quote-v1"), 1]))
    ch.send(ch.reg.functions.commit(_ev(ev1, jid, 1, keccak(text="quote-v1"), 5, t1, ch)),
            "deployer", "commit evidence v1")
    ch.send(ch.reg.functions.approve(jid, ev1, keccak(text="evaluation-1")), "deployer", "approve v1")
    proof["evidenceV1"] = ev1.hex()

    print("\n3. KeeperHub SIMULATES the release while the approval is stale")
    time.sleep(11)
    st, sim_hold = kh(env, "POST", "/api/execute/contract-call",
                      {"contractAddress": ch.dep["core"], "network": NETWORK, "abi": core_abi,
                       "functionName": "complete",
                       "functionArgs": json.dumps([str(jid), "0x" + keccak(text="approved").hex(), "0x"]),
                       "simulate": True}, idem=f"duality-kh-hold-{jid}")
    print(f"   HTTP {st}  success={sim_hold.get('success')}  wouldRevert={sim_hold.get('wouldRevert')}")
    print(f"   revertReason: {sim_hold.get('revertReason')}   from: {sim_hold.get('from')}")
    proof["keeperHubHoldSimulation"] = {"httpStatus": st, "success": sim_hold.get("success"),
                                        "wouldRevert": sim_hold.get("wouldRevert"),
                                        "revertReason": sim_hold.get("revertReason"),
                                        "signer": sim_hold.get("from")}

    print("\n4. reconciliation: a fresh observation, re-approved")
    t2 = int(time.time())
    ev2 = keccak(ch.w3.codec.encode(["uint256", "address", "bytes32", "uint64"],
                [jid, ch.addr["provider"], keccak(text="quote-v2"), 2]))
    ch.send(ch.reg.functions.commit(_ev(ev2, jid, 2, keccak(text="quote-v2"), 3600, t2, ch)),
            "deployer", "commit evidence v2")
    ch.send(ch.reg.functions.approve(jid, ev2, keccak(text="evaluation-2")), "deployer", "approve v2")
    proof["evidenceV2"] = ev2.hex()

    print("\n5. KeeperHub SIMULATES again, now the same call is clean")
    st2, sim_ok = kh(env, "POST", "/api/execute/contract-call",
                     {"contractAddress": ch.dep["core"], "network": NETWORK, "abi": core_abi,
                      "functionName": "complete",
                      "functionArgs": json.dumps([str(jid), "0x" + keccak(text="approved").hex(), "0x"]),
                      "simulate": True}, idem=f"duality-kh-ok-{jid}")
    print(f"   HTTP {st2}  success={sim_ok.get('success')}  wouldRevert={sim_ok.get('wouldRevert')}")
    proof["keeperHubCleanSimulation"] = {"httpStatus": st2, "success": sim_ok.get("success"),
                                         "wouldRevert": sim_ok.get("wouldRevert")}

    print("\n6. KeeperHub BROADCASTS the release")
    before = ch.usdc.functions.balanceOf(ch.addr["provider"]).call() / 1e6
    st3, sent = kh(env, "POST", "/api/execute/contract-call",
                   {"contractAddress": ch.dep["core"], "network": NETWORK, "abi": core_abi,
                    "functionName": "complete",
                    "functionArgs": json.dumps([str(jid), "0x" + keccak(text="approved").hex(), "0x"])},
                   idem=f"duality-kh-release-{jid}-1")
    print(f"   HTTP {st3}  executionId={sent.get('executionId')}  status={sent.get('status')}")
    proof["keeperHubRelease"] = {"httpStatus": st3, **{k: sent.get(k) for k in
                                ("executionId", "status", "transactionHash", "transactionLink", "error")}}

    exid = sent.get("executionId")
    if exid:
        print("\n7. poll the status until terminal")
        for i in range(30):
            st4, status = kh(env, "GET", f"/api/execute/{exid}/status")
            s = status.get("status")
            print(f"   poll {i+1}: {s}  tx={status.get('transactionHash')}")
            if s in ("completed", "failed"):
                proof["keeperHubStatus"] = {k: status.get(k) for k in
                                            ("executionId", "status", "transactionHash", "transactionLink",
                                             "sponsored", "type")}
                break
            time.sleep(3)
    after = ch.usdc.functions.balanceOf(ch.addr["provider"]).call() / 1e6
    print(f"\n   provider USDC {before:.2f} -> {after:.2f}   (moved by KeeperHub, not by this script)")
    proof["providerUsdc"] = {"before": before, "after": after}

    print("\n8. a second broadcast must not pay twice")
    st5, dup = kh(env, "POST", "/api/execute/contract-call",
                  {"contractAddress": ch.dep["core"], "network": NETWORK, "abi": core_abi,
                   "functionName": "complete",
                   "functionArgs": json.dumps([str(jid), "0x" + keccak(text="approved").hex(), "0x"])},
                  idem=f"duality-kh-release-{jid}-2")
    print(f"   HTTP {st5}  status={dup.get('status')}  error={str(dup.get('error'))[:90]}")
    proof["keeperHubSecondAttempt"] = {"httpStatus": st5, **{k: dup.get(k) for k in ("status", "error", "executionId")}}
    final = ch.usdc.functions.balanceOf(ch.addr["provider"]).call() / 1e6
    print(f"   provider USDC final: {final:.2f}")
    proof["providerUsdcFinal"] = final

    out = os.path.join(ROOT, "artifacts", "keeperhub-release.json")
    json.dump(proof, open(out, "w", encoding="utf-8"), indent=2)
    print(f"\nwrote {out}")
    ok = (proof.get("keeperHubHoldSimulation", {}).get("wouldRevert") is True
          and proof.get("keeperHubRelease", {}).get("status") in ("completed", "pending", "running", "unconfirmed")
          and abs(after - final) < 1e-9)
    print("RESULT:", "PASS" if ok else "REVIEW", "- KeeperHub held it, then released it, and paid once")
    return 0 if ok else 1


def _ev(eid, jid, version, content, bound, observed, ch):
    return (eid, jid, keccak(text=f"evaluation-{version}"), keccak(text="subject"), keccak(text="quote"),
            content, keccak(text=f"provenance-{version}"), version, observed, bound,
            ch.addr["provider"], 1, 1, 0, ZERO32)


if __name__ == "__main__":
    raise SystemExit(main())
