"""Pointer from a ledger row back to the raw bytes it came from."""

from dataclasses import dataclass


@dataclass(frozen=True)
class EvidenceRef:
    """`pull_id` names the evidence pull; `locator` is the row/byte offset in the raw file."""

    pull_id: str
    locator: str

    def __str__(self) -> str:
        return f"{self.pull_id}#{self.locator}"

    @classmethod
    def parse(cls, s: str) -> "EvidenceRef":
        """Inverse of `str()`. Splits on the first '#'; the locator may itself contain '#'."""
        pull_id, sep, locator = s.partition("#")
        if not sep or not pull_id or not locator:
            raise ValueError(f"malformed evidence ref: {s!r}")
        return cls(pull_id, locator)
