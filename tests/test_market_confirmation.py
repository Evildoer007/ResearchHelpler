from __future__ import annotations

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

    def test_global_korean_company_request_requires_cross_market_confirmation(self) -> None:
        from unittest.mock import MagicMock

        client = MagicMock()
        client.available.return_value = False
        parsed = __import__("core.brief", fromlist=["parse"]).parse(
            "SK海力士发了业绩，全球市场芯片科技板块异动明显", client=client)
        self.assertEqual(parsed.市场范围, "跨市场")

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


if __name__ == "__main__":
    unittest.main()
