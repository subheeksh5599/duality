"""Every reason code the registry can return must be named by the service.

A registry constant the service has no wording for surfaces to a reader as
"unrecognised reason code", which is the same class of defect as a stale comment:
the contract can return it and the interface cannot explain it. This test reads the
codes off the deployed contract rather than from a list maintained here, so a code
added on-chain fails this suite until it is named.
"""
from __future__ import annotations

import pytest


def registry_constant_names(chain) -> list[str]:
    """Public bytes32 constants, matched on shape rather than on an exact dict.

    The compiled ABI carries an `internalType` alongside each output, so an equality
    check against a hand-written dict matches nothing and the suite silently tests
    an empty list.
    """
    names = []
    for item in chain.reg.abi:
        outputs = item.get("outputs") or []
        if (item.get("type") == "function" and item.get("stateMutability") == "view"
                and not item.get("inputs") and len(outputs) == 1
                and outputs[0].get("type") == "bytes32"):
            names.append(item["name"])
    return sorted(names)


def test_registry_exposes_the_reason_constants(chain):
    names = registry_constant_names(chain)
    assert "E_STALE" in names and "OK" in names
    assert len(names) >= 10


def test_every_reason_code_has_wording_and_a_decision(svc, chain):
    import duality_service as S

    missing_wording, missing_decision, codes = [], [], []
    for name in registry_constant_names(chain):
        raw = getattr(chain.reg.functions, name)().call()
        code = bytes(raw).rstrip(b"\x00").decode(errors="replace") or "OK"
        codes.append(code)
        if code not in S.REASON:
            missing_wording.append(code)
        if code not in S.DECISION:
            missing_decision.append(code)
    assert codes, "no reason constants read off the registry"
    assert not missing_wording, f"reason codes the service cannot explain: {missing_wording}"
    assert not missing_decision, f"reason codes with no decision: {missing_decision}"


def test_decision_map_only_names_decisions_the_surface_understands(svc):
    import duality_service as S

    allowed = {"RELEASE", "HOLD", "RECONCILIATION_REQUIRED", "SETTLED"}
    assert set(S.DECISION.values()) <= allowed
