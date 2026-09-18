"""Stream Wikidata JSON entries into PostgreSQL."""

import bz2
from collections import deque
from concurrent.futures import Future, ProcessPoolExecutor
from contextlib import ExitStack, closing
import gzip
import json
import multiprocessing
from pathlib import Path

import indexed_bzip2
import psycopg

from const import Settings
from helper import Database, OrderedBatchWriter, ProgressBar, StopSignal, WorkerPlanner
from wikidata_index import WikidataBlockIndex


class WikidataEntryImporter:
    """Decode, parse, and persist every Wikidata entity in source order."""

    ENTITY_UPSERT = (
        "INSERT INTO entities (id, entity_type, data) VALUES (%s, %s, %s::jsonb) "
        "ON CONFLICT (id) DO UPDATE SET "
        "entity_type = EXCLUDED.entity_type, data = EXCLUDED.data"
    )

    def __init__(self, path: Path = Settings.DUMP_PATH,
                 batch_size: int = Settings.BATCH_SIZE,
                 index_path: Path = Settings.WIKIDATA_INDEX_PATH) -> None:
        self.path = path
        self.batch_size = batch_size
        self.block_index = WikidataBlockIndex(path, index_path)

    # Yield raw entities with decoded and compressed offsets for parallel parsing.
    # -------------------------------------------------------------------------
    def _records(self, block_offsets: dict[int, int] | None,
                 start_offset: int, decompressor_count: int):
        with ExitStack() as opened:
            if block_offsets is not None:
                stream = opened.enter_context(
                    indexed_bzip2.open(
                        str(self.path), parallelization=decompressor_count
                    )
                )
                stream.set_block_offsets(block_offsets)
                compressed_position = lambda: stream.tell_compressed() // 8
            else:
                raw = opened.enter_context(self.path.open("rb"))
                if self.path.suffix == ".bz2":
                    stream = opened.enter_context(bz2.BZ2File(raw))
                elif self.path.suffix == ".gz":
                    stream = opened.enter_context(gzip.GzipFile(fileobj=raw))
                else:
                    stream = raw
                compressed_position = raw.tell
            if start_offset:
                stream.seek(start_offset)
            while line := stream.readline():
                next_offset = stream.tell()
                line = line.strip()
                if line in (b"", b"[", b"]"):
                    continue
                yield line.removesuffix(b",").strip(), next_offset, compressed_position()

    # Parse a bounded group in another process; static methods can be pickled.
    # ----------------------------------------------------------------------
    @staticmethod
    def parse_batch(records: list[tuple[int, bytes, int]]) -> list[tuple[str, str, str, int]]:
        rows = []
        for position, payload, decoded_offset in records:
            try:
                entity = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Entry {position}: expected one complete JSON entity") from exc
            if not isinstance(entity, dict) or not isinstance(entity.get("id"), str):
                raise ValueError(f"Entry {position}: entity has no string id")
            rows.append((entity["id"], str(entity.get("type", "unknown")),
                         payload.decode("utf-8"), decoded_offset))
        return rows

    # Explain the restart cost and ask before importing without a block index.
    # ---------------------------------------------------------------------
    def _confirm_without_index(self) -> bool:
        print(
            f"Warning: Wikidata bzip2 block index is missing: {self.block_index.index_path}\n"
            "The index maps compressed blocks to decompressed byte positions so "
            "an interrupted import can seek near its checkpoint. Without it, each "
            "restart must decompress and scan from the beginning to skip already "
            "committed entries; this may take hours.\n"
            "Build it first with: python main.py --build-wikidata-index",
            flush=True,
        )
        try:
            return input("Continue without the index? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False

    # Consume parser results in source order and queue bounded SQL batches.
    # ---------------------------------------------------------------
    def _consume_parsed(self) -> None:
        for entity_id, entity_type, payload, decoded_offset in self.parse_pending.popleft().result():
            self.sql_batch.append((entity_id, entity_type, payload))
            self.sql_batch_bytes += len(payload)
            self.last_id, self.last_offset = entity_id, decoded_offset
            if (len(self.sql_batch) >= self.batch_size
                    or self.sql_batch_bytes >= Settings.MAX_BATCH_BYTES):
                self.queued_count += len(self.sql_batch)
                self.writer.submit(self.sql_batch.copy(), self.queued_count,
                                   self.last_id, self.last_offset)
                self.sql_batch.clear()
                self.sql_batch_bytes = 0

    # Import resumable batches, draining queued work when Ctrl+C is requested.
    # ----------------------------------------------------------------------
    def run(self) -> bool:
        key, description = Database.source_identity(self.path)
        with StopSignal() as stopped, psycopg.connect(Settings.TARGET_DSN) as db:
            processed, last_id, completed, saved_offset = Database.checkpoint(db, key)
            if completed:
                print(f"Entry import already complete: {processed} entries")
                return True
            block_offsets = None
            if self.path.suffix == ".bz2":
                if self.block_index.index_path.is_file():
                    block_offsets = self.block_index.load()
                elif not self._confirm_without_index():
                    print("Entry import cancelled before reading the dump")
                    return False
            start_offset = saved_offset if block_offsets is not None and saved_offset else 0
            if processed and not start_offset:
                print("Scanning the dump from the beginning to reach the checkpoint", flush=True)
            if processed:
                print(f"Resuming entries after {processed}; last ID: {last_id}", flush=True)
            plan = WorkerPlanner.choose()
            print(f"Workers: {plan.decompressors} bzip2, {plan.parsers} JSON, "
                  f"{plan.writers} PostgreSQL", flush=True)
            progress = ProgressBar("Wikidata entries", self.path.stat().st_size, "entries")
            progress.update(0, processed, force=True)
            seen = processed if start_offset else 0
            self.last_id = last_id
            self.last_offset = saved_offset if start_offset else None
            self.queued_count = processed
            self.sql_batch: list[tuple] = []
            self.sql_batch_bytes = 0
            self.parse_pending: deque[Future[list[tuple[str, str, str, int]]]] = deque()
            self.writer = OrderedBatchWriter(self.ENTITY_UPSERT, key, description)
            try:
                with ProcessPoolExecutor(
                    max_workers=plan.parsers,
                    mp_context=multiprocessing.get_context("spawn"),
                ) as parsers:
                    parse_records: list[tuple[int, bytes, int]] = []
                    parse_bytes = 0
                    with closing(self._records(
                        block_offsets, start_offset, plan.decompressors
                    )) as records:
                        for index, (payload, decoded_offset, compressed_offset) in enumerate(
                            records, start=processed + 1 if start_offset else 1
                        ):
                            seen = index
                            progress.update(compressed_offset, index)
                            if index == processed:
                                self.last_offset = decoded_offset
                            if index > processed:
                                parse_records.append((index, payload, decoded_offset))
                                parse_bytes += len(payload)
                                if len(parse_records) >= 64 or parse_bytes >= 4 * 1024 * 1024:
                                    self.parse_pending.append(
                                        parsers.submit(self.parse_batch, parse_records)
                                    )
                                    parse_records = []
                                    parse_bytes = 0
                                    if len(self.parse_pending) >= plan.parsers * 2:
                                        self._consume_parsed()
                            if stopped():
                                break
                    if parse_records:
                        self.parse_pending.append(parsers.submit(self.parse_batch, parse_records))
                    while self.parse_pending:
                        self._consume_parsed()
                if self.sql_batch:
                    self.queued_count += len(self.sql_batch)
                    self.writer.submit(self.sql_batch.copy(), self.queued_count,
                                       self.last_id, self.last_offset)
            except BaseException:
                progress.finish(False)
                raise
            finally:
                self.writer.close()
            processed = self.queued_count
            if stopped():
                progress.finish(False)
                print(f"Paused at {processed} entries. Run the same command to resume.")
                return False
            if seen < processed:
                raise ValueError("Stored checkpoint exceeds the number of dump entries")
            Database.mark_complete(db, key, description, processed,
                                   self.last_id, self.last_offset)
            progress.count = processed
            progress.finish()
            print(f"Entry import complete: {processed} entries")
            return True


if __name__ == "__main__":
    WikidataEntryImporter().run()
