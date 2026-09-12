#!/usr/bin/env python3
"""DUALITY on-chain end-to-end, Base Sepolia (84532).

Runs the mandated demo sequence against the DEPLOYED contracts with real
transactions, and prints the transaction hash for every step:

    create job -> provider prices -> client funds -> provider delivers
    -> evidence v1 committed and approved at evaluation time
    -> the freshness window lapses
    -> release is BLOCKED and the money does not move
    -> reconciliation commits v2 and re-approves
    -> the SAME release call now succeeds
    -> a third attempt cannot pay twice

Usage:
    python scripts/onchain_e2e.py [env-file]

The env file supplies: RPC_URL, ADDRESS, PRIVATE_KEY, BUYER, BUYER_KEY,
PROVIDER, PROVIDER_KEY, JUDGE, JUDGE_KEY.  Values are never printed.
"""
from __future__ import annotations

import json
import os
import sys
import time

from eth_utils import keccak
from web3 import Web3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ART = os.path.join(ROOT, "contracts", "out")
DEPLOYMENT = os.path.join(ROOT, "deployments", "base-sepolia.json")
EXPLORER = "https://sepolia.basescan.org/tx/"

ZERO32 = b"\x00" * 32


def load_env(path: str) -> dict:
    env = {}
    if path and os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k in env or k.isupper()})
    return env


def abi(folder: str, name: str) -> list:
    with open(os.path.join(ART, folder, f"{name}.json"), encoding="utf-8") as fh:
        return json.load(fh)["abi"]


def b32(text: str) -> bytes:
    raw = text.encode()
    assert len(raw) <= 32, text
    return raw + b"\x00" * (32 - len(raw))


ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "v", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
]


