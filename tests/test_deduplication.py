"""Unit tests for the deduplication module."""

import time
import unittest

from utils.deduplication import MemoryDeduplicationStore


class TestMemoryDeduplication(unittest.TestCase):
    def setUp(self):
        self.store = MemoryDeduplicationStore(ttl_seconds=2, similarity_threshold=0.85)

    def test_not_duplicate_initially(self):
        self.assertFalse(self.store.is_duplicate("MSCU1234567"))

    def test_duplicate_after_mark(self):
        self.store.mark_seen("MSCU1234567")
        self.assertTrue(self.store.is_duplicate("MSCU1234567"))

    def test_fuzzy_match(self):
        self.store.mark_seen("MSCU1234567")
        # One character difference — should match at 0.85 threshold
        self.assertTrue(self.store.is_duplicate("MSCU1234568"))

    def test_different_container(self):
        self.store.mark_seen("MSCU1234567")
        self.assertFalse(self.store.is_duplicate("ABCD9999999"))

    def test_ttl_expiry(self):
        self.store.mark_seen("MSCU1234567")
        self.assertTrue(self.store.is_duplicate("MSCU1234567"))
        time.sleep(2.5)
        self.assertFalse(self.store.is_duplicate("MSCU1234567"))


class TestContainerInfoParsing(unittest.TestCase):
    def test_iso_pattern(self):
        from ocr.container_ocr import ISO_PATTERN
        cases = [
            ("MSCU1234567", True),
            ("ABCD 123456 7", True),
            ("AB1234567", False),
            ("XXXX9999999", True),
        ]
        for text, expected in cases:
            match = ISO_PATTERN.search(text)
            self.assertEqual(bool(match), expected, f"Failed for: {text}")


if __name__ == "__main__":
    unittest.main()
