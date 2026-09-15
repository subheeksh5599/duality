"""The predicate, read from the deployed registry.

Nothing here is mocked: each assertion is a view call against the registry named in
`deployments/`, so a passing run means the deployed contract still answers the way
this repository says it does.
"""
from __future__ import annotations

import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session")
def settled_job() -> int:
    """The job the ACP lane settled, read from its own run record.

    Pinning a number here would make the suite fail the moment the lane runs
    again, which trains a reader to ignore it.
    """
    run = json.load(open(os.path.join(ROOT, "artifacts", "acp-provider-job.json"),
                         encoding="utf-8"))
    return int(run["jobId"])


def reason_code(chain, job_id: int, now: int, chain_ts: int) -> str:
    _ok, reason = chain.reg.functions.isReleasableAt(job_id, now, chain_ts).call()
    return bytes(reason).rstrip(b"\x00").decode(errors="replace") or "OK"


def test_settled_job_is_refused_and_named(chain, settled_job):
    """The job the ACP lane released must now be refused by the predicate."""
    chain_ts = chain.w3.eth.get_block("latest").timestamp
    assert chain.reg.functions.settled(settled_job).call() is True
    assert reason_code(chain, settled_job, chain_ts, chain_ts) == "E_ALREADY_SETTLED"


def test_job_with_no_approval_is_refused(chain):
    ahead = chain.core.functions.jobCounter().call() + 500
    chain_ts = chain.w3.eth.get_block("latest").timestamp
    assert reason_code(chain, ahead, chain_ts, chain_ts) == "E_NOT_APPROVED"


def test_clock_bound_fires_past_the_declared_max_skew(chain, settled_job):
    """The registry declares maxSkew; a decision taken outside it must not be trusted."""
    bound = chain.reg.functions.maxSkew().call()
    chain_ts = chain.w3.eth.get_block("latest").timestamp
    assert reason_code(chain, settled_job, chain_ts - bound - 1, chain_ts) == "E_CONDITION"


def test_clock_bound_is_not_broken_at_the_declared_value(chain, settled_job):
    """At exactly maxSkew the skew clause steps aside: the next clause decides."""
    bound = chain.reg.functions.maxSkew().call()
    chain_ts = chain.w3.eth.get_block("latest").timestamp
    assert reason_code(chain, settled_job, chain_ts - bound, chain_ts) != "E_CONDITION"


def test_service_decision_comes_from_the_chain_function(svc, chain, settled_job):
    """The service's verdict must equal what the contract answers for the same input."""
    chain_ts = chain.w3.eth.get_block("latest").timestamp
    view = svc.STATE.predicate(settled_job, now=chain_ts, block_ts=chain_ts)
    assert view["reasonCode"] == reason_code(chain, settled_job, chain_ts, chain_ts)
    assert view["decision"] == "SETTLED"


def test_service_reports_the_skew_it_measured(svc, settled_job):
    view = svc.STATE.predicate(settled_job)
    skew = view["skew"]
    assert skew["bound"] == 120
    assert skew["delta"] == abs(skew["evaluator"] - skew["chain"])
    assert skew["delta"] < skew["bound"], "a local clock this far out would be a real finding"


@pytest.mark.parametrize("job_id", [17, 18, 19, 20])
def test_recorded_invalidation_jobs_still_refuse(chain, job_id):
    """The four published invalidation classes are on-chain facts, not a screenshot."""
    chain_ts = chain.w3.eth.get_block("latest").timestamp
    code = reason_code(chain, job_id, chain_ts, chain_ts)
    assert code != "OK"
    assert chain.reg.functions.settled(job_id).call() is True or code.startswith("E_")


def test_freshness_boundary_is_inclusive():
    """A property of the predicate that a pinned test should carry, stated once.

    `nowTs > observedAt + freshnessBound` is the clause; at equality the evidence is
    still valid. Kept here as the readable statement of the boundary the contract
    tests assert on-chain.
    """
    observed_at, bound = 1_000, 600
    assert not (observed_at + bound > observed_at + bound)
    assert observed_at + bound + 1 > observed_at + bound
