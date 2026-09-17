#!/usr/bin/env python3
"""Regression tests for feed JSONL rotation."""

import gzip
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from marketflow.feeds import rotation


class TestFeedRotation(unittest.TestCase):
    def test_size_rotation_archives_and_recreates_active_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = root / "runtime" / "feeds" / "oversize.jsonl"
            archive = root / "runtime" / "archive" / "feeds-daily"
            locks = root / "runtime" / "feeds" / ".locks"
            active.parent.mkdir(parents=True)
            active.write_text("x" * 32, encoding="utf-8")

            with patch.object(rotation, "ARCHIVE_ROOT", archive), \
                 patch.object(rotation, "LOCK_DIR", locks), \
                 patch.object(rotation, "GZIP_BIN", "/usr/bin/gzip"), \
                 patch.dict(os.environ, {"MARKETFLOW_FEED_MAX_ACTIVE_BYTES": "16"}):
                rotation.append_jsonl_line(active, '{"ok": true}\n')

            self.assertEqual(active.read_text(encoding="utf-8"), '{"ok": true}\n')

            gzip_deadline = time.time() + 5
            archived = []
            while time.time() < gzip_deadline:
                archived = list(archive.glob("*/*.gz"))
                if archived:
                    break
                time.sleep(0.05)
            self.assertEqual(len(archived), 1)
            self.assertIn("oversize.segment-", archived[0].name)
            with gzip.open(archived[0], "rt", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "x" * 32)


if __name__ == "__main__":
    unittest.main()
