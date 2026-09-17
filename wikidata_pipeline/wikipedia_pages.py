"""Stream Wikipedia XML pages into PostgreSQL."""

import argparse
import bz2
import gzip
from pathlib import Path
import xml.etree.ElementTree as ET

import psycopg

from config import target_dsn
from const import ENWIKI_DUMP_PATH
from helper import BATCH_SIZE, checkpoint, save_batch, source_identity, stop_on_sigint


PAGE_UPSERT = (
    "INSERT INTO wikipedia_pages "
    "(page_id, title, namespace, redirect_title, revision_id, text) "
    "VALUES (%s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (page_id) DO UPDATE SET "
    "title = EXCLUDED.title, namespace = EXCLUDED.namespace, "
    "redirect_title = EXCLUDED.redirect_title, "
    "revision_id = EXCLUDED.revision_id, text = EXCLUDED.text"
)


def _name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in element if _name(child.tag) == name), None)


def _required_text(element: ET.Element, name: str) -> str:
    child = _child(element, name)
    if child is None or child.text is None:
        raise ValueError(f"Wikipedia page is missing {name}")
    return child.text


def dump_pages(path: Path):
    opener = bz2.open if path.suffix == ".bz2" else gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as stream:
        events = ET.iterparse(stream, events=("start", "end"))
        _, root = next(events)
        for event, element in events:
            if event != "end" or _name(element.tag) != "page":
                continue
            revision = _child(element, "revision")
            redirect = _child(element, "redirect")
            content = _child(revision, "text") if revision is not None else None
            row = (
                int(_required_text(element, "id")),
                _required_text(element, "title"),
                int(_required_text(element, "ns")),
                redirect.get("title") if redirect is not None else None,
                int(_required_text(revision, "id")) if revision is not None else None,
                content.text if content is not None else None,
            )
            yield row
            root.clear()


def import_pages(path: Path, batch_size: int) -> bool:
    key, description = source_identity(path, "wikipedia-xml")
    with stop_on_sigint() as stopped, psycopg.connect(target_dsn()) as db:
        processed, last_id = checkpoint(db, key)
        if processed:
            print(f"Resuming pages after {processed}; last ID: {last_id}", flush=True)
        batch = []
        seen = 0
        for index, page in enumerate(dump_pages(path), 1):
            seen = index
            if index > processed:
                batch.append(page)
                if len(batch) == batch_size:
                    save_batch(db, PAGE_UPSERT, batch, key, description, index,
                               str(batch[-1][0]))
                    processed = index
                    print(f"Processed {processed} pages; last ID: {batch[-1][0]}", flush=True)
                    batch.clear()
            if stopped():
                break
        if batch:
            processed += len(batch)
            save_batch(db, PAGE_UPSERT, batch, key, description, processed,
                       str(batch[-1][0]))
            print(f"Processed {processed} pages; last ID: {batch[-1][0]}", flush=True)
        if stopped():
            print(f"Paused at {processed} pages. Run the same command to resume.")
            return False
        if seen < processed:
            raise ValueError("Stored checkpoint exceeds the number of dump pages")
        print(f"Page import complete: {processed} pages")
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=ENWIKI_DUMP_PATH)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    import_pages(args.path, args.batch_size)


if __name__ == "__main__":
    main()
