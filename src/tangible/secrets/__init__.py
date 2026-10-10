"""Secret storage for connector tokens. Tokens live here, never in the database or in logs.

`SecretStore` is the interface; `EnvSecrets` is the dev implementation, `aws.AwsSecrets` the
production one (Secrets Manager, one KMS key per tenant).
"""

import os
import re
from collections.abc import Iterable
from typing import Protocol
from urllib.parse import quote

REDACTED = "[REDACTED]"


class SecretNotFound(KeyError):
    pass


class SecretStore(Protocol):
    def get(self, ref: str) -> str: ...

    def put(self, ref: str, value: str) -> None: ...


class EnvSecrets:
    """Dev store. Refs look like `env:NAME`; the value is the process environment variable.

    `put` sets the variable for this process only (nothing is persisted).
    """

    PREFIX = "env:"

    def _name(self, ref: str) -> str:
        if not ref.startswith(self.PREFIX) or len(ref) == len(self.PREFIX):
            raise ValueError(f"not an env secret ref: {ref!r}")
        return ref[len(self.PREFIX) :]

    def get(self, ref: str) -> str:
        name = self._name(ref)
        try:
            return os.environ[name]
        except KeyError:
            raise SecretNotFound(f"environment variable {name} is not set") from None

    def put(self, ref: str, value: str) -> None:
        os.environ[self._name(ref)] = value


# Credential-looking fragments scrubbed even when the exact secret value is not known.
_PATTERNS = (
    re.compile(r"(?i)((?:access_token|client_secret|secret|token|password)=)[^&\s\"']+"),
    re.compile(r"(?i)(\"(?:access_token|client_secret|secret|token|password)\"\s*:\s*\")[^\"]*"),
    re.compile(r"(?i)((?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]+"),
)


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove secret values (and common credential patterns) from `text` before it is logged,
    stored as `connection.last_error` or put in an exception message."""
    for s in sorted({s for s in secrets if s}, key=len, reverse=True):
        for variant in {s, quote(s, safe=""), quote(s)}:
            text = text.replace(variant, REDACTED)
    for pat in _PATTERNS:
        text = pat.sub(lambda m: m.group(1) + REDACTED, text)
    return text
