from datetime import UTC, datetime, timedelta

import pytest

from tangible.policy import ActionInput, effective_state

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def at(n: int) -> datetime:
    return T0 + timedelta(hours=n)


def test_no_actions_is_open():
    assert effective_state([]) == "open"


@pytest.mark.parametrize(
    ("action", "state"),
    [
        ("accept", "accepted"),
        ("reject", "rejected"),
        ("dispute", "disputed"),
        ("request_more", "open"),
    ],
)
def test_action_mapping(action, state):
    assert effective_state([ActionInput(action, at(1))]) == state


def test_irrelevant_actions_ignored():
    assert effective_state([ActionInput("sign", at(1)), ActionInput("override", at(2))]) == "open"


def test_latest_wins_regardless_of_input_order():
    acts = [
        ActionInput("reject", at(3)),
        ActionInput("accept", at(1)),
        ActionInput("dispute", at(2)),
    ]
    assert effective_state(acts) == "rejected"
    assert effective_state(list(reversed(acts))) == "rejected"


def test_request_more_reopens():
    assert (
        effective_state([ActionInput("accept", at(1)), ActionInput("request_more", at(2))])
        == "open"
    )


def test_tie_broken_by_action_name_independent_of_order():
    a, b = ActionInput("accept", at(1)), ActionInput("reject", at(1))
    assert effective_state([a, b]) == effective_state([b, a]) == "rejected"
