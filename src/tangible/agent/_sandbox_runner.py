"""Child-process bootstrap for the analysis sandbox. Not importable API: run as a script by
`tangible.agent.sandbox.run_python` as `python -I _sandbox_runner.py <args>`.

argv: code_path snapshot_path result_path workdir cpu_seconds memory_bytes

Order matters: read the code, set rlimits, start the memory watchdog, install the audit hook,
only then execute the agent code. Everything the runner needs later is imported up front.
"""

import builtins
import datetime
import decimal
import json
import os
import resource
import signal
import sqlite3
import sys
import threading
import time
from typing import Any

MEMORY_EXIT_CODE = 97
_FSIZE_BYTES = 4 * 1024 * 1024
_NOFILE = 64

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

_BLOCKED_PREFIXES = (
    "socket.",
    "ctypes.",
    "subprocess.",
    "os.exec",
    "os.spawn",
    "os.posix_spawn",
    "os.fork",
    "os.system",
    "os.kill",
    "os.startfile",
    "pty.",
    "signal.pthread_kill",
    "webbrowser.",
    "sqlite3.enable_load",
    "sqlite3.load",
)
_BLOCKED_IMPORTS = frozenset({"_posixsubprocess", "ctypes", "_ctypes"})
# Events whose every path-like argument must lie inside the writable scratch area.
_MUTATING_EVENTS = frozenset(
    {
        "os.chmod",
        "os.chown",
        "os.lchown",
        "os.mkdir",
        "os.remove",
        "os.rename",
        "os.rmdir",
        "os.symlink",
        "os.link",
        "os.truncate",
        "os.utime",
        "os.mkfifo",
        "os.mknod",
        "os.setxattr",
        "os.removexattr",
        "os.chflags",
        "os.lchflags",
        "os.chroot",
        "shutil.copyfile",
        "shutil.copymode",
        "shutil.copystat",
        "shutil.copytree",
        "shutil.move",
        "shutil.rmtree",
        "shutil.make_archive",
        "shutil.unpack_archive",
        "shutil.chown",
        "tempfile.mkstemp",
        "tempfile.mkdtemp",
    }
)
# Events that observe the filesystem: paths must lie inside the readable roots.
_READING_EVENTS = frozenset(
    {
        "os.listdir",
        "os.scandir",
        "os.chdir",
        "os.fwalk",
        "glob.glob",
        "glob.glob/2",
        "pathlib.Path.glob",
        "pathlib.Path.rglob",
    }
)
_ALLOWED_DEVICES = frozenset({"/dev/null", "/dev/urandom", "/dev/random"})


def _set_limit(name: str, soft: int, hard: int | None = None) -> None:
    try:
        resource.setrlimit(getattr(resource, name), (soft, soft if hard is None else hard))
    except (ValueError, OSError, AttributeError):
        pass  # not supported / not permitted on this platform: best effort


def _peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # darwin: bytes, linux: KiB


def _start_memory_watchdog(limit: int) -> None:
    def watch() -> None:
        while True:
            if _peak_rss_bytes() > limit:
                os._exit(MEMORY_EXIT_CODE)
            time.sleep(0.01)

    threading.Thread(target=watch, daemon=True).start()


