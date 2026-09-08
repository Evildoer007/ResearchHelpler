from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from core.evidence_discovery import (
    EvidenceDocument,
    DiscoveredEvidence,
    SearchHit,
    assemble_inference_chains,
    classify_documents,
    discover,
    plan_query_groups,
    plan_queries,
    search_web,
)
from llm.client import ChatResult


class FakeClient:
    def __init__(self, data: dict, *, available: bool = True) -> None:
        self.data = data
        self.is_available = available

    def available(self) -> bool:
        return self.is_available

    def chat_json(self, *_args, **_kwargs) -> ChatResult:
        return ChatResult(True, data=self.data)


class SequenceClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)

    def available(self) -> bool:
        return True

    def chat_json(self, *_args, **_kwargs) -> ChatResult:
        return ChatResult(True, data=self.responses.pop(0))


class EvidenceDiscoveryTests(unittest.TestCase):
    def test_query_planner_uses_fallback_without_llm(self) -> None:
        queries, warnings = plan_queries("某公司新品上市对A股产业链的影响", FakeClient({}, available=False))
        self.assertGreaterEqual(len(queries), 3)
        self.assertTrue(any("官方公告" in item for item in queries))
        self.assertTrue(warnings)

    def test_query_planner_separates_three_evidence_channels(self) -> None:
        groups, warnings = plan_query_groups("某公司新品上市对A股产业链的影响", FakeClient({
            "event_fact_queries": ["公司新品上市 官方公告"],
            "industry_mechanism_queries": ["新品上市 上游器件需求机制"],
            "ashare_exposure_queries": ["A股器件公司 主营业务 官方"],
        }))
        self.assertFalse(warnings)
        self.assertEqual(groups["事件事实"], ["公司新品上市 官方公告"])
        self.assertEqual(groups["产业机制"], ["新品上市 上游器件需求机制"])
        self.assertEqual(groups["A股暴露"], ["A股器件公司 主营业务 官方"])

    def test_classifier_only_accepts_literal_quote_from_referenced_document(self) -> None:
        original = "公司公告显示，新产品已于本季度完成客户验证，并进入小批量交付阶段。"
        document = EvidenceDocument(
            document_id="D1", title="公告", source="公司公告 p3",
            reference="https://example.com/notice", text=original, source_kind="公开网页",
        )
        client = FakeClient({"candidates": [
            {"document_id": "D1", "type": "事件事实", "quote": original,
             "relation": "", "reason": "验证产品进展"},
            {"document_id": "D1", "type": "传导证据", "quote": "这句原文并不存在",
             "relation": "供应链", "reason": "模型自行推测"},
        ]})

        candidates, warnings = classify_documents("新品上市影响", [document], client)

        self.assertTrue(any("已丢弃" in item for item in warnings))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].content, original)
        self.assertEqual(candidates[0].evidence_type, "事件事实")

    def test_discovery_combines_search_fetch_classification_without_auto_accepting(self) -> None:
        fact_quote = "公司公告显示，新产品已于本季度完成客户验证，并进入小批量交付阶段。"
        mechanism_quote = "产业研究显示，该产品升级将增加上游关键器件的单位用量。"
        exposure_quote = "公司年报显示，其主营产品包括该类上游关键器件及配套组件。"
        client = SequenceClient([
            {"event_fact_queries": ["产品升级 官方披露"],
             "industry_mechanism_queries": ["产品升级 上游用量机制"],
             "ashare_exposure_queries": ["A股关键器件 主营业务"]},
            {"candidates": [{
                "document_id": "D1", "type": "事件事实", "quote": fact_quote,
                "relation": "", "reason": "验证产品进展",
            }]},
            {"candidates": [{
                "document_id": "D1", "type": "产业机制", "quote": mechanism_quote,
                "relation": "供应链", "reason": "连接产品事件与上游需求",
            }]},
            {"candidates": [{
                "document_id": "D1", "type": "A股暴露", "quote": exposure_quote,
                "reason": "证明A股公司业务暴露",
            }]},
            {"chains": [{"fact_ids": ["F1"], "mechanism_ids": ["M1"],
                         "exposure_ids": ["E1"], "conclusion": "该产品进展可能通过关键器件需求影响相关A股公司。",
                         "direction": "正向", "confidence": "中", "reason": "仍需订单验证"}]},
        ])
        fact_hit = SearchHit("公司公告", "https://example.com/notice")
        mechanism_hit = SearchHit("产业研究", "https://example.com/research")
        exposure_hit = SearchHit("公司年报", "https://example.com/annual")

        def fetcher(hit: SearchHit) -> EvidenceDocument:
            return EvidenceDocument(
                document_id="", title=hit.title, source=f"{hit.title} 2026-09-01",
                reference=hit.url,
                text=(fact_quote if hit == fact_hit else mechanism_quote if hit == mechanism_hit else exposure_quote),
                source_kind="公开网页",
            )

        with tempfile.TemporaryDirectory() as directory:
            result = discover(
                "产品升级对A股产业链影响", sources_dir=Path(directory), client=client,
                searcher=lambda query: ([fact_hit] if "官方披露" in query else
                                        [mechanism_hit] if "用量机制" in query else [exposure_hit]),
                fetcher=fetcher,
            )

        self.assertEqual(result.searched_documents, 3)
        self.assertEqual(len(result.candidates), 3)
        self.assertEqual({item.evidence_type for item in result.candidates}, {"事件事实", "产业机制", "A股暴露"})
        self.assertTrue(any(item.relation == "供应链" for item in result.candidates))
        self.assertEqual(len(result.chains), 1)

    def test_discovery_retries_a_targeted_mechanism_search(self) -> None:
        fact_quote = "公司公告显示，本季度产品已完成客户验证。"
        mechanism_quote = "研究指出，该变化将通过上游存储器采购影响封测环节的订单需求。"
        exposure_quote = "公司公告显示，主营业务包含存储器封装和测试服务。"
        client = SequenceClient([
            {"event_fact_queries": ["产品升级 官方公告"],
             "industry_mechanism_queries": ["产品升级 初次机制"],
             "ashare_exposure_queries": ["A股封测 主营业务"]},
            {"candidates": [{"document_id": "D1", "type": "事件事实", "quote": fact_quote, "reason": "事件"}]},
            {"candidates": [{"document_id": "D1", "type": "A股暴露", "quote": exposure_quote, "reason": "暴露"}]},
            {"candidates": [{"document_id": "D1", "type": "产业机制", "quote": mechanism_quote,
                                "relation": "供应链", "reason": "机制"}]},
            {"chains": [{"fact_ids": ["F1"], "mechanism_ids": ["M1"], "exposure_ids": ["E1"],
                         "conclusion": "该产品变化可能经存储器采购影响A股封测业务。"}]},
        ])
        fact_hit = SearchHit("公司公告", "https://example.com/notice")
        exposure_hit = SearchHit("A股公司公告", "https://example.com/exposure")
        retry_hit = SearchHit("产业机制研究", "https://example.com/link")

        def fetcher(hit: SearchHit) -> EvidenceDocument:
            return EvidenceDocument("", hit.title, hit.title, hit.url,
                                    fact_quote if hit == fact_hit else
                                    exposure_quote if hit == exposure_hit else mechanism_quote, "公开网页")

        with tempfile.TemporaryDirectory() as directory:
            result = discover(
                "产品升级对A股产业链影响", sources_dir=Path(directory), client=client,
                searcher=lambda query: [fact_hit] if "官方公告" in query else
                ([exposure_hit] if "主营业务" in query else
                 [retry_hit] if "行业影响机制" in query else []), fetcher=fetcher,
            )
        self.assertIn("产业机制补检", result.query_groups)
        self.assertEqual(sum(item.evidence_type == "产业机制" for item in result.candidates), 1)

    def test_discovery_filters_yahoo_quote_pages(self) -> None:
        client = FakeClient({"event_fact_queries": ["事件 官方公告"],
                             "industry_mechanism_queries": ["事件 产业机制"],
                             "ashare_exposure_queries": ["事件 A股暴露"]})
        quote_hit = SearchHit("SK hynix Inc. 走势图 - Yahoo股市", "https://finance.yahoo.com/quote/000660.KS")
        with tempfile.TemporaryDirectory() as directory:
            result = discover(
                "SK海力士业绩对A股存储影响", sources_dir=Path(directory), client=client,
                searcher=lambda _query: [quote_hit],
                fetcher=lambda _hit: self.fail("纯行情页不应被抓取"),
            )
        self.assertEqual(result.filtered_low_value_hits, 1)
        self.assertTrue(any("已过滤" in warning for warning in result.warnings))

    def test_foundation_mode_only_searches_fact_and_mechanism_with_progress(self) -> None:
        searched: list[str] = []
        progress: list[tuple[int, str]] = []
        with tempfile.TemporaryDirectory() as directory:
            result = discover(
                "SK海力士业绩对A股存储产业链的影响",
                sources_dir=Path(directory), client=FakeClient({}, available=False),
                searcher=lambda query: searched.append(query) or [],
                fetcher=lambda _hit: None, mode="foundation",
                progress=lambda value, message: progress.append((value, message)),
            )

        self.assertEqual(set(result.query_groups), {"事件事实", "产业机制", "产业机制补检"})
        self.assertTrue(searched)
        self.assertFalse(any("ETF 跟踪指数" in query or "A股 公司" in query for query in searched))
        self.assertEqual(progress[-1][0], 100)
        self.assertEqual([value for value, _ in progress], sorted(value for value, _ in progress))

    def test_complete_mode_uses_confirmed_target_and_keeps_existing_foundation(self) -> None:
        searched: list[str] = []
        existing = {
            "事件事实": [{"证据ID": "F1", "内容": "公司公告确认季度业绩增长。", "来源": "公司公告"}],
            "产业机制": [{"证据ID": "M1", "内容": "HBM需求会占用标准DRAM产能。", "来源": "产业报告"}],
        }
        context = {
            "research_theme": "A股存储芯片",
            "research_mode": "theme_basket",
            "theme_basket": [{"code": "603986.SH", "name": "兆易创新"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            result = discover(
                "SK海力士业绩对A股存储芯片的影响",
                sources_dir=Path(directory), client=FakeClient({}, available=False),
                searcher=lambda query: searched.append(query) or [],
                fetcher=lambda _hit: None, mode="complete",
                research_context=context, existing_evidence=existing,
            )

        self.assertIn("A股暴露", result.query_groups)
        self.assertNotIn("事件事实", result.query_groups)
        self.assertNotIn("产业机制", result.query_groups)
        self.assertTrue(any("兆易创新" in query and "603986.SH" in query for query in searched))
        self.assertEqual({item.evidence_id for item in result.candidates}, {"F1", "M1"})

    def test_complete_mode_uses_confirmed_context_and_keeps_existing_foundation(self) -> None:
        searched: list[str] = []
        context = {
            "research_theme": "存储芯片",
            "research_mode": "theme_basket",
            "theme_basket": [{"code": "603986.SH", "name": "兆易创新"}],
        }
        existing = {
            "事件事实": [{"内容": "公司公告确认季度业绩增长。", "来源": "公司公告"}],
            "产业机制": [{"内容": "HBM需求会挤占标准DRAM产能。", "来源": "产业研究"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            result = discover(
                "SK海力士业绩对A股存储产业链的影响",
                sources_dir=Path(directory), client=FakeClient({}, available=False),
                searcher=lambda query: searched.append(query) or [],
                fetcher=lambda _hit: None, mode="complete",
                research_context=context, existing_evidence=existing,
            )

        self.assertIn("A股暴露", result.query_groups)
        self.assertNotIn("事件事实", result.query_groups)
        self.assertNotIn("产业机制", result.query_groups)
        self.assertTrue(any("兆易创新" in query and "603986.SH" in query for query in searched))
        self.assertEqual(
            {item.evidence_type for item in result.candidates}, {"事件事实", "产业机制"})

    def test_chain_builder_rejects_uncited_new_number(self) -> None:
        candidates = [
            DiscoveredEvidence("事件事实", "公司公告确认产品进入量产。", "公告", "https://example.com/f", "F1"),
            DiscoveredEvidence("产业机制", "量产会增加相关器件采购需求。", "产业报告", "https://example.com/m", "M1"),
            DiscoveredEvidence("A股暴露", "公司主营业务包含相关器件。", "年报", "https://example.com/e", "E1"),
        ]
        chains, warnings = assemble_inference_chains("产品量产影响", candidates, FakeClient({"chains": [{
            "fact_ids": ["F1"], "mechanism_ids": ["M1"], "exposure_ids": ["E1"],
            "conclusion": "因此相关A股公司利润将增长50%。",
        }]}))
        self.assertFalse(warnings)
        self.assertEqual(chains, [])

    @patch("core.evidence_discovery._public_https", return_value=True)
    def test_public_search_parses_traceable_https_results(self, _safe) -> None:
        rss = b"""<?xml version='1.0'?><rss><channel>
        <item><title>Official filing</title><link>https://example.com/a</link>
        <description>Published result</description></item></channel></rss>"""

        class Response:
            content = rss

            @staticmethod
            def raise_for_status() -> None:
                return None

        class Session:
            @staticmethod
            def get(*_args, **_kwargs):
                return Response()

        hits = search_web("事件 公告", session=Session)
        self.assertEqual(hits, [SearchHit(
            title="Official filing", url="https://example.com/a", summary="Published result",
        )])


if __name__ == "__main__":
    unittest.main()
