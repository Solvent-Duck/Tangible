"""A corrected finding replaces the earlier one: schema, the current-findings helper, freeze,
policy inputs and replay."""

import uuid
from typing import Any

import psycopg
import pytest

from tangible.assessment import freeze, replay, run_baseline
from tangible.checks.registry import FindingDraft
from tangible.db.findings import (
    StoredFinding,
    current_findings,
    insert_finding,
    load_findings,
    replaced_by,
)
from tangible.db.session import Conn, tenant_session
from tangible.policy import Policy, load_policy
from test_assessment import (  # noqa: F401
    POLICY_PATH,
    World,
    act,
    baseline_and_freeze,
    datasets,
    do_replay,
    new_case,
)
from test_assessment import clean as clean_world  # noqa: F401
from test_assessment import hidden as hidden_world  # noqa: F401
from test_schema_0002 import make_case, seed_tenant

pytestmark = pytest.mark.db


@pytest.fixture
def policy() -> Policy:
    return load_policy(POLICY_PATH)


@pytest.fixture
def cw(clean_world: World) -> World:  # noqa: F811
    return clean_world


@pytest.fixture
def hw(hidden_world: World) -> World:  # noqa: F811
    return hidden_world


# ---------------------------------------------------------------- schema


def raw_finding(
    conn: psycopg.Connection, tenant: str, borrower: str, case: str, **extra: Any
) -> str:
    cols = ["tenant_id", "borrower_id", "check_key", "check_version", "severity", "pillar"]
    vals: list[Any] = [tenant, borrower, "agent:k", 1, "review", "integrity"]
    cols += ["summary", "case_id"]
    vals += ["s", case]
    for k, v in extra.items():
        cols.append(k)
        vals.append(v)
    row = conn.execute(
        f"INSERT INTO finding ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
        "RETURNING id",
        vals,
    ).fetchone()
    assert row
    return str(row[0])


def test_replaces_column_constraints(test_db_url: str) -> None:
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        ta, ba = seed_tenant(conn, "lender-rep-a")
        case = make_case(conn, ta, ba)
        first = raw_finding(conn, ta, ba, case)
        assert conn.execute(
            "SELECT replaces_id FROM finding WHERE id = %s", (first,)
        ).fetchone() == (None,)
        second = raw_finding(conn, ta, ba, case, replaces_id=first)
        assert conn.execute(
            "SELECT replaces_id FROM finding WHERE id = %s", (second,)
        ).fetchone() == (uuid.UUID(first),)
        # at most once
        with pytest.raises(psycopg.errors.UniqueViolation):
            raw_finding(conn, ta, ba, case, replaces_id=first)
        # not itself
        me = str(uuid.uuid4())
        with pytest.raises(psycopg.errors.CheckViolation):
            raw_finding(conn, ta, ba, case, id=me, replaces_id=me)
        # unknown id
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            raw_finding(conn, ta, ba, case, replaces_id=str(uuid.uuid4()))
        # a chain is fine: a correction of a correction
        raw_finding(conn, ta, ba, case, replaces_id=second)


def test_rls_still_holds_on_finding(test_db_url: str) -> None:
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        ta, ba = seed_tenant(conn, "lender-rep-b")
        case_a = make_case(conn, ta, ba)
        fa = raw_finding(conn, ta, ba, case_a)
        raw_finding(conn, ta, ba, case_a, replaces_id=fa)
        open_a = raw_finding(conn, ta, ba, case_a)  # not replaced by anything

        flags = conn.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'finding'"
        ).fetchone()
        assert flags == (True, True)

        tb, bb = seed_tenant(conn, "lender-rep-c")  # now acting as tenant B
        assert conn.execute("SELECT count(*) FROM finding").fetchone() == (0,)
        case_b = make_case(conn, tb, bb)
        # B cannot write a finding for tenant A, and cannot replace A's finding even though the
        # id is valid: the tenant-scoped reference rejects it.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            raw_finding(conn, ta, ba, case_a)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            raw_finding(conn, tb, bb, case_b, replaces_id=open_a)

        conn.execute("RESET app.tenant_id")
        assert conn.execute("SELECT count(*) FROM finding").fetchone() == (0,)


# ---------------------------------------------------------------- helper


def stored(fid: str, replaces: str | None = None) -> StoredFinding:
    return StoredFinding(
        fid, FindingDraft("agent:k", 1, "review", "integrity", "s"), "model", "c", ("t",), True,
        replaces,
    )  # fmt: skip


def test_current_findings_helper_is_pure() -> None:
    a, b, c, d = stored("a"), stored("b", "a"), stored("c", "b"), stored("d")
    assert current_findings([a, b, c, d]) == [c, d]
    assert current_findings([a, b]) == [b]
    assert current_findings([a, d]) == [a, d]
    assert current_findings([]) == []


