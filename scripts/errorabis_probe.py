#!/usr/bin/env python3
"""What the hosted execution API does with the merged `errorAbis` field, and what
shape actually makes a hook's refusal decode.

Why this is its own artifact rather than a sentence in the README: DUALITY's refusals
are raised inside a hook, so whether the caller can see the reason code depends
entirely on which ABI the request carries. KeeperHub merged a field for exactly this
(#2457, documented in #2472), and this project consumes it. Whether the HOSTED API
honours it is a separate question from whether the code contains it, so it is measured
rather than assumed.

Two experiments, both against the live API:

  1. Field validity. The field's documented contract says a document whose errors the
     decoder cannot build is rejected with a 400 rather than accepted and ignored, and
     that at most four documents are allowed. So an empty document, a non-ABI string
     and five documents must all be REJECTED if the field is read at all.

  2. Decode shape. One job whose `complete()` reverts inside the hook, asked three
     ways: the call target's ABI alone, that ABI plus `errorAbis`, and the hook's
     errors appended to the target's ABI in `abi` itself. Whichever names
     `ReleaseBlocked` is what works today.

Usage:
    python scripts/errorabis_probe.py [--job <id>] [env-file]
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import keeperhub_release as K  # noqa: E402

OUT = os.path.join(K.ROOT, "artifacts", "errorabis-probe.json")
RELEASE_BLOCKED = "5192a3c5"


def main() -> int:
    argv = sys.argv[1:]
    job_id = None
    if "--job" in argv:
        job_id = int(argv[argv.index("--job") + 1])
    env = K.load_env(None)
    ch = K.Chain(env)
    if job_id is None:
        run = os.path.join(K.ROOT, "artifacts", "acp-provider-job.json")
        job_id = int(json.load(open(run, encoding="utf-8"))["jobId"]) if os.path.exists(run) else None
    if job_id is None:
        raise SystemExit("no job to probe: pass --job <id>")

    core_abi = K.Chain.abi("ERC8183.sol", "ERC8183")
    gate_errors = json.loads(K.gate_error_abis()[0])
    call = {"contractAddress": ch.dep["core"], "network": K.NETWORK, "functionName": "complete",
            "functionArgs": json.dumps([str(job_id), "0x" + "ab" * 32, "0x"]), "simulate": True}

    record: dict = {"network": K.NETWORK, "core": ch.dep["core"], "jobId": job_id,
                    "probedAt": int(time.time())}
    print(f"probing the hosted API with `complete()` on job {job_id}\n")

    print("1. is the field read at all?")
    validity = []
    for label, extra, should_reject in [
        ("control: no errorAbis", {}, False),
        ("empty document", {"errorAbis": ["[]"]}, True),
        ("a document that is not an ABI", {"errorAbis": ["not an abi"]}, True),
        ("five documents, over the documented cap of four", {"errorAbis": [json.dumps(gate_errors)] * 5}, True),
    ]:
        _status, body = K.kh(env, "POST", "/api/execute/contract-call", {**call, **extra},
                             idem=f"duality-validity-{job_id}-{len(validity)}")
        message = str(body.get("error") or body.get("message") or body.get("revertReason") or "")[:220]
        rejected = "errorabis" in message.lower() or "abi document" in message.lower()
        validity.append({"label": label, "httpStatus": _status, "message": message,
                         "expectedRejectedIfFieldIsRead": should_reject,
                         "rejected": rejected,
                         "behavedAsThoughFieldAbsent": should_reject and not rejected})
        print(f"   {_status}  {label}: {'rejected' if rejected else 'accepted'}")
    record["fieldValidity"] = validity
    read_at_all = any(v["rejected"] for v in validity if v["expectedRejectedIfFieldIsRead"])
    record["hostedApiReadsErrorAbis"] = read_at_all
    print(f"   -> the field is {'read' if read_at_all else 'accepted and ignored'}\n")

    print("2. which shape makes a hook's refusal decode?")
    shapes = []
    for label, extra in [
        ("abi = the call target's ABI alone", {"abi": json.dumps(core_abi)}),
        ("abi = the target's, errorAbis = the hook's errors",
         {"abi": json.dumps(core_abi), "errorAbis": [json.dumps(gate_errors)]}),
        ("abi = the target's with the hook's errors appended",
         {"abi": K.decoding_abi()}),
    ]:
        _status, body = K.kh(env, "POST", "/api/execute/contract-call", {**call, **extra},
                             idem=f"duality-shape-{job_id}-{len(shapes)}")
        reason = str(body.get("revertReason") or body.get("error") or "")
        shapes.append({"label": label, "httpStatus": _status, "revertReason": reason[:400],
                       "decoded": "ReleaseBlocked" in reason})
        print(f"   {_status}  {label}: decoded={shapes[-1]['decoded']}")
        if not shapes[-1]["decoded"] and RELEASE_BLOCKED not in reason:
            print("        (and the raw refusal does not carry the gate's selector either, so "
                  "this job's refusal may be raised elsewhere)")
    record["decodeShapes"] = shapes

    hook_refusal = any(RELEASE_BLOCKED in s["revertReason"] for s in shapes)
    decoding_shape = next((s["label"] for s in shapes if s["decoded"]), None)
    if not hook_refusal:
        record["verdict"] = ("job {0}'s refusal is not raised inside the hook, so the shape "
                             "comparison is inconclusive: pick a job whose complete() is "
                             "refused by the gate".format(job_id))
    else:
        record["decodingShape"] = decoding_shape
        record["verdict"] = (f"the hosted API does not honour errorAbis, and a hook refusal "
                             f"decodes only through the shape: {decoding_shape}"
                             if not read_at_all and decoding_shape else
                             f"decoded through: {decoding_shape}")
    json.dump(record, open(OUT, "w", encoding="utf-8"), indent=2, sort_keys=True)
    print(f"\nVERDICT: {record['verdict']}")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
