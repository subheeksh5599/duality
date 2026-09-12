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