def test_load_findings_and_replaced_by(cw: World) -> None:
    with tenant_session(cw.url, cw.tenant) as conn:
        case = new_case(conn, cw)
        ids = model_chain(conn, cw, case.id, ["review", "review", "info"])
        # rows of one transaction share created_at, so compare by id, not by position
        everything = {f.id: f for f in load_findings(conn, case_id=case.id)}
        assert set(everything) == set(ids)
        assert [everything[i].replaces_id for i in ids] == [None, ids[0], ids[1]]
        assert [f.id for f in load_findings(conn, case_id=case.id, current_only=True)] == [ids[2]]
        assert replaced_by(conn, ids) == {ids[0]: ids[1], ids[1]: ids[2]}
        assert replaced_by(conn, []) == {}


def model_chain(conn: Conn, w: World, case_id: str, severities: list[str]) -> list[str]:
    """Model findings where each one replaces the one before; returns their ids in order."""
    ids: list[str] = []
    for i, sev in enumerate(severities):
        ids.append(
            insert_finding(
                conn,
                w.tenant,
                w.borrower,
                FindingDraft(f"agent:v{i}", 1, sev, "integrity", f"version {i}"),  # type: ignore[arg-type]
                origin="model",
                case_id=case_id,
                tool_call_ids=["c"],
                verified=i > 0,
                replaces_id=ids[-1] if ids else None,
            )  # fmt: skip
        )
    return ids


# ---------------------------------------------------------------- freeze and policy


def test_freeze_uses_current_findings_only(cw: World, policy: Policy) -> None:
    """An accepted, unverified hold blocks a decision; once a review-severity correction replaces
    it, the hold feeds neither the policy nor the assessment items."""
    with tenant_session(cw.url, cw.tenant) as conn:
        control = new_case(conn, cw)
        run_baseline(conn, control, policy)
        (hold_c,) = model_chain(conn, cw, control.id, ["hold"])
        act(conn, cw, hold_c, "accept")
        corrected = new_case(conn, cw)
        run_baseline(conn, corrected, policy)
        hold, fix = model_chain(conn, cw, corrected.id, ["hold", "review"])
        act(conn, cw, hold, "accept")  # accepted, then replaced

    with tenant_session(cw.url, cw.tenant) as conn:
        unreplaced = freeze(conn, control, policy, created_by="ana")
    assert unreplaced.result.band is None  # the model hold, accepted, is a knockout

    with tenant_session(cw.url, cw.tenant) as conn:
        frozen = freeze(conn, corrected, policy, created_by="ana")
        assert frozen.result.band is not None  # same hold, but replaced: not an input
        items = {
            str(r[0])
            for r in conn.execute(
                "SELECT ref_id FROM assessment_item WHERE assessment_id = %s AND kind = 'finding'",
                (frozen.assessment_id,),
            ).fetchall()
        }
        assert fix in items and hold not in items
        # the replaced finding is still on file, unchanged
        assert {f.id for f in load_findings(conn, case_id=corrected.id)} >= {hold, fix}

    assert do_replay(cw, frozen.assessment_id) == (True, [])
    assert do_replay(cw, unreplaced.assessment_id) == (True, [])


def test_the_replacement_is_what_the_policy_sees(cw: World, policy: Policy) -> None:
    """The mirror image: a harmless finding replaced by an accepted hold does knock out."""
    with tenant_session(cw.url, cw.tenant) as conn:
        case = new_case(conn, cw)
        run_baseline(conn, case, policy)
        info, hold = model_chain(conn, cw, case.id, ["info", "hold"])
        act(conn, cw, hold, "accept")
    with tenant_session(cw.url, cw.tenant) as conn:
        frozen = freeze(conn, case, policy, created_by="ana")
    assert frozen.result.band is None
    assert do_replay(cw, frozen.assessment_id) == (True, [])


# ---------------------------------------------------------------- replay of older assessments


def test_replay_of_an_assessment_frozen_before_replacements_is_unchanged(
    hw: World, policy: Policy
) -> None:
    case_id, aid = baseline_and_freeze(hw, policy)
    with tenant_session(hw.url, hw.tenant) as conn:
        # nothing in this assessment (or its case) was ever replaced
        assert all(f.replaces_id is None for f in load_findings(conn, case_id=case_id))
        n_items = conn.execute(
            "SELECT count(*) FROM assessment_item WHERE assessment_id = %s AND kind = 'finding'",
            (aid,),
        ).fetchone()
        assert n_items == (len(load_findings(conn, case_id=case_id)),)
    assert do_replay(hw, aid) == (True, [])
    # and replay does not change when a later correction is added beside the frozen findings
    with tenant_session(hw.url, hw.tenant) as conn:
        model_chain(conn, hw, case_id, ["info", "review"])
        rep = replay(conn, aid, POLICY_PATH)
    assert rep.match and rep.diffs == []
