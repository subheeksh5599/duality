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


def test_the_refusal_is_decoded_by_us_not_by_the_caller(published):
    """The recorded refusal arrived as raw hex; the record must name what it was."""
    import acp_provider_job as A

    _binding, run = published
    decoded = run["keeperHubHoldSimulation"]["decodedByUs"]
    assert decoded and decoded["reason"] == "E_DISQUALIFIED"
    assert decoded["jobId"] == run["jobId"]
    # and the decoder still reads the same shape out of the raw text
    again = A.decode_release_blocked(run["keeperHubHoldSimulation"]["revertReason"])
    assert again == decoded


def test_decoder_refuses_text_without_the_gate_error():
    import acp_provider_job as A

    assert A.decode_release_blocked("Simulation reverted: execution reverted (no data)") is None
    assert A.decode_release_blocked("") is None
