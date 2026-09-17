"""Database and checkpoint operations shared by the two dump importers."""

from contextlib import contextmanager
import hashlib
from pathlib import Path
import signal

import psycopg
from psycopg import sql

from config import admin_dsn, target_dsn
from const import SCHEMA_PATH


BATCH_SIZE = 1000


# Create the target database if needed, then apply its schema.
# ----------------------------------------------------------
def setup() -> None:
    with psycopg.connect(admin_dsn(), autocommit=True) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", ("wikidata",)
        ).fetchone()
        if not exists:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier("wikidata")))
            print("Created database wikidata")
    with psycopg.connect(target_dsn()) as db:
        db.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    print("Wikidata schema is ready")


# Identify a dump by its kind, resolved path, size, and modification time.
# ----------------------------------------------------------------------
def source_identity(path: Path, kind: str = "") -> tuple[str, str]:
    stat = path.stat()
    description = f"{path.resolve()} size={stat.st_size} mtime_ns={stat.st_mtime_ns}"
    if kind:
        description = f"{kind}: {description}"
    return hashlib.sha256(description.encode()).hexdigest(), description


# Read the last committed position and ID for an import source.
# ----------------------------------------------------------
def checkpoint(db: psycopg.Connection, key: str) -> tuple[int, str | None]:
    row = db.execute(
        "SELECT processed, last_id FROM import_progress WHERE source_key = %s", (key,)
    ).fetchone()
    # A SELECT starts a transaction. Close it before the batch transaction.
    db.commit()
    return (row[0], row[1]) if row else (0, None)


# Write rows and their progress checkpoint in one transaction.
# ----------------------------------------------------------
def save_batch(db: psycopg.Connection, statement: str, rows: list[tuple],
               key: str, description: str, processed: int,
               last_id: str | None) -> None:
    with db.transaction():
        with db.cursor() as cur:
            cur.executemany(statement, rows)
            cur.execute(
                "INSERT INTO import_progress "
                "(source_key, source_description, processed, last_id) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (source_key) DO UPDATE SET "
                "processed = EXCLUDED.processed, last_id = EXCLUDED.last_id, "
                "updated_at = now()",
                (key, description, processed, last_id),
            )


# Turn Ctrl+C into a stop flag so the current batch can be committed.
# ---------------------------------------------------------------
@contextmanager
def stop_on_sigint():
    stopped = False

    # Record a stop request without interrupting the active write.
    # ---------------------------------------------------------
    def request_stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    previous = signal.signal(signal.SIGINT, request_stop)
    try:
        yield lambda: stopped
    finally:
        signal.signal(signal.SIGINT, previous)
