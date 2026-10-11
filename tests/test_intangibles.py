"""Intangibles v1 slice: entity matching, signal tools, ms-3 metrics, cs-5 checks, policy uplift.

Fixtures only (web/demo_fixtures/us). No network, no model.
"""

import copy
import dataclasses
import json
import socket
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml

import tangible.checks  # noqa: F401  (registers checks)
import tangible.metrics  # noqa: F401  (registers metrics)
from tangible.agent.tools import ToolContext, call_tool, open_context, tool_definitions
from tangible.checks.registry import run_check
from tangible.checks.sets import check_set
from tangible.datasets.synthetic import generate
from tangible.db.cases import create_case
from tangible.db.repo import create_borrower, load_ledger
from tangible.db.session import admin_connect, tenant_session
from tangible.db.signals import insert_application, review_match
from tangible.demo.us_intangibles import BY_KEY, US_BORROWERS, stage_public_signals
from tangible.evidence.store import LocalEvidenceStore
from tangible.ingest.synthetic_dir import ingest_synthetic_dir
from tangible.intangibles.loyalty import mentions_loyalty_v1
from tangible.intangibles.matching import Identity, identity_of, score_v1
from tangible.ledger.types import (
    Application,
    InventorySnapshot,
    Invoice,
    Ledger,
    Order,
    PublicSignal,
)
from tangible.metrics.registry import compute
from tangible.metrics.sets import metric_set
from tangible.policy import FindingInput, PolicyResult, evaluate, load_policy, parse_policy
from tangible.web.signals import US_FIXTURES_DIR, demo_signal_provider
from test_synthetic import write_lines

POLICIES = Path(__file__).parent.parent / "policies"
POLICY = load_policy(POLICIES / "default.yaml")
RAW = yaml.safe_load((POLICIES / "default.yaml").read_text())
AS_OF = datetime(2026, 10, 1, 23, 59, tzinfo=UTC)
SEEN = AS_OF - timedelta(days=1)
D0 = D(0)


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("network use in test")

    monkeypatch.setattr(socket, "socket", boom)


def app_of(key: str, observed: datetime = SEEN) -> Application:
    return BY_KEY[key].application(observed)


def listing_data(source: str, query: str, ref: str | None = None) -> dict[str, Any]:
    found = demo_signal_provider().place_search(source, query)
    r = found[0] if ref is None else next(x for x in found if x.source_ref == ref)
    return r.data


def sig(
    source: str, ref: str, data: dict[str, Any], *, observed: datetime = SEEN, ok: bool = True,
    kind: str = "listing",
) -> PublicSignal:  # fmt: skip
    return PublicSignal(
        source, ref, kind, json.dumps(data, sort_keys=True), observed,
        match_score=D("1") if ok else D("0.4"), match_ok=ok,
    )  # fmt: skip


def juniper_signals() -> tuple[PublicSignal, ...]:
    rdap = demo_signal_provider().rdap("juniperandrye.example")
    assert rdap is not None
    return (
        sig("google_places", "gp-juniper-rye-division",
            listing_data("google_places", "Juniper & Rye Bakery")),
        sig("yelp", "juniper-and-rye-bakery-portland",
            listing_data("yelp", "Juniper & Rye Bakery")),
        sig("rdap", "juniperandrye.example", rdap.data, kind="domain_registration"),
    )  # fmt: skip


def m(key: str, ledger: Ledger, as_of: datetime = AS_OF) -> D | None:
    return compute(key, dict(metric_set("ms-3"))[key], ledger, as_of)


# ---------------------------------------------------------------- entity_match v1


def test_true_listings_score_one_and_the_decoy_scores_below_threshold() -> None:
    ident = identity_of(app_of("northgate"))
    real = listing_data(
        "google_places", "Northgate Phone Repair", "gp-northgate-phone-repair-speedway"
    )
    decoy = listing_data(
        "google_places", "Northgate Phone Repair", "gp-northgate-phone-repair-spokane"
    )
    score, comps = score_v1(ident, "listing", real)
    assert score == D("1.0000") and comps == {
        "name": "1.0000", "address": "1.0000", "phone": "1.0000", "website": "1.0000",
    }  # fmt: skip
    score, comps = score_v1(ident, "listing", decoy)
    assert score == D("0.4000") and score < D("0.9")
    assert comps["name"] == "1.0000" and comps["address"] == comps["phone"] == "0.0000"
    yelp_decoy = listing_data("yelp", "Northgate Phone Repair", "northgate-phone-repair-spokane")
    assert score_v1(ident, "listing", yelp_decoy)[0] < D("0.9")


def test_dba_and_yelp_without_website_still_match() -> None:
    ident = identity_of(app_of("juniper_rye"))
    assert score_v1(ident, "listing", listing_data("yelp", "Juniper & Rye"))[0] == D("1.0000")
    no_site = {**listing_data("yelp", "Juniper & Rye"), "website": None}
    assert score_v1(ident, "listing", no_site)[0] == D("1.0000")


def test_name_alone_never_clears_the_threshold() -> None:
    ident = identity_of(app_of("juniper_rye"))
    score, comps = score_v1(ident, "listing", {"name": "Juniper & Rye Bakery"})
    assert score == D("0.5000") and "capped" in comps["rule"]
    assert score_v1(Identity((), None, None, None, None), "listing", {"name": "x"})[0] == D0


def test_rdap_matches_only_the_applications_domain() -> None:
    ident = identity_of(app_of("juniper_rye"))
    assert score_v1(ident, "domain_registration", {"domain": "juniperandrye.example"})[0] == 1
    assert score_v1(ident, "domain_registration", {"domain": "other.example"})[0] == 0


