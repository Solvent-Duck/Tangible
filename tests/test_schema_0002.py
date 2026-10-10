import uuid

import psycopg
import pytest

from tangible.db.migrate import migrate

pytestmark = pytest.mark.db


def seed_tenant(conn: psycopg.Connection, name: str) -> tuple[str, str]:
    """Create a tenant + borrower as the admin role, then act as that tenant under RLS."""
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


def make_case(conn: psycopg.Connection, tenant_id: str, borrower_id: str) -> str:
    row = conn.execute(
        "INSERT INTO case_file (tenant_id, borrower_id, as_of, budget_steps, budget_usd) "
        "VALUES (%s, %s, now(), 10, 5) RETURNING id",
        (tenant_id, borrower_id),
    ).fetchone()
    assert row
    return str(row[0])


def make_tool_call(conn: psycopg.Connection, tenant_id: str, case_id: str) -> str:
    row = conn.execute(
        "INSERT INTO tool_call (tenant_id, case_id, tool, tool_version, args, result, started_at) "
        "VALUES (%s, %s, 't', 1, '{}', '{}', now()) RETURNING id",
        (tenant_id, case_id),
    ).fetchone()
    assert row
    return str(row[0])


def make_plan(conn: psycopg.Connection, tenant_id: str, case_id: str) -> str:
    row = conn.execute(
        "INSERT INTO agent_plan (tenant_id, case_id, version, body) "
        "VALUES (%s, %s, 1, '{}') RETURNING id",
        (tenant_id, case_id),
    ).fetchone()
    assert row
    return str(row[0])


def make_question(conn: psycopg.Connection, tenant_id: str, case_id: str) -> str:
    row = conn.execute(
        "INSERT INTO borrower_question (tenant_id, case_id, body) VALUES (%s, %s, 'q?') "
        "RETURNING id",
        (tenant_id, case_id),
    ).fetchone()
    assert row
    return str(row[0])


def make_model_call(conn: psycopg.Connection, tenant_id: str, case_id: str) -> str:
    row = conn.execute(
        "INSERT INTO model_call (tenant_id, case_id, model_id, prompt_key, prompt_version, "
        "prompt_sha256, input_tokens, output_tokens, cost_usd) "
        "VALUES (%s, %s, 'm', 'k', '1', 'h', 1, 1, 0.001) RETURNING id",
        (tenant_id, case_id),
    ).fetchone()
    assert row
    return str(row[0])


def make_audit(conn: psycopg.Connection, tenant_id: str, case_id: str) -> str:
    row = conn.execute(
        "INSERT INTO audit_log (tenant_id, actor, action, object_kind, object_id) "
        "VALUES (%s, 'a', 'x', 'case_file', %s) RETURNING id",
        (tenant_id, case_id),
    ).fetchone()
    assert row
    return str(row[0])


APPEND_ONLY = {
    "agent_plan": make_plan,
    "tool_call": make_tool_call,
    "model_call": make_model_call,
    "borrower_question": make_question,
    "audit_log": make_audit,
}


def test_migrate_is_idempotent(test_db_url: str):
    assert migrate(test_db_url) == []


def test_rls_isolates_new_tables(test_db_url: str):
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        ta, ba = seed_tenant(conn, "lender-a2")
        case_a = make_case(conn, ta, ba)
        for make in APPEND_ONLY.values():
            make(conn, ta, case_a)
        for table in ["case_file", *APPEND_ONLY]:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (1,), table
        seed_tenant(conn, "lender-b2")  # current tenant is now b
        for table in ["case_file", *APPEND_ONLY]:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), table
        # cannot write a case for tenant A while acting as B
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO case_file (tenant_id, borrower_id, as_of, budget_steps, budget_usd) "
                "VALUES (%s, %s, now(), 1, 1)",
                (ta, ba),
            )


def test_no_tenant_set_sees_nothing_new_tables(test_db_url: str):
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        ta, ba = seed_tenant(conn, "lender-c2")
        case_id = make_case(conn, ta, ba)
        for make in APPEND_ONLY.values():
            make(conn, ta, case_id)
        conn.execute("RESET app.tenant_id")
        for table in ["case_file", *APPEND_ONLY]:
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), table


