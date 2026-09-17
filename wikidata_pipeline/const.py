"""Filesystem paths used by the Wikidata pipeline and server setup."""

from pathlib import Path


DATA_DIR = Path("/home/ilia/Data/wiki")
DUMP_PATH = DATA_DIR / "latest-all.json.bz2"
ENWIKI_DUMP_PATH = DATA_DIR / "enwiki-latest-pages-articles-multistream.xml.bz2"
POSTGRES_DATA_DIR = Path("/data/wiki/postgresql/14/main")
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
