"""Stream Wikipedia XML pages into PostgreSQL."""

import bz2
import os
from pathlib import Path
import tempfile
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import psycopg

from const import Settings
from helper import Database, OrderedBatchWriter, ProgressBar, StopSignal, WorkerPlanner


class WikipediaPageImporter:
    """Read indexed XML streams and persist every page in source order."""

    PAGE_UPSERT = (
        "INSERT INTO wikipedia_pages "
        "(page_id, title, namespace, redirect_title, revision_id, text) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (page_id) DO UPDATE SET "
        "title = EXCLUDED.title, namespace = EXCLUDED.namespace, "
        "redirect_title = EXCLUDED.redirect_title, "
        "revision_id = EXCLUDED.revision_id, text = EXCLUDED.text"
    )

    def __init__(self, path: Path = Settings.ENWIKI_DUMP_PATH,
                 batch_size: int = Settings.BATCH_SIZE,
                 index_path: Path = Settings.ENWIKI_INDEX_PATH) -> None:
        self.path = path
        self.batch_size = batch_size
        self.index_path = index_path

    # Download a missing index only when the XML matches the latest dump size.
    # -----------------------------------------------------------------------
    def ensure_index(self) -> None:
        if self.index_path.is_file():
            return
        if self.index_path.exists():
            raise ValueError(f"Wikipedia index path is not a file: {self.index_path}")
        local_size = self.path.stat().st_size
        dump_url = f"{Settings.ENWIKI_DUMP_BASE_URL}/{self.path.name}"
        index_url = f"{Settings.ENWIKI_DUMP_BASE_URL}/{self.index_path.name}"
        with urlopen(Request(dump_url, method="HEAD"), timeout=30) as response:
            remote_size = response.headers.get("Content-Length")
        if remote_size is None or int(remote_size) != local_size:
            raise ValueError(
                "Local Wikipedia XML does not match the current latest dump size; "
                "provide the index from the same dump run"
            )
        print(f"Downloading Wikipedia index to {self.index_path}", flush=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.index_path.parent, prefix=f".{self.index_path.name}.",
                suffix=".part", delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                with urlopen(index_url, timeout=60) as response:
                    expected_size = response.headers.get("Content-Length")
                    progress = (ProgressBar("Wikipedia index download", int(expected_size))
                                if expected_size else None)
                    if progress:
                        progress.update(0, force=True)
                    downloaded = 0
                    while chunk := response.read(1024 * 1024):
                        temporary.write(chunk)
                        downloaded += len(chunk)
                        if progress:
                            progress.update(downloaded)
                if expected_size is not None and downloaded != int(expected_size):
                    raise ValueError("Wikipedia index download ended before the expected size")
            with bz2.open(temporary_path, "rb") as index:
                if not index.readline().startswith(b"0:"):
                    raise ValueError("Downloaded Wikipedia index has an invalid first entry")
            os.replace(temporary_path, self.index_path)
            if progress:
                progress.finish()
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    # Remove the XML namespace from an element tag.
    # ---------------------------------------------
    @staticmethod
    def _name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    # Find a direct child by its local XML tag name.
    # ---------------------------------------------
    @classmethod
    def _child(cls, element: ET.Element, name: str) -> ET.Element | None:
        return next((child for child in element if cls._name(child.tag) == name), None)

    # Read required text from a direct child, reporting missing page fields.
    # -------------------------------------------------------------------
    @classmethod
    def _required_text(cls, element: ET.Element, name: str) -> str:
        child = cls._child(element, name)
        if child is None or child.text is None:
            raise ValueError(f"Wikipedia page is missing {name}")
        return child.text

    # Read index groups, starting with the stream containing the next page.
    # ---------------------------------------------------------------
    def _indexed_streams(self, processed: int, last_id: str | None):
        with bz2.open(self.index_path, "rt", encoding="utf-8") as index:
            offset = None
            first_id = None
            group_start = 1
            count = 0
            total = 0
            for total, line in enumerate(index, 1):
                fields = line.rstrip("\n").split(":", 2)
                if len(fields) != 3:
                    raise ValueError(f"Invalid Wikipedia index line {total}")
                current_offset, page_id = int(fields[0]), int(fields[1])
                if current_offset < 0:
                    raise ValueError(f"Invalid Wikipedia index offset on line {total}")
                if total == processed and str(page_id) != last_id:
                    raise ValueError("Wikipedia index does not match the saved page checkpoint")
                if offset is None:
                    if current_offset != 0:
                        raise ValueError("Wikipedia index does not begin at byte zero")
                    offset, first_id = current_offset, page_id
                elif current_offset != offset:
                    if current_offset < offset:
                        raise ValueError("Wikipedia index offsets are out of order")
                    if group_start + count - 1 > processed:
                        yield offset, current_offset, first_id, count, group_start
                    offset, first_id = current_offset, page_id
                    group_start = total
                    count = 0
                count += 1
            if total == 0 or processed > total:
                raise ValueError("Wikipedia index is empty or shorter than the checkpoint")
            if group_start + count - 1 > processed:
                yield offset, None, first_id, count, group_start

    # Parse one independently compressed stream into page rows.
    # --------------------------------------------------------
    @classmethod
    def _stream_pages(cls, stream: bytes, expected_count: int, first_id: int):
        start = stream.find(b"<page>")
        end = stream.rfind(b"</page>")
        if start < 0 or end < start:
            raise ValueError("Indexed Wikipedia stream contains no pages")
        root = ET.fromstring(b"<root>" + stream[start:end + len(b"</page>")] + b"</root>")
        pages = list(root)
        if len(pages) != expected_count or int(cls._required_text(pages[0], "id")) != first_id:
            raise ValueError("Wikipedia index does not match the compressed dump")
        for element in pages:
            revision = cls._child(element, "revision")
            redirect = cls._child(element, "redirect")
            content = cls._child(revision, "text") if revision is not None else None
            yield (
                int(cls._required_text(element, "id")),
                cls._required_text(element, "title"),
                int(cls._required_text(element, "ns")),
                redirect.get("title") if redirect is not None else None,
                int(cls._required_text(revision, "id")) if revision is not None else None,
                content.text if content is not None else None,
            )

    # Seek to indexed bzip2 streams and skip only pages in the saved stream.
    # -------------------------------------------------------------------
    def _dump_pages(self, processed: int = 0, last_id: str | None = None):
        with self.path.open("rb") as dump:
            size = self.path.stat().st_size
            for offset, next_offset, first_id, count, group_start in self._indexed_streams(
                processed, last_id
            ):
                end = size if next_offset is None else next_offset
                if end > size or end <= offset:
                    raise ValueError("Wikipedia index offset is outside the compressed dump")
                dump.seek(offset)
                pages = self._stream_pages(bz2.decompress(dump.read(end - offset)),
                                           count, first_id)
                skip = max(0, processed - group_start + 1)
                for position, page in enumerate(pages, group_start):
                    if position == processed and str(page[0]) != last_id:
                        raise ValueError("Wikipedia dump does not match the saved page checkpoint")
                    if position >= group_start + skip:
                        yield position, page, end

    # Import pages in resumable batches, saving progress on Ctrl+C.
    # ----------------------------------------------------------
    def run(self) -> bool:
        self.ensure_index()
        key, description = Database.source_identity(self.path, "wikipedia-xml")
        with StopSignal() as stopped, psycopg.connect(Settings.TARGET_DSN) as db:
            processed, last_id, completed, _ = Database.checkpoint(db, key)
            if completed:
                print(f"Page import already complete: {processed} pages")
                return True
            if processed:
                print(f"Resuming pages after {processed}; last ID: {last_id}", flush=True)
            print(f"Workers: {WorkerPlanner.choose().writers} PostgreSQL; "
                  "Wikipedia multistreams are decoded one at a time", flush=True)
            progress = ProgressBar("Wikipedia pages", self.path.stat().st_size, "pages")
            progress.update(0, processed, force=True)
            batch = []
            batch_bytes = 0
            writer = OrderedBatchWriter(self.PAGE_UPSERT, key, description)
            try:
                for index, page, compressed_offset in self._dump_pages(processed, last_id):
                    progress.update(compressed_offset, index)
                    batch.append(page)
                    batch_bytes += len(page[5] or "")
                    if len(batch) >= self.batch_size or batch_bytes >= Settings.MAX_BATCH_BYTES:
                        writer.submit(batch.copy(), index, str(batch[-1][0]))
                        processed = index
                        last_id = str(batch[-1][0])
                        batch.clear()
                        batch_bytes = 0
                    if stopped():
                        break
                if batch:
                    processed += len(batch)
                    writer.submit(batch.copy(), processed, str(batch[-1][0]))
                    last_id = str(batch[-1][0])
            except BaseException:
                progress.finish(False)
                raise
            finally:
                writer.close()
            if stopped():
                progress.finish(False)
                print(f"Paused at {processed} pages. Run the same command to resume.")
                return False
            Database.mark_complete(db, key, description, processed, last_id)
            progress.count = processed
            progress.finish()
            print(f"Page import complete: {processed} pages")
            return True


if __name__ == "__main__":
    WikipediaPageImporter().run()
