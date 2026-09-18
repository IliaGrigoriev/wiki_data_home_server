"""Build and load a reusable bzip2 block map for the Wikidata dump."""

import json
import os
from pathlib import Path
import tempfile

import indexed_bzip2

from const import Settings
from helper import Database, ProgressBar


class WikidataBlockIndex:
    """Manage the block map associated with one compressed Wikidata dump."""

    def __init__(self, dump_path: Path = Settings.DUMP_PATH,
                 index_path: Path = Settings.WIKIDATA_INDEX_PATH) -> None:
        self.dump_path = dump_path
        self.index_path = index_path

    # Check that a saved block map belongs to the current compressed dump.
    # --------------------------------------------------------------------
    def load(self) -> dict[int, int]:
        with self.index_path.open("r", encoding="utf-8") as saved:
            document = json.load(saved)
        key, _ = Database.source_identity(self.dump_path)
        if (not isinstance(document, dict) or document.get("format") != 1
                or document.get("source_key") != key):
            raise ValueError(f"Wikidata block index is stale or unsupported: {self.index_path}")
        offsets = document.get("block_offsets")
        if not isinstance(offsets, list) or not offsets:
            raise ValueError(f"Wikidata block index is empty: {self.index_path}")
        try:
            block_offsets = {int(compressed): int(decoded) for compressed, decoded in offsets}
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Wikidata block index is malformed: {self.index_path}") from exc
        if (len(block_offsets) != len(offsets) or min(block_offsets.values()) != 0
                or min(block_offsets) < 0 or min(block_offsets.values()) < 0):
            raise ValueError(f"Wikidata block index has invalid offsets: {self.index_path}")
        return block_offsets

    # Scan bzip2 blocks in parallel and atomically save their byte positions.
    # ---------------------------------------------------------------------
    def build(self) -> None:
        dump_size = self.dump_path.stat().st_size
        if self.index_path.is_file():
            try:
                offsets = self.load()
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            else:
                print(f"Wikidata block index already exists: {self.index_path} "
                      f"({len(offsets)} blocks)")
                return
        print(f"Scanning {self.dump_path} with {Settings.INDEX_WORKERS} "
              "decompression workers", flush=True)
        progress = ProgressBar("Wikidata index", dump_size)
        progress.update(0, force=True)
        try:
            with indexed_bzip2.open(
                str(self.dump_path), parallelization=Settings.INDEX_WORKERS
            ) as stream:
                while stream.read(4 * 1024 * 1024):
                    progress.update(stream.tell_compressed() // 8)
                offsets = stream.block_offsets()
        except BaseException:
            progress.finish(False)
            raise
        if not offsets:
            raise ValueError("Wikidata dump contains no bzip2 blocks")
        key, _ = Database.source_identity(self.dump_path)
        document = {
            "format": 1,
            "source_key": key,
            "block_offsets": sorted(offsets.items()),
        }
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.index_path.parent,
                prefix=f".{self.index_path.name}.", suffix=".part", delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(document, temporary, separators=(",", ":"))
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self.index_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        progress.finish()
        print(f"Saved {len(offsets)} bzip2 block offsets to {self.index_path}")
