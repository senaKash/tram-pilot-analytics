import json
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from scripts.run_example import make_archive
from tram_pilot.archive_reader import read_documents
from tram_pilot.database.importer import BatchImporter

ROOT = Path(__file__).resolve().parents[1]


class ReaderTests(unittest.TestCase):
    def test_archive_and_directory_have_same_documents(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "synthetic.tar.zst"
            make_archive(archive)
            self.assertEqual(list(read_documents(ROOT / "examples", 10)),
                             list(read_documents(archive, 10)))
            self.assertEqual(len(list(read_documents(archive, 1))), 1)

    def test_invalid_json_is_reported_without_stopping_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.json").write_text("{bad", encoding="utf-8")
            (root / "b.json").write_text('{"event_type":"Track"}', encoding="utf-8")
            documents = list(read_documents(root, 10))
            self.assertEqual(len(documents), 2)
            self.assertIsNone(documents[0][1])
            self.assertIn("Некорректный JSON", documents[0][2])
            self.assertIsNone(documents[1][2])


class BoundedBatchTests(unittest.TestCase):
    def test_batches_remain_bounded_on_many_documents(self):
        sample = json.loads((ROOT / "examples/direct_track.json").read_text(encoding="utf-8"))
        importer = BatchImporter(object(), ROOT / "examples", batch_size=32)
        sizes = []

        def documents(*_args):
            for index in range(5000):
                record = dict(sample)
                record["timestamp"] = f"2026-01-01T09:{index // 60 % 60:02d}:{index % 60:02d}Z"
                yield f"{index}.json", record, None

        def fake_write(_connection, tracks, events, _source, _run_id):
            sizes.append(len(tracks) + len(events))
            return 0, 0

        tracemalloc.start()
        try:
            with patch("tram_pilot.database.importer.read_documents", documents), \
                 patch("tram_pilot.database.importer.write_batch", fake_write):
                importer._scan(1)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(importer.stats.track_count, 5000)
        self.assertEqual(sum(sizes), 5000)
        self.assertLessEqual(max(sizes), 32)
        self.assertLess(peak, 12 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
