import json
import shutil
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import NamedTuple

import psycopg
import pytest

from tangible.assessment import (
    freeze,
    record_override,
    replay,
    replay_sample,
    run_baseline,
)
from tangible.assessment.cli import main as replay_main
from tangible.assessment.hashing import canonical_json, evidence_set_hash, ledger_input_hash
from tangible.config import database_url
from tangible.datasets.synthetic import generate
from tangible.db.cases import Case, create_case, get_case
from tangible.db.findings import load_findings
from tangible.db.repo import create_borrower, load_ledger
from tangible.db.session import Conn, admin_connect, tenant_session
from tangible.evidence.store import LocalEvidenceStore
from tangible.ingest.synthetic_dir import ingest_synthetic_dir
from tangible.metrics.sets import metric_set
from tangible.policy import Policy, load_policy
from test_synthetic import write_lines

pytestmark = pytest.mark.db

POLICY_PATH = Path(__file__).parent.parent / "policies/default.yaml"


class World(NamedTuple):
    url: str
    tenant: str
    borrower: str
    as_of: datetime


@pytest.fixture(scope="module")
def datasets(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, Path]]:
    root = tmp_path_factory.mktemp("assessment")
    lines = write_lines(root / "lines.csv", days=200)
    out: dict[str, tuple[Path, Path]] = {}
    for name, plant in (("clean", ()), ("hidden_reserve", ("hidden_reserve",))):
        generate(lines, root / name, seed=7, plant=set(plant))
        out[name] = (root / name, lines)
    return out


@pytest.fixture
def policy() -> Policy:
    return load_policy(POLICY_PATH)


def make_world(url: str, data: tuple[Path, Path], store: Path, name: str = "assess") -> World:
    with admin_connect(url) as admin:
        row = admin.execute(
            "INSERT INTO tenant (name) VALUES (%s) RETURNING id", (name,)
        ).fetchone()
        assert row
        tenant = str(row[0])
    with tenant_session(url, tenant) as conn:
        borrower = create_borrower(conn, tenant, name)
        ingest_synthetic_dir(conn, LocalEvidenceStore(store), tenant, borrower, data[0], data[1])
        ledger = load_ledger(conn, borrower)
    last = max(t.posted_on for t in ledger.bank_txns)
    return World(url, tenant, borrower, datetime.combine(last + timedelta(days=1), time(12), UTC))


@pytest.fixture
def clean(test_db_url: str, datasets: dict[str, tuple[Path, Path]], tmp_path: Path) -> World:
    return make_world(test_db_url, datasets["clean"], tmp_path / "s")


@pytest.fixture
def hidden(test_db_url: str, datasets: dict[str, tuple[Path, Path]], tmp_path: Path) -> World:
    return make_world(test_db_url, datasets["hidden_reserve"], tmp_path / "s")


def new_case(conn: Conn, w: World) -> Case:
    return create_case(conn, w.tenant, w.borrower, w.as_of)


def baseline_and_freeze(w: World, policy: Policy) -> tuple[str, str]:
    """(case_id, assessment_id) with baseline and freeze in separate transactions."""
    with tenant_session(w.url, w.tenant) as conn:
        case = new_case(conn, w)
        run_baseline(conn, case, policy)
    with tenant_session(w.url, w.tenant) as conn:
        frozen = freeze(conn, case, policy, created_by="alice")
    return case.id, frozen.assessment_id


def do_replay(w: World, assessment_id: str, path: Path = POLICY_PATH) -> tuple[bool, list[str]]:
    with tenant_session(w.url, w.tenant) as conn:
        rep = replay(conn, assessment_id, path)
    return rep.match, rep.diffs


def act(conn: Conn, w: World, finding_id: str, action: str) -> None:
    conn.execute(
        "INSERT INTO underwriter_action (tenant_id, finding_id, user_id, action, reason) "
        "VALUES (%s, %s, 'ana', %s, 'because')",
        (w.tenant, finding_id, action),
    )


