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

import psycopg
from psycopg import sql

from config import admin_dsn, target_dsn


SCHEMA = Path(__file__).with_name("schema.sql")
BATCH_SIZE = 1000


def setup() -> None:
    """Create the database if needed, then apply the idempotent schema."""
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


def upsert_batch(db: psycopg.Connection, rows: list[tuple[str, str, str]],
                 source_key: str, source_description: str, processed: int,
                 last_id: str | None = None) -> None:
    """Commit data and checkpoint together so a retry cannot lose a batch."""
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


def checkpoint(db: psycopg.Connection, key: str) -> int:
    row = db.execute(
        "SELECT processed FROM import_progress WHERE source_key = %s", (key,)
    ).fetchone()
    # A SELECT starts an implicit transaction; close it before batch transactions.
    db.commit()
    return row[0] if row else 0


def migration_checkpoint(db: psycopg.Connection, key: str) -> tuple[int, str | None]:
    row = db.execute(
        "SELECT processed, last_id FROM import_progress WHERE source_key = %s", (key,)
    ).fetchone()
    db.commit()
    return (row[0], row[1]) if row else (0, None)


def source_identity(path: Path) -> tuple[str, str]:
    stat = path.stat()
    description = f"{path.resolve()} size={stat.st_size} mtime_ns={stat.st_mtime_ns}"
    return hashlib.sha256(description.encode()).hexdigest(), description


def dump_entities(path: Path):
    """Read Wikidata's line-oriented JSON array, one entity at a time."""
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


def import_dump(path: Path, batch_size: int) -> None:
    key, description = source_identity(path)
    with psycopg.connect(target_dsn()) as db:
        processed = checkpoint(db, key)
        batch = []
        seen = 0
        for index, entity in enumerate(dump_entities(path), 1):
            seen = index
            if index <= processed:
                continue
            batch.append((entity["id"], str(entity.get("type", "unknown")),
                          json.dumps(entity, ensure_ascii=False)))
            if len(batch) == batch_size:
                upsert_batch(db, batch, key, description, index)
                processed = index
                print(f"Imported {processed} entities", flush=True)
                batch.clear()
        if batch:
            processed += len(batch)
            upsert_batch(db, batch, key, description, processed)
        if seen < processed:
            raise ValueError("Stored checkpoint exceeds the number of dump entities")
        print(f"Import complete: {processed} entities")


def migrate(source_dsn: str, source_table: str, id_column: str,
            json_column: str, batch_size: int) -> None:
    """Copy a source table with text ids and JSON/JSONB entity documents."""
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


def validate() -> None:
    with psycopg.connect(target_dsn()) as db:
        count, missing_type = db.execute(
            "SELECT count(*), count(*) FILTER (WHERE entity_type = 'unknown') "
            "FROM entities"
        ).fetchone()
        print(f"Entities: {count}; unknown type: {missing_type}")
        for description, processed, updated_at in db.execute(
            "SELECT source_description, processed, updated_at "
            "FROM import_progress ORDER BY updated_at DESC"
        ):
            print(f"{description}: {processed} processed at {updated_at}")


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