def test_address_normalisation() -> None:
    ident = Identity(("Acme",), "12 North Main Street, Springfield", "IL", None, None)
    data = {"name": "ACME, Inc.", "address": "12 N Main St, Springfield, IL 62701, USA"}
    assert score_v1(ident, "listing", data)[0] == D("1.0000")
    moved = {**data, "address": "14 N Main St, Springfield, IL 62701"}
    assert score_v1(ident, "listing", moved)[0] == D("0.5714")  # (0.4) / 0.7


# ---------------------------------------------------------------- loyalty_mentions v1


@pytest.mark.parametrize(
    "text,hit",
    [
        ("We've been coming here every Saturday for years.", True),
        ("Regulars know to come early.", True),
        ("My go-to since 2019.", True),
        ("Keep coming back for the galettes.", True),
        ("Great croissants, a little pricey.", False),
        ("Coffee was lukewarm today.", False),
        ("Best phone repair ever!!", False),
    ],
)
def test_loyalty_keyword_rule(text: str, hit: bool) -> None:
    assert mentions_loyalty_v1(text) is hit


# ---------------------------------------------------------------- ledger: PublicSignal time rule


def test_signals_go_through_known_as_of() -> None:
    early = sig("google_places", "p1", {"rating": 4.0, "review_count": 10}, observed=SEEN)
    revised = sig("google_places", "p1", {"rating": 4.5, "review_count": 20}, observed=AS_OF)
    late = sig("yelp", "y1", {"rating": 5, "review_count": 99}, observed=AS_OF + timedelta(days=1))
    ledger = Ledger(public_signals=(late, revised, early))
    assert ledger.known(SEEN).public_signals == (early,)
    assert ledger.known(AS_OF).public_signals == (revised,)
    assert m("review_count", ledger, SEEN) == 10 and m("review_count", ledger) == 20
    # Same source_ref under another source is a different record.
    other = sig("yelp", "p1", {"rating": 3, "review_count": 1})
    assert len(Ledger(public_signals=(early, other)).known(AS_OF).public_signals) == 2


# ---------------------------------------------------------------- ms-3 metrics


def test_ms3_is_ms2_plus_the_intangibles_metrics() -> None:
    ms2, ms3 = metric_set("ms-2"), metric_set("ms-3")
    assert ms3[: len(ms2)] == ms2 and len(ms3) == len(ms2) + 9
    assert {k for k, _ in ms3[len(ms2):]} == {
        "review_rating_shrunk", "review_count", "owner_response_rate", "years_trading",
        "presence_channels", "loyalty_mention_share", "stock_value_at_cost", "stock_cover",
        "entity_match_ok",
    }  # fmt: skip
    assert len(ms2) == 23  # released set untouched


def test_reputation_metrics_from_matched_listings() -> None:
    ledger = Ledger(public_signals=juniper_signals())
    assert m("review_count", ledger) == 310
    assert (
        m("review_rating_shrunk", ledger)
        == (D(25) * D("3.8") + D("4.7") * 212 + D("4.5") * 98) / 335
    )
    assert m("owner_response_rate", ledger) == D(11) / D(16)
    assert m("loyalty_mention_share", ledger) == D(7) / D(16)
    days = (date(2026, 10, 1) - date(2016, 9, 21)).days
    assert m("years_trading", ledger) == (D(days) / D("365.25")).quantize(D("0.01"))
    assert m("presence_channels", ledger) == 3
    assert m("entity_match_ok", ledger) == 1


def test_shrinkage_pulls_a_few_glowing_reviews_toward_the_prior() -> None:
    few = Ledger(public_signals=(sig("google_places", "p", {"rating": 5.0, "review_count": 3}),))
    assert m("review_rating_shrunk", few) == (D(95) + D(15)) / D(28)  # ~3.93, not 5


def test_unmatched_signals_are_not_used_but_flip_entity_match_ok() -> None:
    decoy = sig("google_places", "gp-decoy", {"rating": 4.6, "review_count": 388}, ok=False)
    ledger = Ledger(public_signals=(*juniper_signals(), decoy))
    assert m("review_count", ledger) == 310
    assert m("entity_match_ok", ledger) == 0
    assert m("presence_channels", ledger) == 3


def test_metrics_without_signals_are_not_evaluated() -> None:
    empty = Ledger()
    for key in (
        "review_rating_shrunk", "review_count", "owner_response_rate", "years_trading",
        "presence_channels", "loyalty_mention_share", "stock_value_at_cost", "stock_cover",
        "entity_match_ok",
    ):  # fmt: skip
        assert m(key, empty) is None, key


def test_years_trading_ignores_dates_after_as_of() -> None:
    ledger = Ledger(public_signals=juniper_signals())
    assert m("years_trading", ledger, datetime(2016, 1, 1, tzinfo=UTC)) is None


def copperline_ledger(requested: D = D("80000")) -> Ledger:
    end = AS_OF.date()
    start = end - timedelta(days=90)

    def snap(sku: str, units: int, day: date) -> InventorySnapshot:
        return InventorySnapshot(f"{sku}-{day}", sku, D(units), D(50), day, SEEN)

    stock = (
        snap("A", 1000, start - timedelta(days=5)), snap("A", 900, end - timedelta(days=45)),
        snap("A", 800, end - timedelta(days=1)),
        snap("B", 1000, start - timedelta(days=5)), snap("B", 950, end - timedelta(days=45)),
        snap("B", 900, end - timedelta(days=1)),
    )  # fmt: skip
    purchase = Invoice(
        "inv-1", "payable", "supplier", D(70000), end - timedelta(days=60),
        end - timedelta(days=30), end - timedelta(days=31), SEEN,
    )  # fmt: skip
    orders = tuple(
        Order(f"o{i}", D(500), D0, AS_OF - timedelta(days=1 + i % 80), SEEN) for i in range(200)
    )
    app = dataclasses.replace(app_of("copperline"), requested_amount=requested)
    return Ledger(
        orders=orders, invoices=(purchase,), inventory_snapshots=stock, applications=(app,)
    )


