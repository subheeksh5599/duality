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
