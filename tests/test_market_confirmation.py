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
            "515030.SH": "新能源车ETF",
            "588999.SH": "AI算力ETF",
        })
        self.liquid = patch("core.history.series", return_value=[2.0e8] * 20)
        self.industry = patch("core.universe.validate_industries",
                              side_effect=lambda names, provider=None: (
                                  [n for n in names if n in {"半导体", "通信设备", "计算机设备", "元件", "汽车", "电子"}],
                                  [n for n in names if n not in {"半导体", "通信设备", "计算机设备", "元件", "汽车", "电子"}],
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