@pytest.mark.parametrize("table", list(APPEND_ONLY))
def test_append_only_tables_reject_update_and_delete(test_db_url: str, table: str):
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        tenant_id, borrower_id = seed_tenant(conn, f"lender-ao-{table}")
        case_id = make_case(conn, tenant_id, borrower_id)
        row_id = APPEND_ONLY[table](conn, tenant_id, case_id)
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute(f"UPDATE {table} SET tenant_id = tenant_id WHERE id = %s", (row_id,))
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute(f"DELETE FROM {table} WHERE id = %s", (row_id,))


def test_case_file_status_is_mutable_and_checked(test_db_url: str):
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        tenant_id, borrower_id = seed_tenant(conn, "lender-cs")
        case_id = make_case(conn, tenant_id, borrower_id)
        conn.execute("UPDATE case_file SET status = 'review' WHERE id = %s", (case_id,))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE case_file SET status = 'bogus' WHERE id = %s", (case_id,))


def test_tool_call_result_xor_error(test_db_url: str):
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        tenant_id, borrower_id = seed_tenant(conn, "lender-tc")
        case_id = make_case(conn, tenant_id, borrower_id)
        insert = (
            "INSERT INTO tool_call (tenant_id, case_id, tool, tool_version, args, result, error, "
            "started_at) VALUES (%s, %s, 't', 1, '{}', %s, %s, now())"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert, (tenant_id, case_id, None, None))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert, (tenant_id, case_id, "{}", "boom"))
        conn.execute(insert, (tenant_id, case_id, "{}", None))
        conn.execute(insert, (tenant_id, case_id, None, "boom"))


def test_underwriter_action_targets_and_actions(test_db_url: str):
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        tenant_id, borrower_id = seed_tenant(conn, "lender-ua")
        case_id = make_case(conn, tenant_id, borrower_id)
        plan_id = make_plan(conn, tenant_id, case_id)
        question_id = make_question(conn, tenant_id, case_id)
        finding = conn.execute(
            "INSERT INTO finding (tenant_id, borrower_id, check_key, check_version, severity, "
            "pillar, summary) VALUES (%s, %s, 'k', 1, 'review', 'integrity', 's') RETURNING id",
            (tenant_id, borrower_id),
        ).fetchone()
        assert finding
        finding_id = str(finding[0])

        def act(action: str, **targets: str | None) -> None:
            cols = ["finding_id", "assessment_id", "case_id", "question_id", "plan_id"]
            vals = [targets.get(c) for c in cols]
            conn.execute(
                "INSERT INTO underwriter_action (tenant_id, finding_id, assessment_id, case_id, "
                "question_id, plan_id, user_id, action, reason) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'u', %s, 'because')",
                (tenant_id, *vals, action),
            )

        act("dispute", finding_id=finding_id)  # old style still works
        act("accept", finding_id=finding_id)
        act("reject", finding_id=finding_id)
        act("approve_plan", plan_id=plan_id)
        act("approve_question", question_id=question_id)
        act("sign", case_id=case_id)
        with pytest.raises(psycopg.errors.CheckViolation):
            act("sign")  # zero targets
        with pytest.raises(psycopg.errors.CheckViolation):
            act("sign", case_id=case_id, plan_id=plan_id)  # two targets
        with pytest.raises(psycopg.errors.CheckViolation):
            act("bogus", case_id=case_id)


def test_finding_case_id_and_verified(test_db_url: str):
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        tenant_id, borrower_id = seed_tenant(conn, "lender-fc")
        case_id = make_case(conn, tenant_id, borrower_id)
        insert = (
            "INSERT INTO finding (tenant_id, borrower_id, check_key, check_version, severity, "
            "pillar, summary, case_id, verified) "
            "VALUES (%s, %s, 'k', 1, 'review', 'integrity', 's', %s, %s) "
            "RETURNING case_id, verified"
        )
        row = conn.execute(insert, (tenant_id, borrower_id, case_id, True)).fetchone()
        assert row == (uuid.UUID(case_id), True)
        row = conn.execute(insert, (tenant_id, borrower_id, None, None)).fetchone()
        assert row == (None, None)
