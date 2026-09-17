"""Set up PostgreSQL and import Wikidata entries and Wikipedia pages."""

import argparse
import os
from pathlib import Path
import xml.etree.ElementTree as ET

import psycopg

from config import target_dsn
from const import DUMP_PATH, ENWIKI_DUMP_PATH, POSTGRES_DATA_DIR
from helper import BATCH_SIZE, setup
from wikidata_entries import import_entries, migrate
from wikipedia_pages import import_pages


# Report row counts and the latest saved position for each import source.
# -------------------------------------------------------------------
def validate() -> None:
    with psycopg.connect(target_dsn()) as db:
        entities, missing_type = db.execute(
            "SELECT count(*), count(*) FILTER (WHERE entity_type = 'unknown') "
            "FROM entities"
        ).fetchone()
        pages, redirects = db.execute(
            "SELECT count(*), count(*) FILTER (WHERE redirect_title IS NOT NULL) "
            "FROM wikipedia_pages"
        ).fetchone()
        print(f"Entities: {entities}; unknown type: {missing_type}")
        print(f"Wikipedia pages: {pages}; redirects: {redirects}")
        for description, processed, last_id, updated_at in db.execute(
            "SELECT source_description, processed, last_id, updated_at "
            "FROM import_progress ORDER BY updated_at DESC"
        ):
            print(f"{description}: {processed} processed; last ID: {last_id}; "
                  f"updated at {updated_at}")


# Route setup, import, migration, and validation commands.
# -------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="Create database, PostGIS extension and tables")

    both = commands.add_parser("import-all", help="Import entries, then Wikipedia pages")
    both.add_argument("--entries-path", type=Path, default=DUMP_PATH)
    both.add_argument("--pages-path", type=Path, default=ENWIKI_DUMP_PATH)
    both.add_argument("--batch-size", type=int, default=BATCH_SIZE)

    for name in ("import-entries", "import-dump", "import-pages"):
        kind = "Wikidata JSON" if name != "import-pages" else "Wikipedia XML"
        command = commands.add_parser(name, help=f"Stream a {kind} dump")
        default = DUMP_PATH if name != "import-pages" else ENWIKI_DUMP_PATH
        command.add_argument("path", nargs="?", type=Path, default=default,
                             help=f"Dump file (default: {default})")
        command.add_argument("--batch-size", type=int, default=BATCH_SIZE)

    migration = commands.add_parser("migrate", help="Copy id + JSON from a source table")
    migration.add_argument("--source-dsn", default=os.environ.get("WIKIDATA_SOURCE_DSN"))
    migration.add_argument("--source-table", required=True)
    migration.add_argument("--id-column", default="id")
    migration.add_argument("--json-column", default="data")
    migration.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    commands.add_parser("validate", help="Count rows and show checkpoints")
    commands.add_parser("paths", help="Show configured filesystem paths")
    args = parser.parse_args()

    if args.command == "migrate" and not args.source_dsn:
        parser.error("migrate requires WIKIDATA_SOURCE_DSN or --source-dsn")
    if hasattr(args, "batch_size") and args.batch_size < 1:
        parser.error("--batch-size must be positive")

    try:
        if args.command == "setup":
            setup()
        elif args.command == "import-all":
            args.entries_path.stat()
            args.pages_path.stat()
            setup()
            if import_entries(args.entries_path, args.batch_size):
                import_pages(args.pages_path, args.batch_size)
        elif args.command in ("import-entries", "import-dump"):
            import_entries(args.path, args.batch_size)
        elif args.command == "import-pages":
            import_pages(args.path, args.batch_size)
        elif args.command == "migrate":
            migrate(args.source_dsn, args.source_table, args.id_column,
                    args.json_column, args.batch_size)
        elif args.command == "validate":
            validate()
        else:
            print(f"Wikidata dump: {DUMP_PATH}")
            print(f"Wikipedia archive: {ENWIKI_DUMP_PATH}")
            print(f"PostgreSQL data directory: {POSTGRES_DATA_DIR}")
    except (psycopg.Error, OSError, ValueError, ET.ParseError) as exc:
        parser.exit(1, f"{args.command}: {exc}\n")


if __name__ == "__main__":
    main()
