import logging
from typing import Any

import pytest

from tangible.connectors.base import ConnectorAccount, ConnectorError
from tangible.connectors.plaid import PlaidConnector
from tangible.secrets import EnvSecrets, SecretNotFound, redact
from tangible.secrets.aws import AwsSecrets
from test_connectors_common import TOKEN, Script, secrets


def test_env_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TANGIBLE_TEST_TOKEN", "abc")
    s = EnvSecrets()
    assert s.get("env:TANGIBLE_TEST_TOKEN") == "abc"
    s.put("env:TANGIBLE_TEST_NEW", "xyz")
    assert s.get("env:TANGIBLE_TEST_NEW") == "xyz"
    monkeypatch.delenv("TANGIBLE_TEST_NEW")
    with pytest.raises(SecretNotFound):
        s.get("env:TANGIBLE_TEST_NEW")
    with pytest.raises(ValueError):
        s.get("plain-name")


def test_connector_error_and_logs_never_contain_token(caplog: pytest.LogCaptureFixture) -> None:
    acct = ConnectorAccount("a", "t", "b", "c", "plaid", "mem:token")
    s = Script(*[__import__("httpx").Response(500, text=f"x {TOKEN}") for _ in range(4)])
    c = PlaidConnector(s.client(), secrets(), "mem:cid", "mem:csecret", sleep=lambda x: None)
    with caplog.at_level(logging.DEBUG), pytest.raises(ConnectorError) as e:
        c.pull(acct, {})
    assert TOKEN not in str(e.value) and TOKEN not in caplog.text
    assert "client-secret-ABC" not in str(e.value) and "client-secret-ABC" not in caplog.text
    assert len(s.requests) == 4  # 1 try + 3 retries


def test_redact_known_values_and_encodings() -> None:
    assert redact("tok=a/b+c", ["a/b+c"]) == "tok=[REDACTED]"
    assert "a%2Fb" not in redact("url?x=a%2Fb%2Bc", ["a/b+c"])


class FakeSM:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.created: list[dict[str, Any]] = []

    def create_secret(self, Name: str, SecretString: str, KmsKeyId: str) -> None:
        if Name in self.store:
            err = Exception("exists")
            err.response = {"Error": {"Code": "ResourceExistsException"}}  # type: ignore[attr-defined]
            raise err
        self.created.append({"Name": Name, "KmsKeyId": KmsKeyId})
        self.store[Name] = SecretString

    def put_secret_value(self, SecretId: str, SecretString: str) -> None:
        self.store[SecretId] = SecretString

    def get_secret_value(self, SecretId: str) -> dict[str, str]:
        if SecretId not in self.store:
            err = Exception("nf")
            err.response = {"Error": {"Code": "ResourceNotFoundException"}}  # type: ignore[attr-defined]
            raise err
        return {"SecretString": self.store[SecretId]}


def test_aws_secrets_with_fake_client() -> None:
    fake = FakeSM()
    s = AwsSecrets("kms-tenant-key", client=fake)
    with pytest.raises(SecretNotFound):
        s.get("plaid/t1")
    s.put("plaid/t1", "v1")
    s.put("plaid/t1", "v2")  # exists -> new version
    assert s.get("plaid/t1") == "v2"
    assert fake.created == [{"Name": "plaid/t1", "KmsKeyId": "kms-tenant-key"}]
