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

# The web assets are addressed through an allowlist rather than a path join, so
# a crafted request cannot walk out of this directory.
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/dashboard": ("dashboard.html", "text/html; charset=utf-8"),
    "/dashboard.html": ("dashboard.html", "text/html; charset=utf-8"),
    "/duality.css": ("duality.css", "text/css; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
    # the static build loads these only when no service answers, so the local
    # page does not pull a 500 KB library it will not use
    "/chain.js": ("chain.js", "text/javascript; charset=utf-8"),
    "/abis.json": ("abis.json", "application/json"),
    "/chain-config.json": ("chain-config.json", "application/json"),
    "/audit-log.json": ("audit-log.json", "application/json"),
    "/vendor/ethers.umd.min.js": ("vendor/ethers.umd.min.js", "text/javascript; charset=utf-8"),
}
FONTS = {
    "Geist-Regular.woff2", "Geist-Medium.woff2", "Geist-SemiBold.woff2",
    "GeistMono-Regular.woff2", "GeistMono-Medium.woff2",
}
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
    "E_HASH_MISMATCH": "the deliverable does not match the committed content hash",
    "E_INVALIDATED": "the evidence was invalidated: disputed or unrecoverable",
    "OK": "valid at release time",
}
DECISION = {
    "E_STALE": "HOLD",
    "E_DISQUALIFIED": "HOLD",
    "E_QUAL_OBSERVATION": "HOLD",
    "E_PROVENANCE": "HOLD",
    "E_SUBJECT": "HOLD",
    "E_NOT_APPROVED": "HOLD",
    "E_HASH_MISMATCH": "HOLD",
    "E_INVALIDATED": "HOLD",
    "E_CONDITION": "HOLD",
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
        # one implementation of the decoding shape, shared with the scripts: the hook's
        # errors appended to the core's ABI, because the hosted API does not yet honour
        # the `errorAbis` field that exists for exactly this
        self.merged_abi = json.loads(K.decoding_abi())
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

    def recorded(self) -> dict:
        """Totals folded from the audit log rather than from process memory.

        The log is the record; the counters in memory are not. Reading them from
        memory meant a restarted service showed zeros next to a log full of
        state changes, which reads as a broken surface.
        """
        k = Counter(e.get("kind") for e in self.history(limit=100000))
        rec = {
            "held": k["release_held"],
            "released": k["released"],
            "release_failed": k["release_failed"],
            "reconciliations": k["reconciled"],
            "observations": k["evidence_observed"],
            "approvals": k["evidence_approved"],
            "checks": k["release_checked"],
            "mutations": k["fact_mutated"],
        }
        rec["reconciliation_succeeded"] = sum(
            1 for e in self.history(limit=100000) if e.get("kind") == "reconciled" and e.get("ok"))
        return rec

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
        """The service's decision, taken from the chain's own function.

        `isReleasableAt` rather than `isReleasable`, because the registry carries a
        maxSkew bound and a decision taken from a clock far away from chain time is
        not one this service should act on. The bound was declared on-chain from the
        first deployment and nothing read it until now; a service whose clock has
        drifted refuses rather than guesses, and the refusal names clause 9's reason
        code instead of reporting a state the chain never showed.
        """
        now = now or int(time.time())
        chain_ts = block_ts if block_ts is not None else self.ch.w3.eth.get_block("latest").timestamp
        ok, reason = self.ch.reg.functions.isReleasableAt(job_id, now, chain_ts).call()
        code = bytes(reason).rstrip(b"\x00").decode(errors="replace") or "OK"
        skew_bound = self.ch.reg.functions.maxSkew().call()
        return {"ok": bool(ok), "reasonCode": code,
                "decision": DECISION.get(code, "HOLD"),
                "explanation": REASON.get(code, "unrecognised reason code"),
                "checkedAt": now,
                "skew": {"evaluator": now, "chain": chain_ts, "delta": abs(now - chain_ts),
                         "bound": skew_bound},
                "blockTimestamp": chain_ts}

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
        """Ask the rail to release, and report only what actually happened.

        Two gates before anything is broadcast, because each has been measured wrong on
        its own: this service's reading of the predicate, which is the chain's own
        function, and the rail's simulation of the same call.

        Both are needed. Trusting the simulation alone produced a false success in the
        record: the rail answered from a node whose view lagged the revocation, reported
        the release as clean, the broadcast was then refused by the gate, and this method
        wrote `released` with no execution id and no status - a lie in the log the rest of
        this project treats as the record. Trusting the predicate alone would drop the
        rail's second opinion, which is the thing that can see a block this process has
        not read.

        A broadcast that produces no execution is therefore recorded as a failure, not as
        a release, and a terminal failure is recorded as one too.
        """
        verdict = self.predicate(job_id)
        body = {"contractAddress": self.ch.dep["core"], "network": K.NETWORK,
                "abi": json.dumps(self.merged_abi), "functionName": "complete",
                # the gate is a hook, so without this a refusal comes back as hex and the
                # control surface shows a reader "unknown custom error" instead of the
                # reason code the gate actually returned
                "errorAbis": K.gate_error_abis(),
                "functionArgs": json.dumps([str(job_id), "0x" + keccak(text="approved").hex(), "0x"])}
        # The rail is asked first even when this service already refuses, because its
        # simulation is the evidence that the refusal is the gate's and not this process's
        # opinion - it names the error the hook raised, with the job id and the reason
        # code. Short-circuiting on our own verdict would be cheaper and would throw that
        # away.
        st, sim = K.kh(self.env, "POST", "/api/execute/contract-call", dict(body, simulate=True),
                       idem=f"duality-sim-{job_id}-{int(time.time())}")
        if sim.get("wouldRevert"):
            self.counters["held"] += 1
            return self.event("release_held", corr, jobId=job_id, via="keeperhub",
                              wouldRevert=True, keeperHubReason=str(sim.get("revertReason"))[:240],
                              reasonCode=verdict["reasonCode"], decision=verdict["decision"])
        if not verdict["ok"]:
            # The rail's view lagged: it offered to release what this service's reading of
            # the chain refuses. Nothing is broadcast on one clean simulation - that is the
            # path that produced a false success in this log once already.
            self.counters["held"] += 1
            return self.event("release_held", corr, jobId=job_id, via="predicate",
                              wouldRevert=False, railSimulationClean=True,
                              reasonCode=verdict["reasonCode"], decision=verdict["decision"],
                              explanation=verdict["explanation"])
        st2, sent = K.kh(self.env, "POST", "/api/execute/contract-call", body,
                         idem=f"duality-release-{job_id}-{int(time.time())}")
        exid = sent.get("executionId")
        if not exid:
            self.counters["release_failed"] += 1
            return self.event("release_failed", corr, jobId=job_id, via="keeperhub",
                              httpStatus=st2, reasonCode=verdict["reasonCode"],
                              error=str(sent.get("error") or sent)[:300])
        link = sent.get("transactionLink")
        status = sent.get("status")
        for _ in range(30):
            _s, polled = K.kh(self.env, "GET", f"/api/execute/{exid}/status")
            status = polled.get("status") or status
            link = polled.get("transactionLink") or link
            sent = {**sent, **polled}
            if status in ("completed", "failed"):
                break
            time.sleep(3)
        if status == "failed":
            self.counters["release_failed"] += 1
            return self.event("release_failed", corr, jobId=job_id, via="keeperhub",
                              executionId=exid, status=status, reasonCode=verdict["reasonCode"],
                              error=str(sent.get("error"))[:300])
        self.counters["released"] += 1
        return self.event("released", corr, jobId=job_id, via="keeperhub", executionId=exid,
                          status=status, transactionLink=link)

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
        # A predicate answer is a statement about a block. A browser told otherwise will
        # heuristically cache this and re-render a verdict from before the write that
        # changed it, which is the precise failure this project exists to refuse - and it
        # was found here by filming it: the page said RELEASE while the service said
        # E_SUPERSEDED, and only the page was reading stale bytes.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Correlation-Id", self.corr)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str, ctype: str):
        body = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if ctype.startswith("font/"):
            self.send_header("Cache-Control", "public, max-age=604800, immutable")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self.corr = uuid.uuid4().hex
        try:
            if self.path in STATIC:
                name, ctype = STATIC[self.path]
                return self._send_file(os.path.join(WEB, name), ctype)
            if self.path.startswith("/fonts/"):
                name = self.path[len("/fonts/"):]
                if name in FONTS:
                    return self._send_file(os.path.join(WEB, "fonts", name), "font/woff2")
                return self._send(404, {"error": "not found", "correlationId": self.corr})
            if self.path == "/health":
                return self._send(200, {"ok": True, "chainId": STATE.ch.dep["chainId"],
                                        "core": STATE.ch.dep["core"], "counters": STATE.recorded()})
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
            job_id, action = int(parts[1]), parts[2]
            if action == "check":
                return self._send(200, STATE.check(job_id, self.corr))
            if action == "observe":
                return self._send(200, STATE.observe(job_id, 3600, self.corr))
            if action == "approve":
                return self._send(200, STATE.approve(job_id, self.corr))
            if action == "invalidate":
                return self._send(200, STATE.invalidate(job_id, "supersede", self.corr))
            if action == "invalidate-stale":
                return self._send(200, STATE.invalidate(job_id, "stale", self.corr))
            if action == "invalidate-disqualify":
                return self._send(200, STATE.invalidate(job_id, "disqualify", self.corr))
            if action == "reconcile":
                return self._send(200, STATE.reconcile(job_id, self.corr))
            if action == "release":
                return self._send(200, STATE.release(job_id, self.corr))
            return self._send(404, {"error": f"unknown action {action}", "correlationId": self.corr})
        except Exception as exc:  # noqa: BLE001
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}", "correlationId": self.corr})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--env", default=None, help="env file; defaults to $DUALITY_ENV or ./.env")
    args = ap.parse_args()
    global STATE
    STATE = State(K.load_env(args.env))
    print(f"DUALITY service on :{args.port}  chain {STATE.ch.dep['chainId']}")
    print(f"  core {STATE.ch.dep['core']}  gate {STATE.ch.dep['gateHook']}")
    # warm the list cache off the request path, so the first page load is fast
    threading.Thread(target=lambda: (STATE.list_jobs(), print("  list cache warm")), daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())