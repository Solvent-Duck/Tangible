import copy
import json
from decimal import Decimal as D
from pathlib import Path

import pytest
import yaml

from tangible.policy import FindingInput, evaluate, load_policy, parse_policy

POLICIES = Path(__file__).parent.parent / "policies"
POLICY = load_policy(POLICIES / "default.yaml")
RAW = yaml.safe_load((POLICIES / "default.yaml").read_text())

GOOD = {
    "refund_rate": D("0.02"),
    "gross_margin_est": D("0.40"),
    "payables_discipline": D("0.95"),
    "net_revenue": D("100000"),
    "history_days": D("365"),
}


def m(**kw):
    return {**GOOD, **{k: (None if v is None else D(str(v))) for k, v in kw.items()}}


def f(sev, state="open", fid="f1"):
    return FindingInput(fid, "chk", sev, state)


# --- schema ---------------------------------------------------------------------------------


def test_default_parses():
    p = parse_policy(RAW)
    assert p.version == "0.2.0" and p.metric_set == "ms-2" and p.check_set == "cs-1"
    assert [b.band for b in p.bands] == ["A", "B", "C", "D"]
    assert p.limit.share["A"] == D("0.4")


def mutated(fn):
    spec = copy.deepcopy(RAW)
    fn(spec)
    return spec


@pytest.mark.parametrize(
    ("fn", "path"),
    [
        (lambda s: s.update(bogus=1), "policy"),
        (lambda s: s["knockout"].update(x=1), "policy.knockout"),
        (lambda s: s["limit"]["history"].update(x=1), "policy.limit.history"),
        (lambda s: s["bands"].reverse(), "policy.bands"),
        (lambda s: s["bands"][3].update(when={"refund_rate": {"max": 1}}), "policy.bands[3]"),
        (lambda s: s["bands"][0].update(band="Z"), "policy.bands[0].band"),
        (lambda s: s.pop("version"), "policy"),
        (lambda s: s.update(version="x"), "policy.version"),
        (lambda s: s["limit"]["share"].update(A="high"), "policy.limit.share.A"),
        (lambda s: s["limit"]["share"].pop("D"), "policy.limit.share"),
        (lambda s: s["bands"][0]["when"]["refund_rate"].update(avg=1), "refund_rate"),
        (lambda s: s["knockout"].update(hold_states=["bogus"]), "hold_states[0]"),
    ],
)
def test_schema_rejects(fn, path):
    with pytest.raises(ValueError, match=path.replace("[", r"\[").replace("]", r"\]")):
        parse_policy(mutated(fn))


# --- knockout -------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["open", "accepted"])
def test_hold_blocks_decision(state):
    r = evaluate(POLICY, m(), [f("hold", state)])
    assert r.band is None and r.limit is None
    assert r.rules_fired[-1]["outcome"] == "knockout"


@pytest.mark.parametrize("state", ["rejected", "disputed"])
def test_hold_not_blocking_in_other_states(state):
    r = evaluate(POLICY, m(), [f("hold", state)])
    assert r.band == "A" and r.limit is not None


# --- bands ----------------------------------------------------------------------------------


def test_band_a_and_boundaries_inclusive():
    assert (
        evaluate(
            POLICY, m(refund_rate="0.03", gross_margin_est="0.35", payables_discipline="0.90"), []
        ).band
        == "A"
    )
    assert evaluate(POLICY, m(refund_rate="0.0301"), []).band == "B"
    assert evaluate(POLICY, m(gross_margin_est="0.3499"), []).band == "B"
    assert evaluate(POLICY, m(payables_discipline="0.8999"), []).band == "B"


def test_band_b_c_d_boundaries():
    assert evaluate(POLICY, m(refund_rate="0.05"), []).band == "B"
    assert evaluate(POLICY, m(refund_rate="0.0501"), []).band == "C"
    assert evaluate(POLICY, m(refund_rate="0.04", gross_margin_est="0.2499"), []).band == "C"
    assert evaluate(POLICY, m(refund_rate="0.08"), []).band == "C"
    assert evaluate(POLICY, m(refund_rate="0.0801"), []).band == "D"