def test_stock_metrics() -> None:
    ledger = copperline_ledger()
    assert m("stock_value_at_cost", ledger) == D(85000)
    assert m("stock_cover", ledger) == D(85000) / D(80000)
    doi = m("days_of_inventory", ledger)
    assert doi is not None and doi.quantize(D("0.0001")) == 90
    assert m("aged_stock_share", ledger) == 0
    assert m("stock_cover", copperline_ledger(D(0))) is None


# ---------------------------------------------------------------- cs-5 checks


def run(key: str, ledger: Ledger) -> list[Any]:
    return run_check(key, dict(check_set("cs-5"))[key], ledger, AS_OF)


def test_cs5_is_cs4_plus_three_checks() -> None:
    cs4, cs5 = check_set("cs-4"), check_set("cs-5")
    assert cs5[: len(cs4)] == cs4 and len(cs5) == len(cs4) + 3
    assert dict(cs5[len(cs4):]) == {
        "listing_closed": 1, "review_volume_vs_orders": 1, "listing_identity_mismatch": 1,
    }  # fmt: skip


def test_no_signal_no_finding() -> None:
    ledger = copperline_ledger()
    for key in ("listing_closed", "review_volume_vs_orders", "listing_identity_mismatch"):
        assert run(key, ledger) == []


def test_listing_closed_is_a_hold() -> None:
    data = {
        **listing_data("google_places", "Juniper & Rye"),
        "business_status": "CLOSED_PERMANENTLY",
    }
    (f,) = run("listing_closed", Ledger(public_signals=(sig("google_places", "gp", data),)))
    assert f.severity == "hold" and f.facts["business_status"] == "CLOSED_PERMANENTLY"
    unmatched = Ledger(public_signals=(sig("google_places", "gp", data, ok=False),))
    assert run("listing_closed", unmatched) == []


def northgate_ledger(n_orders: int) -> Ledger:
    real_g = listing_data(
        "google_places", "Northgate Phone Repair", "gp-northgate-phone-repair-speedway"
    )
    real_y = listing_data("yelp", "Northgate Phone Repair", "northgate-phone-repair-tucson")
    orders = tuple(Order(f"o{i}", D(80), D0, AS_OF - timedelta(days=i % 300), SEEN)
                   for i in range(n_orders))  # fmt: skip
    return Ledger(
        orders=orders,
        applications=(app_of("northgate"),),
        public_signals=(sig("google_places", "g", real_g), sig("yelp", "y", real_y)),
    )


def test_review_volume_vs_orders_flags_bought_reviews() -> None:
    (f,) = run("review_volume_vs_orders", northgate_ledger(400))
    assert f.severity == "review"
    assert f.facts == {"reviews": "1152", "orders": "400", "ratio": "2.8800", "threshold": "0.25"}
    assert run("review_volume_vs_orders", northgate_ledger(5000)) == []
    juniper = Ledger(
        orders=tuple(Order(f"o{i}", D(9), D0, SEEN, SEEN) for i in range(2000)),
        public_signals=juniper_signals(),
    )
    assert run("review_volume_vs_orders", juniper) == []


def test_listing_identity_mismatch_on_a_confirmed_listing() -> None:
    data = {**listing_data("google_places", "Juniper & Rye"), "phone": "(503) 555-0199"}
    ledger = Ledger(applications=(app_of("juniper_rye"),),
                    public_signals=(sig("google_places", "gp", data),))  # fmt: skip
    (f,) = run("listing_identity_mismatch", ledger)
    assert f.severity == "review" and f.facts["phone"] == "differs"
    assert "address_similarity" not in f.facts
    clean = Ledger(applications=(app_of("juniper_rye"),), public_signals=juniper_signals())
    assert run("listing_identity_mismatch", clean) == []


# ---------------------------------------------------------------- policy 0.4.0 uplift

BAND_C = {"refund_rate": D("0.06"), "net_revenue": D("100000"), "history_days": D("365")}
LOYAL = {"repeat_customer_share": D("0.41"), "loyalty_mention_share": D("0.2")}
STOCK = {"stock_cover": D("1.2"), "aged_stock_share": D("0.1"), "days_of_inventory": D("90")}
REPUTATION = {
    "review_rating_shrunk": D("4.6"), "review_count": D("300"), "years_trading": D("9"),
    "entity_match_ok": D("1"),
}  # fmt: skip


def uplift_rules(r: PolicyResult) -> list[dict[str, str]]:
    return [x for x in r.rules_fired if x["step"] == "uplift"]


def test_uplift_lifts_one_notch_and_records_the_values_seen() -> None:
    r = evaluate(POLICY, {**BAND_C, **LOYAL}, [])
    assert r.band == "B"
    texts = [x["rule"] for x in uplift_rules(r)]
    assert (
        "uplift C→B via loyalty: repeat_customer_share 0.41 >= 0.35; "
        "loyalty_mention_share 0.20 >= 0.15"
    ) in texts
    outcomes = {x.get("route"): x["outcome"] for x in uplift_rules(r) if x["op"] == "all"}
    assert outcomes == {"reputation": "disabled", "loyalty": "pass", "stock": "not_evaluated"}
    # Limit follows the lifted band.
    assert r.limit == D("30000.00")
    # Order: bands, then uplift, then the limit.
    steps = [x["step"] for x in r.rules_fired]
    assert steps.index("uplift") > max(i for i, s in enumerate(steps) if s == "band")


