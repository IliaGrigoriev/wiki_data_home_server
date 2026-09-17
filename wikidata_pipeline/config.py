"""Connection settings for the Wikidata pipeline."""

import os


def target_dsn() -> str:
    # libpq also accepts PGHOST, PGUSER, PGPORT and PGPASSWORD.
    return os.environ.get("WIKIDATA_DSN", "dbname=wikidata")


def admin_dsn() -> str:
    return os.environ.get("WIKIDATA_ADMIN_DSN", "dbname=postgres")
