import os
import sqlite3
from pathlib import Path

import pytest

from tangible.agent.sandbox import Limits, SandboxResult, run_python

FAST = Limits(wall_seconds=20.0, cpu_seconds=10, memory_bytes=1024 * 1024 * 1024)
OPEN_RO = 'sqlite3.connect(f"file:{SNAPSHOT_DB}?mode=ro", uri=True)'


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    path = tmp_path / "snap.db"
    con = sqlite3.connect(path)
    con.execute("create table t (amount integer)")
    con.executemany("insert into t values (?)", [(10,), (20,), (30,)])
    con.commit()
    con.close()
    return path


def run(code: str, snapshot: Path, limits: Limits = FAST) -> SandboxResult:
    return run_python(code, snapshot, limits)


def assert_blocked(res: SandboxResult) -> None:
    assert res.error is not None and "blocked" in res.error, res
    assert res.result is None


def test_happy_path(snapshot: Path) -> None:
    code = (
        "import sqlite3\n"
        f"RESULT = {OPEN_RO}.execute('select sum(amount) from t').fetchone()[0]\n"
        "print('hello')\n"
    )
    res = run(code, snapshot)
    assert res.error is None
    assert res.result == 60
    assert res.stdout.strip() == "hello"
    assert res.duration_ms >= 0


def test_decimal_and_no_result(snapshot: Path) -> None:
    res = run("from decimal import Decimal\nRESULT = {'a': Decimal('1.50')}", snapshot)
    assert res.result == {"a": "1.50"}
    assert run("x = 1", snapshot).result is None


def test_unserialisable_result(snapshot: Path) -> None:
    res = run("RESULT = {1, 2}", snapshot)
    assert res.error is not None and "serialisable" in res.error
    assert res.result is None


def test_exception(snapshot: Path) -> None:
    res = run("RESULT = 1\nraise ValueError('boom')", snapshot)
    assert res.error == "ValueError: boom"
    assert res.result is None


def test_syntax_error(snapshot: Path) -> None:
    res = run("def (:", snapshot)
    assert res.error is not None and res.error.startswith("SyntaxError")


def test_timeout(snapshot: Path) -> None:
    res = run("while True: pass", snapshot, Limits(wall_seconds=1.0, cpu_seconds=60))
    assert res.error == "timeout"
    assert res.duration_ms < 10_000


def test_cpu_limit(snapshot: Path) -> None:
    res = run("while True: pass", snapshot, Limits(wall_seconds=30.0, cpu_seconds=1))
    assert res.error == "cpu limit"
    assert res.duration_ms < 15_000


def test_memory_limit(snapshot: Path) -> None:
    # Linux: RLIMIT_AS -> MemoryError. darwin: enforced by the RSS watchdog instead.
    limits = Limits(wall_seconds=30.0, cpu_seconds=20, memory_bytes=256 * 1024 * 1024)
    res = run("x = b'x' * (1024 * 1024 * 1024)\nRESULT = len(x)", snapshot, limits)
    assert res.error == "memory limit"
    assert res.result is None


def test_network_blocked(snapshot: Path) -> None:
    res = run("import socket\nsocket.create_connection(('127.0.0.1', 9), timeout=1)", snapshot)
    assert_blocked(res)
    res = run("import urllib.request\nurllib.request.urlopen('http://127.0.0.1:9/')", snapshot)
    assert_blocked(res)


@pytest.mark.parametrize(
    "code",
    [
        "import subprocess\nsubprocess.run(['ls'])",
        "import os\nos.system('ls')",
        "import os\nos.fork()",
        "import os\nos.execv('/bin/ls', ['ls'])",
        "import os\nos.posix_spawn('/bin/ls', ['ls'], {})",
        "import os\nos.kill(os.getppid(), 0)",
        "import ctypes",
    ],
)
def test_process_creation_blocked(snapshot: Path, code: str) -> None:
    assert_blocked(run(code, snapshot))


def test_write_outside_blocked(snapshot: Path, tmp_path: Path) -> None:
    target = tmp_path / "escaped.txt"
    res = run(f"open({str(target)!r}, 'w').write('x')", snapshot)
    assert_blocked(res)
    assert not target.exists()
    res = run(f"import os\nos.remove({str(snapshot)!r})", snapshot)
    assert_blocked(res)
    assert snapshot.exists()


def test_read_outside_blocked(snapshot: Path) -> None:
    here = Path(__file__).resolve()
    assert_blocked(run(f"RESULT = open({str(here)!r}).read()", snapshot))
    assert_blocked(run(f"import os\nRESULT = os.listdir({str(here.parent)!r})", snapshot))
    assert_blocked(run("import sqlite3\nsqlite3.connect('/etc/other.db')", snapshot))


def test_scratch_write_allowed(snapshot: Path) -> None:
    code = "open('scratch.txt', 'w').write('hi')\nRESULT = open('scratch.txt').read()"
    res = run(code, snapshot)
    assert res.error is None
    assert res.result == "hi"


def test_snapshot_immutable(snapshot: Path) -> None:
    before = snapshot.read_bytes()
    code = (
        "import sqlite3\n"
        f"con = {OPEN_RO}\n"
        "try:\n"
        "    con.execute('insert into t values (1)')\n"
        "except sqlite3.OperationalError as e:\n"
        "    RESULT = str(e)\n"
    )
    res = run(code, snapshot)
    assert res.error is None
    assert isinstance(res.result, str) and "readonly" in res.result
    assert_blocked(run("open(SNAPSHOT_DB, 'r+b').write(b'x')", snapshot))
    assert_blocked(run("import sqlite3\nsqlite3.connect(SNAPSHOT_DB)", snapshot))
    assert snapshot.read_bytes() == before


def test_env_scrubbed(snapshot: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TANGIBLE_SECRET", "hunter2")
    res = run("import os\nRESULT = dict(os.environ)", snapshot)
    assert res.error is None
    assert isinstance(res.result, dict)
    assert "TANGIBLE_SECRET" not in res.result
    assert not {"HOME", "PATH", "USER"} & set(res.result)
    assert "hunter2" not in str(res.result)


def test_output_truncated(snapshot: Path) -> None:
    limits = Limits(wall_seconds=20.0, cpu_seconds=10, max_output_bytes=100)
    res = run("print('a' * 10000)\nRESULT = 1", snapshot, limits)
    assert res.error is None
    assert res.stdout == "a" * 100 + "…[truncated]"


def test_tempdir_removed(snapshot: Path) -> None:
    res = run("import os\nopen('x', 'w').write('1')\nRESULT = os.getcwd()", snapshot)
    assert isinstance(res.result, str)
    assert not os.path.exists(res.result)


def test_missing_snapshot(tmp_path: Path) -> None:
    res = run("RESULT = 1", tmp_path / "nope.db")
    assert res.error is not None and res.result is None


def test_sqlite_attach_is_denied(snapshot: Path, tmp_path: Path) -> None:
    target = tmp_path / "escape.sqlite"
    via_snapshot = f"c = {OPEN_RO}\nc.execute(\"ATTACH DATABASE '{target}' AS x\")\nRESULT = 1\n"
    res = run("import sqlite3\n" + via_snapshot, snapshot)
    assert res.result is None and res.error, res
    via_memory = (
        f"import sqlite3\nsqlite3.connect(':memory:').execute(\"ATTACH '{target}' AS x\")\n"
    )
    res = run(via_memory, snapshot)
    assert res.error, res
    assert not target.exists()