def test_uplift_never_stacks_and_never_passes_the_ceiling() -> None:
    both = evaluate(POLICY, {**BAND_C, **LOYAL, **STOCK}, [])
    assert both.band == "B"
    lifted = [x for x in uplift_rules(both) if x["outcome"] == "lifted"]
    assert len(lifted) == 1 and lifted[0]["route"] == "loyalty"
    d_band = {**BAND_C, "refund_rate": D("0.20"), **LOYAL}
    assert evaluate(POLICY, d_band, []).band == "C"  # D -> C, one notch only
    b_band = {**BAND_C, "refund_rate": D("0.04"), "gross_margin_est": D("0.30"), **LOYAL}
    r = evaluate(POLICY, b_band, [])
    assert r.band == "B" and uplift_rules(r)[0]["outcome"] == "not_applicable"
    a_band = {**b_band, "refund_rate": D("0.02"), "gross_margin_est": D("0.4"),
              "payables_discipline": D("0.95")}  # fmt: skip
    assert evaluate(POLICY, a_band, []).band == "A"


def test_uplift_never_runs_after_a_knockout() -> None:
    r = evaluate(
        POLICY, {**BAND_C, **LOYAL}, [FindingInput("f1", "listing_closed", "hold", "open")]
    )
    assert r.band is None and r.limit is None and uplift_rules(r) == []


def test_review_cap_still_applies_after_an_uplift() -> None:
    r = evaluate(POLICY, {**BAND_C, **LOYAL},
                 [FindingInput("f1", "review_volume_vs_orders", "review", "open")])  # fmt: skip
    assert r.band == "C"
    steps = [(x["step"], x["outcome"]) for x in r.rules_fired]
    assert steps.index(("uplift", "lifted")) < steps.index(("review_cap", "capped"))


def test_missing_metric_is_not_evaluated_and_failures_say_why() -> None:
    r = evaluate(POLICY, {**BAND_C, "repeat_customer_share": D("0.5")}, [])
    assert r.band == "C"
    loyalty = next(x for x in uplift_rules(r) if x.get("route") == "loyalty")
    assert loyalty["outcome"] == "not_evaluated" and loyalty["status"] == "not_evaluated"
    assert "loyalty_mention_share none (needs >= 0.15)" in loyalty["saw"]
    r = evaluate(POLICY, {**BAND_C, **LOYAL, "repeat_customer_share": D("0.2"), **STOCK,
                          "days_of_inventory": D("200")}, [])  # fmt: skip
    rules = {x.get("route"): x for x in uplift_rules(r)}
    assert rules["loyalty"]["outcome"] == "fail"
    assert "repeat_customer_share 0.20 < 0.35" in rules["loyalty"]["saw"]
    assert "days_of_inventory 200 > 120" in rules["stock"]["saw"]
    assert rules[None]["rule"] == "uplift: no route fired; band stays C"


def test_disabled_route_does_not_fire_and_requires_gates_reputation() -> None:
    assert evaluate(POLICY, {**BAND_C, **REPUTATION}, []).band == "C"
    raw = copy.deepcopy(RAW)
    raw["uplift"]["routes"]["reputation"]["enabled"] = True
    on = parse_policy(raw)
    assert evaluate(on, {**BAND_C, **REPUTATION}, []).band == "B"
    r = evaluate(on, {**BAND_C, **REPUTATION, "entity_match_ok": D0}, [])
    assert r.band == "C"
    rep = next(x for x in uplift_rules(r) if x.get("route") == "reputation")
    assert "entity_match_ok 0 < 1" in rep["saw"]
    # requires_for names reputation only: the stock route is not gated by entity_match_ok.
    assert evaluate(on, {**BAND_C, **STOCK}, []).band == "B"


@pytest.mark.parametrize(
    "patch,msg",
    [
        ({"max_notches": 2}, "max_notches"),
        ({"ceiling": "Z"}, "ceiling"),
        ({"requires_for": ["nope"]}, "requires_for"),
        ({"surprise": 1}, "unknown key"),
        ({"routes": {"x": {"enabled": True}}}, "at least one metric"),
        ({"routes": {"x": {"enabled": "yes", "m": {"min": 1}}}}, "enabled"),
    ],
)
def test_uplift_schema_is_strict(patch: dict[str, Any], msg: str) -> None:
    raw = copy.deepcopy(RAW)
    raw["uplift"].update(patch)
    if "routes" in patch:
        raw["uplift"].pop("requires_for")
    with pytest.raises(ValueError, match=msg):
        parse_policy(raw)


def test_end_to_end_loyalty_and_stock_lifts_from_the_ledger() -> None:
    def all_metrics(ledger: Ledger) -> dict[str, D | None]:
        return {k: compute(k, v, ledger, AS_OF) for k, v in metric_set("ms-3")}

    # Juniper & Rye: 40 customers ordering 3 times each in the window, plus its matched signals.
    orders = tuple(
        Order(f"o{i}", D(40), D0, AS_OF - timedelta(days=1 + i), SEEN, customer_ref=f"c{i % 40}")
        for i in range(120)
    )
    juniper = Ledger(orders=orders, applications=(app_of("juniper_rye"),),
                     public_signals=juniper_signals())  # fmt: skip
    r = evaluate(POLICY, all_metrics(juniper), [])
    assert r.band == "B" and any(
        x["rule"].startswith("uplift C→B via loyalty") for x in r.rules_fired
    )
    # Copperline: no signals at all; the stock route alone lifts it.
    r = evaluate(POLICY, all_metrics(copperline_ledger()), [])
    assert r.band == "B" and any(
        x["rule"].startswith("uplift C→B via stock") for x in r.rules_fired
    )


