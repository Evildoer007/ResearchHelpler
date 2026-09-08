from __future__ import annotations

import unittest

from core.product_profile import build_from_series, render_for_prompt
from core.product_profile_worker import RESULT_PREFIX, extract_result


class ProductProfileTests(unittest.TestCase):
    def test_profile_calculates_comparable_underlying_facts(self) -> None:
        prices = [100 + index * 0.2 for index in range(260)]
        # 留下一个可验证的近年回撤，避免测试只覆盖单调上涨路径。
        prices[-10:] = [145, 141, 138, 142, 144, 146, 147, 149, 150, 151]
        amounts = [1_000_000_000] * 30
        profile = build_from_series("510300.SH", prices=prices, amounts=amounts, name="沪深300ETF")

        self.assertTrue(profile["ok"])
        self.assertEqual(profile["code"], "510300.SH")
        self.assertAlmostEqual(profile["avg_daily_amount_20d_yi"], 10.0)
        self.assertIsNotNone(profile["return_20d"])
        self.assertIsNotNone(profile["return_60d"])
        self.assertLess(profile["max_drawdown_1y"], 0)
        self.assertIn("historical_scenarios", profile)

    def test_prompt_states_boundary_between_recommendation_and_pricing(self) -> None:
        profile = build_from_series("510300.SH", prices=[100 + index for index in range(250)])
        rendered = render_for_prompt(profile)

        self.assertIn("标的产品画像", rendered)
        self.assertIn("近20日收益", rendered)
        self.assertIn("正式报价仍须由 OptionHelper 自行取得定价行情", rendered)

    def test_missing_series_is_a_disclosed_gap_not_a_product_recommendation(self) -> None:
        profile = build_from_series("510300.SH", prices=[], amounts=[])

        self.assertFalse(profile["ok"])
        self.assertIn("未取得标的收盘价序列", profile["gaps"])
        self.assertIn("取数缺口", render_for_prompt(profile))

    def test_worker_result_ignores_ifind_stdout_noise(self) -> None:
        output = "C:\\Users\\analyst\\iFinDPy.pth\n" + RESULT_PREFIX + (
            '{"ok": true, "profile": {"code": "561160.SH", "return_20d": -6.93}}\n'
        )

        payload = extract_result(output)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["profile"]["code"], "561160.SH")
        self.assertEqual(payload["profile"]["return_20d"], -6.93)

    def test_worker_result_accepts_legacy_bare_json_after_noise(self) -> None:
        output = 'iFinD startup message\n{"ok": false, "message": "failed"}\n'

        self.assertEqual(extract_result(output)["message"], "failed")


if __name__ == "__main__":
    unittest.main()