def hold_ids(conn: Conn, case_id: str) -> list[str]:
    return [f.id for f in load_findings(conn, case_id=case_id) if f.draft.severity == "hold"]


# ---------------------------------------------------------------- hashing


def test_canonical_json_is_stable() -> None:
    from decimal import Decimal

    a = canonical_json({"b": Decimal("1.50"), "a": [datetime(2020, 1, 1, tzinfo=UTC)]})
    assert a == '{"a":["2020-01-01T00:00:00+00:00"],"b":"1.50"}'


def test_hashes_are_deterministic(clean: World) -> None:
    with tenant_session(clean.url, clean.tenant) as conn:
        known = load_ledger(conn, clean.borrower).known(clean.as_of)
        assert ledger_input_hash(known) == ledger_input_hash(known)
        assert evidence_set_hash(conn, known) == evidence_set_hash(conn, known)
        assert ledger_input_hash(known) != ledger_input_hash(
            load_ledger(conn, clean.borrower).known(clean.as_of - timedelta(days=30))
        )


# ---------------------------------------------------------------- baseline


def test_baseline_writes_everything_and_is_idempotent(clean: World, policy: Policy) -> None:
    with tenant_session(clean.url, clean.tenant) as conn:
        case = new_case(conn, clean)
        first = run_baseline(conn, case, policy)
    assert policy.parsed
    assert set(first.metric_ids) == set(metric_set(policy.parsed.metric_set))
    with tenant_session(clean.url, clean.tenant) as conn:
        stored = load_findings(conn, case_id=case.id)
        assert len(stored) == len(first.finding_ids) and all(f.origin == "rule" for f in stored)
        n_metrics = conn.execute("SELECT count(*) FROM metric_value").fetchone()
        second = run_baseline(conn, case, policy)
        assert second == first
        assert len(load_findings(conn, case_id=case.id)) == len(stored)
        assert conn.execute("SELECT count(*) FROM metric_value").fetchone() == n_metrics


# ---------------------------------------------------------------- freeze


def test_freeze_clean_writes_assessment(clean: World, policy: Policy) -> None:
    case_id, aid = baseline_and_freeze(clean, policy)
    with tenant_session(clean.url, clean.tenant) as conn:
        row = conn.execute(
            "SELECT decision_band, suggested_limit, policy_hash, created_by, rules_fired "
            "FROM assessment WHERE id = %s",
            (aid,),
        ).fetchone()
        assert row and row[0] is not None and row[1] is not None
        assert row[2] == policy.sha256 and row[3] == "alice" and row[4]
        kinds = dict(
            conn.execute(
                "SELECT kind, count(*) FROM assessment_item WHERE assessment_id = %s GROUP BY kind",
                (aid,),
            ).fetchall()
        )
        assert policy.parsed
        assert kinds["metric"] == len(metric_set(policy.parsed.metric_set))
        assert kinds.get("finding", 0) == len(load_findings(conn, case_id=case_id))
        assert get_case(conn, case_id).status == "signed"
        assert conn.execute(
            "SELECT count(*) FROM underwriter_action WHERE case_id = %s AND action = 'sign'",
            (case_id,),
        ).fetchone() == (1,)


def test_freeze_hold_is_knockout(hidden: World, policy: Policy) -> None:
    _, aid = baseline_and_freeze(hidden, policy)
    with tenant_session(hidden.url, hidden.tenant) as conn:
        row = conn.execute(
            "SELECT decision_band, suggested_limit FROM assessment WHERE id = %s", (aid,)
        ).fetchone()
        assert row == (None, None)
    assert do_replay(hidden, aid) == (True, [])


def test_freeze_requires_baseline_and_cannot_repeat(clean: World, policy: Policy) -> None:
    with tenant_session(clean.url, clean.tenant) as conn:
        case = new_case(conn, clean)
    with pytest.raises(ValueError, match="no baseline"):
        with tenant_session(clean.url, clean.tenant) as conn:
            freeze(conn, case, policy, created_by="alice")
    with tenant_session(clean.url, clean.tenant) as conn:
        run_baseline(conn, case, policy)
        freeze(conn, case, policy, created_by="alice")
    with pytest.raises(ValueError, match="already signed"):
        with tenant_session(clean.url, clean.tenant) as conn:
            freeze(conn, case, policy, created_by="alice")


