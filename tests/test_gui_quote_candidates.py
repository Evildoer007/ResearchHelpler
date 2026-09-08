from __future__ import annotations

import json
import unittest

from gui.app import ResearchHelperWindow


class QuoteCandidateFlowTests(unittest.TestCase):
    def test_theme_etf_research_target_does_not_hide_other_system_candidates(self) -> None:
        summary = {
            "request": "研究光模块并推荐产品",
            "metadata": {
                "系统建议挂钩工具": json.dumps([
                    {"code": "515880.SH", "name": "通信ETF"},
                    {"code": "515050.SH", "name": "5GETF"},
                ], ensure_ascii=False),
            },
        }
        candidates = ResearchHelperWindow._quote_underlying_candidates(summary, "515880.SH")
        self.assertEqual([item["code"] for item in candidates], ["515880.SH", "515050.SH"])
        self.assertEqual(candidates[0]["origin"], "研究取数目标")

    def test_client_named_pool_is_not_expanded_by_system_candidates(self) -> None:
        summary = {
            "request": "比较 300308.SZ 和 515050.SH 的产品机会",
            "metadata": {
                "系统建议挂钩工具": json.dumps([
                    {"code": "515880.SH", "name": "通信ETF"},
                ], ensure_ascii=False),
            },
        }
        candidates = ResearchHelperWindow._quote_underlying_candidates(summary, "515880.SH")
        self.assertEqual([item["code"] for item in candidates], ["300308.SZ", "515050.SH"])
        self.assertTrue(all(item["origin"] == "客户点名" for item in candidates))


if __name__ == "__main__":
    unittest.main()
