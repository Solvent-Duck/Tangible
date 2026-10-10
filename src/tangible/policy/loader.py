"""Load a versioned policy file. The hash goes into every assessment so replay can verify it."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tangible.policy.schema import PolicySpec, parse_policy


@dataclass(frozen=True)
class Policy:
    version: str
    sha256: str
    spec: dict[str, Any]
    parsed: PolicySpec | None = None


def load_policy(path: Path) -> Policy:
    raw = path.read_bytes()
    spec = yaml.safe_load(raw)
    if not isinstance(spec, dict) or "version" not in spec:
        raise ValueError(f"{path}: policy must be a mapping with a 'version'")
    return Policy(
        version=str(spec["version"]),
        sha256=hashlib.sha256(raw).hexdigest(),
        spec=spec,
        parsed=parse_policy(spec),
    )
