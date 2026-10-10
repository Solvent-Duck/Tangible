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


def test_migrate_is_idempotent(test_db_url: str):
    assert migrate(test_db_url) == []


def test_rls_isolates_tenants(test_db_url: str):
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        seed_tenant(conn, "lender-a")
        seed_tenant(conn, "lender-b")  # current tenant is now b
        assert conn.execute("SELECT name FROM borrower").fetchall() == [("lender-b",)]
        assert conn.execute("SELECT count(*) FROM tenant").fetchone() == (1,)
        # cannot write into another tenant
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO borrower (tenant_id, name) VALUES (gen_random_uuid(), 'x')")


def test_no_tenant_set_sees_nothing(test_db_url: str):
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        conn.execute("SET ROLE tangible_app")
        conn.execute("RESET app.tenant_id")
        assert conn.execute("SELECT count(*) FROM borrower").fetchone() == (0,)


def test_assessment_is_append_only(test_db_url: str):
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        tenant_id, borrower_id = seed_tenant(conn, "lender-c")
        row = conn.execute(
            "INSERT INTO assessment (tenant_id, borrower_id, as_of, evidence_set_hash, "
            "metric_set_version, policy_version, policy_hash, created_by) "
            "VALUES (%s, %s, now(), 'h', 'm1', 'p1', 'ph', 'test') RETURNING id",
            (tenant_id, borrower_id),
        ).fetchone()
        assert row
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("UPDATE assessment SET decision_band = 'A' WHERE id = %s", (row[0],))
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("DELETE FROM assessment WHERE id = %s", (row[0],))


def test_model_finding_requires_tool_citations(test_db_url: str):
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        tenant_id, borrower_id = seed_tenant(conn, "lender-d")
        insert = (
            "INSERT INTO finding (tenant_id, borrower_id, check_key, check_version, origin, "
            "severity, pillar, summary, tool_call_ids) "
            "VALUES (%s, %s, 'k', 1, 'model', 'review', 'integrity', 's', %s)"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert, (tenant_id, borrower_id, []))
        conn.execute(insert, (tenant_id, borrower_id, ["call_1"]))
