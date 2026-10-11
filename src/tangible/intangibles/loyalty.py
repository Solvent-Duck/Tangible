"""loyalty_mentions: a versioned keyword rule over review excerpts. No model, no sentiment.

An excerpt "mentions loyalty" when it speaks of coming back, being a regular, or years as a
customer. A changed phrase list is a new version (add LOYALTY_PATTERNS_V2); never edit v1.
Review text is untrusted: it is only ever matched against these patterns, never interpreted.
"""

import re
from collections.abc import Iterable

LOYALTY_MENTIONS_VERSION = 1

LOYALTY_PATTERNS_V1: tuple[str, ...] = (
    r"\bregulars?\b",
    r"\bevery (?:week|weekend|saturday|sunday|morning|visit)\b",
    r"\bfor (?:years|\d+ years|over a decade)\b",
    r"\b(?:years|months) now\b",
    r"\bkeep (?:coming|going) back\b",
    r"\b(?:come|came|coming) back\b",
    r"\breturn(?:ing)? customers?\b",
    r"\bloyal\b",
    r"\bsince (?:19|20)\d\d\b",
    r"\bmy (?:go-to|usual)\b",
    r"\bgo-to\b",
    r"\bbeen (?:coming|going|shopping|ordering|buying) (?:here|from them|with them)\b",
)
_V1 = re.compile("|".join(LOYALTY_PATTERNS_V1), re.IGNORECASE)


def mentions_loyalty_v1(text: str) -> bool:
    return bool(_V1.search(text))


def loyalty_share_v1(excerpts: Iterable[str]) -> tuple[int, int]:
    """(excerpts matching v1, excerpts seen)."""
    seen = hits = 0
    for t in excerpts:
        seen += 1
        hits += mentions_loyalty_v1(t)
    return hits, seen
