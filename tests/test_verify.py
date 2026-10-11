from tangible.agent.verify import verify_numbers


def test_empty_text_ok() -> None:
    r = verify_numbers("", [])
    assert r.ok and r.checked == 0


def test_invented_number_caught() -> None:
    r = verify_numbers("Revenue was £1,234.56 and margin 42.5%.", [{"revenue": 1234.56}])
    assert not r.ok and r.unmatched == ("42.5%",) and r.checked == 2


def test_percentage_forms() -> None:
    assert verify_numbers("Margin is 9.4%.", [{"m": 0.0940}]).ok
    assert verify_numbers("Margin is 9.4%.", [{"m": 9.4}]).ok
    assert not verify_numbers("Margin is 9.5%.", [{"m": 0.0940}]).ok


def test_currency_and_strings() -> None:
    assert verify_numbers("Total £1,234.56.", [{"t": "1234.56"}]).ok
    assert verify_numbers("Total GBP 1,234.", [{"t": 1234}]).ok
    assert verify_numbers("Total $1,234.", [{"t": "1,234"}]).ok


def test_suffixes() -> None:
    assert verify_numbers("About £12.5k.", [{"t": 12500}]).ok
    assert verify_numbers("About £2.1m.", [{"t": 2_100_000}]).ok
    assert not verify_numbers("About £12.5k.", [{"t": 13500}]).ok


def test_rounding() -> None:
    assert verify_numbers("Ratio 0.38.", [{"r": 0.3839}]).ok
    assert not verify_numbers("Ratio 0.39.", [{"r": 0.3839}]).ok


def test_dates_years_ids_ignored() -> None:
    text = (
        "On 2010-05-12 and 12/05/2010 (year 2010), per call toolu_01A9 and "
        "123e4567-e89b-12d3-a456-426614174000 at 2010-05-12T10:30:00Z:\n"
        "1. first point\n2) second point"
    )
    r = verify_numbers(text, [])
    assert r.ok and r.checked == 0


def test_count_must_match_exactly() -> None:
    assert verify_numbers("There were 3 gaps.", [{"n": 3}]).ok
    assert not verify_numbers("There were 3 gaps.", [{"n": 3.4}]).ok
    assert not verify_numbers("There were 4 gaps.", [{"n": 3}]).ok


def test_nested_results() -> None:
    results = [{"a": [{"b": {"c": [0.125, "7"]}}]}, ["x", 99]]
    assert verify_numbers("Values 12.5%, 7 and 99.", results).ok
    assert verify_numbers("Thousands 1,500 here.", [{"v": [{"w": 1500}]}]).ok


def test_booleans_not_numbers() -> None:
    assert not verify_numbers("Count 1.", [{"flag": True}]).ok


def test_window_phrases_ignored() -> None:
    for text in (
        "in the last 90 days",
        "latest weekly 30-day revenue",
        "a 13-week slope",
        "outside the prior 25 weeks",
        "over 12 months",
        "trailing 90d revenue",
        "trailing 13w average",
        "within 5 business days",
        "the last 2 quarters and 1 yr and 3 wks and 6 mo and 2 years",
        "A 30 Day window",
    ):
        r = verify_numbers(text, [])
        assert r.ok and r.checked == 0, text


def test_sigma_phrases_ignored() -> None:
    for text in ("outside mean +/- 2 sigma", "a 3σ move", "beyond 2 σ", "2.5 standard deviations"):
        r = verify_numbers(text, [])
        assert r.ok and r.checked == 0, text


def test_identifier_tokens_ignored() -> None:
    text = "change_30v30 = 0.5; slope_13w, revenue_yoy_90d and revenue_break_26w are set"
    r = verify_numbers(text, [{"x": 0.5}])
    assert r.ok and r.checked == 1  # only the 0.5 is a claim


def test_mixed_window_and_claim() -> None:
    r = verify_numbers("Over the last 90 days revenue fell 7.0% (30-day basis).", [{"d": -0.07}])
    assert r.ok and r.checked == 1


def test_sign_tolerance() -> None:
    pool = [{"change": -0.165}]
    assert verify_numbers("declining (change_30v30 = -16.5%)", pool).ok
    assert verify_numbers("declining 16.5%", pool).ok
    assert verify_numbers("Slope was -0.38.", [{"s": 0.3839}]).ok
    assert verify_numbers("Slope was 0.38.", [{"s": -0.3839}]).ok
    assert verify_numbers("There were 3 gaps.", [{"n": -3}]).ok
    assert not verify_numbers("declining 16.6%", pool).ok


def test_counts_after_non_unit_words_still_checked() -> None:
    r = verify_numbers("There were 21 debits and 305 SKUs.", [{"n": 5}])
    assert not r.ok and r.unmatched == ("21", "305") and r.checked == 2
    assert verify_numbers("There were 21 debits and 305 SKUs.", [{"a": 21, "b": 305}]).ok


def test_derived_percent_flagged_alone() -> None:
    r = verify_numbers("26.0% (41278.16 of 158975.11)", [{"a": 41278.16, "b": 158975.11}])
    assert not r.ok and r.unmatched == ("26.0%",) and r.checked == 3


def test_full_precision_claim_against_large_values_does_not_raise() -> None:
    text = "aged_stock_share: 0.8024050297982365579989458011 of stock worth 28,800.00"
    pool = [{"aged_stock_share": "0.8024050297982365579989458011", "value": "28800.00"}]
    assert verify_numbers(text, pool).ok
    assert verify_numbers(text, [{"value": "123456789012.50"}]).unmatched == (
        "0.8024050297982365579989458011",
        "28,800.00",
    )


def test_whole_currency_amount_matches_rounded_value_but_bare_count_is_exact() -> None:
    pool = [{"revenue": "136831.92", "orders": 199}]
    assert verify_numbers("revenue of GBP 136,832 and £136,832", pool).ok
    assert verify_numbers("199 orders", pool).ok
    assert verify_numbers("136832 orders", pool).unmatched == ("136832",)


def test_hyphenated_reference_ids_are_not_claims() -> None:
    assert verify_numbers("invoice INV-574941 and PO-1182", []).ok


def test_window_ranges_are_not_claims() -> None:
    assert verify_numbers("lag 31-60 days ago and 61–90 days ago", []).ok
