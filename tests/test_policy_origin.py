"""Policy 0.3.0: an agent (model-origin) hold blocks only once an analyst accepts it."""

import copy
import json
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
import yaml

from tangible.policy import FindingInput, PolicyResult, evaluate, load_policy, parse_policy

POLICIES = Path(__file__).parent.parent / "policies"
NEW = load_policy(POLICIES / "default-0.3.0.yaml")
OLD = load_policy(POLICIES / "default-0.2.2.yaml")
RAW = yaml.safe_load((POLICIES / "default-0.3.0.yaml").read_text())

METRICS = {
    "refund_rate": D("0.02"),
    "gross_margin_est": D("0.40"),
    "payables_discipline": D("0.95"),
    "net_revenue": D("100000"),
    "history_days": D("365"),
}


def hold(fid: str, key: str, state: Any, origin: Any = "rule") -> FindingInput:
    return FindingInput(fid, key, "hold", state, origin)


def texts(result: PolicyResult) -> list[str]:
    return [r["rule"] for r in result.rules_fired]


def test_open_agent_hold_does_not_knock_out_and_is_visible():
    r = evaluate(NEW, METRICS, [hold("f1", "unverified_claim", "open", "model")])
    assert r.band == "A" and r.limit is not None
    assert (
        "knockout: agent hold finding unverified_claim is open (not accepted), not blocking"
        in texts(r)
    )
    entry = next(x for x in r.rules_fired if x.get("finding_id") == "f1")
    assert entry["outcome"] == "not_blocking" and entry["origin"] == "model"
    assert texts(r).count("knockout: no blocking hold finding") == 1
    json.dumps(r.rules_fired)
    assert all(isinstance(v, str) for rule in r.rules_fired for v in rule.values())


def test_accepted_agent_hold_knocks_out():
    r = evaluate(NEW, METRICS, [hold("f1", "unverified_claim", "accepted", "model")])
    assert r.band is None and r.limit is None
    assert texts(r) == ["knockout: agent hold finding unverified_claim is accepted"]
    assert r.rules_fired[-1]["outcome"] == "knockout"


@pytest.mark.parametrize("state", ["open", "accepted"])
def test_rule_hold_knocks_out_open_or_accepted(state):
    r = evaluate(NEW, METRICS, [hold("f1", "payouts_vs_deposits", state)])
    assert r.band is None and r.limit is None
    assert texts(r) == [f"knockout: hold finding payouts_vs_deposits is {state}"]


@pytest.mark.parametrize("state", ["rejected", "disputed"])
def test_rejected_or_disputed_holds_never_block(state):
    for origin in ("rule", "model"):
        assert evaluate(NEW, METRICS, [hold("f1", "x", state, origin)]).band == "A", origin
    r = evaluate(NEW, METRICS, [hold("f1", "x", state, "model")])
    assert f"knockout: agent hold finding x is {state}, not blocking" in texts(r)


def test_mixed_findings_block_on_the_rule_hold_and_still_report_the_open_agent_hold():
    r = evaluate(
        NEW,
        METRICS,
        [hold("f1", "agent_thing", "open", "model"), hold("f2", "payouts_vs_deposits", "open")],
    )
    assert r.band is None
    t = texts(r)
    assert "knockout: agent hold finding agent_thing is open (not accepted), not blocking" in t
    assert "knockout: hold finding payouts_vs_deposits is open" in t


def test_agent_review_findings_still_cap_the_band():
    # Only holds are gated on acceptance; a review finding can only lower the band.
    r = evaluate(NEW, METRICS, [FindingInput("f1", "x", "review", "open", "model")])
    assert r.band == "C"


def test_missing_origin_defaults_to_rule_and_blocks_when_open():
    r = evaluate(NEW, METRICS, [FindingInput("f1", "x", "hold", "open")])
    assert r.band is None


# --- 0.2.2 is unchanged ----------------------------------------------------------------------


@pytest.mark.parametrize("origin", ["rule", "model"])
@pytest.mark.parametrize("state", ["open", "accepted"])
def test_022_blocks_any_origin_exactly_as_before(origin, state):
    r = evaluate(OLD, METRICS, [hold("f1", "x", state, origin)])
    assert r.band is None and r.limit is None
    assert r.rules_fired == [
        {
            "rule": f"knockout: hold finding x is {state}",
            "step": "knockout",
            "finding_id": "f1",
            "saw": state,
            "op": "in",
            "threshold": "open,accepted",
            "outcome": "knockout",
        }
    ]


def test_022_result_hash_does_not_depend_on_origin():
    a = evaluate(OLD, METRICS, [hold("f1", "x", "rejected", "rule")])
    b = evaluate(OLD, METRICS, [hold("f1", "x", "rejected", "model")])
    assert a.result_sha256 == b.result_sha256 and a.band == "A"
    assert a.policy_version == "0.2.2"
    assert a.rules_fired[0] == {
        "rule": "knockout: no blocking hold finding",
        "step": "knockout",
        "saw": "1",
        "op": "in",
        "threshold": "open,accepted",
        "outcome": "pass",
    }


def test_policy_hash_and_result_hash_change_with_the_new_version():
    a = evaluate(OLD, METRICS, [])
    b = evaluate(NEW, METRICS, [])
    assert a.policy_sha256 != b.policy_sha256 and a.policy_version != b.policy_version
    assert a.result_sha256 != b.result_sha256  # the knockout pass entry names both state lists
    assert (a.band, a.limit) == (b.band, b.limit)


# --- schema ----------------------------------------------------------------------------------


def mutated(fn: Any) -> dict[str, Any]:
    spec = copy.deepcopy(RAW)
    fn(spec)
    return spec


@pytest.mark.parametrize(
    ("fn", "path"),
    [
        (lambda s: s["knockout"].update(model_hold_states=["bogus"]), r"model_hold_states\[0\]"),
        (lambda s: s["knockout"].update(model_hold_states=[]), "model_hold_states"),
        (lambda s: s["knockout"].update(model_hold_states="accepted"), "model_hold_states"),
        (
            lambda s: s["knockout"].update(hold_states=["open"], model_hold_states=["accepted"]),
            "not in hold_states",
        ),
    ],
)
def test_schema_rejects_bad_model_hold_states(fn, path):
    with pytest.raises(ValueError, match=path):
        parse_policy(mutated(fn))


def test_model_hold_states_optional_and_parsed():
    assert parse_policy(RAW).knockout.model_hold_states == ("accepted",)
    spec = mutated(lambda s: s["knockout"].pop("model_hold_states"))
    assert parse_policy(spec).knockout.model_hold_states is None
