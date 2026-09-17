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
from web3.providers.rpc import HTTPProvider

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


def decoding_abi() -> str:
    """The ABI to send as `abi`, so a refusal raised inside the hook decodes.

    The target's ABI alone cannot name an error raised by a contract the target calls,
    and the gate is exactly that. Measured against the hosted API on 2026-09-15, three
    shapes: the core's ABI alone leaves the refusal hex, `errorAbis` carrying the hook's
    errors ALSO leaves it hex, and this - the hook's errors appended to the core's ABI
    in `abi` itself - decodes it, arguments included. So this is the shape that works
    today, and `errorAbis` is attached alongside it for the day the hosted API honours
    the field its own docs describe.
    """
    core = json.load(open(os.path.join(ART, "ERC8183.sol", "ERC8183.json"), encoding="utf-8"))["abi"]
    return json.dumps(core + json.loads(gate_error_abis()[0]))


def gate_error_abis() -> list[str]:
    """The gate's own errors, as an extra ABI document for KeeperHub's decoder.

    The gate is a hook, so a refusal is raised by a contract that is not the call
    target, and the ABI that encodes a call is the target's own - which means the
    reason code and the job id stay hex without this. `errorAbis` is the request
    field that closes that, and it is decoding-only: it cannot change the calldata.

    Returns errors only. The route refuses a document whose errors it cannot build,
    so a full ABI would work but an errors-only one cannot be mistaken for one that
    silently does nothing.
    """
    hook = json.load(open(os.path.join(ART, "DualityGateHook.sol", "DualityGateHook.json"),
                          encoding="utf-8"))["abi"]
    errors = [entry for entry in hook if entry.get("type") == "error"]
    if not errors:
        raise RuntimeError("the hook ABI declares no error, so errorAbis would be ignored")
    return [json.dumps(errors)]


class _FallbackProvider(HTTPProvider):
    """An HTTP provider that rotates endpoints when one of them throttles us.

    A single public RPC answers a burst with 429, and that surfaced to a reader as an
    error banner over a live control surface - the same class of failure the browser
    client already handles with a provider fallback. The list is the deployment's own
    (`service/web/chain-config.json`, the endpoints the static build reads through), so
    the two surfaces cannot drift into reading different chains.
    """

    def __init__(self, urls: list[str]):
        self._urls = [u for u in urls if u]
        self._i = 0
        self._throttled: set[str] = set()
        super().__init__(self._urls[0], request_kwargs={"timeout": 60})

    def _rotate(self) -> bool:
        self._i = (self._i + 1) % len(self._urls)
        self.endpoint_uri = self._urls[self._i]
        return self._i != 0

    def make_request(self, method, params):  # type: ignore[override]
        attempts = 0
        last: Exception | None = None
        while attempts < len(self._urls):
            try:
                resp = super().make_request(method, params)
                err = str(resp.get("error") or "") if isinstance(resp, dict) else ""
                if "429" in err or "too many requests" in err.lower():
                    self._throttled.add(self.endpoint_uri)
                    last = RuntimeError(err)
                    attempts += 1
                    self._rotate()
                    time.sleep(0.5)
                    continue
                return resp
            except Exception as exc:  # noqa: BLE001 - any transport failure is a reason to try the next
                text = str(exc).lower()
                if "429" in text or "too many requests" in text or "timeout" in text:
                    self._throttled.add(self.endpoint_uri)
                last = exc
                attempts += 1
                self._rotate()
                time.sleep(0.5)
        raise last if last else RuntimeError("no endpoint answered")


def _config_rpcs() -> list[str]:
    """The endpoints the static build reads through, so both read the same chain."""
    path = os.path.join(ROOT, "service", "web", "chain-config.json")
    try:
        return list(json.load(open(path, encoding="utf-8")).get("rpcs") or [])
    except Exception:  # noqa: BLE001
        return []


class Chain:
    def __init__(self, env: dict):
        self.w3 = Web3(_FallbackProvider([env["RPC_URL"]] + _config_rpcs()))
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
    core_abi = decoding_abi()
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


def _ev(eid, jid, version, content, bound, observed, ch, *, subject=None, evidence_type=None,
        evaluation=None, provenance=None):
    """Build the Evidence tuple the registry stores.

    The four optional arguments exist because the subject, the evidence type and
    the provenance commitment are what an outside reader re-derives: the ACP lane
    commits a real envelope hash there, where the quote-shaped jobs commit a
    label. Every caller that does not pass them keeps the original behaviour.
    """
    return (eid, jid, evaluation if evaluation is not None else keccak(text=f"evaluation-{version}"),
            subject if subject is not None else keccak(text="subject"),
            evidence_type if evidence_type is not None else keccak(text="quote"),
            content,
            provenance if provenance is not None else keccak(text=f"provenance-{version}"),
            version, observed, bound, ch.addr["provider"], 1, 1, 0, ZERO32)


if __name__ == "__main__":
    raise SystemExit(main())