# ---------------------------------------------------------------- replay of older policies

# result_sha256 of the pre-0.4.0 evaluator (commit c145cd9) on the same inputs; the 0.4.0
# evaluator must reproduce them byte for byte for policies without `uplift`.
GOLDEN = {
    ("default-0.2.2.yaml", "clean_a"):
        "6a72c54f8b36b4f820ca6794afbcad48438bc3ce0ab84cd9e99c3c7ed2c146bb",
    ("default-0.2.2.yaml", "band_c_review"):
        "e42c65987484019b071ea87d76b2924d9cad6cd3fabda8beaa53a3bd09a8529e",
    ("default-0.2.2.yaml", "knockout"):
        "f5de009f7bfab29b79206030146d847d4ec1173d6e3b66d1b094076a3b6bb324",
    ("default-0.2.2.yaml", "missing"):
        "3b61f93289af7778b5eb7fc37eadbd582a5b7216318557378c613e6abb33859c",
    ("default-0.3.0.yaml", "clean_a"):
        "41c6d5207e37e5771114672fd4677b2e11418178697683b44c15fcd1d7e65d01",
    ("default-0.3.0.yaml", "band_c_review"):
        "fc42fd44a9ab336a57bcceb56c79f91bea7d73cfda93d4a1fdc3c8c9e6f9b184",
    ("default-0.3.0.yaml", "knockout"):
        "99715060a12b6e34d6515222add8fb4b2d6da0e878251860173570a85ca0dd67",
    ("default-0.3.0.yaml", "missing"):
        "8d3aa912ec158d5ef0d423595beef4308c847d4f3d4cd3786d4c1ab741c97dbe",
}  # fmt: skip
SCENARIOS: dict[str, tuple[dict[str, D], list[FindingInput]]] = {
    "clean_a": (
        {"refund_rate": D("0.02"), "gross_margin_est": D("0.40"), "payables_discipline": D("0.95"),
         "net_revenue": D("100000"), "history_days": D("365"), **LOYAL},
        [],
    ),
    "band_c_review": (
        {"refund_rate": D("0.06"), "net_revenue": D("40000"), "history_days": D("100"), **LOYAL},
        [FindingInput("f1", "x", "review", "open")],
    ),
    "knockout": ({"refund_rate": D("0.02")}, [FindingInput("f2", "y", "hold", "open")]),
    "missing": ({}, []),
}  # fmt: skip


@pytest.mark.parametrize("file,scenario", list(GOLDEN))
def test_older_policies_replay_byte_identically(file: str, scenario: str) -> None:
    policy = load_policy(POLICIES / file)
    assert policy.parsed is not None and policy.parsed.uplift is None
    metrics, findings = SCENARIOS[scenario]
    r = evaluate(policy, metrics, findings)
    assert r.result_sha256 == GOLDEN[(file, scenario)]
    assert all(x["step"] != "uplift" for x in r.rules_fired)


# ---------------------------------------------------------------- fixtures


def test_us_fixtures_resolve_and_parse() -> None:
    p = demo_signal_provider()
    refs = [r.source_ref for r in p.place_search("google_places", "Northgate Phone Repair LLC")]
    assert refs == ["gp-northgate-phone-repair-speedway", "gp-northgate-phone-repair-spokane"]
    assert p.place_search("yelp", "Copperline Hardware") == []  # no Yelp listing: empty, not error
    assert p.place_search("google_places", "Nobody At All") == []
    assert p.web_search("Juniper & Rye Bakery Portland")[0].data["title"].startswith("Juniper")
    for b in US_BORROWERS:
        rec = p.rdap(b.domain)
        assert rec is not None and rec.data["registered_on"]
    assert p.rdap("unknown.example") is None
    index = json.loads((US_FIXTURES_DIR / "index.json").read_text())
    files = {f for t in index["places"].values() for f in t.values()}
    files |= set(index["search"].values()) | set(index["rdap"].values())
    assert all((US_FIXTURES_DIR / f).is_file() for f in files)


# ---------------------------------------------------------------- DB: tables, RLS, append-only


def seed_tenant(conn: psycopg.Connection[Any], name: str) -> tuple[str, str]:
    conn.execute("RESET ROLE")
    tenant = conn.execute("INSERT INTO tenant (name) VALUES (%s) RETURNING id", (name,)).fetchone()
    assert tenant
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (str(tenant[0]),))
    borrower = conn.execute(
        "INSERT INTO borrower (tenant_id, name) VALUES (%s, %s) RETURNING id", (tenant[0], name)
    ).fetchone()
    assert borrower
    conn.execute("SET ROLE tangible_app")
    return str(tenant[0]), str(borrower[0])