def test_assessment_stays_immutable(clean: World, policy: Policy) -> None:
    _, aid = baseline_and_freeze(clean, policy)
    for stmt in (
        "UPDATE assessment SET decision_band = 'D' WHERE id = %s",
        "DELETE FROM assessment WHERE id = %s",
        "DELETE FROM assessment_item WHERE assessment_id = %s",
    ):
        with pytest.raises(psycopg.Error, match="immutable"):
            with tenant_session(clean.url, clean.tenant) as conn:
                conn.execute(stmt, (aid,))


def test_record_override_sits_beside(clean: World, policy: Policy) -> None:
    _, aid = baseline_and_freeze(clean, policy)
    q = "SELECT row(a.*)::text FROM assessment a WHERE id = %s"
    with tenant_session(clean.url, clean.tenant) as conn:
        before = conn.execute(q, (aid,)).fetchone()
    with tenant_session(clean.url, clean.tenant) as conn:
        record_override(conn, aid, "boss", "known customer")
    with tenant_session(clean.url, clean.tenant) as conn:
        assert conn.execute(q, (aid,)).fetchone() == before
        assert conn.execute(
            "SELECT user_id, reason FROM underwriter_action "
            "WHERE assessment_id = %s AND action = 'override'",
            (aid,),
        ).fetchall() == [("boss", "known customer")]
    assert do_replay(clean, aid) == (True, [])


# ---------------------------------------------------------------- replay


def test_replay_fresh_matches(clean: World, policy: Policy) -> None:
    _, aid = baseline_and_freeze(clean, policy)
    assert do_replay(clean, aid) == (True, [])


def test_reject_before_freeze_proceeds_and_replays(hidden: World, policy: Policy) -> None:
    with tenant_session(hidden.url, hidden.tenant) as conn:
        case = new_case(conn, hidden)
        run_baseline(conn, case, policy)
        holds = hold_ids(conn, case.id)
    assert holds
    with tenant_session(hidden.url, hidden.tenant) as conn:
        for fid in holds:
            act(conn, hidden, fid, "reject")
    with tenant_session(hidden.url, hidden.tenant) as conn:
        frozen = freeze(conn, case, policy, created_by="alice")
    assert frozen.result.band is not None
    assert do_replay(hidden, frozen.assessment_id) == (True, [])

    # a reject recorded after freeze, and a later reversal, do not change replay
    with tenant_session(hidden.url, hidden.tenant) as conn:
        for fid in holds:
            act(conn, hidden, fid, "accept")
    assert do_replay(hidden, frozen.assessment_id) == (True, [])


def test_reject_after_freeze_does_not_change_replay(hidden: World, policy: Policy) -> None:
    case_id, aid = baseline_and_freeze(hidden, policy)
    with tenant_session(hidden.url, hidden.tenant) as conn:
        holds = hold_ids(conn, case_id)
    assert holds
    with tenant_session(hidden.url, hidden.tenant) as conn:
        for fid in holds:
            act(conn, hidden, fid, "reject")
    assert do_replay(hidden, aid) == (True, [])


