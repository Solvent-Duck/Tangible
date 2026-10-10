"""Versioned prompt registry. Prompts are files `<name>@v<N>.md` and are immutable once released.

Editing a released prompt changes its sha256 and fails `PROMPT_HASHES` pinning; a change means a
new `@vN` file plus a new hash entry.
"""

import hashlib
import re
from dataclasses import dataclass
from importlib import resources

PROMPT_HASHES: dict[str, str] = {
    "document_reader@v1": "e73cd532015c3f4ecc0414b65f79bf9ea8676c50e3c7a4ce2f4668ed2fa2741f",
    "system@v1": "fe46c1908321d9fe5eb3c6be51aa347258abcebb9a9a6583c997ce9c503e2243",
    "plan@v1": "fd6e6417c07ec9e726e239cfb18eebfa05c55d478fe2495b27281f1fd4521e09",
    "investigate@v1": "adb3e1a9518e48f1483bdf49599ef3ccce415fb2913883f1a6479b7c73cc4a8d",
    "plan@v2": "c43dd85c1538e68e72edd3c4a934152c24648cd77ec61c962e9c6d1a435bf6bb",
    "investigate@v2": "8770555cac98f4e0da16f9838b5636f885e9bcb2d0ef56704b3a1f33126a30ba",
}

_FILE = re.compile(r"^(?P<name>[\w-]+)@v(?P<version>\d+)\.md$")


@dataclass(frozen=True)
class Prompt:
    key: str
    version: int
    text: str
    sha256: str


def _files() -> dict[tuple[str, int], str]:
    out: dict[tuple[str, int], str] = {}
    for f in resources.files(__package__).iterdir():
        m = _FILE.match(f.name)
        if m:
            out[(m["name"], int(m["version"]))] = f.name
    return out


def load_prompt(name: str, version: int) -> Prompt:
    fname = _files().get((name, version))
    if fname is None:
        raise KeyError(f"no prompt {name}@v{version}")
    data = (resources.files(__package__) / fname).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if PROMPT_HASHES.get(f"{name}@v{version}") != sha:
        raise ValueError(f"prompt {name}@v{version} does not match its pinned hash")
    return Prompt(name, version, data.decode("utf-8"), sha)


def latest(name: str) -> Prompt:
    versions = [v for (n, v) in _files() if n == name]
    if not versions:
        raise KeyError(f"no prompt named {name}")
    return load_prompt(name, max(versions))