def seed_rows(conn: psycopg.Connection[Any], tenant: str, borrower: str) -> dict[str, str]:
    case = conn.execute(
        "INSERT INTO case_file (tenant_id, borrower_id, as_of, budget_steps, budget_usd) "
        "VALUES (%s, %s, now(), 10, 5) RETURNING id",
        (tenant, borrower),
    ).fetchone()
    assert case
    call = conn.execute(
        "INSERT INTO tool_call (tenant_id, case_id, tool, tool_version, args, result, started_at) "
        "VALUES (%s, %s, 't', 1, '{}', '{}', now()) RETURNING id",
        (tenant, case[0]),
    ).fetchone()
    assert call
    app = insert_application(conn, tenant, borrower, app_of("juniper_rye"))
    match = conn.execute(
        "INSERT INTO entity_match (id, tenant_id, case_id, source, source_ref, source_call_id, "
        "candidate, score, matcher_version, tool_call_id) VALUES (gen_random_uuid(), %s, %s, "
        "'yelp', 'y1', %s, '{}', 0.95, 1, %s) RETURNING id",
        (tenant, case[0], call[0], call[0]),
    ).fetchone()
    assert match
    signal = conn.execute(
        "INSERT INTO public_signal (id, tenant_id, borrower_id, source, source_ref, kind, payload, "
        "entity_match_id, observed_at) VALUES (gen_random_uuid(), %s, %s, 'yelp', 'y1', "
        "'listing', '{}', %s, now()) RETURNING id",
        (tenant, borrower, match[0]),
    ).fetchone()
    assert signal
    return {"application": app, "entity_match": str(match[0]), "public_signal": str(signal[0])}


NEW_TABLES = ("application", "entity_match", "public_signal")


@pytest.mark.db
def test_rls_isolates_the_new_tables(test_db_url: str) -> None:
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        ta, ba = seed_tenant(conn, "intangibles-a")
        ids = seed_rows(conn, ta, ba)
        for table in NEW_TABLES:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (1,), table
        seed_tenant(conn, "intangibles-b")  # acting as tenant B now
        for table in NEW_TABLES:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), table
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            insert_application(conn, ta, ba, app_of("northgate"))
        # B cannot attach its signal or confirmation to A's match (composite tenant FK).
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(
                "INSERT INTO underwriter_action (tenant_id, entity_match_id, user_id, action, "
                "reason) VALUES (current_setting('app.tenant_id')::uuid, %s, 'u', "
                "'confirm_match', 'r')",
                (ids["entity_match"],),
            )
        conn.execute("RESET app.tenant_id")
        for table in NEW_TABLES:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), table


@pytest.mark.db
def test_new_tables_are_append_only(test_db_url: str) -> None:
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        ta, ba = seed_tenant(conn, "intangibles-ao")
        ids = seed_rows(conn, ta, ba)
        for table in NEW_TABLES:
            with pytest.raises(psycopg.errors.RaiseException):
                conn.execute(
                    f"UPDATE {table} SET tenant_id = tenant_id WHERE id = %s", (ids[table],)
                )
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("DELETE FROM entity_match WHERE id = %s", (ids["entity_match"],))
        # match actions need a match target, and a match target takes only match actions
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO underwriter_action (tenant_id, entity_match_id, user_id, action, "
                "reason) VALUES (%s, %s, 'u', 'accept', 'r')",
                (ta, ids["entity_match"]),
            )
        review_match(conn, ta, ids["entity_match"], "u", "reject_match", "other business")


@pytest.mark.db
def test_application_round_trip(test_db_url: str) -> None:
    with admin_connect(test_db_url) as admin:
        row = admin.execute("INSERT INTO tenant (name) VALUES ('app-rt') RETURNING id").fetchone()
        assert row
        tenant = str(row[0])
    with tenant_session(test_db_url, tenant) as conn:
        borrower = create_borrower(conn, tenant, "b")
        old_style = Application("app-0", D("1000"), ("ACME CURRENT",), ("J DOE",), SEEN)
        insert_application(conn, tenant, borrower, old_style)
        new = app_of("juniper_rye", SEEN + timedelta(hours=1))
        insert_application(conn, tenant, borrower, new)
        loaded = load_ledger(conn, borrower)
    assert loaded.applications == (old_style, new)
    assert loaded.applications[0].legal_name is None and loaded.application == new


# ---------------------------------------------------------------- DB: tools through call_tool


@pytest.fixture(scope="module")
def world(test_db_url: str, tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str, datetime]:
    root = tmp_path_factory.mktemp("intangibles")
    lines = write_lines(root / "lines.csv", days=90)
    generate(lines, root / "out", seed=5)
    with admin_connect(test_db_url) as admin:
        row = admin.execute("INSERT INTO tenant (name) VALUES ('intg') RETURNING id").fetchone()
        assert row
        tenant = str(row[0])
    with tenant_session(test_db_url, tenant) as conn:
        borrower = create_borrower(conn, tenant, "Northgate Phone Repair LLC")
        ingest_synthetic_dir(
            conn, LocalEvidenceStore(root / "store"), tenant, borrower, root / "out", lines
        )
        ledger = load_ledger(conn, borrower)
        last = max(t.posted_on for t in ledger.bank_txns)
        as_of = datetime(last.year, last.month, last.day, tzinfo=UTC) + timedelta(days=1)
        insert_application(conn, tenant, borrower, app_of("northgate", as_of - timedelta(days=2)))
    return tenant, borrower, as_of


@pytest.fixture
def ctx(
    test_db_url: str, world: tuple[str, str, datetime], tmp_path: Path
) -> Iterator[ToolContext]:
    tenant, borrower, as_of = world
    with tenant_session(test_db_url, tenant) as conn:
        case = create_case(conn, tenant, borrower, as_of)
        provider = demo_signal_provider(clock=lambda: as_of - timedelta(hours=1))
        yield open_context(conn, case, POLICY, tmp_path, signal_provider=provider)
        conn.rollback()  # keep the module's borrower free of this test's signals


def logged(ctx: ToolContext) -> list[tuple[str, int, Any, Any, str | None]]:
    return ctx.conn.execute(
        "SELECT tool, tool_version, args, result, error FROM tool_call WHERE case_id = %s "
        "ORDER BY started_at",
        (ctx.case.id,),
    ).fetchall()


