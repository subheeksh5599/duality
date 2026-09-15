"""The HTTP surface: the routes the control surface actually calls.

The service is started on an ephemeral port in-process, so these are requests
against the real handler with the real chain behind it.
"""
from __future__ import annotations

from conftest import raw_request


def test_health_reports_the_deployment_and_folded_counters(http):
    status, body = http("/health")
    assert status == 200
    assert body["ok"] is True
    assert body["chainId"] == 84532
    assert body["core"].startswith("0x")
    # counters are folded from the audit log, so they must be present even cold
    for key in ("held", "released", "reconciliations", "observations", "approvals", "checks"):
        assert key in body["counters"]


def test_jobs_list_is_a_bounded_window(http):
    status, body = http("/jobs")
    assert status == 200
    jobs = body["jobs"]
    assert 0 < len(jobs) <= 26
    assert all("jobId" in j and "decision" in j for j in jobs)


def test_job_read_carries_the_decision_and_the_skew(http):
    status, body = http("/jobs/22")
    assert status == 200
    assert body["status"] == "Completed"
    assert body["decision"]["reasonCode"] == "E_ALREADY_SETTLED"
    assert body["decision"]["skew"]["bound"] == 120


def test_check_returns_a_decision_and_is_recorded(svc):
    verdict = svc.STATE.check(22, "test-correlation")
    assert verdict["reasonCode"] == "E_ALREADY_SETTLED"
    assert verdict["decision"] == "SETTLED"
    # the audit log is the record: the check must be readable back out of it
    rows = [e for e in svc.STATE.history(limit=2000)
            if e.get("kind") == "release_checked" and e.get("correlationId") == "test-correlation"]
    assert rows and rows[-1]["reasonCode"] == "E_ALREADY_SETTLED"


def test_unknown_action_is_not_found(http):
    status, body = http("/jobs/22/not-an-action", method="POST")
    assert status == 404
    assert "unknown action" in body["error"]


def test_non_job_paths_are_not_found(http):
    status, _ = http("/nothing/here", method="POST")
    assert status == 404


def test_static_pages_and_design_system_are_served(http):
    for path in ("/", "/dashboard", "/duality.css", "/dashboard.js", "/chain-config.json"):
        status, _ = http(path)
        assert status == 200, path


def test_assets_outside_the_allowlist_are_refused(base_url):
    """The allowlist is the guard: a crafted path must not walk out of the directory.

    urllib would normalise `/../` away, which is why the request is sent by hand.
    """
    host_port = ("127.0.0.1", int(base_url.rsplit(":", 1)[1]))
    assert " 404 " in raw_request(host_port, "/../.env")
    assert " 404 " in raw_request(host_port, "/../duality_service.py")
    assert " 404 " in raw_request(host_port, "/fonts/../../.env")


def test_correlation_id_is_echoed(base_url):
    import urllib.request

    with urllib.request.urlopen(base_url + "/health", timeout=60) as resp:
        assert resp.headers.get("X-Correlation-Id")
