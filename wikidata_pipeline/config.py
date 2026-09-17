"""Connection settings for the Wikidata pipeline."""

import os


# Return the connection string for the Wikidata database.
# -------------------------------------------------------
def target_dsn() -> str:
    # libpq also accepts PGHOST, PGUSER, PGPORT and PGPASSWORD.
    return os.environ.get("WIKIDATA_DSN", "dbname=wikidata")


# Return the connection string used to create the database.
# ---------------------------------------------------------
def admin_dsn() -> str:
    return os.environ.get("WIKIDATA_ADMIN_DSN", "dbname=postgres")