def test_missing_metric_not_evaluated_and_falls_lower():
    r = evaluate(POLICY, m(payables_discipline=None), [])
    assert r.band == "B"
    ne = [x for x in r.rules_fired if x["outcome"] == "not_evaluated"]
    assert ne and ne[0]["metric"] == "payables_discipline"
    metrics = {k: v for k, v in GOOD.items() if k != "refund_rate"}
    r2 = evaluate(POLICY, metrics, [])
    assert r2.band == "D"


def test_rules_fired_explains_failure():
    r = evaluate(POLICY, m(refund_rate="0.094"), [])
    fail = [x for x in r.rules_fired if x.get("band") == "C" and x.get("outcome") == "fail"]
    assert fail[0]["saw"] == "0.094" and fail[0]["threshold"] == "0.08" and fail[0]["op"] == "<="
    assert r.band == "D"


# --- review cap -----------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["open", "accepted"])
def test_review_cap(state):
    r = evaluate(POLICY, m(), [f("review", state)])
    assert r.band == "C"


def test_review_cap_not_applied_when_resolved_or_already_worse():
    assert evaluate(POLICY, m(), [f("review", "rejected")]).band == "A"
    assert evaluate(POLICY, m(), [f("review", "disputed")]).band == "A"
    assert evaluate(POLICY, m(refund_rate="0.2"), [f("review")]).band == "D"
    assert evaluate(POLICY, m(), [f("info")]).band == "A"


# --- limit ----------------------------------------------------------------------------------


def test_limit_share():
    r = evaluate(POLICY, m(net_revenue=100000), [])
    assert r.band == "A" and r.limit == D("40000.00")


def test_limit_short_history_halves_share():
    assert evaluate(POLICY, m(history_days=182), []).limit == D("20000.00")
    assert evaluate(POLICY, m(history_days=183), []).limit == D("40000.00")
    r = evaluate(POLICY, m(history_days=None), [])
    assert r.limit == D("20000.00")
    assert any(x.get("saw") == "none" and x["outcome"] == "short_history" for x in r.rules_fired)


def test_limit_max_cap():
    r = evaluate(POLICY, m(net_revenue=10_000_000), [])
    assert r.limit == D("250000.00")
    assert r.rules_fired[-1]["outcome"] == "capped_at_max"


def test_limit_none_base():
    r = evaluate(POLICY, m(net_revenue=None), [])
    assert r.band == "A" and r.limit is None
    assert r.rules_fired[-1]["status"] == "not_evaluated"


def test_limit_quantized():
    assert evaluate(POLICY, m(net_revenue="1000.123"), []).limit == D("400.05")


# --- determinism ----------------------------------------------------------------------------


def test_identical_inputs_identical_hash_and_metadata():
    a = evaluate(POLICY, m(), [f("review")])
    b = evaluate(POLICY, m(), [f("review")])
    assert a == b and a.result_sha256 == b.result_sha256 and len(a.result_sha256) == 64
    assert a.policy_version == "0.2.0" and a.policy_sha256 == POLICY.sha256
    assert evaluate(POLICY, m(net_revenue=1), []).result_sha256 != a.result_sha256


def test_spec_input_uses_explicit_sha():
    assert POLICY.parsed is not None
    r = evaluate(POLICY.parsed, m(), [], policy_sha256="abc")
    assert r.policy_sha256 == "abc"


def test_rules_fired_json_serialisable_strings_only():
    for findings in ([], [f("hold")], [f("review")]):
        r = evaluate(POLICY, m(payables_discipline=None), findings)
        json.dumps(r.rules_fired)
        assert all(isinstance(v, str) for rule in r.rules_fired for v in rule.values())
