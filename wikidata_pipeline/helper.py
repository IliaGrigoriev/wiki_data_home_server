"""Shared database, worker, progress, and stop helpers."""

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import signal
import sys
from time import monotonic

import psycopg
from psycopg import sql

from const import Settings


@dataclass(frozen=True)
class WorkerPlan:
    decompressors: int
    parsers: int
    writers: int = 1


class WorkerPlanner:
    """Choose small pools at runtime so PostgreSQL has CPU and memory."""

    # Use CPU affinity and available memory on the host running the importer.
    # ---------------------------------------------------------------------
    @staticmethod
    def choose() -> WorkerPlan:
        try:
            cpus = len(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            cpus = os.cpu_count() or 1
        try:
            memory_kib = next(
                int(line.split()[1])
                for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith("MemAvailable:")
            )
        except (OSError, StopIteration, ValueError):
            memory_kib = 0
        decompressors = 2 if cpus >= 4 and memory_kib >= 4 * 1024 * 1024 else 1
        parsers = 2 if cpus >= 8 and memory_kib >= 8 * 1024 * 1024 else 1
        return WorkerPlan(decompressors, parsers)


class Database:
    """Keep schema and checkpoint operations shared by both importers."""

    PROGRESS_UPSERT = (
        "INSERT INTO import_progress "
        "(source_key, source_description, processed, last_id, completed, decompressed_offset) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (source_key) DO UPDATE SET "
        "processed = EXCLUDED.processed, last_id = EXCLUDED.last_id, "
        "completed = EXCLUDED.completed, "
        "decompressed_offset = EXCLUDED.decompressed_offset, updated_at = now()"
    )

    # Create the target database if needed and apply its schema.
    # --------------------------------------------------------
    @staticmethod
    def setup() -> None:
        with psycopg.connect(Settings.ADMIN_DSN, autocommit=True) as admin:
            exists = admin.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", ("wikidata",)
            ).fetchone()
            if not exists:
                admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier("wikidata")))
                print("Created database wikidata")
        with psycopg.connect(Settings.TARGET_DSN) as db:
            db.execute(Settings.SCHEMA_PATH.read_text(encoding="utf-8"))
        print("Wikidata schema is ready")

    # Identify a dump by kind, resolved path, size, and modification time.
    # ---------------------------------------------------------------
    @staticmethod
    def source_identity(path: Path, kind: str = "") -> tuple[str, str]:
        stat = path.stat()
        description = f"{path.resolve()} size={stat.st_size} mtime_ns={stat.st_mtime_ns}"
        if kind:
            description = f"{kind}: {description}"
        return hashlib.sha256(description.encode()).hexdigest(), description

    # Read the last committed position, ID, completion flag, and byte offset.
    # ----------------------------------------------------------------------
    @staticmethod
    def checkpoint(db: psycopg.Connection, key: str) -> tuple[int, str | None, bool, int | None]:
        row = db.execute(
            "SELECT processed, last_id, completed, decompressed_offset "
            "FROM import_progress WHERE source_key = %s",
            (key,),
        ).fetchone()
        db.commit()
        return tuple(row) if row else (0, None, False, None)

    # Commit rows and their progress checkpoint in one transaction.
    # ----------------------------------------------------------
    @classmethod
    def save_batch(cls, db: psycopg.Connection, statement: str, rows: list[tuple],
                   key: str, description: str, processed: int,
                   last_id: str | None, decompressed_offset: int | None = None) -> None:
        with db.transaction():
            with db.cursor() as cur:
                cur.executemany(statement, rows)
                cur.execute(
                    cls.PROGRESS_UPSERT,
                    (key, description, processed, last_id, False, decompressed_offset),
                )

    # Mark a fully read source complete so later runs skip it.
    # -----------------------------------------------------
    @classmethod
    def mark_complete(cls, db: psycopg.Connection, key: str, description: str,
                      processed: int, last_id: str | None,
                      decompressed_offset: int | None = None) -> None:
        db.execute(
            cls.PROGRESS_UPSERT,
            (key, description, processed, last_id, True, decompressed_offset),
        )
        db.commit()


