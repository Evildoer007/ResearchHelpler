import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from render.layout import CHART_FALLBACKS, _chart_for, _echarts_assets, _one_chart, _ordered_time_labels


class ChartAxisTests(unittest.TestCase):
    def test_metric_names_are_not_treated_as_a_trend_axis(self) -> None:
        self.assertFalse(_ordered_time_labels(["PB历史分位", "归母净利同比", "板块区间涨跌幅"]))

    def test_ordered_reporting_periods_are_valid_trend_axis(self) -> None:
        self.assertTrue(_ordered_time_labels(["2025Q2", "2025Q3", "2025Q4"]))

    def test_llm_dual_axis_for_unrelated_metrics_falls_back_to_cards(self) -> None:
        logic = SimpleNamespace(图表规格={
            "类型": "bar_line", "标题": "ROE与净利率同比变动",
            "数据点": [
                {"标签": "ROE", "值": "19.3%", "值2": "14.0%"},
                {"标签": "净利率", "值": "12.3%", "值2": "9.99%"},
            ],
        })
        # 非程序受控的双轴图不会继续渲染成暗示联动的柱线图。
        self.assertIsNotNone(_chart_for(logic))

    def test_mixed_unit_bar_falls_back_to_cards(self) -> None:
        CHART_FALLBACKS.clear()
        logic = SimpleNamespace(图表规格={
            "类型": "bar", "标题": "关键指标并不构成横向比较",
            "数据点": [
                {"标签": "PB", "值": "18.2倍"},
                {"标签": "净利同比", "值": "12.4%"},
                {"标签": "资金净流入", "值": "5.6亿元"},
            ],
        })
        self.assertIsNotNone(_chart_for(logic))
        self.assertTrue(any("量纲不一致" in message for message in CHART_FALLBACKS))

    def test_chart_metadata_is_rendered(self) -> None:
        logic = SimpleNamespace(图表规格={
            "类型": "bar", "标题": "主题篮子涨跌存在分化",
            "数据截至": "2026-08-27", "单位": "%", "样本口径": "主题篮子（5只）",
            "数据点": [
                {"标签": "甲", "值": "1.2%"},
                {"标签": "乙", "值": "-0.5%"},
            ],
        })
        html = _chart_for(logic)
        self.assertIn("截至：2026-08-27", html or "")
        self.assertIn("样本：主题篮子（5只）", html or "")

    def test_qualified_static_bar_has_interactive_html_layer_and_print_fallback(self) -> None:
        html = _one_chart({
            "类型": "bar", "标题": "主题篮子收益分化",
            "数据点": [
                {"标签": "甲", "值": "2.4%"},
                {"标签": "乙", "值": "0.8%"},
                {"标签": "丙", "值": "-1.1%"},
            ],
        }, "T1")
        self.assertIn('data-rh-echarts=', html)
        self.assertIn('class="chart-print"', html)

    def test_mixed_unit_data_never_gets_interactive_axis_chart(self) -> None:
        html = _one_chart({
            "类型": "bar", "标题": "不可比指标",
            "数据点": [
                {"标签": "PB", "值": "18.2倍"},
                {"标签": "净利同比", "值": "12.4%"},
                {"标签": "资金净流入", "值": "5.6亿元"},
            ],
        }, "T2")
        self.assertNotIn('data-rh-echarts=', html)

    def test_echarts_initializes_only_after_hidden_holders_join_layout(self) -> None:
        script = _echarts_assets()
        self.assertIn("classList.add('rh-echarts-preparing')", script)
        self.assertIn("requestAnimationFrame(() =>", script)
        self.assertIn("classList.add('rh-echarts-item-ready')", script)
        self.assertIn("rendered.forEach(chart => chart.resize())", script)
        self.assertNotIn("classList.add('rh-echarts-ready')", script)
        self.assertNotIn("toolbox:", script)

    def test_recommender_status_replaces_stale_not_called_text(self) -> None:
        from render.gaps import refresh_optionhelper_recommender_result

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.md"
            path.write_text(
                "## 三、OptionHelper 正式参考报价调用结果\n\n（本次未调用）\n\n---\n\n## 四、缺口\n",
                encoding="utf-8",
            )
            self.assertTrue(refresh_optionhelper_recommender_result(str(path), {
                "status": "completed", "message": "结构推荐已完成",
                "candidates": [{"product_name": "看涨期权", "reason": "方向匹配"}],
            }))
            text = path.read_text(encoding="utf-8")
            self.assertIn("结构推荐**：已完成", text)
            self.assertIn("正式参考报价**：尚未发起", text)
