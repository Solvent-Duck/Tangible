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
