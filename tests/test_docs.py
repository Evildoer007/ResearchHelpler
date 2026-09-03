from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.docs import read_pages, source_label


class PastedMaterialTests(unittest.TestCase):
    def test_pasted_text_keeps_manual_source_out_of_extractable_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "补充文字-20260902-abc123.txt"
            path.write_text(
                "来源：公司公告《2026 年半年报》p4\n"
                "录入时间：2026-09-02T10:00:00+08:00\n"
                "---\n"
                "公司披露上半年收入同比增长 20%。\n",
                encoding="utf-8",
            )
            self.assertEqual(source_label(path), "公司公告《2026 年半年报》p4")
            self.assertEqual(read_pages(path), ["公司披露上半年收入同比增长 20%。"])

    def test_plain_text_falls_back_to_filename_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "20260902-机构-主题.txt"
            path.write_text("一段普通文字材料。", encoding="utf-8")
            self.assertIn("机构", source_label(path))
            self.assertEqual(read_pages(path), ["一段普通文字材料。"])
