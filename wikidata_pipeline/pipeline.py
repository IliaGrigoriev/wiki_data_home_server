"""Set up and populate a PostgreSQL Wikidata database.

Run ``python pipeline.py --help`` for commands and connection options.
"""

import argparse
import bz2
import gzip
import hashlib
import json
import os
from pathlib import Path
import signal

import psycopg
from psycopg import sql

from config import admin_dsn, target_dsn


SCHEMA = Path(__file__).with_name("schema.sql")
BATCH_SIZE = 1000


# Create the database if needed, then apply its schema and PostGIS extension.
# -------------------------------------------------------------------------
def setup() -> None:
    with psycopg.connect(admin_dsn(), autocommit=True) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", ("wikidata",)
        ).fetchone()
        if not exists:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier("wikidata")))
            print("Created database wikidata")
    with psycopg.connect(target_dsn()) as db:
        db.execute(SCHEMA.read_text(encoding="utf-8"))
    print("Wikidata schema is ready")


# Save a batch of entities and its checkpoint in one transaction.
# ---------------------------------------------------------------
def upsert_batch(db: psycopg.Connection, rows: list[tuple[str, str, str]],
                 source_key: str, source_description: str, processed: int,
                 last_id: str | None = None) -> None:
    with db.transaction():
        with db.cursor() as cur:
            cur.executemany(
                "INSERT INTO entities (id, entity_type, data) "
                "VALUES (%s, %s, %s::jsonb) "
                "ON CONFLICT (id) DO UPDATE SET "
                "entity_type = EXCLUDED.entity_type, data = EXCLUDED.data",
                rows,
            )
            cur.execute(
                "INSERT INTO import_progress "
                "(source_key, source_description, processed, last_id) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (source_key) DO UPDATE SET "
                "processed = EXCLUDED.processed, last_id = EXCLUDED.last_id, "
                "updated_at = now()",
                (source_key, source_description, processed, last_id),
            )


# Read the saved count and last ID for an import or migration.
# ---------------------------------------------------------
def migration_checkpoint(db: psycopg.Connection, key: str) -> tuple[int, str | None]:
    row = db.execute(
        "SELECT processed, last_id FROM import_progress WHERE source_key = %s", (key,)
    ).fetchone()
    # A SELECT starts an implicit transaction; close it before batch transactions.
    db.commit()
    return (row[0], row[1]) if row else (0, None)


# Identify a dump by its path, size, and modification time.
# -------------------------------------------------------
def source_identity(path: Path) -> tuple[str, str]:
    stat = path.stat()
    description = f"{path.resolve()} size={stat.st_size} mtime_ns={stat.st_mtime_ns}"
    return hashlib.sha256(description.encode()).hexdigest(), description


# Yield entities from a line-oriented Wikidata dump without loading it all.
# -----------------------------------------------------------------------
def dump_entities(path: Path):
    opener = bz2.open if path.suffix == ".bz2" else gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            line = line.strip()
            if line in ("", "[", "]"):
                continue
            line = line.removesuffix(",").strip()
            try:
                entity = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Line {line_number}: expected one complete JSON entity per line"
                ) from exc
            if not isinstance(entity, dict) or not isinstance(entity.get("id"), str):
                raise ValueError(f"Line {line_number}: entity has no string id")
            yield entity


# Import a dump in batches, allowing Ctrl+C to save progress and pause.
# ------------------------------------------------------------------
def import_dump(path: Path, batch_size: int) -> None:
    key, description = source_identity(path)
    stop_requested = False

    # Record a stop request so the active batch can finish before exit.
    # ---------------------------------------------------------------
    def request_stop(_signum, _frame):
        nonlocal stop_requested
        stop_requested = True

    previous_handler = signal.signal(signal.SIGINT, request_stop)
    try:
        with psycopg.connect(target_dsn()) as db:
            processed, last_id = migration_checkpoint(db, key)
            if processed:
                print(f"Resuming after {processed} entities; last ID: {last_id}", flush=True)
            batch = []
            seen = 0
            for index, entity in enumerate(dump_entities(path), 1):
                seen = index
                if index > processed:
                    batch.append((entity["id"], str(entity.get("type", "unknown")),
                                  json.dumps(entity, ensure_ascii=False)))
                    if len(batch) == batch_size:
                        upsert_batch(db, batch, key, description, index, batch[-1][0])
                        processed = index
                        print(f"Processed {processed} entities; last ID: {batch[-1][0]}",
                              flush=True)
                        batch.clear()
                if stop_requested:
                    break
            if batch:
                processed += len(batch)
                upsert_batch(db, batch, key, description, processed, batch[-1][0])
                print(f"Processed {processed} entities; last ID: {batch[-1][0]}",
                      flush=True)
            if stop_requested:
                print(f"Paused at {processed} entities. Run the same command to resume.")
                return
            if seen < processed:
                raise ValueError("Stored checkpoint exceeds the number of dump entities")
            print(f"Import complete: {processed} entities")
    finally:
        signal.signal(signal.SIGINT, previous_handler)


