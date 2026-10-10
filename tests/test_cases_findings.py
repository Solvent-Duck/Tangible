from datetime import UTC, datetime

import pytest

from tangible.checks.registry import FindingDraft
from tangible.db.cases import create_case, get_case, set_case_status
from tangible.db.findings import insert_finding, load_findings
from tangible.db.repo import create_borrower
from tangible.db.session import admin_connect, tenant_session

pytestmark = pytest.mark.db


def test_case_and_finding_round_trip(test_db_url: str) -> None:
    with admin_connect(test_db_url) as admin:
        row = admin.execute("INSERT INTO tenant (name) VALUES ('t') RETURNING id").fetchone()
        assert row
        tenant = str(row[0])
    with tenant_session(test_db_url, tenant) as conn:
        borrower = create_borrower(conn, tenant, "b")
        case = create_case(conn, tenant, borrower, datetime(2010, 7, 1, tzinfo=UTC))
        assert get_case(conn, case.id).status == "open"
        set_case_status(conn, case.id, "investigating")
        assert get_case(conn, case.id).status == "investigating"
        draft = FindingDraft(
            "k", 1, "review", "integrity", "s", ("p#row=1",), facts={"gap": "0.12"}
        )
        fid = insert_finding(conn, tenant, borrower, draft, case_id=case.id)
        model = insert_finding(
            conn, tenant, borrower, draft, origin="model", case_id=case.id,
            tool_call_ids=["c1"], verified=False,
        )  # fmt: skip
        (a, b) = load_findings(conn, case_id=case.id)
        assert {a.id, b.id} == {fid, model}
        assert a.draft == draft and a.draft.facts == {"gap": "0.12"}
        stored_model = a if a.id == model else b
        assert stored_model.origin == "model" and stored_model.verified is False
        assert stored_model.tool_call_ids == ("c1",)