def search(ctx: ToolContext, source: str = "google_places") -> Any:
    r = call_tool(ctx, "place_search", {"source": source, "query": "Northgate Phone Repair"})
    assert r.error is None
    return r


def match(ctx: ToolContext, call_id: str, ref: str) -> Any:
    return call_tool(ctx, "match_entity", {"source_call_id": call_id, "source_ref": ref})


REAL, DECOY = "gp-northgate-phone-repair-speedway", "gp-northgate-phone-repair-spokane"


@pytest.mark.db
def test_definitions_take_no_url_and_no_score() -> None:
    defs = {d["name"]: d["input_schema"] for d in tool_definitions()}
    assert set(defs["place_search"]["properties"]) == {"source", "query"}
    assert set(defs["web_search"]["properties"]) == {"query"}
    assert set(defs["rdap_lookup"]["properties"]) == {"domain"}
    assert set(defs["match_entity"]["properties"]) == {"source_call_id", "source_ref"}
    assert set(defs["record_signal"]["properties"]) == {"match_id"}


@pytest.mark.db
def test_every_new_tool_call_is_logged(ctx: ToolContext) -> None:
    s = search(ctx)
    call_tool(ctx, "web_search", {"query": "Northgate Phone Repair Tucson"})
    call_tool(ctx, "rdap_lookup", {"domain": "northgatephonerepair.example"})
    mt = match(ctx, s.call_id, REAL)
    call_tool(ctx, "record_signal", {"match_id": mt.result["match_id"]})
    rows = logged(ctx)
    assert [(r[0], r[1], r[4]) for r in rows] == [
        ("place_search", 1, None), ("web_search", 1, None), ("rdap_lookup", 1, None),
        ("match_entity", 1, None), ("record_signal", 1, None),
    ]  # fmt: skip
    assert rows[0][2] == {"source": "google_places", "query": "Northgate Phone Repair"}
    assert rows[0][3]["untrusted"] is True and rows[1][3]["untrusted"] is True


@pytest.mark.db
def test_match_writes_entity_match_linked_to_the_call(ctx: ToolContext) -> None:
    s = search(ctx)
    real, decoy = match(ctx, s.call_id, REAL), match(ctx, s.call_id, DECOY)
    assert real.result["score"] == "1.0000" and real.result["status"] == "matched"
    assert decoy.result["score"] == "0.4000"
    assert decoy.result["status"] == "needs_analyst_confirmation"
    rows = ctx.conn.execute(
        "SELECT id, tool_call_id, source_call_id, source_ref, score, matcher_version "
        "FROM entity_match WHERE case_id = %s ORDER BY score DESC",
        (ctx.case.id,),
    ).fetchall()
    assert [(str(r[0]), str(r[1]), str(r[2]), r[3], r[4], r[5]) for r in rows] == [
        (real.result["match_id"], real.call_id, s.call_id, REAL, D("1.0000"), 1),
        (decoy.result["match_id"], decoy.call_id, s.call_id, DECOY, D("0.4000"), 1),
    ]


@pytest.mark.db
def test_record_signal_refuses_the_decoy_until_an_analyst_confirms(ctx: ToolContext) -> None:
    s = search(ctx)
    decoy = match(ctx, s.call_id, DECOY)
    refused = call_tool(ctx, "record_signal", {"match_id": decoy.result["match_id"]})
    assert refused.error and "below 0.9" in refused.error and "no analyst" in refused.error
    assert ctx.conn.execute("SELECT count(*) FROM public_signal").fetchone() == (0,)
    review_match(ctx.conn, ctx.case.tenant_id, decoy.result["match_id"], "ana", "confirm_match",
                 "checked: same owner, second shop")  # fmt: skip
    ok = call_tool(ctx, "record_signal", {"match_id": decoy.result["match_id"]})
    assert ok.error is None and ok.result["analyst_confirmed"] is True
    again = call_tool(ctx, "record_signal", {"match_id": decoy.result["match_id"]})
    assert again.error and "already recorded" in again.error


@pytest.mark.db
def test_recorded_signal_reaches_the_ledger_and_metrics(ctx: ToolContext) -> None:
    s, y = search(ctx), search(ctx, "yelp")
    for call_id, ref in ((s.call_id, REAL), (y.call_id, "northgate-phone-repair-tucson")):
        mt = match(ctx, call_id, ref)
        r = call_tool(ctx, "record_signal", {"match_id": mt.result["match_id"]})
        assert r.error is None and r.result["known_at_case_as_of"] is True
    assert {x.source_ref for x in ctx.known.public_signals} == {
        REAL,
        "northgate-phone-repair-tucson",
    }
    loaded = load_ledger(ctx.conn, ctx.case.borrower_id)
    assert all(x.match_ok and x.match_score == D("1.0000") for x in loaded.public_signals)
    assert loaded.public_signals[0].payload["review_count"] in (940, 212)
    got = call_tool(ctx, "compute_metric", {"key": "review_count"})
    assert got.error is None and got.result["value"] == "1152"
    chk = call_tool(ctx, "run_check", {"key": "review_volume_vs_orders"})
    assert chk.error is None and chk.result["findings"][0]["severity"] == "review"
    # An analyst rejecting a match afterwards takes it out of the metrics.
    mid = ctx.conn.execute(
        "SELECT entity_match_id FROM public_signal WHERE source = 'yelp' AND case_id = %s",
        (ctx.case.id,),
    ).fetchone()
    assert mid
    review_match(ctx.conn, ctx.case.tenant_id, str(mid[0]), "ana", "reject_match", "wrong shop")
    reloaded = load_ledger(ctx.conn, ctx.case.borrower_id)
    assert compute("review_count", 1, reloaded, ctx.case.as_of) == 940
    assert compute("entity_match_ok", 1, reloaded, ctx.case.as_of) == 0