def add_bank_row(w: World, ref: str, observed_at: datetime, posted_on: str, pull: bool) -> None:
    with tenant_session(w.url, w.tenant) as conn:
        pull_id = None
        if pull:
            row = conn.execute(
                "INSERT INTO evidence_pull (tenant_id, borrower_id, source, fetched_at, s3_key, "
                "sha256, byte_size, status) VALUES (%s, %s, 'bank', %s, 'k', %s, 1, 'ok') "
                "RETURNING id",
                (w.tenant, w.borrower, observed_at, ref * 8),
            ).fetchone()
            assert row
            pull_id = row[0]
        else:
            row = conn.execute(
                "SELECT evidence_pull_id FROM bank_txn WHERE borrower_id = %s LIMIT 1",
                (w.borrower,),
            ).fetchone()
            assert row
            pull_id = row[0]
        conn.execute(
            "INSERT INTO bank_txn (tenant_id, borrower_id, source_ref, account_ref, amount, "
            "direction, counterparty, description, posted_on, observed_at, evidence_pull_id, "
            "evidence_locator) VALUES (%s, %s, %s, 'acct', 1234.50, 'in', 'x', 'late', %s, %s, "
            "%s, 'row=1')",
            (w.tenant, w.borrower, ref, posted_on, observed_at, pull_id),
        )


def test_rows_observed_after_as_of_do_not_matter(clean: World, policy: Policy) -> None:
    _, aid = baseline_and_freeze(clean, policy)
    later = clean.as_of + timedelta(days=3)
    add_bank_row(clean, "late-1", later, (clean.as_of + timedelta(days=2)).date().isoformat(), True)
    assert do_replay(clean, aid) == (True, [])


def test_backdated_tamper_is_reported(clean: World, policy: Policy) -> None:
    _, aid = baseline_and_freeze(clean, policy)
    earlier = clean.as_of - timedelta(hours=1)
    add_bank_row(
        clean, "tamper-1", earlier, (clean.as_of - timedelta(days=10)).date().isoformat(), True
    )
    match, diffs = do_replay(clean, aid)
    assert not match
    text = "\n".join(diffs)
    assert "ledger input hash changed" in text
    assert "evidence set hash changed" in text


def test_policy_change_is_reported(clean: World, policy: Policy, tmp_path: Path) -> None:
    _, aid = baseline_and_freeze(clean, policy)
    edited = tmp_path / "policy.yaml"
    shutil.copy(POLICY_PATH, edited)
    edited.write_text(edited.read_text().replace("max: 0.03", "max: 0.02", 1))
    assert edited.read_bytes() != POLICY_PATH.read_bytes()
    match, diffs = do_replay(clean, aid, edited)
    assert not match and any(d.startswith("policy changed") for d in diffs)


def test_replay_uses_the_policy_file_the_assessment_was_frozen_under(clean: World) -> None:
    old = load_policy(POLICY_PATH.parent / "default-0.2.2.yaml")
    assert old.sha256 != load_policy(POLICY_PATH).sha256
    _, aid = baseline_and_freeze(clean, old)
    assert do_replay(clean, aid, POLICY_PATH) == (True, [])


def test_replay_sample_and_cli(
    clean: World, policy: Policy, capsys: pytest.CaptureFixture[str]
) -> None:
    _, aid = baseline_and_freeze(clean, policy)
    with tenant_session(clean.url, clean.tenant) as conn:
        reports = replay_sample(conn, 5, POLICY_PATH)
    assert [r.assessment_id for r in reports] == [aid] and reports[0].match

    import os

    old = os.environ.get("TANGIBLE_DATABASE_URL")
    os.environ["TANGIBLE_DATABASE_URL"] = clean.url
    try:
        assert database_url() == clean.url
        assert (
            replay_main(
                ["--tenant-id", clean.tenant, "--sample", "3", "--policy", str(POLICY_PATH)]
            )
            == 0
        )
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] and out["replayed"] == 1
        add_bank_row(
            clean,
            "tamper-2",
            clean.as_of - timedelta(hours=1),
            (clean.as_of - timedelta(days=5)).date().isoformat(),
            True,
        )
        assert (
            replay_main(
                ["--tenant-id", clean.tenant, "--sample", "3", "--policy", str(POLICY_PATH)]
            )
            == 1
        )
        out = json.loads(capsys.readouterr().out)
        assert not out["ok"] and out["mismatches"][0]["assessment_id"] == aid
    finally:
        if old is None:
            os.environ.pop("TANGIBLE_DATABASE_URL", None)
        else:
            os.environ["TANGIBLE_DATABASE_URL"] = old
