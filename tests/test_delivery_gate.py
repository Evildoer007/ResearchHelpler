from __future__ import annotations

import unittest
from types import SimpleNamespace
from pathlib import Path
import tempfile

import main
from core.run_tracker import RunTracker
from render.gaps import delivery_check_markdown


class DeliveryGateTests(unittest.TestCase):
    @staticmethod
    def _content():
        logic = SimpleNamespace(逻辑id="T1", 论述="可复核正文" * 50, 图表规格列表=[{}, {}])
        return SimpleNamespace(plan=None), SimpleNamespace(核心结论="结论" * 30, logics=[logic])

    def test_one_page_is_the_only_passing_formal_delivery(self) -> None:
        ma, rc = self._content()
        result = delivery_check_markdown(ma, rc, pdf_pages=1)
        self.assertIn("状态：通过", result)
        self.assertIn("真实 PDF 页数：**1 页**", result)

    def test_multi_page_is_explicitly_blocked_with_reduction_actions(self) -> None:
        ma, rc = self._content()
        result = delivery_check_markdown(
            ma, rc, pdf_pages=2,
            layout_audit={"overflow": [{"label": "策略逻辑二"}, {"label": "推荐结构·参考报价"}]},
        )
        self.assertIn("状态：不通过（禁止正式交付）", result)
        self.assertIn("真实 PDF 页数：**2 页**", result)
        self.assertIn("`T1`", result)
        self.assertIn("`策略逻辑二`", result)

    def test_missing_pdf_is_unverified_not_silently_accepted(self) -> None:
        ma, rc = self._content()
        result = delivery_check_markdown(ma, rc)
        self.assertIn("状态：未校验", result)
        self.assertIn("仅可作内部草稿", result)

    def test_tracker_marks_research_complete_but_not_deliverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tracker = RunTracker.create(root=Path(directory), request="测试")

            def work():
                tracker.add_metadata("一页通交付校验", "不通过：PDF 实测 2 页，禁止正式交付")
                return "research.html"

            self.assertEqual(main._run_tracked(tracker, work), "research.html")
            self.assertEqual(tracker.status, "completed_not_deliverable")


if __name__ == "__main__":
    unittest.main()