# Copy entity JSON from a source PostgreSQL table in resumable batches.
# ------------------------------------------------------------------
def migrate(source_dsn: str, source_table: str, id_column: str,
            json_column: str, batch_size: int) -> None:
    key = "migration:" + hashlib.sha256(
        f"{source_dsn}|{source_table}|{id_column}|{json_column}".encode()
    ).hexdigest()
    description = f"source table {source_table} ({id_column}, {json_column})"
    query = sql.SQL(
        "SELECT {}, {} FROM {} WHERE (%s::text IS NULL OR {} > %s) "
        "ORDER BY {} LIMIT %s"
    ).format(
        sql.Identifier(id_column), sql.Identifier(json_column),
        sql.Identifier(*source_table.split(".")), sql.Identifier(id_column),
        sql.Identifier(id_column),
    )
    with psycopg.connect(source_dsn) as source, psycopg.connect(target_dsn()) as target:
        processed, last_id = migration_checkpoint(target, key)
        while True:
            rows = source.execute(query, (last_id, last_id, batch_size)).fetchall()
            if not rows:
                break
            batch = []
            for entity_id, payload in rows:
                if not isinstance(entity_id, str):
                    raise ValueError("Source IDs must be text")
                if isinstance(payload, str):
                    payload = json.loads(payload)
                if not isinstance(payload, dict):
                    raise ValueError(f"Source entity {entity_id} is not a JSON object")
                batch.append((entity_id, str(payload.get("type", "unknown")),
                              json.dumps(payload, ensure_ascii=False)))
            last_id = rows[-1][0]
            if batch:
                processed += len(batch)
                upsert_batch(target, batch, key, description, processed, last_id)
                print(f"Migrated {processed} rows", flush=True)
        print(f"Migration complete: {processed} rows")


# Report entity counts and the latest saved progress for each source.
# ---------------------------------------------------------------
def validate() -> None:
    with psycopg.connect(target_dsn()) as db:
        count, missing_type = db.execute(
            "SELECT count(*), count(*) FILTER (WHERE entity_type = 'unknown') "
            "FROM entities"
        ).fetchone()
        print(f"Entities: {count}; unknown type: {missing_type}")
        for description, processed, last_id, updated_at in db.execute(
            "SELECT source_description, processed, last_id, updated_at "
            "FROM import_progress ORDER BY updated_at DESC"
        ):
            print(f"{description}: {processed} processed; last ID: {last_id}; "
                  f"updated at {updated_at}")


# Parse the command line and run the requested pipeline operation.
# ------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="Create database, PostGIS extension and schema")
    migration = commands.add_parser("migrate", help="Copy id + JSON documents from a source table")
    migration.add_argument("--source-dsn", default=os.environ.get("WIKIDATA_SOURCE_DSN"))
    migration.add_argument("--source-table", required=True)
    migration.add_argument("--id-column", default="id")
    migration.add_argument("--json-column", default="data")
    importer = commands.add_parser("import-dump", help="Stream a Wikidata JSON dump")
    importer.add_argument("path", type=Path)
    importer.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    migration.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    commands.add_parser("validate", help="Count entities and show checkpoints")
    args = parser.parse_args()
    if args.command == "migrate" and not args.source_dsn:
        parser.error("migrate requires WIKIDATA_SOURCE_DSN or --source-dsn")
    if args.command in ("migrate", "import-dump") and args.batch_size < 1:
        parser.error("--batch-size must be positive")
    try:
        if args.command == "setup":
            setup()
        elif args.command == "migrate":
            migrate(args.source_dsn, args.source_table, args.id_column,
                    args.json_column, args.batch_size)
        elif args.command == "import-dump":
            import_dump(args.path, args.batch_size)
        else:
            validate()
    except (psycopg.Error, OSError, ValueError) as exc:
        parser.exit(1, f"{args.command}: {exc}\n")


if __name__ == "__main__":
    main()
