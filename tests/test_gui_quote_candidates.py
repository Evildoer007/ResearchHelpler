from __future__ import annotations

import json
import unittest

from gui.app import ResearchHelperWindow


class QuoteCandidateFlowTests(unittest.TestCase):
    def test_theme_etf_research_target_is_the_only_default_candidate(self) -> None:
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
        self.assertEqual([item["code"] for item in candidates], ["515880.SH"])
        self.assertEqual(candidates[0]["name"], "通信ETF")
        self.assertEqual(candidates[0]["origin"], "研究取数目标")

    def test_analyst_can_explicitly_expand_other_system_candidates(self) -> None:
        summary = {
            "request": "研究光模块并推荐产品",
            "metadata": {
                "系统建议挂钩工具": json.dumps([
                    {"code": "515880.SH", "name": "通信ETF", "note": "主题直接暴露"},
                    {"code": "515050.SH", "name": "5GETF", "note": "部分主题暴露"},
                ], ensure_ascii=False),
            },
        }
        candidates = ResearchHelperWindow._quote_underlying_candidates_with_options(
            summary, "515880.SH", include_system_discovered=True)
        self.assertEqual([item["code"] for item in candidates], ["515880.SH", "515050.SH"])
        self.assertEqual(candidates[0]["name"], "通信ETF")
        self.assertEqual(candidates[0]["note"], "主题直接暴露")
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

    def test_client_named_candidate_is_enriched_without_adding_other_tools(self) -> None:
        summary = {
            "request": "分析 515050.SH 的产品机会",
            "metadata": {
                "系统建议挂钩工具": json.dumps([
                    {"code": "515050.SH", "name": "华夏中证5G通信主题ETF", "note": "主题暴露已核验"},
                    {"code": "515880.SH", "name": "通信ETF"},
                ], ensure_ascii=False),
            },
        }
        candidates = ResearchHelperWindow._quote_underlying_candidates(summary, "515880.SH")
        self.assertEqual([item["code"] for item in candidates], ["515050.SH"])
        self.assertEqual(candidates[0]["name"], "华夏中证5G通信主题ETF")
        self.assertEqual(candidates[0]["note"], "主题暴露已核验")
        self.assertEqual(candidates[0]["origin"], "客户点名")


if __name__ == "__main__":
    unittest.main()
