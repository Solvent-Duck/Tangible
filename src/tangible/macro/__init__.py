"""Systemic risk v1 (docs/systemic-risk-plan.md): loading dated public facts about the world.

v1 loads tariff actions from a normalised JSON file (the demo fixtures, or an export of
Federal Register notices joined to their HTS changes). Rows go in through the owning role only.
"""

import argparse
import json
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tangible.config import database_url
from tangible.db.macro import insert_macro_signal
from tangible.db.session import Conn, admin_connect

DEMO_TARIFFS = Path(__file__).parent / "fixtures" / "demo_tariff_actions.json"


class MacroError(ValueError):
    pass


def _day(v: Any, field: str) -> datetime:
    try:
        return datetime.combine(date.fromisoformat(str(v)), time(), UTC)
    except ValueError as e:
        raise MacroError(f"{field}: not an ISO date: {v!r}") from e


def parse_tariff_action(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate one normalised tariff action; returns the payload to store."""
    try:
        duty = Decimal(str(raw["additional_duty"]))
    except (KeyError, InvalidOperation) as e:
        raise MacroError(f"additional_duty missing or not a number in {raw!r}") from e
    if not Decimal(0) < duty <= Decimal(5):
        raise MacroError(f"additional_duty out of range: {duty}")
    chapters = [str(c) for c in raw.get("hs_chapters") or []]
    countries = [str(c).upper() for c in raw.get("countries") or []]
    if not chapters or not all(len(c) == 2 and c.isdigit() for c in chapters):
        raise MacroError(f"hs_chapters must be two-digit codes: {chapters}")
    if not countries or not all(len(c) == 2 and c.isalpha() for c in countries):
        raise MacroError(f"countries must be ISO alpha-2 codes: {countries}")
    return {
        "document_number": str(raw["document_number"]),
        "title": str(raw.get("title", ""))[:500],
        "publication_date": _day(raw["publication_date"], "publication_date").date().isoformat(),
        "effective_on": _day(raw["effective_on"], "effective_on").date().isoformat(),
        "hs_chapters": chapters,
        "countries": countries,
        "additional_duty": str(duty),
    }


def load_tariff_actions(admin: Conn, path: Path = DEMO_TARIFFS) -> int:
    """Load a normalised tariff-action file; returns how many new rows were added."""
    data = json.loads(path.read_text("utf-8"))
    added = 0
    for raw in data["actions"]:
        p = parse_tariff_action(raw)
        added += insert_macro_signal(
            admin,
            source="federal_register",
            source_ref=p["document_number"],
            kind="tariff_action",
            payload=p,
            observed_at=_day(p["publication_date"], "publication_date"),  # when it became public
            occurred_at=_day(p["effective_on"], "effective_on"),
            url=raw.get("url"),
        )
    return added


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tangible-macro", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    load = sub.add_parser("load", help="load tariff actions (default: the fictional demo set)")
    load.add_argument("--file", type=Path, default=DEMO_TARIFFS)
    args = ap.parse_args(argv)
    with admin_connect(database_url()) as admin:
        n = load_tariff_actions(admin, args.file)
    print(f"loaded {n} new tariff action(s) from {args.file}")
    return 0
