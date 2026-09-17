"""Stream Wikidata JSON entries into PostgreSQL."""

import argparse
import bz2
import gzip
import hashlib
import json
from pathlib import Path

import psycopg
from psycopg import sql

from config import target_dsn
from const import DUMP_PATH
from helper import BATCH_SIZE, checkpoint, save_batch, source_identity, stop_on_sigint


ENTITY_UPSERT = (
    "INSERT INTO entities (id, entity_type, data) VALUES (%s, %s, %s::jsonb) "
    "ON CONFLICT (id) DO UPDATE SET "
    "entity_type = EXCLUDED.entity_type, data = EXCLUDED.data"
)


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


def import_entries(path: Path, batch_size: int) -> bool:
    key, description = source_identity(path)
    with stop_on_sigint() as stopped, psycopg.connect(target_dsn()) as db:
        processed, last_id = checkpoint(db, key)
        if processed:
            print(f"Resuming entries after {processed}; last ID: {last_id}", flush=True)
        batch = []
        seen = 0
        for index, entity in enumerate(dump_entities(path), 1):
            seen = index
            if index > processed:
                batch.append((entity["id"], str(entity.get("type", "unknown")),
                              json.dumps(entity, ensure_ascii=False)))
                if len(batch) == batch_size:
                    save_batch(db, ENTITY_UPSERT, batch, key, description, index, batch[-1][0])
                    processed = index
                    print(f"Processed {processed} entries; last ID: {batch[-1][0]}", flush=True)
                    batch.clear()
            if stopped():
                break
        if batch:
            processed += len(batch)
            save_batch(db, ENTITY_UPSERT, batch, key, description, processed, batch[-1][0])
            print(f"Processed {processed} entries; last ID: {batch[-1][0]}", flush=True)
        if stopped():
            print(f"Paused at {processed} entries. Run the same command to resume.")
            return False
        if seen < processed:
            raise ValueError("Stored checkpoint exceeds the number of dump entries")
        print(f"Entry import complete: {processed} entries")
        return True


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
        processed, last_id = checkpoint(target, key)
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
            processed += len(batch)
            save_batch(target, ENTITY_UPSERT, batch, key, description, processed, last_id)
            print(f"Migrated {processed} rows", flush=True)
        print(f"Migration complete: {processed} rows")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DUMP_PATH)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    import_entries(args.path, args.batch_size)


if __name__ == "__main__":
    main()
