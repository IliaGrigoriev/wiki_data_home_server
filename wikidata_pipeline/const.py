"""Fixed paths and settings for the Wikidata pipeline."""

import os
from pathlib import Path


class Settings:
    DATA_DIR = Path("/home/ilia/Data/wiki")
    DUMP_PATH = DATA_DIR / "latest-all.json.bz2"
    WIKIDATA_INDEX_PATH = DATA_DIR / "latest-all.json.bz2.blocks.json"
    INDEX_WORKERS = min(8, max(2, (os.cpu_count() or 2) // 4))
    ENWIKI_DUMP_PATH = DATA_DIR / "enwiki-latest-pages-articles-multistream.xml.bz2"
    ENWIKI_INDEX_PATH = DATA_DIR / "enwiki-latest-pages-articles-multistream-index.txt.bz2"
    ENWIKI_DUMP_BASE_URL = "https://dumps.wikimedia.org/enwiki/latest"
    # This documents the server cluster; changing it does not move PostgreSQL.
    POSTGRES_DATA_DIR = Path("/data/wiki/postgresql/14/main")
    SCHEMA_PATH = Path(__file__).with_name("schema.sql")
    ADMIN_DSN = "dbname=postgres"
    TARGET_DSN = "dbname=wikidata"
    BATCH_SIZE = 1000
    MAX_BATCH_BYTES = 8 * 1024 * 1024
