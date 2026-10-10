import os

DEFAULT_DATABASE_URL = "postgresql://tangible:tangible@localhost:5432/tangible"


def database_url() -> str:
    return os.environ.get("TANGIBLE_DATABASE_URL", DEFAULT_DATABASE_URL)