class OrderedBatchWriter:
    """Overlap SQL with reading while checkpoints stay in source order."""

    def __init__(self, statement: str, key: str, description: str) -> None:
        self.statement = statement
        self.key = key
        self.description = description
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wiki-sql")
        self.pending: deque[Future[None]] = deque()
        self.connection: psycopg.Connection | None = None
        self.failure: BaseException | None = None

    # Queue at most two batches so large records cannot fill memory.
    # -----------------------------------------------------------
    def submit(self, rows: list[tuple], processed: int, last_id: str,
               decompressed_offset: int | None = None) -> None:
        self.pending.append(self.executor.submit(
            self._write, rows, processed, last_id, decompressed_offset
        ))
        if len(self.pending) >= 2:
            self.pending.popleft().result()

    # Write one batch and its checkpoint atomically on the writer thread.
    # ---------------------------------------------------------------
    def _write(self, rows: list[tuple], processed: int, last_id: str,
               decompressed_offset: int | None) -> None:
        if self.failure is not None:
            raise RuntimeError("An earlier batch failed") from self.failure
        try:
            if self.connection is None:
                self.connection = psycopg.connect(Settings.TARGET_DSN)
            Database.save_batch(self.connection, self.statement, rows, self.key,
                                self.description, processed, last_id, decompressed_offset)
        except BaseException as exc:
            self.failure = exc
            raise

    # Wait for all writes and close the connection on its own thread.
    # -------------------------------------------------------------
    def close(self) -> None:
        failure = self.failure
        try:
            while self.pending:
                try:
                    self.pending.popleft().result()
                except BaseException as exc:
                    failure = self.failure or failure or exc
            if self.connection is not None:
                self.executor.submit(self.connection.close).result()
        finally:
            self.executor.shutdown(wait=True)
        if failure:
            raise failure


class ProgressBar:
    """Show approximate compressed-file progress without extra dependencies."""

    def __init__(self, label: str, total_bytes: int, unit: str = "") -> None:
        self.label = label
        self.total = total_bytes
        self.unit = unit
        self.last_rendered = 0.0
        self.last_percent = -1
        self.position = 0
        self.count = 0
        self.terminal = sys.stdout.isatty()

    # Render at most four times per second on a terminal, or every 5% in logs.
    # ----------------------------------------------------------------------
    def update(self, compressed_bytes: int, count: int = 0, force: bool = False) -> None:
        self.position = min(self.total, max(self.position, compressed_bytes))
        self.count = count
        percent = int(100 * self.position / self.total) if self.total else 100
        percent = min(percent, 99)
        now = monotonic()
        if not force:
            if self.terminal and now - self.last_rendered < 0.25:
                return
            if not self.terminal and percent // 5 <= self.last_percent // 5:
                return
        self.last_rendered = now
        self.last_percent = percent
        filled = min(24, percent * 24 // 100)
        bar = "#" * filled + "." * (24 - filled)
        amount = f" | {count:,} {self.unit}" if self.unit else ""
        line = (f"{self.label} [{bar}] {percent:3d}% "
                f"({self.position / 1_000_000_000:.2f}/{self.total / 1_000_000_000:.2f} GB){amount}")
        print(("\r" if self.terminal else "") + line, end="" if self.terminal else "\n", flush=True)

    # Leave a complete bar on success or a clean line on interruption.
    # ---------------------------------------------------------------
    def finish(self, completed: bool = True) -> None:
        if completed:
            self.position = self.total
            amount = f" | {self.count:,} {self.unit}" if self.unit else ""
            print(("\r" if self.terminal else "") +
                  f"{self.label} [{'#' * 24}] 100% "
                  f"({self.total / 1_000_000_000:.2f}/{self.total / 1_000_000_000:.2f} GB)"
                  + amount, flush=True)
        elif self.terminal:
            print()


class StopSignal:
    """Turn Ctrl+C into a flag so the current batch can finish."""

    def __init__(self) -> None:
        self.stopped = False
        self.previous = None

    # Install a temporary signal handler for an import run.
    # ----------------------------------------------------
    def __enter__(self):
        self.previous = signal.signal(signal.SIGINT, self._request_stop)
        return self

    # Restore the caller's signal handler when the import ends.
    # --------------------------------------------------------
    def __exit__(self, *_args) -> None:
        signal.signal(signal.SIGINT, self.previous)

    # Record a stop request without interrupting the active write.
    # ---------------------------------------------------------
    def _request_stop(self, _signum, _frame) -> None:
        self.stopped = True

    # Let import loops check the flag with a short call.
    # -----------------------------------------------
    def __call__(self) -> bool:
        return self.stopped
