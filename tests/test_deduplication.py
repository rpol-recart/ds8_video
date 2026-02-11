"""Unit tests for the deduplication module."""

import time
import unittest

from utils.deduplication import MemoryDeduplicationStore, _ocr_variants


class TestMemoryDeduplication(unittest.TestCase):
    def setUp(self):
        self.store = MemoryDeduplicationStore(ttl_seconds=2, similarity_threshold=0.85)

    def test_not_duplicate_initially(self):
        self.assertFalse(self.store.is_duplicate("MSCU1234567"))

    def test_duplicate_after_mark(self):
        self.store.mark_seen("MSCU1234567")
        self.assertTrue(self.store.is_duplicate("MSCU1234567"))

    def test_fuzzy_match_ocr_confusion_O_vs_0(self):
        self.store.mark_seen("MSCU1234567")
        # O in owner code misread as 0
        self.assertTrue(self.store.is_duplicate("MSCU123456 7"))

    def test_fuzzy_match_ocr_confusion_I_vs_1(self):
        self.store.mark_seen("MICU1234567")
        # I misread as 1
        self.assertTrue(self.store.is_duplicate("M1CU1234567"))

    def test_fuzzy_match_ocr_confusion_S_vs_5(self):
        self.store.mark_seen("MSCU1234567")
        # S misread as 5
        self.assertTrue(self.store.is_duplicate("M5CU1234567"))

    def test_fuzzy_match_ocr_confusion_B_vs_8(self):
        self.store.mark_seen("ABCU1234567")
        # B misread as 8
        self.assertTrue(self.store.is_duplicate("A8CU1234567"))

    def test_different_container(self):
        self.store.mark_seen("MSCU1234567")
        self.assertFalse(self.store.is_duplicate("ABCD9999999"))

    def test_ttl_expiry(self):
        self.store.mark_seen("MSCU1234567")
        self.assertTrue(self.store.is_duplicate("MSCU1234567"))
        time.sleep(2.5)
        self.assertFalse(self.store.is_duplicate("MSCU1234567"))

    def test_normalization_strips_spaces_dashes(self):
        self.store.mark_seen("MSCU 123456-7")
        self.assertTrue(self.store.is_duplicate("MSCU1234567"))


class TestOCRVariants(unittest.TestCase):
    def test_generates_variants(self):
        variants = _ocr_variants("MSCU1234567")
        self.assertGreater(len(variants), 0)
        # S→5 variant
        self.assertIn("M5CU1234567", variants)
        # 1→I variant
        self.assertIn("MSCUI234567", variants)

    def test_max_variants_limit(self):
        variants = _ocr_variants("OOOOOO00000", max_variants=5)
        self.assertEqual(len(variants), 5)

    def test_no_variants_for_clean_chars(self):
        # Characters with no known OCR confusions
        variants = _ocr_variants("ABCD")
        # Only B→8 should produce a variant
        self.assertEqual(len(variants), 1)
        self.assertIn("A8CD", variants)


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
