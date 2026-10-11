"""A live, display-only trail of what the agents are doing, per case, in this process.

An investigation commits once, at the end, so its tool_call rows are not visible while it runs.
The engine posts short lines here and the workbench's status poll shows them. Nothing here is
stored or trusted: the audit trail is the database, written as before.
"""

import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class FeedLine:
    at: float
    agent: str
    kind: str  # start | step | tool | error | blocked | finding | stop
    text: str


class ProgressFeed:
    def __init__(self, per_case: int = 200) -> None:
        self._lock = threading.Lock()
        self._lines: dict[str, deque[FeedLine]] = {}
        self._per_case = per_case

    def start(self, case_id: str) -> None:
        with self._lock:
            self._lines[case_id] = deque(maxlen=self._per_case)

    def add(self, case_id: str, agent: str, kind: str, text: str) -> None:
        with self._lock:
            lines = self._lines.setdefault(case_id, deque(maxlen=self._per_case))
            lines.append(FeedLine(time.time(), agent, kind, text))

    def recent(self, case_id: str, n: int = 12) -> list[FeedLine]:
        with self._lock:
            return list(self._lines.get(case_id, ()))[-n:]


FEED = ProgressFeed()
