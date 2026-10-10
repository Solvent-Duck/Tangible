"""tangible-pull: run one connector pull for a connector account.

Dev default: EnvSecrets (refs like env:NAME; client credentials from RUTTER_CLIENT_ID /
RUTTER_CLIENT_SECRET / PLAID_CLIENT_ID / PLAID_CLIENT_SECRET) and a LocalEvidenceStore. With
--s3-bucket (or TANGIBLE_EVIDENCE_BUCKET) it uses S3EvidenceStore plus AwsSecrets, both with the
tenant's KMS key (--kms-key / TANGIBLE_KMS_KEY); client credential refs are then Secrets Manager
names (defaults: <provider>/client_id, <provider>/client_secret).

Exit status: 0 complete, 1 failed (recorded), 2 connection revoked.
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from tangible.config import database_url
from tangible.connectors.base import Connector
from tangible.connectors.plaid import BASE_URL as PLAID_URL
from tangible.connectors.plaid import PlaidConnector
from tangible.connectors.pull import ConnectionRevoked, load_account, run_pull
from tangible.connectors.rutter import BASE_URL as RUTTER_URL
from tangible.connectors.rutter import STREAMS, RutterConnector
from tangible.db.session import tenant_session
from tangible.evidence.store import EvidenceStore, LocalEvidenceStore
from tangible.secrets import EnvSecrets, SecretStore


def build_connector(
    provider: str, secrets: SecretStore, client: httpx.Client, aws: bool, rutter_streams: str
) -> Connector:
    def ref(name: str) -> str:
        return f"{provider}/{name}" if aws else f"env:{provider.upper()}_{name.upper()}"

    if provider == "plaid":
        return PlaidConnector(
            client,
            secrets,
            ref("client_id"),
            ref("client_secret"),
            base_url=os.environ.get("PLAID_BASE_URL", PLAID_URL),
        )
    if provider == "rutter":
        streams = tuple(s for s in rutter_streams.split(",") if s)
        return RutterConnector(
            client,
            secrets,
            ref("client_id"),
            ref("client_secret"),
            base_url=os.environ.get("RUTTER_BASE_URL", RUTTER_URL),
            streams=streams,
        )
    raise ValueError(f"unknown provider {provider!r}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="tangible-pull")
    p.add_argument("--tenant-id", required=True)
    p.add_argument("--account-id", required=True)
    p.add_argument("--store", type=Path, default=Path(".evidence"))
    p.add_argument("--s3-bucket", default=os.environ.get("TANGIBLE_EVIDENCE_BUCKET"))
    p.add_argument("--s3-prefix", default=os.environ.get("TANGIBLE_EVIDENCE_PREFIX", ""))
    p.add_argument("--kms-key", default=os.environ.get("TANGIBLE_KMS_KEY"))
    p.add_argument(
        "--rutter-streams",
        default="orders,payouts",
        help=f"comma list from {','.join(STREAMS)} (Rutter only)",
    )
    args = p.parse_args(argv)

    store: EvidenceStore
    secrets: SecretStore
    if args.s3_bucket:
        from tangible.evidence.s3 import S3EvidenceStore
        from tangible.secrets.aws import AwsSecrets

        if not args.kms_key:
            p.error("--kms-key (or TANGIBLE_KMS_KEY) is required with --s3-bucket")
        store = S3EvidenceStore(args.s3_bucket, args.s3_prefix, kms_key_id=args.kms_key)
        secrets = AwsSecrets(args.kms_key)
    else:
        store, secrets = LocalEvidenceStore(args.store), EnvSecrets()

    status = 0
    with (
        httpx.Client(timeout=30.0) as client,
        tenant_session(database_url(), args.tenant_id) as conn,
    ):
        account = load_account(conn, args.account_id)
        connector = build_connector(
            account.provider, secrets, client, bool(args.s3_bucket), args.rutter_streams
        )
        try:
            outcome = run_pull(conn, store, secrets, account, connector, datetime.now(UTC))
        except ConnectionRevoked as e:
            print(json.dumps({"status": "revoked", "error": str(e)}))
            sys.exit(2)
        print(json.dumps({"status": outcome.status, "pull_id": outcome.pull_id,
                          "rows": outcome.rows_by_table, "error": outcome.error}))  # fmt: skip
        if outcome.status != "complete":
            status = 1
    sys.exit(status)


if __name__ == "__main__":
    main()