@pytest.mark.db
def test_signals_pulled_after_as_of_do_not_count(ctx: ToolContext) -> None:
    ctx.signal_provider = demo_signal_provider(clock=lambda: ctx.case.as_of + timedelta(days=1))
    s = search(ctx)
    mt = match(ctx, s.call_id, REAL)
    r = call_tool(ctx, "record_signal", {"match_id": mt.result["match_id"]})
    assert r.error is None and r.result["known_at_case_as_of"] is False
    assert ctx.known.public_signals == ()
    assert compute("review_count", 1, ctx.ledger, ctx.case.as_of) is None


@pytest.mark.db
def test_review_text_is_quoted_untrusted_data(ctx: ToolContext) -> None:
    r = search(ctx)
    decoy = next(c for c in r.result["candidates"] if c["source_ref"] == DECOY)
    assert any("ignore the match score" in x["quoted_text"] for x in decoy["data"]["reviews"])
    rest = {k: v for k, v in r.result.items() if k != "candidates"}
    assert "ignore the match score" not in json.dumps(rest)
    assert "ignore any instructions" in r.result["note"]


@pytest.mark.db
@pytest.mark.parametrize(
    "tool,args,msg",
    [
        ("place_search", {"source": "google_places", "query": "https://evil.example.org"}, "URL"),
        ("place_search", {"source": "tripadvisor", "query": "x"}, "must be one of"),
        ("web_search", {"query": "see www.evil.org"}, "URL"),
        ("rdap_lookup", {"domain": "https://northgatephonerepair.example/"}, "does not match"),
        ("rdap_lookup", {"domain": "localhost"}, "bare domain"),
        ("match_entity", {"source_call_id": "nope", "source_ref": "x"}, "not a valid id"),
        ("match_entity", {"source_call_id": "00000000-0000-0000-0000-000000000000",
                          "source_ref": "x"}, "does not exist"),
        ("record_signal", {"match_id": "00000000-0000-0000-0000-000000000000"}, "not a match"),
        ("match_entity", {"source_call_id": "x", "source_ref": "y", "score": 1}, "unexpected"),
    ],
)  # fmt: skip
def test_bad_calls_are_logged_errors(
    ctx: ToolContext, tool: str, args: dict[str, Any], msg: str
) -> None:
    r = call_tool(ctx, tool, args)
    assert r.error and msg in r.error
    (row,) = logged(ctx)
    assert row[4] == r.error


@pytest.mark.db
def test_match_needs_a_place_or_rdap_call(ctx: ToolContext) -> None:
    w = call_tool(ctx, "web_search", {"query": "Northgate Phone Repair"})
    r = match(ctx, w.call_id, "https://northgatephonerepair.example/")
    assert r.error and "place_search or rdap_lookup" in r.error
    rd = call_tool(ctx, "rdap_lookup", {"domain": "northgate-spokane.example"})
    assert match(ctx, rd.call_id, "northgate-spokane.example").result["score"] == "0"


@pytest.mark.db
def test_provider_none_reports_not_available(ctx: ToolContext) -> None:
    ctx.signal_provider = None
    for tool, args in (
        ("place_search", {"source": "yelp", "query": "x"}),
        ("web_search", {"query": "x"}),
        ("rdap_lookup", {"domain": "example.com"}),
    ):
        assert call_tool(ctx, tool, args).error == "public signal search not available"


@pytest.mark.db
def test_stage_public_signals_for_the_us_demo(
    test_db_url: str, world: tuple[str, str, datetime], tmp_path: Path
) -> None:
    tenant, _, as_of = world
    with tenant_session(test_db_url, tenant) as conn:
        borrower = create_borrower(conn, tenant, "Northgate Phone Repair LLC (staged)")
        case = create_case(conn, tenant, borrower, as_of)
        calls = stage_public_signals(conn, case, POLICY, tmp_path, BY_KEY["northgate"])
        refused = [c for c in calls if c.tool == "record_signal" and c.error]
        recorded = [c for c in calls if c.tool == "record_signal" and not c.error]
        assert len(refused) == 2  # the Spokane decoy on Google and on Yelp
        assert {c.result["source_ref"] for c in recorded} == {
            REAL, "northgate-phone-repair-tucson", "northgatephonerepair.example",
        }  # fmt: skip
        ledger = load_ledger(conn, borrower)
        assert ledger.application is not None and ledger.application.state == "AZ"
        assert compute("presence_channels", 1, ledger, as_of) == 3
        assert compute("entity_match_ok", 1, ledger, as_of) == 1


@pytest.mark.db
def test_parallel_agents_record_the_same_listing_once(ctx: ToolContext) -> None:
    # Two search agents pull and match the same listing; only the first record sticks.
    first = match(ctx, search(ctx).call_id, REAL)
    second = match(ctx, search(ctx).call_id, REAL)
    ok = call_tool(ctx, "record_signal", {"match_id": first.result["match_id"]})
    dup = call_tool(ctx, "record_signal", {"match_id": second.result["match_id"]})
    assert ok.error is None
    assert dup.error and "already on file for this case" in dup.error
    assert ctx.conn.execute(
        "SELECT count(*) FROM public_signal WHERE case_id = %s", (ctx.case.id,)
    ).fetchone() == (1,)
