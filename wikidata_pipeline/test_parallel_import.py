"""Small dump checks for ordered parallel imports and resume positions."""

import bz2
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import helper
import wikidata_entries
import wikidata_index
import wikipedia_pages


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def close(self):
        pass


class ImportTests(unittest.TestCase):
    def test_worker_plan_stays_small_on_e540(self):
        with patch.object(helper.os, "sched_getaffinity", return_value=set(range(4))), \
             patch.object(helper.Path, "read_text", return_value="MemAvailable: 12000000 kB\n"):
            self.assertEqual(helper.WorkerPlanner.choose(), helper.WorkerPlan(2, 1, 1))
        with patch.object(helper.os, "sched_getaffinity", return_value=set(range(4))), \
             patch.object(helper.Path, "read_text", return_value="MemAvailable: 2000000 kB\n"):
            self.assertEqual(helper.WorkerPlanner.choose(), helper.WorkerPlan(1, 1, 1))

    def test_entries_resume_after_ordered_parallel_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / "latest-all.json.bz2"
            index = Path(directory) / "latest-all.json.bz2.blocks.json"
            records = [json.dumps({"id": f"Q{number}", "type": "item",
                                   "text": "x" * 2000}).encode()
                       for number in range(130)]
            dump.write_bytes(bz2.compress(b"[\n" + b",\n".join(records) + b"\n]\n"))
            wikidata_index.WikidataBlockIndex(dump, index).build()
            state = {"checkpoint": (0, None, False, None), "rows": {},
                     "stop_at": 55, "seen": 0}

            class FakeProgress:
                def __init__(self, *_args):
                    pass

                def update(self, _offset, count=0, **_kwargs):
                    state["seen"] = count

                def finish(self, *_args):
                    pass

            @contextmanager
            def fake_stop():
                yield lambda: state["stop_at"] is not None and state["seen"] >= state["stop_at"]

            def save(_db, _statement, rows, _key, _description,
                     processed, last_id, decoded_offset):
                self.assertEqual(processed, state["checkpoint"][0] + len(rows))
                for entity_id, _entity_type, payload in rows:
                    state["rows"][entity_id] = json.loads(payload)
                state["checkpoint"] = (processed, last_id, False, decoded_offset)

            def complete(_db, _key, _description, processed, last_id, decoded_offset):
                state["checkpoint"] = (processed, last_id, True, decoded_offset)

            with patch.object(helper.psycopg, "connect", return_value=FakeConnection()), \
                 patch.object(helper.Database, "save_batch", side_effect=save), \
                 patch.object(helper.Database, "checkpoint",
                              side_effect=lambda *_: state["checkpoint"]), \
                 patch.object(helper.Database, "mark_complete", side_effect=complete), \
                 patch.object(wikidata_entries, "StopSignal", side_effect=fake_stop), \
                 patch.object(wikidata_entries, "ProgressBar", FakeProgress), \
                 patch.object(helper.WorkerPlanner, "choose",
                              return_value=helper.WorkerPlan(2, 1)):
                importer = wikidata_entries.WikidataEntryImporter(dump, 10, index)
                self.assertFalse(importer.run())
                self.assertEqual(state["checkpoint"][0], 55)
                self.assertIsNotNone(state["checkpoint"][3])
                state["stop_at"] = None
                self.assertTrue(importer.run())
            self.assertEqual(state["checkpoint"][0], 130)
            self.assertTrue(state["checkpoint"][2])
            self.assertEqual(len(state["rows"]), 130)

    def test_writer_does_not_advance_checkpoint_after_failure(self):
        calls = []

        def fail_first(_db, _statement, rows, *_args):
            calls.append(rows)
            raise ValueError("database write failed")

        with patch.object(helper.psycopg, "connect", return_value=FakeConnection()), \
             patch.object(helper.Database, "save_batch", side_effect=fail_first):
            writer = helper.OrderedBatchWriter("insert", "key", "source")
            with self.assertRaisesRegex(ValueError, "database write failed"):
                writer.submit([("Q1",)], 1, "Q1")
                writer.submit([("Q2",)], 2, "Q2")
            with self.assertRaisesRegex(ValueError, "database write failed"):
                writer.close()
        self.assertEqual(len(calls), 1)

    def test_page_import_uses_ordered_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / "pages.xml.bz2"
            index = Path(directory) / "pages-index.txt.bz2"
            pages = b"".join(
                f"<page><title>P{number}</title><ns>0</ns><id>{number}</id></page>".encode()
                for number in range(1, 6)
            )
            dump.write_bytes(bz2.compress(b"<mediawiki>" + pages + b"</mediawiki>"))
            index.write_bytes(bz2.compress(b"".join(
                f"0:{number}:P{number}\n".encode() for number in range(1, 6)
            )))
            state = {"checkpoint": (0, None, False, None), "rows": {}}

            def save(_db, _statement, rows, _key, _description, processed,
                     last_id, _offset):
                self.assertEqual(processed, state["checkpoint"][0] + len(rows))
                state["rows"].update((row[0], row) for row in rows)
                state["checkpoint"] = (processed, last_id, False, None)

            def complete(_db, _key, _description, processed, last_id):
                state["checkpoint"] = (processed, last_id, True, None)

            with patch.object(helper.psycopg, "connect", return_value=FakeConnection()), \
                 patch.object(helper.Database, "save_batch", side_effect=save), \
                 patch.object(wikipedia_pages.WikipediaPageImporter, "ensure_index"), \
                 patch.object(helper.Database, "checkpoint",
                              side_effect=lambda *_: state["checkpoint"]), \
                 patch.object(helper.Database, "mark_complete", side_effect=complete):
                self.assertTrue(wikipedia_pages.WikipediaPageImporter(dump, 2, index).run())
            self.assertEqual(state["checkpoint"][0], 5)
            self.assertEqual(set(state["rows"]), {1, 2, 3, 4, 5})


if __name__ == "__main__":
    unittest.main()
