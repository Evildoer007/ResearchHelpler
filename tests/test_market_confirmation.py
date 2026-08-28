from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import instruments
from core.brief import Brief, TargetRef
from core.market_confirmation import (
    Confirmation, apply_to_brief, needs_confirmation, proposal, verify,
)
from core.provider import FetchResult


class FakeProvider:
    name = "fake"

    def __init__(self, names: dict[str, str] | None = None) -> None:
        self.names = names or {}

    def available(self) -> bool:
        return True

    def get_basic(self, codes, indicators, params="") -> FetchResult:
        code = codes[0]
        name = self.names.get(code)
        if not name:
            return FetchResult(False, "fake", error="代码不存在")
        data = {code: {indicator: (name if indicator == "ths_stock_short_name_stock" else "")
                       for indicator in indicators}}
        return FetchResult(True, "fake", data=data)


def brief(raw: str, market: str, sectors: list[str], parts: list[str] | None = None) -> Brief:
    return Brief(原始需求=raw, 市场范围=market, 主题=raw, 涉及板块=sectors,
                 宽口径成分行业=parts or [], ok=True)


class MarketConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        instruments.clear_temporary()
        self.provider = FakeProvider({
            "512000.SH": "华宝中证全指证券公司ETF",
            "513050.SH": "中概互联ETF",
            "159992.SZ": "创新药ETF",
            "515980.SH": "人工智能ETF",
            "515050.SH": "通信ETF",
            "515030.SH": "新能源车ETF",
            "588999.SH": "AI算力ETF",
            "159937.SZ": "博时黄金ETF",
            "516520.SH": "华泰柏瑞智能驾驶ETF",
        })
        self.liquid = patch("core.history.series", return_value=[2.0e8] * 20)
        self.industry = patch("core.universe.validate_industries",
                              side_effect=lambda names, provider=None: (
                                  [n for n in names if n in {"半导体", "通信设备", "计算机设备", "元件", "汽车", "电子", "白酒"}],
                                  [n for n in names if n not in {"半导体", "通信设备", "计算机设备", "元件", "汽车", "电子", "白酒"}],
                              ))
        self.liquid.start()
        self.industry.start()

    def tearDown(self) -> None:
        patch.stopall()
        instruments.clear_temporary()

    def test_hk_innovation_drug_never_silently_maps_to_a_share_pharma(self) -> None:
        b = brief("基于港股创新药板块的投资机会", "港股", ["化学制药"])
        self.assertTrue(needs_confirmation(b))
        checked = verify(Confirmation("港股", "港股创新药", research_only=True), b,
                         provider=self.provider)
        self.assertTrue(checked.ok)
        apply_to_brief(checked, b, provider=self.provider)
        self.assertEqual(b.市场范围, "港股")
        self.assertEqual(b.涉及板块, ["化学制药"])  # 非 A 股确认不会覆写为另一个伪口径

    def test_explicit_etf_alias_accepts_verified_fund_full_name(self) -> None:
        from core import brief as brief_module, pipeline, topics

        check = topics._verify_code("512000.SH", "券商ETF", self.provider)
        self.assertTrue(check.startswith("ok:"), check)
        self.assertIn("别名已核验", check)
        b = brief("根据目前券商ETF 512000.SH推荐产品", "A股", ["证券"])
        # 即使 LLM 完全遗漏候选标的，用户原文中的代码也必须从受控目录直接进入候选池。
        b.候选标的 = []
        brief_module._inject_explicit_codes(b, b.原始需求, self.provider)
        self.assertEqual(b.候选标的[0].代码, "512000.SH")
        self.assertEqual(b.候选标的[0].名称, "券商ETF")
        b.候选标的[0].校验 = check
        self.assertEqual(pipeline._explicit_etf_from_brief(b), "512000.SH")

    def test_catalogued_explicit_etf_skips_confirmation_even_without_llm_candidate(self) -> None:
        """用户点名已知 ETF 时，不应再要求选择同一只工具或主题公司篮子。"""
        # 自然语言里代码后通常直接接“的”，不能依赖 ASCII 的单词边界。
        b = brief("根据目前酒ETF 512690.SH的市场情况推荐产品", "A股", ["白酒"])
        b.候选标的 = []  # 模拟解析器遗漏候选；判断必须只看用户原文和受控目录。
        self.assertFalse(needs_confirmation(b))

    def test_ordinary_sector_does_not_turn_llm_candidates_into_theme_basket(self) -> None:
        """普通白酒行业不应把单个贵州茅台显示为“主题研究篮子”。"""
        b = brief("白酒板块投资机会", "A股", ["白酒"])
        b.候选标的 = [TargetRef("贵州茅台", "600519.SH", "ok:贵州茅台")]
        payload = proposal(b, provider=self.provider)
        self.assertEqual(payload["theme_basket_candidates"], [])

    def test_system_normalizes_colloquial_wine_sector_before_showing_confirmation(self) -> None:
        """分析师不应看到系统自填、但数据源不承认的“酒类”选项。"""
        b = brief("酒ETF投资机会", "A股", ["酒类"])
        payload = proposal(b, provider=self.provider)
        self.assertEqual(payload["verified_scope_options"], ["白酒"])

    def test_explicit_etf_is_forwarded_to_sector_derived_fetches(self) -> None:
        """用户点名 ETF 时，行情字段不得回退到板块的默认 ETF。"""
        from core import fetcher

        provider = object()
        values = []
        def record(*args):
            values.append(args)
            return fetcher.FieldValue(field=str(args[0]), ok=True)

        with patch("core.fetcher._fetch_one", side_effect=record), \
             patch("core.instruments.resolve_analysis_etf") as resolve:
            fetcher.fetch_fields(
                ["年化波动率"], "600030.SH", provider, "证券", analysis_etf="512000.SH",
            )
        resolve.assert_not_called()
        self.assertEqual(values[0][-1], "512000.SH")

    def test_generic_gold_etf_requires_confirmation_and_is_temporary_commodity_etf(self) -> None:
        """泛称黄金 ETF 不得回退为贵金属个股或静态名单外的空对象。"""
        b = brief("近期黄金价格波动加大，客户想了解黄金 ETF 的投资机会",
                  "A股", ["贵金属"])
        self.assertTrue(needs_confirmation(b))
        checked = verify(Confirmation("A股", "贵金属", "159937.SZ"), b,
                         provider=self.provider)
        self.assertTrue(checked.ok, checked.errors)
        self.assertEqual(checked.instrument.类型, "商品ETF")
        apply_to_brief(checked, b, provider=self.provider)
        self.assertEqual(b.确认挂钩标的, "159937.SZ")
        self.assertEqual(b.确认挂钩标的类型, "商品ETF")
        self.assertEqual(b.候选标的, [])  # 不建立“紫金矿业”之类的错误数据锚点

    def test_commodity_fetch_uses_confirmed_etf_without_sector_fallback(self) -> None:
        from core import fetcher

        calls = []
        def record(*args, **kwargs):
            calls.append((args, kwargs))
            return fetcher.FieldValue(field=str(args[0]), ok=True)

        with patch("core.fetcher._fetch_one", side_effect=record):
            fetcher.fetch_fields(["年化波动率"], "159937.SZ", object(), None,
                                 analysis_etf="159937.SZ", asset_type="商品ETF")
        self.assertEqual(calls[0][0][4], "159937.SZ")
        self.assertEqual(calls[0][1]["asset_type"], "商品ETF")

    def test_viewpoint_recovers_missing_structured_direction_from_conclusion(self) -> None:
        """Writer 偶发漏 JSON 的推荐方向时，不能把已有的明确结论丢给报价链。"""
        from core.viewpoint import _resolve_direction
        from core.writer import ReportContent

        ma = SimpleNamespace(plan=SimpleNamespace(整体方向=""))
        rc = ReportContent(主题="测试", 类型="板块机会",
                           核心结论="多空交织，整体呈现震荡格局，方向倾向震荡。")
        self.assertEqual(_resolve_direction(ma, rc), ("震荡", "核心结论中的明确方向"))

    def test_company_document_is_not_auto_recommended_as_sector_spine(self) -> None:
        """单公司业绩点评可以人工作为案例选，但不能被证据厚度算法推成板块主轴。"""
        from core.pipeline import Candidate, recommend

        company = Candidate(kind="doc", id="doc_1", 名称="某公司利润高增",
                            类别="盈利/基本面类", 方向="看涨", 证据范围="公司级")
        self.assertEqual(recommend([company]), [])

    def test_confirmation_keeps_research_theme_separate_from_basket_scope(self) -> None:
        b = brief("光模块需求上修", "A股", ["通信设备"])
        checked = verify(Confirmation("A股", "通信设备", "515050.SH",
                                      research_theme="光模块"), b, provider=self.provider)
        self.assertTrue(checked.ok, checked.errors)
        apply_to_brief(checked, b, provider=self.provider)
        self.assertEqual(b.研究主题, "光模块")
        self.assertEqual(b.研究篮子口径, "通信设备")

    def test_confirmation_proposal_separates_fine_theme_from_standard_industry(self) -> None:
        """系统不能把“通信设备、光模块”原样塞进标准行业校验字段。"""
        b = brief("光模块需求上修", "A股", ["通信设备", "光模块"])
        payload = proposal(b, provider=self.provider)
        self.assertEqual(payload["proposed_theme"], "光模块")
        self.assertEqual(payload["verified_scope_options"], ["通信设备"])
        self.assertEqual(payload["proposed_scope"], "通信设备")

    def test_confirmed_theme_keeps_verified_company_basket_not_etf_constituents(self) -> None:
        from core.pipeline import _theme_basket_from_brief

        b = brief("光模块需求上修", "A股", ["通信设备"])
        b.候选标的 = [
            TargetRef("中际旭创", "300308.SZ", "ok:中际旭创"),
            TargetRef("新易盛", "300502.SZ", "ok:新易盛"),
            TargetRef("天孚通信", "300394.SZ", "ok:天孚通信"),
        ]
        checked = verify(Confirmation("A股", "通信设备", "515050.SH",
                                      research_theme="光模块"), b, provider=self.provider)
        self.assertTrue(checked.ok, checked.errors)
        apply_to_brief(checked, b, provider=self.provider)
        self.assertEqual(b.涉及板块, ["光模块"])
        self.assertEqual([item.代码 for item in _theme_basket_from_brief(b)],
                         ["300308.SZ", "300502.SZ", "300394.SZ"])

    def test_theme_basket_prevents_etf_from_overwriting_research_subject(self) -> None:
        """光模块篮子配通信 ETF 时，ETF 不得把研究对象改回通信设备。"""
        from core import pipeline, universe

        basket = [universe.Leader("300308.SZ", "中际旭创")]
        with patch("core.pipeline._all_fields", return_value=[]), \
                patch("core.fetcher.fetch_fields", return_value=([], [], object())), \
                patch("core.pipeline._components_detail", return_value=None), \
                patch("core.pipeline._subsector_detail", return_value=None), \
                patch("core.instruments.sector_of_etf") as aligned:
            profile = pipeline.fetch_profile(
                "300308.SZ", "光模块", self.provider,
                analysis_etf="515050.SH", theme_basket=basket, theme_basket_required=True,
            )
        aligned.assert_not_called()
        self.assertEqual(profile["__sector__"], "光模块")
        self.assertTrue(profile["__theme_basket__"])

    def test_underlying_reason_explains_theme_basket_to_etf_mapping(self) -> None:
        """客户版必须解释前文主题篮子与报价 ETF 的关系，不能只写“主题匹配”。"""
        from core import scenario_band, viewpoint
        from core.writer import ReportContent

        ma = SimpleNamespace(
            ok=True, rep_code="300308.SZ", rep_name="中际旭创", field_values={
                "__sector__": "光模块", "__etf__": "515050.SH",
            }, 确认挂钩标的="515050.SH", 仅研究=False, 挂钩择优=None,
            研究主题="光模块", 研究篮子口径="通信设备",
            研究篮子=["中际旭创（300308.SZ）", "新易盛（300502.SZ）"],
            板块理由="", plan=SimpleNamespace(整体方向=""), logics=[],
        )
        rc = ReportContent(主题="光模块", 类型="产业趋势", 推荐方向="看涨", ok=True)
        with patch.object(scenario_band, "calculate", side_effect=RuntimeError("skip")):
            pkg = viewpoint.build(ma, rc)
        self.assertIn("主题公司篮子", pkg.标的选择说明)
        self.assertIn("通信设备", pkg.标的选择说明)
        self.assertIn("可交易行业表达", pkg.标的选择说明)

    def test_analyst_selected_theme_basket_is_frozen_for_this_run(self) -> None:
        b = brief("光模块需求上修", "A股", ["通信设备"])
        pool = [TargetRef(f"候选{i}", f"300{i:03d}.SZ", "ok:主题候选") for i in range(6)]
        b.主题篮子候选池 = pool
        selected = [item.代码 for item in pool[:5]]
        checked = verify(Confirmation(
            "A股", "通信设备", "515050.SH", research_theme="光模块",
            theme_basket_codes=selected, theme_basket_confirmed=True,
        ), b, provider=self.provider)
        self.assertTrue(checked.ok, checked.errors)
        apply_to_brief(checked, b, provider=self.provider)
        self.assertEqual([item.代码 for item in b.主题篮子候选], selected)

    def test_sub_minimum_theme_basket_never_falls_back_to_wide_sector(self) -> None:
        """3 只核心样本可观察，但不能偷换为通信设备的行业整体数据。"""
        from core import pipeline, universe

        calls = []
        def fake_fields(*args, **kwargs):
            calls.append((args, kwargs))
            return [], [], object()

        small_basket = [universe.Leader("300308.SZ", "中际旭创"),
                        universe.Leader("300502.SZ", "新易盛"),
                        universe.Leader("300394.SZ", "天孚通信")]
        with patch("core.pipeline._all_fields", return_value=[]), \
                patch("core.fetcher.fetch_fields", side_effect=fake_fields), \
                patch("core.pipeline._components_detail", return_value=None), \
                patch("core.pipeline._subsector_detail", return_value=None):
            profile = pipeline.fetch_profile(
                "300308.SZ", "光模块", self.provider,
                analysis_etf="515050.SH", theme_basket=small_basket, theme_basket_required=True,
            )
        self.assertIsNone(calls[0][0][3])  # fetcher 的板块参数，禁止回退为通信设备
        self.assertIn("不生成行业整体指标", profile["__etf_basket_status__"])

    def test_dynamic_etf_discovery_pre_filters_and_caps_rows_before_official_checks(self) -> None:
        """问财返回上千行时，确认页不能逐只发基础数据请求。"""
        from core.market_confirmation import _DISCOVERY_SCAN_LIMIT, _discovery_rows

        b = brief("光模块投资机会", "A股", ["通信设备"])
        codes = [f"{500000 + i:06d}.SH" for i in range(_DISCOVERY_SCAN_LIMIT + 30)]
        names = [f"通信ETF{i}" for i in range(len(codes))]
        names[-1] = "光通信主题ETF"
        rows = _discovery_rows(codes, names, b)
        self.assertEqual(len(rows), _DISCOVERY_SCAN_LIMIT)
        self.assertEqual(rows[0][1], "光通信主题ETF")

    def test_dynamic_discovery_only_fills_remaining_suggestion_slots(self) -> None:
        from core import market_confirmation

        b = brief("光模块投资机会", "A股", ["通信设备"])
        with patch.object(market_confirmation, "discover_etfs", return_value=[]) as discover:
            market_confirmation._suggestions(b, provider=self.provider)
        self.assertEqual(discover.call_args.kwargs["limit"], 7)  # 通信ETF 已是起始候选

    def test_product_request_without_explicit_etf_code_requires_review(self) -> None:
        b = brief("光模块产业链的产品机会", "A股", ["通信设备"])
        b.客户产品诉求 = "推荐产品"
        self.assertTrue(needs_confirmation(b))

    def test_global_korean_company_request_requires_cross_market_confirmation(self) -> None:
        from unittest.mock import MagicMock

        client = MagicMock()
        client.available.return_value = False
        parsed = __import__("core.brief", fromlist=["parse"]).parse(
            "SK海力士发了业绩，全球市场芯片科技板块异动明显", client=client)
        self.assertEqual(parsed.市场范围, "跨市场")

    def test_event_report_is_blocked_without_entity_fact_and_transmission_evidence(self) -> None:
        from core import pipeline
        from core.genres import TYPE_EVENT

        b = Brief(
            原始需求="SK海力士业绩对 A 股芯片板块的影响",
            市场范围="A股",
            市场确认={"market": "A股", "research_scope": "半导体", "research_only": True},
            主导类型=TYPE_EVENT,
            触发实体=TargetRef("SK海力士", "000660.KS", "ok:SK海力士"),
            ok=True,
        )
        result = pipeline.run_from_brief(b)
        self.assertFalse(result.ok)
        self.assertIn("无法生成事件影响报告", result.error)

    def test_event_evidence_is_source_required_and_attached_as_separate_fields(self) -> None:
        from core import event_evidence, pipeline
        from core.genres import TYPE_EVENT

        b = Brief(
            原始需求="SK海力士业绩对 A 股芯片板块的影响",
            主导类型=TYPE_EVENT,
            触发实体=TargetRef("SK海力士", "000660.KS", "ok:SK海力士"),
            ok=True,
        )
        incomplete = event_evidence.parse({"事件事实": [{"内容": "营收增长"}], "传导关系": []})
        self.assertFalse(event_evidence.assess(b, incomplete).ready)

        evidence = event_evidence.parse({
            "事件事实": [{"内容": "公司披露本季 HBM 出货增长", "来源": "SK hynix 季报 p4"}],
            "传导关系": [{"关系": "供应链", "内容": "该变化影响 A 股存储产业链预期",
                       "来源": "产业链研报 p8"}],
        })
        self.assertTrue(event_evidence.assess(b, evidence).ready)
        ma = pipeline.MarketAnalysis(plan=None, rep_code="159995.SZ", ok=True)
        event_evidence.attach_to_analysis(ma, b, evidence)
        self.assertIn("触发标的_事件事实1", ma.field_values)
        self.assertIn("事件传导证据1", ma.field_values)
        self.assertEqual(ma.事件证据["事件主体"], "SK海力士 000660.KS")

    def test_hk_internet_accepts_only_explicit_cross_border_tool(self) -> None:
        b = brief("基于港股互联网板块的投资机会", "港股", ["传媒"])
        checked = verify(Confirmation("港股", "港股互联网", "513050.SH"), b,
                         provider=self.provider)
        self.assertTrue(checked.ok, checked.errors)
        self.assertEqual(checked.instrument.代码, "513050.SH")
        self.assertNotEqual(checked.instrument.代码, "512980.SH")

    def test_ai_compute_requires_multi_industry_confirmation(self) -> None:
        b = brief("AI算力产业链景气变化", "A股", ["通信设备"])
        self.assertTrue(needs_confirmation(b))
        value = Confirmation("A股", "半导体、通信设备、计算机设备、元件", "515980.SH")
        checked = verify(value, b, provider=self.provider)
        self.assertTrue(checked.ok, checked.errors)
        lead = SimpleNamespace(简称="中际旭创", 代码="300308.SZ")
        with patch("core.universe.pick_representative", return_value=lead):
            apply_to_brief(checked, b, provider=self.provider)
        self.assertEqual(b.宽口径成分行业, ["半导体", "通信设备", "计算机设备", "元件"])
        self.assertEqual(b.确认挂钩标的, "515980.SH")

    def test_auto_electronics_keeps_basket_instead_of_forcing_one_industry(self) -> None:
        b = brief("基于A股汽车电子产业智能化加速的投资机会", "A股", ["汽车", "电子"])
        self.assertEqual(proposal(b)["proposed_scope"], "汽车、电子")
        self.assertEqual(proposal(b)["suggested_instruments"], [])
        checked = verify(Confirmation("A股", "汽车、电子", research_only=True), b,
                         provider=self.provider)
        self.assertTrue(checked.ok, checked.errors)
        self.assertEqual(checked.confirmation.industries, ["汽车", "电子"])

    def test_auto_electronics_expands_to_related_etf_search_terms(self) -> None:
        """发散只生成检索语义，不直接猜基金代码。"""
        from core.market_confirmation import _discovery_terms

        b = brief("基于A股汽车电子产业智能化加速的投资机会", "A股", ["汽车电子"])
        b.ETF检索词 = ["汽车电子", "智能驾驶"]
        terms = _discovery_terms(b)
        self.assertIn("汽车电子", terms)
        self.assertIn("智能驾驶", terms)
        self.assertIn("智能汽车", terms)
        self.assertNotIn("516520.SH", terms)

    def test_verified_theme_etf_route_does_not_require_fake_standard_industry(self) -> None:
        """汽车电子可按智能驾驶 ETF 真实成分研究，不必把主题冒充标准行业。"""
        b = brief("基于A股汽车电子产业智能化加速的投资机会", "A股", ["汽车电子"])
        checked = verify(Confirmation(
            "A股", "汽车电子", "516520.SH",
            research_theme="汽车电子", research_mode="theme_etf",
        ), b, provider=self.provider)
        self.assertTrue(checked.ok, checked.errors)
        apply_to_brief(checked, b, provider=self.provider)
        self.assertEqual(b.研究主题, "汽车电子")
        self.assertEqual(b.研究篮子口径, "汽车电子")
        self.assertEqual(b.涉及板块, ["汽车电子"])
        self.assertEqual(b.确认挂钩标的, "516520.SH")
        self.assertEqual(b.主题篮子候选, [])

    def test_common_ss_suffix_is_normalized_for_ifind(self) -> None:
        from core.market_confirmation import from_dict

        value = from_dict({
            "market": "A股", "research_scope": "汽车电子",
            "research_theme": "汽车电子", "research_mode": "theme_etf",
            "underlying_code": "516520.SS",
        })
        self.assertEqual(value.underlying_code, "516520.SH")

    def test_theme_etf_between_minimum_and_preferred_liquidity_passes_with_warning(self) -> None:
        b = brief("汽车电子智能化投资机会", "A股", ["汽车电子"])
        with patch("core.history.series", return_value=[4.5e7] * 20):
            checked = verify(Confirmation(
                "A股", "汽车电子", "516520.SH",
                research_theme="汽车电子", research_mode="theme_etf",
            ), b, provider=self.provider)
        self.assertTrue(checked.ok, checked.errors)
        self.assertTrue(any("优选门槛" in warning for warning in checked.warnings))

    def test_theme_etf_below_minimum_liquidity_is_rejected(self) -> None:
        b = brief("汽车电子智能化投资机会", "A股", ["汽车电子"])
        with patch("core.history.series", return_value=[2.0e7] * 20):
            checked = verify(Confirmation(
                "A股", "汽车电子", "516520.SH",
                research_theme="汽车电子", research_mode="theme_etf",
            ), b, provider=self.provider)
        self.assertFalse(checked.ok)
        self.assertTrue(any("最低门槛" in error for error in checked.errors))

    def test_hk_innovation_cannot_be_silently_confirmed_as_chemical_pharma(self) -> None:
        b = brief("基于港股创新药板块的投资机会", "港股", ["化学制药"])
        checked = verify(Confirmation("A股", "化学制药", "159992.SZ"), b,
                         provider=self.provider)
        self.assertFalse(checked.ok)
        self.assertTrue(any("研究口径" in error or "主题暴露" in error for error in checked.errors))

    def test_nonsense_industry_is_rejected(self) -> None:
        b = brief("分析行业123", "A股", ["123"])
        checked = verify(Confirmation("A股", "123", research_only=True), b,
                         provider=self.provider)
        self.assertFalse(checked.ok)
        self.assertTrue(any("数字" in error for error in checked.errors))

    def test_nonexistent_security_code_is_rejected(self) -> None:
        b = brief("AI算力产业链景气变化", "A股", ["半导体"])
        checked = verify(Confirmation("A股", "半导体", "123456.SH"), b,
                         provider=self.provider)
        self.assertFalse(checked.ok)
        self.assertTrue(any("无法从数据源验证" in error for error in checked.errors))

    def test_verified_code_outside_whitelist_is_temporary_only(self) -> None:
        b = brief("AI算力产业链景气变化", "A股", ["半导体", "通信设备"])
        self.assertIsNone(instruments.get("588999.SH"))
        checked = verify(Confirmation("A股", "半导体、通信设备", "588999.SH"), b,
                         provider=self.provider)
        self.assertTrue(checked.ok, checked.errors)
        self.assertEqual(instruments.get("588999.SH").代码, "588999.SH")
        self.assertNotIn("588999.SH", {item.代码 for item in instruments.INSTRUMENTS})

    def test_illiquid_etf_is_rejected(self) -> None:
        b = brief("AI算力产业链景气变化", "A股", ["半导体", "通信设备"])
        with patch("core.history.series", return_value=[2.0e7] * 20):
            checked = verify(Confirmation("A股", "半导体、通信设备", "515980.SH"), b,
                             provider=self.provider)
        self.assertFalse(checked.ok)
        self.assertTrue(any("低于" in error for error in checked.errors))

    def test_research_only_disables_underlying_selection(self) -> None:
        from core import pipeline

        b = brief("AI算力产业链景气变化", "A股", ["半导体"])
        b.主导类型 = "产业趋势"
        b.市场确认 = {"market": "A股", "research_scope": "半导体", "research_only": True}
        b.候选标的 = [TargetRef("中际旭创", "300308.SZ", "ok:中际旭创")]
        generated = pipeline.MarketAnalysis(plan=object(), rep_code="300308.SZ", ok=True)
        generated.挂钩择优 = object()
        with patch("core.pipeline.run", return_value=generated):
            result = pipeline.run_from_brief(b)
        self.assertTrue(result.仅研究)
        self.assertIsNone(result.挂钩择优)

    def test_json_output_empty_response_uses_strict_plain_fallback(self) -> None:
        from llm.client import ChatResult, DeepSeekClient

        client = DeepSeekClient()
        empty = ChatResult(False, content="", finish_reason="stop",
                           error="JSON 解析失败: Expecting value")
        plain = ChatResult(True, content='```json\n{"ok": true}\n```', finish_reason="stop")
        with patch.object(client, "chat", side_effect=[empty, empty, empty, plain]) as mocked:
            with patch("llm.client.time.sleep"):
                result = client.chat_json("只输出 JSON", "返回结果")
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data, {"ok": True})
        self.assertFalse(mocked.call_args_list[-1].kwargs["json_mode"])

    def test_llm_requests_disable_thinking_for_machine_readable_output(self) -> None:
        from unittest.mock import Mock

        from llm.client import DeepSeekClient

        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 5},
        }
        client = DeepSeekClient()
        with patch("llm.client.requests.post", return_value=response) as post:
            result = client.chat_json("只输出 JSON。", "返回 JSON。", retries=0)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(post.call_args.kwargs["json"]["thinking"], {"type": "disabled"})

    def test_optionhelper_constraints_drop_stale_underlying_and_view(self) -> None:
        from core.optionhelper_bridge import _effective_constraints

        selection = {
            "constraints": {
                "underlying": "512690.SH",
                "market_view": "震荡",
                "horizon": "6个月",
                "max_loss": "20%",
                "principal_fluctuation": True,
                "output_type": "backtest",
                "format": "pdf",
            }
        }
        result = _effective_constraints(
            selection,
            underlying="159995.SZ",
            market_view="看涨",
        )
        self.assertEqual(result["underlying"], "159995.SZ")
        self.assertEqual(result["market_view"], "看涨")
        self.assertEqual(result["horizon"], "6个月")
        self.assertEqual(result["max_loss"], "20%")
        self.assertEqual(result["output_type"], "quote")
        self.assertEqual(result["format"], "html")

    def test_optionhelper_selection_is_claimed_once_then_archived(self) -> None:
        from core.optionhelper_bridge import OptionHelperResult, _archive_selection, _claim_selection

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pending = root / ".optionhelper" / "selection.pending.json"
            legacy = root / ".optionhelper" / "selection.json"
            pending.parent.mkdir(parents=True)
            legacy.write_text('{"selection": {"product_id": "4.1"}}', encoding="utf-8")
            pending.write_text('{"selection": {"product_id": "3.1", "underlyings": ["159995.SZ"]}}',
                               encoding="utf-8")
            with patch("core.optionhelper_bridge.config.OPTIONHELPER_SELECTION_PATH", str(pending)), \
                    patch("core.optionhelper_bridge.config.OPTIONHELPER_HOST_URL", ""):
                lease, error = _claim_selection(root, "run-test-one")
                self.assertFalse(error)
                self.assertIsNotNone(lease)
                self.assertFalse(pending.exists())
                self.assertTrue(lease.active_path.exists())
                archived = _archive_selection(
                    lease,
                    OptionHelperResult(ok=False, stage="configuration", error="凭据未就绪"),
                )
                self.assertTrue(Path(archived).is_file())
                self.assertFalse(lease.active_path.exists())
                payload = json.loads(Path(archived).read_text(encoding="utf-8"))
                self.assertEqual(payload["lifecycle"]["state"], "consumed")
                self.assertEqual(payload["lifecycle"]["run_id"], "run-test-one")
                next_lease, next_error = _claim_selection(root, "run-test-two")
                self.assertIsNone(next_lease)
                self.assertIn("没有一次性 selection", next_error)
                self.assertTrue(legacy.is_file())  # 旧静态文件不会被偷偷复用。

    def test_optionhelper_failed_attempt_consumes_its_pending_selection(self) -> None:
        from core.optionhelper_bridge import run_full
        from core.viewpoint import ViewPackage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pending = root / ".optionhelper" / "selection.pending.json"
            pending.parent.mkdir(parents=True)
            pending.write_text(
                '{"selection": {"product_id": "3.1", "underlyings": ["159995.SZ"]}}',
                encoding="utf-8",
            )
            with patch("core.optionhelper_bridge.config.OPTIONHELPER_SELECTION_PATH", str(pending)), \
                    patch("core.optionhelper_bridge.config.OPTIONHELPER_HOST_URL", ""), \
                    patch("core.optionhelper_bridge.missing_setup", return_value=["缺少解释器"]):
                result = run_full(
                    ViewPackage(标的代码="159995.SZ", ok=True),
                    project_root=root,
                    run_id="run-failed-quote",
                )
            self.assertEqual(result.stage, "configuration")
            self.assertFalse(pending.exists())
            archive = root / ".optionhelper" / "selections" / "archive" / "run-failed-quote.json"
            self.assertTrue(archive.is_file())
            payload = json.loads(archive.read_text(encoding="utf-8"))
            self.assertEqual(payload["lifecycle"]["quote_status"], "failed")


if __name__ == "__main__":
    unittest.main()
