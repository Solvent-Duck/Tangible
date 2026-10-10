"""Secrets Manager implementation of SecretStore. One KMS key per tenant (the lender's key)."""

from typing import Any

from tangible.secrets import SecretNotFound


class AwsSecrets:
    """Refs are Secrets Manager secret names (or ARNs). `kms_key_id` encrypts new secrets."""

    def __init__(self, kms_key_id: str, client: Any = None, region_name: str | None = None) -> None:
        if client is None:
            import boto3

            client = boto3.client("secretsmanager", region_name=region_name)
        self.client = client
        self.kms_key_id = kms_key_id

    def get(self, ref: str) -> str:
        try:
            resp = self.client.get_secret_value(SecretId=ref)
        except Exception as e:
            if _code(e) == "ResourceNotFoundException":
                raise SecretNotFound(f"secret {ref} not found") from None
            raise
        value = resp.get("SecretString")
        if value is None:
            raise SecretNotFound(f"secret {ref} has no string value")
        return str(value)

    def put(self, ref: str, value: str) -> None:
        try:
            self.client.create_secret(Name=ref, SecretString=value, KmsKeyId=self.kms_key_id)
        except Exception as e:
            if _code(e) != "ResourceExistsException":
                raise
            self.client.put_secret_value(SecretId=ref, SecretString=value)


def _code(e: Exception) -> str:
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        return str(resp.get("Error", {}).get("Code", ""))
    return ""
