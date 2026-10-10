"""Sandbox for `run_analysis`: run agent-written Python over a frozen snapshot, no network.

Interface fixed in Wave 2 prep; WP2C implements `run_python`. WP2B's tool wraps it.
"""

from dataclasses import dataclass
from pathlib import Path


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


def run_python(code: str, snapshot_db: Path, limits: Limits = DEFAULT_LIMITS) -> SandboxResult:
    """Run `code` with read-only access to the SQLite file `snapshot_db` (exposed to the code as
    the path in the `SNAPSHOT_DB` global). Never raises for code failures; reports them in
    `SandboxResult.error`."""
    raise NotImplementedError("WP2C")
