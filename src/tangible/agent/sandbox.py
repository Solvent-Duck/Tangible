"""Sandbox for `run_analysis`: run agent-written Python over a frozen snapshot, no network.

Interface fixed in Wave 2 prep; WP2C implements `run_python`. WP2B's tool wraps it.

How it works: the code runs in a child `python -I` (empty environment, fresh temp dir as cwd,
own process group). The child (`_sandbox_runner.py`) sets resource limits, starts a memory
watchdog, installs a `sys.addaudithook` policy, then execs the code. The code's `RESULT` global
comes back through a JSON file; stdout is captured by the parent and truncated.

What is enforced (see also the runner):
- All platforms: wall-clock timeout (whole process group killed), RLIMIT_CPU, RLIMIT_FSIZE,
  RLIMIT_NOFILE, RLIMIT_NPROC (best effort), an in-process peak-RSS watchdog for the memory cap,
  and the audit-hook policy: no socket/subprocess/exec/fork/ctypes/os.kill, writes only inside
  the scratch dir (never the snapshot copy), reads only inside the scratch dir and the Python
  installation (sys.prefix etc.), sqlite only on the snapshot opened `file:<SNAPSHOT_DB>?mode=ro`.
- Linux additionally: RLIMIT_AS is enforced by the kernel (-> MemoryError, "memory limit"), and
  if `unshare -rn` works unprivileged the child runs in a fresh network namespace (detected at
  runtime, silently skipped otherwise).
- darwin: RLIMIT_AS/RLIMIT_DATA are not enforced by the kernel, so the memory cap relies on the
  RSS watchdog (polls every 10 ms and re-checks when the code ends; a single huge allocation can
  briefly exceed the cap before it is noticed). `PYTHONHASHSEED=0` is set but `-I` makes Python
  ignore it, so hash seeds are randomised.

This is defence in depth, NOT a security boundary: audit hooks cannot be removed by
well-behaved code, but a determined attacker in-process (gc/frame introspection, SQLite
`ATTACH`, C-level file access that bypasses audit events) can get around them. Production
isolation is a no-egress Fargate task (documented, not built here).
"""

import functools
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

_RUNNER = Path(__file__).with_name("_sandbox_runner.py")
_MEMORY_EXIT_CODE = 97
_TRUNCATED = "…[truncated]"


@dataclass(frozen=True)
class Limits:
    wall_seconds: float = 20.0
    cpu_seconds: int = 15
    memory_bytes: int = 512 * 1024 * 1024
    max_output_bytes: int = 64 * 1024


@dataclass(frozen=True)
class SandboxResult:
    """`result` is the JSON-serialisable value the code assigned to `RESULT` (or None).
    `error` is set (and `result` None) on timeout, crash, limit or policy violation."""

    stdout: str
    result: object
    error: str | None
    duration_ms: int


DEFAULT_LIMITS = Limits()


@functools.cache
def _unshare_prefix() -> tuple[str, ...]:
    """`unshare -rn` (new network namespace) when available and permitted; else nothing."""
    if not sys.platform.startswith("linux"):
        return ()
    exe = shutil.which("unshare")
    if exe is None:
        return ()
    try:
        ok = subprocess.run([exe, "-rn", "true"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return ()
    return (exe, "-rn") if ok else ()


def _drain(stream: IO[bytes], keep: int, out: list[bytes], total: list[int]) -> None:
    """Read to EOF, retaining at most `keep` bytes (so a chatty child can't fill memory)."""
    kept = 0
    while chunk := stream.read(65536):
        total[0] += len(chunk)
        if kept < keep:
            piece = chunk[: keep - kept]
            out.append(piece)
            kept += len(piece)


def _exit_error(returncode: int) -> str:
    if returncode == _MEMORY_EXIT_CODE:
        return "memory limit"
    if returncode == 98:
        return "output limit"
    if returncode == 99:
        return "could not write result"
    if returncode < 0:
        sig = -returncode
        if sig == signal.SIGXCPU:
            return "cpu limit"
        if sig == signal.SIGXFSZ:
            return "output limit"
        if sig == signal.SIGKILL:
            return "killed (memory limit?)"
        return f"crashed (signal {sig})"
    return f"crashed (exit code {returncode})"


def _execute(code: str, snapshot_db: Path, limits: Limits, workdir: Path) -> SandboxResult:
    start = time.monotonic()

    def done(stdout: str, result: object, error: str | None) -> SandboxResult:
        return SandboxResult(stdout, result, error, int((time.monotonic() - start) * 1000))

    ro_dir = workdir / "snapshot"
    ro_dir.mkdir()
    snap = ro_dir / "snapshot.db"
    try:
        shutil.copyfile(snapshot_db, snap)
    except OSError as exc:
        return done("", None, f"snapshot unavailable: {type(exc).__name__}")
    snap.chmod(0o444)
    ro_dir.chmod(0o555)
    code_path = workdir / "analysis.py"
    code_path.write_text(code, encoding="utf-8")
    result_path = workdir / "result.json"

    cmd = [
        *_unshare_prefix(),
        sys.executable,
        "-I",
        str(_RUNNER),
        str(code_path),
        str(snap),
        str(result_path),
        str(workdir),
        str(limits.cpu_seconds),
        str(limits.memory_bytes),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=workdir,
        env={"PYTHONHASHSEED": "0"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert proc.stdout is not None
    chunks: list[bytes] = []
    total = [0]
    reader = threading.Thread(
        target=_drain, args=(proc.stdout, limits.max_output_bytes, chunks, total), daemon=True
    )
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=limits.wall_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # also reaps anything left in the group
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
        reader.join(timeout=5)
        proc.stdout.close()

    stdout = b"".join(chunks).decode("utf-8", errors="replace")
    if total[0] > limits.max_output_bytes:
        stdout += _TRUNCATED
    if timed_out:
        return done(stdout, None, "timeout")
    try:
        outcome = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return done(stdout, None, _exit_error(proc.returncode))
    error = outcome.get("error")
    if error is not None:
        return done(stdout, None, str(error))
    return done(stdout, outcome.get("result"), None)


def run_python(code: str, snapshot_db: Path, limits: Limits = DEFAULT_LIMITS) -> SandboxResult:
    """Run `code` with read-only access to the SQLite file `snapshot_db` (exposed to the code as
    the path in the `SNAPSHOT_DB` global). Never raises for code failures; reports them in
    `SandboxResult.error`."""
    start = time.monotonic()
    workdir = Path(os.path.realpath(tempfile.mkdtemp(prefix="tangible-sandbox-")))
    try:
        return _execute(code, snapshot_db, limits, workdir)
    except Exception as exc:  # infrastructure failure, not agent code
        elapsed = int((time.monotonic() - start) * 1000)
        return SandboxResult("", None, f"sandbox failure: {type(exc).__name__}", elapsed)
    finally:
        try:
            (workdir / "snapshot").chmod(0o755)
        except OSError:
            pass
        shutil.rmtree(workdir, ignore_errors=True)