def _install_audit_hook(workdir: str, snapshot: str, read_roots: tuple[str, ...]) -> None:
    ro_dir = os.path.dirname(snapshot)
    readable = (workdir, *read_roots)
    realpath = os.path.realpath
    sep = os.sep

    def norm(p: object) -> str | None:
        if p is None or isinstance(p, int):
            return None
        try:
            return realpath(os.fsdecode(os.fspath(p)))  # type: ignore[call-overload]
        except (TypeError, ValueError):
            return None

    def within(p: str, roots: tuple[str, ...]) -> bool:
        return any(p == r or p.startswith(r + sep) for r in roots)

    def can_write(p: str) -> bool:
        return within(p, (workdir,)) and not within(p, (ro_dir,))

    def can_read(p: str) -> bool:
        return p in _ALLOWED_DEVICES or within(p, readable)

    def block(event: str, why: str = "") -> None:
        raise PermissionError(f"blocked: {event}" + (f" ({why})" if why else ""))

    def check_sqlite(db: object) -> None:
        text = os.fsdecode(os.fspath(db)) if not isinstance(db, str) else db  # type: ignore[call-overload]
        if text in ("", ":memory:"):
            return
        if text.startswith("file:") and "mode=ro" in text:
            from urllib.parse import unquote, urlsplit

            path = unquote(urlsplit(text).path)
            if realpath(path) == snapshot:
                return
        block("sqlite3.connect", "open only SNAPSHOT_DB as file:<path>?mode=ro with uri=True")

    def deny_attach(action: int, *_: Any) -> int:
        # ATTACH opens or creates arbitrary files at the C level, invisible to the "open" event.
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ATTACH else sqlite3.SQLITE_OK

    raw_connect = sqlite3.connect

    def guarded_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        # Defence in depth only: code calling sqlite3.Connection(...) directly, or
        # set_authorizer(None), gets past this. Production isolation is the no-egress task.
        conn: sqlite3.Connection = raw_connect(*args, **kwargs)
        conn.set_authorizer(deny_attach)
        conn.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        return conn

    sqlite3.connect = guarded_connect  # type: ignore[assignment]

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if event == "open":
            p = norm(args[0])
            if p is None:
                return
            flags = args[2] if len(args) > 2 else 0
            if isinstance(flags, int) and flags & _WRITE_FLAGS:
                if not (can_write(p) or p == "/dev/null"):
                    block("open for write", "outside scratch directory")
            elif not can_read(p):
                block("open for read", "outside scratch directory and Python stdlib")
            return
        if event == "import":
            if args[0] in _BLOCKED_IMPORTS:
                block(f"import {args[0]}")
            return
        if event == "sqlite3.connect":
            check_sqlite(args[0])
            return
        if event.startswith(_BLOCKED_PREFIXES):
            block(event)
        if event in _MUTATING_EVENTS:
            for a in args:
                p = norm(a) if isinstance(a, (str, bytes, os.PathLike)) else None
                if p is not None and not can_write(p):
                    block(event, "outside scratch directory")
        elif event in _READING_EVENTS:
            for a in args:
                p = norm(a) if isinstance(a, (str, bytes, os.PathLike)) else None
                if p is not None and not can_read(p):
                    block(event, "outside scratch directory")

    sys.addaudithook(hook)


def _json_default(obj: object) -> str:
    if isinstance(obj, (decimal.Decimal, datetime.date, datetime.datetime, datetime.time)):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serialisable")


def _write_outcome(path: str, result: object, error: str | None) -> None:
    try:
        payload = json.dumps({"result": result, "error": error}, default=_json_default)
    except (TypeError, ValueError) as exc:
        payload = json.dumps({"result": None, "error": f"RESULT not serialisable: {exc}"[:500]})
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(payload)


def _fmt(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]


def main(argv: list[str]) -> None:
    code_path, snapshot_arg, result_path, workdir_arg, cpu, mem = argv[1:7]
    cpu_seconds, memory_bytes = int(cpu), int(mem)
    workdir = os.path.realpath(workdir_arg)
    snapshot = os.path.realpath(snapshot_arg)
    with open(code_path, encoding="utf-8") as fh:
        source = fh.read()
    compiled_ok = True
    try:
        compiled = compile(source, "<analysis>", "exec")
    except SyntaxError as exc:
        compiled, compiled_ok = None, False
        syntax_error = _fmt(exc)

    read_roots = {
        os.path.realpath(p)
        for p in (
            sys.prefix,
            sys.base_prefix,
            sys.exec_prefix,
            sys.base_exec_prefix,
            os.path.dirname(os.__file__),
        )
    }

    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    _set_limit("RLIMIT_CPU", cpu_seconds, cpu_seconds + 1)
    if sys.platform.startswith("linux"):
        _set_limit("RLIMIT_AS", memory_bytes)
    _set_limit("RLIMIT_FSIZE", _FSIZE_BYTES)
    _set_limit("RLIMIT_NOFILE", _NOFILE)
    _set_limit("RLIMIT_CORE", 0)
    _start_memory_watchdog(memory_bytes)
    # NPROC last: it also stops *this* process creating threads, and the watchdog already runs.
    _set_limit("RLIMIT_NPROC", 1)

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    _install_audit_hook(workdir, snapshot, tuple(sorted(read_roots)))
    os.chdir(workdir)

    namespace: dict[str, Any] = {
        "__name__": "__main__",
        "__builtins__": builtins,
        "SNAPSHOT_DB": snapshot,
    }
    error: str | None = None
    if not compiled_ok:
        error = syntax_error
    else:
        try:
            assert compiled is not None
            exec(compiled, namespace)
        except MemoryError:
            error = "memory limit"
        except BaseException as exc:  # agent code may raise anything, incl. SystemExit
            error = _fmt(exc)
    if _peak_rss_bytes() > memory_bytes:
        error = "memory limit"
    result = namespace.get("RESULT") if error is None else None
    try:
        sys.stdout.flush()
    except Exception:
        pass
    try:
        _write_outcome(result_path, result, error)
    except OSError as exc:
        os._exit(98 if "file too large" in str(exc).lower() else 99)
    os._exit(0)


if __name__ == "__main__":
    main(sys.argv)
