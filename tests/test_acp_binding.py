"""The ACP lane's published binding, checked against the chain.

The claim this file tests is the one a reader should be able to attack: that the
commitments in the two published artifacts are the same values the registry holds,
and that the escrow paid the agent the artifacts name. Nothing is taken from the
script that produced them.
"""
from __future__ import annotations

import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BINDING = os.path.join(ROOT, "artifacts", "acp-agent-binding.json")
RUN = os.path.join(ROOT, "artifacts", "acp-provider-job.json")


@pytest.fixture(scope="module")
def published() -> tuple[dict, dict]:
    return (json.load(open(BINDING, encoding="utf-8")),
            json.load(open(RUN, encoding="utf-8")))


def test_binding_and_run_record_are_present_and_complete(published):
    binding, run = published
    for key in ("agentId", "agentWallet", "registry", "chainId", "network"):
        assert binding.get(key), key
    for key in ("jobId", "evidenceId", "subject", "provenanceHash", "envelope", "readback"):
        assert run.get(key), key


def test_subject_re_derives_from_the_binding_file(published, chain):
    import acp_provider_job as A
    from eth_utils import keccak

    binding, run = published
    derived = keccak(A.canonical(binding))
    stored = chain.reg.functions.getEvidence(bytes.fromhex(run["evidenceId"][2:])).call()
    assert derived == bytes(stored[3]), "evidence subject is not the hash of the published binding"
    assert derived.hex() == run["subject"][2:]


def test_provenance_re_derives_from_the_envelope(published, chain):
    import acp_provider_job as A
    from eth_utils import keccak

    _binding, run = published
    derived = keccak(A.canonical(run["envelope"]))
    stored = chain.reg.functions.getEvidence(bytes.fromhex(run["evidenceId"][2:])).call()
    assert derived == bytes(stored[6]), "evidence provenance hash is not the published envelope's hash"
    assert derived.hex() == run["provenanceHash"][2:]


def test_the_escrow_paid_the_wallet_the_binding_names(published, chain):
    binding, run = published
    job = chain.core.functions.getJob(run["jobId"]).call()
    assert job[12].lower() == binding["agentWallet"].lower(), "payoutReceiver is not the bound agent"
    assert chain.reg.functions.settled(run["jobId"]).call() is True
    assert run["readback"]["payoutReceiver"].lower() == binding["agentWallet"].lower()


def test_the_run_recorded_a_refusal_before_the_payment(published):
    _binding, run = published
    hold = run["keeperHubHoldSimulation"]
    assert hold["wouldRevert"] is True
    assert run["keeperHubRelease"]["status"] == "completed"
    assert run["predicateWhileDisqualified"] == "E_DISQUALIFIED"


def test_the_refusal_is_named_and_carries_the_reason_code(published):
    """A refusal that is not named is not a refusal a caller can act on.

    Which side names it depends on the shape the rail honours: once the gate's errors
    travel with the call, KeeperHub names it and there is no hex left to decode here.
    Asserting a particular side would pin this to the rail's deployment schedule, so
    the assertion is the invariant - observed, named, and carrying the reason code.
    """
    _binding, run = published
    hold = run["keeperHubHoldSimulation"]
    assert hold["wouldRevert"] is True, "the release was not refused"
    named_by_us, named_by_rail = hold.get("decodedByUs"), hold.get("decodedByKeeperHub")
    assert named_by_us or named_by_rail, "the refusal was observed but never named"
    if named_by_us:
        assert named_by_us["reason"] == "E_DISQUALIFIED"
        assert named_by_us["jobId"] == run["jobId"]
    else:
        # the rail names the error and prints the bytes32 argument as hex, so the
        # reason code is checked by decoding the same field the gate wrote
        hexed = named_by_rail.split("0x")[-1].rstrip(")")
        decoded_reason = bytes.fromhex(hexed[:64]).rstrip(b"\x00").decode()
        assert decoded_reason == "E_DISQUALIFIED", named_by_rail


def test_decoder_reads_a_raw_refusal_and_refuses_anything_else():
    """The local decoder is the fallback for a caller the rail could not name for."""
    import acp_provider_job as A

    job, reason = 21, b"E_DISQUALIFIED"
    raw = ("Simulation reverted: execution reverted (unknown custom error) (data=\"0x5192a3c5"
           + hex(job)[2:].rjust(64, "0")
           + reason.hex().ljust(64, "0") + "\")")
    decoded = A.decode_release_blocked(raw)
    assert decoded == {"error": "ReleaseBlocked(uint256,bytes32)", "jobId": job,
                       "reason": "E_DISQUALIFIED"}
    assert A.decode_release_blocked("Simulation reverted: execution reverted (no data)") is None
    assert A.decode_release_blocked("") is None
