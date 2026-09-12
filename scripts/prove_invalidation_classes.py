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
