"""Review state of a finding, derived from underwriter actions.

Review state is derived from append-only ``underwriter_action`` rows; ``finding.status`` is
never updated.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

ReviewState = Literal["open", "accepted", "rejected", "disputed"]

_ACTION_STATE: dict[str, ReviewState] = {
    "accept": "accepted",
    "reject": "rejected",
    "dispute": "disputed",
    "request_more": "open",
}

_TIE_RANK = {"reject": 0, "dispute": 1, "accept": 2, "request_more": 3}


@dataclass(frozen=True)
class ActionInput:
    action: str
    at: datetime


def effective_state(actions: Iterable[ActionInput]) -> ReviewState:
    """Latest relevant action (by ``at``) wins; no relevant action means ``open``.

    Actions that do not affect review state (override, approve_plan, sign, ...) are ignored.
    Ties on ``at`` are broken conservatively, so the result does not depend on input order and a
    tie never clears a finding: request_more > accept > dispute > reject (states that keep a hold
    blocking win over states that release it).
    """
    relevant = [a for a in actions if a.action in _ACTION_STATE]
    if not relevant:
        return "open"
    latest = max(relevant, key=lambda a: (a.at, _TIE_RANK[a.action]))
    return _ACTION_STATE[latest.action]
