#!/usr/bin/env python3
"""DUALITY evaluator service.

The control surface behind everything: observe, approve, invalidate, check,
reconcile, release. Every action is a real operation against the deployed
contracts and, for the release, against KeeperHub.

Design rules this file holds to, because they are the product:

  * the decision comes from `EvidenceRegistry.isReleasable` over eth_call, the
    same function the hook runs. There is no second implementation of the
    predicate, so the service and the chain cannot disagree.
  * nothing mutates a stored evidence record. A new observation is a new version.
  * a release always goes through KeeperHub, with simulate first, and the
    simulation's `wouldRevert` is what decides whether it is broadcast at all.
  * every request is logged with a correlation id, and every state change is
    appended to an audit log. The log is never rewritten.

Run:  .venv/bin/python service/duality_service.py [--port 8787]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import keeperhub_release as K  # noqa: E402
from eth_utils import keccak  # noqa: E402

ROOT = K.ROOT
EVENTS = os.path.join(ROOT, "artifacts", "events.jsonl")
ZERO32 = b"\x00" * 32
REASON = {
    "E_NOT_APPROVED": "no approval is bound to this job",
    "E_STALE": "the evidence is past its freshness bound",
    "E_SUPERSEDED": "a newer observation of the same subject exists",
    "E_DISQUALIFIED": "the provider is no longer qualified",
    "E_QUAL_OBSERVATION": "the provider was not qualified when the reading was taken",
    "E_PROVENANCE": "the provenance commitment does not match",
    "E_SUBJECT": "the evidence does not belong to this job",
    "E_ALREADY_SETTLED": "this job has already settled",
    "E_CONDITION": "a job condition no longer holds",
    "OK": "valid at release time",
}
DECISION = {
    "E_STALE": "HOLD",
    "E_DISQUALIFIED": "HOLD",
    "E_QUAL_OBSERVATION": "HOLD",
    "E_PROVENANCE": "HOLD",
    "E_SUBJECT": "HOLD",
    "E_NOT_APPROVED": "HOLD",
    "E_SUPERSEDED": "RECONCILIATION_REQUIRED",
    "E_ALREADY_SETTLED": "SETTLED",
    "OK": "RELEASE",
}


class State:
    def __init__(self, env: dict):
        self.env = env
        self.ch = K.Chain(env)
        self.lock = threading.Lock()
        self.counters: Counter = Counter()
        self.decisions: dict[int, dict] = {}
        self.reconciliations: dict[int, dict] = {}
        self.core_abi = json.load(open(os.path.join(K.ART, "ERC8183.sol", "ERC8183.json"), encoding="utf-8"))["abi"]
        hook_abi = json.load(open(os.path.join(K.ART, "DualityGateHook.sol", "DualityGateHook.json"), encoding="utf-8"))["abi"]
        self.merged_abi = self.core_abi + [x for x in hook_abi if x.get("type") == "error"]
        os.makedirs(os.path.dirname(EVENTS), exist_ok=True)

    # ------------------------------------------------------------------ audit
    def event(self, kind: str, corr: str, **fields) -> dict:
        rec = {"id": str(uuid.uuid4())[:8], "at": int(time.time()), "kind": kind,
               "correlationId": corr, "prev": self._last_id(), **fields}
        with open(EVENTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        return rec

    def _last_id(self) -> str | None:
        try:
            with open(EVENTS, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 4096))
                lines = [l for l in fh.read().decode(errors="replace").splitlines() if l.strip()]
            return json.loads(lines[-1])["id"] if lines else None
        except Exception:  # noqa: BLE001
            return None

    def history(self, limit: int = 200) -> list[dict]:
        if not os.path.exists(EVENTS):
            return []
        out = []
        for line in open(EVENTS, encoding="utf-8"):
            if line.strip():
                out.append(json.loads(line))
        return out[-limit:]

    # -------------------------------------------------------------- the gate
    def predicate(self, job_id: int, now: int | None = None, block_ts: int | None = None) -> dict:
        now = now or int(time.time())
        ok, reason = self.ch.reg.functions.isReleasable(job_id, now).call()
        code = bytes(reason).rstrip(b"\x00").decode(errors="replace") or "OK"
        return {"ok": bool(ok), "reasonCode": code,
                "decision": DECISION.get(code, "HOLD"),
                "explanation": REASON.get(code, "unrecognised reason code"),
                "checkedAt": now,
                "blockTimestamp": block_ts if block_ts is not None
                                  else self.ch.w3.eth.get_block("latest").timestamp}

    def job_view(self, job_id: int, block_ts: int | None = None) -> dict:
        j = self.ch.core.functions.getJob(job_id).call()
        eid = self.ch.reg.functions.approval(job_id).call()[0]
        ev = self.ch.reg.functions.getEvidence(eid).call() if eid != ZERO32 else None
        status = {0: "Open", 1: "Funded", 2: "Submitted", 3: "Completed", 4: "Rejected", 5: "Expired"}[j[1]]
        view = {"jobId": job_id, "status": status, "client": j[0], "provider": j[2],
                "evaluator": j[4], "budget": str(j[6]), "hook": j[7], "expiredAt": j[3],
                "settled": self.ch.reg.functions.settled(job_id).call()}
        if ev:
            view["evidence"] = {
                "evidenceId": "0x" + bytes(ev[0]).hex(), "version": ev[7], "observedAt": ev[8],
                "freshnessBound": ev[9], "expiresAt": ev[8] + ev[9],
                "expired": int(time.time()) > ev[8] + ev[9],
                "qualificationAtObservation": ev[11], "status": ev[13],
                "providerQualificationNow": self.ch.reg.functions.qualification(j[2]).call()[0],
            }
        view["decision"] = self.predicate(job_id, block_ts=block_ts)
        return view

    def list_jobs(self, ids=None, ttl: float = 20.0) -> list[dict]:
        """Bounded recent window, built in parallel, cached for a moment.

        Sequentially this was ~150 round trips and took 98 seconds, which made
        the control surface look broken when it was only slow.
        """
        with self.lock:
            c = getattr(self, "_jobs_cache", None)
            if c and time.time() - c["at"] < ttl:
                return c["data"]
        ids = ids or list(range(1, self.ch.core.functions.jobCounter().call() + 1))[-26:]
        block_ts = self.ch.w3.eth.get_block("latest").timestamp
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=10) as pool:
            views = list(pool.map(lambda i: self.job_view(i, block_ts), ids))
        views = [v for v in views if v]
        with self.lock:
            self._jobs_cache = {"at": time.time(), "data": views}
        return views

    # ------------------------------------------------------------- actions
    def _next_version(self, job_id: int) -> int:
        return self.ch.reg.functions.currentVersion(job_id, keccak(text="subject")).call() + 1

    def observe(self, job_id: int, bound: int, corr: str) -> dict:
        """Commit a new evidence version. Never overwrites a stored one."""
        version = self._next_version(job_id)
        content = keccak(text=f"observation-{job_id}-v{version}-{int(time.time())}")
        eid = keccak(self.ch.w3.codec.encode(["uint256", "address", "bytes32", "uint64"],
                    [job_id, self.ch.addr["provider"], content, version]))
        rc, tx = self.ch.send(self.ch.reg.functions.commit(
            K._ev(eid, job_id, version, content, bound, int(time.time()), self.ch)), "deployer",
            f"  observe v{version}")
        self.counters["observations"] += 1
        return self.event("evidence_observed", corr, jobId=job_id, version=version,
                          freshnessBound=bound, evidenceId="0x" + eid.hex(), tx=tx)

    def approve(self, job_id: int, corr: str) -> dict:
        eid = self.ch.reg.functions.approval(job_id).call()[0]
        latest = self._latest_version_evidence(job_id)
        rc, tx = self.ch.send(self.ch.reg.functions.approve(
            job_id, latest, keccak(text=f"evaluation-{job_id}-{int(time.time())}")), "deployer", "  approve")
        self.counters["approvals"] += 1
        return self.event("evidence_approved", corr, jobId=job_id, evidenceId="0x" + latest.hex(), tx=tx)

    def _latest_version_evidence(self, job_id: int) -> bytes:
        subject = keccak(text="subject")
        ver = self.ch.reg.functions.currentVersion(job_id, subject).call()
        return self.ch.reg.functions.versionEvidence(job_id, subject, ver).call()

    def invalidate(self, job_id: int, kind: str, corr: str) -> dict:
        """The mutation control. Each class changes real state, and nothing else."""
        if kind == "stale":
            # staleness is a property of time, not a state change we can write. The
            # honest way to force it is to observe a one second window, approve it,
            # and let it lapse, so the approved evidence is the current version and
            # clause 5 is what fails.
            self.observe(job_id, 1, corr)
            self.approve(job_id, corr)
            time.sleep(2)
            tx = "0x" + "0" * 64
            self.counters["mutations_stale"] += 1
        elif kind == "supersede":
            ver = self._next_version(job_id)
            content = keccak(text=f"superseding-{job_id}-v{ver}")
            eid = keccak(self.ch.w3.codec.encode(["uint256", "address", "bytes32", "uint64"],
                        [job_id, self.ch.addr["provider"], content, ver]))
            rc, tx = self.ch.send(self.ch.reg.functions.commit(
                K._ev(eid, job_id, ver, content, 3600, int(time.time()), self.ch)), "deployer",
                f"  mutate: supersede with v{ver}")
            self.counters["mutations_superseded"] += 1
        elif kind == "disqualify":
            rc, tx = self.ch.send(self.ch.reg.functions.setQualification(self.ch.addr["provider"], 3),
                                  "deployer", "  mutate: revoke provider qualification")
            self.counters["mutations_disqualified"] += 1
        else:
            raise ValueError(f"unknown invalidation class {kind}")
        return self.event("fact_mutated", corr, jobId=job_id, mutationKind=kind, tx=tx)

    def check(self, job_id: int, corr: str) -> dict:
        v = self.predicate(job_id)
        self.counters["checks"] += 1
        self.counters[f"decision_{v['decision']}"] += 1
        self.decisions[job_id] = v
        self.event("release_checked", corr, jobId=job_id, **v)
        return v

    def release(self, job_id: int, corr: str) -> dict:
        """Simulate through KeeperHub, then broadcast only if the gate allows it."""
        verdict = self.predicate(job_id)
        body = {"contractAddress": self.ch.dep["core"], "network": K.NETWORK,
                "abi": json.dumps(self.merged_abi), "functionName": "complete",
                "functionArgs": json.dumps([str(job_id), "0x" + keccak(text="approved").hex(), "0x"])}
        st, sim = K.kh(self.env, "POST", "/api/execute/contract-call", dict(body, simulate=True),
                       idem=f"duality-sim-{job_id}-{int(time.time())}")
        if sim.get("wouldRevert"):
            self.counters["held"] += 1
            return self.event("release_held", corr, jobId=job_id, via="keeperhub",
                              wouldRevert=True, keeperHubReason=str(sim.get("revertReason"))[:240],
                              reasonCode=verdict["reasonCode"], decision=verdict["decision"])
        st2, sent = K.kh(self.env, "POST", "/api/execute/contract-call", body,
                         idem=f"duality-release-{job_id}-{int(time.time())}")
        exid = sent.get("executionId")
        link = sent.get("transactionLink")
        for _ in range(30):
            if not exid:
                break
            _s, status = K.kh(self.env, "GET", f"/api/execute/{exid}/status")
            if status.get("status") in ("completed", "failed"):
                link = status.get("transactionLink") or link
                sent = {**sent, **status}
                break
            time.sleep(3)
        self.counters["released"] += 1
        return self.event("released", corr, jobId=job_id, via="keeperhub", executionId=exid,
                          status=sent.get("status"), transactionLink=link)

    def _writes_visible(self, job_id: int) -> bool:
        """True once the approval binds the current version, i.e. our writes are readable."""
        approved = self.ch.reg.functions.approval(job_id).call()[0]
        subject = keccak(text="subject")
        ver = self.ch.reg.functions.currentVersion(job_id, subject).call()
        current = self.ch.reg.functions.versionEvidence(job_id, subject, ver).call()
        return approved == current

    def _settled_read(self, job_id: int, tries: int = 6, pause: float = 2.0) -> dict:
        """Read the predicate only after our own writes are visible.

        Reading straight after a write can return the previous state, because a
        node can confirm a transaction before the view it serves reflects it. We
        saw that make a successful reconciliation report as a failure, so the
        counter waits for the write to be readable before it believes the answer.
        """
        for _ in range(tries):
            if self._writes_visible(job_id):
                return self.predicate(job_id)
            time.sleep(pause)
        return self.predicate(job_id)

    def reconcile(self, job_id: int, corr: str) -> dict:
        """Re-observe the minimum fact, then re-run the same predicate."""
        before = self.predicate(job_id)
        if before["ok"]:
            return self.event("reconciliation_skipped", corr, jobId=job_id, reason="already valid")
        rec = self.observe(job_id, 3600, corr)
        ver = rec["version"]
        self.approve(job_id, corr)
        after = self._settled_read(job_id)
        rec = self.event("reconciled", corr, jobId=job_id, before=before["reasonCode"],
                         after=after["reasonCode"], replacementVersion=ver, ok=after["ok"])
        self.reconciliations[job_id] = {"before": before, "after": after, "event": rec["id"]}
        self.counters["reconciliations"] += 1
        self.counters["reconciliation_succeeded"] += 1 if after["ok"] else 0
        return rec


STATE: State | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "duality/0.1"

    def log_message(self, fmt, *args):
        print(f"  [{self.corr[:8]}] " + fmt % args, file=sys.stderr)

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload, indent=2, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Correlation-Id", self.corr)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: str):
        body = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self.corr = uuid.uuid4().hex
        try:
            if self.path in ("/", "/index.html"):
                ui = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
                return self._send_html(ui)
            if self.path == "/health":
                return self._send(200, {"ok": True, "chainId": STATE.ch.dep["chainId"],
                                        "core": STATE.ch.dep["core"], "counters": dict(STATE.counters)})
            if self.path == "/events":
                return self._send(200, {"events": STATE.history()})
            if self.path.startswith("/jobs/"):
                job_id = int(self.path.split("/")[2])
                return self._send(200, STATE.job_view(job_id))
            if self.path == "/jobs":
                return self._send(200, {"jobs": STATE.list_jobs()})
        except Exception as exc:  # noqa: BLE001
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}", "correlationId": self.corr})
        return self._send(404, {"error": "not found", "correlationId": self.corr})

    def do_POST(self):  # noqa: N802
        self.corr = uuid.uuid4().hex
        try:
            parts = self.path.strip("/").split("/")
            if len(parts) < 3 or parts[0] != "jobs":
                return self._send(404, {"error": "not found", "correlationId": self.corr})
