"""Set up PostgreSQL and import Wikidata entries and Wikipedia pages."""

import argparse
import xml.etree.ElementTree as ET

import psycopg

from const import Settings
from helper import Database
from wikidata_entries import WikidataEntryImporter
from wikidata_index import WikidataBlockIndex
from wikipedia_pages import WikipediaPageImporter


class PipelineCLI:
    """Choose the index-only action or the complete ordered import."""

    # Report row counts and the latest saved position for each import source.
    # -------------------------------------------------------------------
    @staticmethod
    def validate() -> None:
        with psycopg.connect(Settings.TARGET_DSN) as db:
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
            for description, processed, last_id, completed, updated_at in db.execute(
                "SELECT source_description, processed, last_id, completed, updated_at "
                "FROM import_progress ORDER BY updated_at DESC"
            ):
                print(f"{description}: {processed} processed; last ID: {last_id}; "
                      f"complete: {completed}; updated at {updated_at}")

    # Parse the two actions and run the selected pipeline stage.
    # ---------------------------------------------------------
    def run(self) -> None:
        parser = argparse.ArgumentParser(description=__doc__)
        action = parser.add_mutually_exclusive_group(required=True)
        action.add_argument("--build-wikidata-index", action="store_true",
                            help="Build the block index for latest-all.json.bz2 only")
        action.add_argument("--import-all", action="store_true",
                            help="Import Wikidata entries and Wikipedia pages into PostgreSQL")
        args = parser.parse_args()
        try:
            if args.build_wikidata_index:
                WikidataBlockIndex().build()
                return
            Settings.DUMP_PATH.stat()
            Settings.ENWIKI_DUMP_PATH.stat()
            Database.setup()
            if not WikidataEntryImporter().run():
                return
            if WikipediaPageImporter().run():
                self.validate()
        except (psycopg.Error, OSError, ValueError, ET.ParseError) as exc:
            raise SystemExit(f"import: {exc}") from exc


if __name__ == "__main__":
    PipelineCLI().run()