class Runner:
    def __init__(self, env: dict):
        self.w3 = Web3(Web3.HTTPProvider(env.get("RPC_URL", "https://sepolia.base.org")))
        assert self.w3.is_connected(), "no RPC"
        dep = json.load(open(DEPLOYMENT, encoding="utf-8"))
        self.chain = dep["chainId"]
        c = Web3.to_checksum_address
        self.core = self.w3.eth.contract(address=c(dep["core"]), abi=abi("ERC8183.sol", "ERC8183"))
        self.reg = self.w3.eth.contract(address=c(dep["registry"]), abi=abi("EvidenceRegistry.sol", "EvidenceRegistry"))
        self.hook = self.w3.eth.contract(address=c(dep["gateHook"]), abi=abi("DualityGateHook.sol", "DualityGateHook"))
        self.usdc = self.w3.eth.contract(address=c(dep["usdc"]), abi=ERC20_ABI)
        self.roles = {k: c(v) for k, v in dep["roles"].items()}
        self.keys = {
            "deployer": env["PRIVATE_KEY"],
            "client": env["BUYER_KEY"],
            "provider": env["PROVIDER_KEY"],
            "evaluator": env["JUDGE_KEY"],
        }
        self.errors = {}
        for name, obj in (("core", self.core), ("registry", self.reg), ("hook", self.hook)):
            for item in obj.abi:
                if item.get("type") == "error":
                    sig = f"{item['name']}({','.join(i['type'] for i in item['inputs'])})"
                    self.errors["0x" + keccak(text=sig)[:4].hex()] = (name, item)
        self.rows: list[tuple] = []

    # ------------------------------------------------------------------ plumbing
    def _tx(self, fn, who: str, label: str, value: int = 0):
        acct = self.w3.eth.account.from_key(self.keys[who]).address
        # gas is supplied here so build_transaction does NOT estimate internally:
        # a public node can still be a block behind the receipt we just waited
        # for, and an in-flight estimate would surface that lag as a contract
        # refusal. We estimate below, under retries, where lag is recoverable.
        tx = fn.build_transaction({
            "from": acct,
            "nonce": self.w3.eth.get_transaction_count(acct, "pending"),
            "chainId": self.chain,
            "gasPrice": self.w3.eth.gas_price,
            "value": value,
            "gas": 1_500_000,
        })
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
        time.sleep(1.5)
        ok = rc["status"] == 1
        self.rows.append((label, who, "OK" if ok else "REVERTED", h.hex()))
        print(f"  {label:<44} {'OK ' if ok else 'REVERTED'}  {h.hex()}")
        return rc, h.hex()

    def _transfer(self, to: str, ether: float, who: str, label: str):
        """A plain value transfer (used to give role wallets gas)."""
        acct = self.w3.eth.account.from_key(self.keys[who]).address
        base = {"from": acct, "to": to, "value": self.w3.to_wei(ether, "ether"),
                "chainId": self.chain, "nonce": self.w3.eth.get_transaction_count(acct, "pending")}
        base["gasPrice"] = self.w3.eth.gas_price
        base["gas"] = 21000
        signed = self.w3.eth.account.sign_transaction(base, self.keys[who])
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        rc = self.w3.eth.wait_for_transaction_receipt(h, timeout=180)
        self.rows.append((label, who, "OK" if rc["status"] == 1 else "REVERTED", h.hex()))
        print(f"  {label:<44} {'OK ' if rc['status'] == 1 else 'REVERTED'}  {h.hex()}")
        return rc, h.hex()

    def _tx_expect_revert(self, fn, who: str, label: str, gas: int = 600_000):
        """Send a transaction we EXPECT to fail, with explicit gas.

        A refusal that is estimated locally is only a simulation. Mining it makes
        the refusal a real transaction on the chain, carrying its own hash and its
        own revert reason, which is the artefact a reviewer can open.
        """
        acct = self.w3.eth.account.from_key(self.keys[who]).address
        tx = fn.build_transaction({
            "from": acct,
            "nonce": self.w3.eth.get_transaction_count(acct, "pending"),
            "chainId": self.chain,
            "gasPrice": self.w3.eth.gas_price,
            "value": 0,
            "gas": gas,
        })
        signed = self.w3.eth.account.sign_transaction(tx, self.keys[who])
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        rc = self.w3.eth.wait_for_transaction_receipt(h, timeout=180)
        blocked = rc["status"] == 0
        self.rows.append((label, who, "BLOCKED" if blocked else "UNEXPECTED SUCCESS", h.hex()))
        print(f"  {label:<44} {'BLOCKED ' if blocked else 'UNEXPECTED SUCCESS '}  {h.hex()}")
        time.sleep(1.5)
        return rc, h.hex()

    def job_id_from(self, rc) -> int:
        """Read the job id the core assigned, from the event, never assumed."""
        for log in rc["logs"]:
            try:
                parsed = self.core.events.JobCreated().process_log(log)
                return int(parsed["args"]["jobId"])
            except Exception:  # noqa: BLE001, PERF203
                continue
        raise RuntimeError("no JobCreated event in the receipt")

    def _why(self, fn, who: str) -> str:
        """Dry-run a call and decode the custom error it reverts with."""
        acct = self.w3.eth.account.from_key(self.keys[who]).address
        try:
            fn.call({"from": acct})
            return "no revert"
        except Exception as exc:  # noqa: BLE001
            data = getattr(exc, "data", None)
            if isinstance(data, dict):
                data = data.get("data")
            if isinstance(data, str) and data[:10] in self.errors:
                name, item = self.errors[data[:10]]
                body = data[10:]
                names = [i["name"] for i in item["inputs"]]
                vals = []
                for i, nm in enumerate(names):
                    chunk = body[i * 64:(i + 1) * 64]
                    if item["inputs"][i]["type"] == "uint256":
                        vals.append(f"{nm}={int(chunk, 16)}")
                    else:
                        vals.append(f"{nm}=0x{chunk[:8]}..{bytes.fromhex(chunk[64-8:])!r}")
                return f"{name}.{item['name']}(" + ", ".join(vals) + ")"
            return str(exc)[:160]

    def balance(self, who: str) -> float:
        return self.usdc.functions.balanceOf(self.roles[who]).call() / 1e6


def main() -> int:
    env = load_env(sys.argv[1] if len(sys.argv) > 1 else None)
    r = Runner(env)
    print(f"chain {r.chain}, core {r.core.address}, gate {r.hook.address}\n")
