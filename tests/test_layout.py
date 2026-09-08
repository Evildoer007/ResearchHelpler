import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from render.layout import (
    CHART_FALLBACKS, _chart_block, _chart_for, _chart_layout_mode, _chart_meta_html,
    _echarts_assets, _one_chart, _ordered_time_labels,
    _report_title, _underlying_block, confirmed_underlying_block, multi_quote_block,
)


class ChartAxisTests(unittest.TestCase):
    def test_chart_layout_uses_semantics_and_density_not_llm_size_hint(self) -> None:
        # 小型同篮子气泡图在半宽中仍能读，适合伴随论述显示。
        self.assertEqual(_chart_layout_mode([{
            "类型": "bubble", "数据点": [{"标签": str(i)} for i in range(5)],
            "展示尺寸": "full",  # 模型即使写了也不能影响程序布局。
        }]), "side")
        # 长时间轴、瀑布拆解和两张图保留完整宽度/并排结构。
        self.assertEqual(_chart_layout_mode([{
            "类型": "line", "数据点": [{"标签": f"2026-{i:02d}"} for i in range(1, 7)],
        }]), "full")
        self.assertEqual(_chart_layout_mode([{"类型": "bar", "数据点": [{"标签": "甲"}, {"标签": "乙"}]},
                                              {"类型": "scatter", "数据点": []}]), "full")

    def test_single_compact_chart_gets_side_wrapper(self) -> None:
        logic = SimpleNamespace(图表规格列表=[{
            "类型": "lollipop", "标题": "主题篮子收益分化",
            "数据点": [{"标签": "甲", "值": "2.4%"}, {"标签": "乙", "值": "0.8%"},
                       {"标签": "丙", "值": "-1.1%"}],
        }], 逻辑id="T0")
        self.assertIn("chart-block--side", _chart_block(logic))

    def test_research_report_never_renders_unconfirmed_underlying(self) -> None:
        # 初始研究报告没有 OptionHelper 正式报价结果，即使内部曾发现 ETF 也不能显示挂钩卡。
        self.assertEqual(_underlying_block(SimpleNamespace(), SimpleNamespace(), None), "")

    def test_confirmed_quote_underlying_card_uses_analyst_selection(self) -> None:
        html = confirmed_underlying_block(
            "561160.SH", name="电池ETF", reason="分析师在研究完成后选择",
            product_profile={"realized_volatility_20d": 18.6,
                             "volatility_percentile_3y": 7.5},
        )
        self.assertIn("561160.SH", html)
        self.assertIn("电池ETF", html)
        self.assertIn("分析师在研究完成后选择", html)
        self.assertIn("20日实现波动率 18.6%", html)

    def test_delivery_title_prefers_full_report_title_over_research_scope(self) -> None:
        analysis = SimpleNamespace(
            报告标题="基于A股汽车电子产业智能化加速的投资机会",
            plan=SimpleNamespace(主题="汽车电子"),
        )
        self.assertEqual(_report_title(analysis), "基于A股汽车电子产业智能化加速的投资机会")

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

    def test_internal_metadata_placeholders_are_hidden_from_client_report(self) -> None:
        self.assertEqual(_chart_meta_html({
            "数据截至": "见底稿", "单位": "见坐标轴/数据卡", "样本口径": "见底稿",
        }), "")
        meta = _chart_meta_html({"数据截至": "见底稿", "单位": "%", "样本口径": "主题篮子（5只）"})
        self.assertNotIn("见底稿", meta)
        self.assertNotIn("截至：", meta)
        self.assertIn("单位：%", meta)

    def test_number_card_canvas_is_stable_and_uses_near_white_background(self) -> None:
        from matplotlib.colors import to_hex
        import matplotlib.pyplot as plt
        from render import charts, style

        short = charts.number_cards([{"label": "市值", "value": "1.42万亿"}], title="指标")
        long = charts.number_cards([{"label": "中国汽车电子市场规模", "value": "净流入42.74亿元"}], title="指标")
        try:
            self.assertEqual(short.get_size_inches().tolist(), long.get_size_inches().tolist())
            self.assertEqual(short.get_size_inches().tolist(), [3.2, 1.55])
            self.assertEqual(to_hex(short.axes[0].patches[0].get_facecolor()), style.DATA_CARD_BG.lower())
        finally:
            plt.close(short)
            plt.close(long)

        html = _chart_for(SimpleNamespace(图表规格={
            "类型": "number_cards", "标题": "指标", "数据点": [{"标签": "市值", "值": "1.42万亿"}],
        }))
        self.assertIn("chart--number-cards", html or "")

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

    def test_lollipop_requires_same_unit_and_keeps_interactive_fallback(self) -> None:
        html = _one_chart({
            "类型": "lollipop", "标题": "成分股收益分化",
            "数据点": [
                {"标签": "甲", "值": "2.4%"}, {"标签": "乙", "值": "0.8%"},
                {"标签": "丙", "值": "-1.1%"},
            ],
        }, "T3")
        self.assertIn('data-rh-echarts=', html)
        self.assertIn('&quot;kind&quot;:&quot;lollipop&quot;', html)

        # 同一横轴中混入估值与收益，不能靠更“花”的图型掩盖不可比性。
        invalid = _chart_for(SimpleNamespace(图表规格={
            "类型": "lollipop", "数据点": [
                {"标签": "PB", "值": "2.4倍"}, {"标签": "收益", "值": "0.8%"},
                {"标签": "资金", "值": "1.1亿元"},
            ],
        }))
        self.assertIsNone(invalid)

    def test_new_semantic_chart_types_have_required_guards(self) -> None:
        # 哑铃图没有明确的两个可比时点时拒绝，而不是把两列任意指标连线。
        dumbbell = {"类型": "dumbbell", "数据点": [
            {"标签": "甲", "值": "10%", "值2": "12%"},
            {"标签": "乙", "值": "8%", "值2": "9%"},
        ]}
        self.assertIsNone(_chart_for(SimpleNamespace(图表规格=dumbbell)))
        dumbbell["可比时点"] = True
        self.assertIsNotNone(_chart_for(SimpleNamespace(图表规格=dumbbell)))

        # 瀑布图只能画明确可加总的贡献项。
        waterfall = {"类型": "waterfall", "数据点": [
            {"标签": "价格", "值": "2.0个百分点"}, {"标签": "销量", "值": "-0.8个百分点"},
        ]}
        self.assertIsNone(_chart_for(SimpleNamespace(图表规格=waterfall)))
        waterfall["可加总"] = True
        self.assertIsNotNone(_chart_for(SimpleNamespace(图表规格=waterfall)))

        # 热力图只吃同规则 0–100 标准化矩阵，原始财务指标不能直接上色阶。
        heatmap = {"类型": "heatmap", "标准化": True,
            "数据点": [{"标签": "甲"}, {"标签": "乙"}, {"标签": "丙"}],
            "系列": [{"名称": "估值分位", "值": [10, 50, 90]}, {"名称": "盈利分位", "值": [80, 60, 20]}]}
        self.assertIsNotNone(_chart_for(SimpleNamespace(图表规格=heatmap)))

    def test_treemap_and_bubble_use_their_declared_encodings(self) -> None:
        treemap = _chart_for(SimpleNamespace(图表规格={"类型": "treemap", "数据点": [
            {"标签": "甲", "值": "100亿元", "颜色值": "10%"},
            {"标签": "乙", "值": "80亿元", "颜色值": "-5%"},
            {"标签": "丙", "值": "60亿元", "颜色值": "3%"},
        ]}))
        self.assertIsNotNone(treemap)
        bubble_html = _one_chart({"类型": "bubble", "标题": "估值、盈利与市值",
            "x轴": "PB(倍)", "y轴": "净利同比(%)", "大小轴": "市值(亿元)",
            "数据点": [{"标签": str(i), "x": i + 1, "y": i * 3, "大小": i + 10} for i in range(5)]}, "T4")
        self.assertIn('&quot;kind&quot;:&quot;bubble&quot;', bubble_html)

    def test_echarts_initializes_only_after_hidden_holders_join_layout(self) -> None:
        script = _echarts_assets()
        self.assertIn("classList.add('rh-echarts-preparing')", script)
        self.assertIn("requestAnimationFrame(() =>", script)
        self.assertIn("classList.add('rh-echarts-item-ready')", script)
        self.assertIn("rendered.forEach(chart => chart.resize())", script)
        self.assertNotIn("classList.add('rh-echarts-ready')", script)
        self.assertNotIn("toolbox:", script)

    def test_static_and_interactive_charts_share_the_approved_palette(self) -> None:
        from render import style

        self.assertEqual(style.PRIMARY_D, "#7D0A0A")
        self.assertEqual(style.PRIMARY, "#BF3131")
        self.assertEqual(style.BLUE, "#316FBF")
        self.assertEqual(style.GREEN, "#31BF73")
        script = _echarts_assets()
        self.assertIn("const red = '#BF3131'", script)
        self.assertIn("green = '#31BF73'", script)

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
                "underlying": "561160.SH",
                "market_prompt": (
                    "共同市场观点：电池产业中期看涨。\n"
                    "本轮已确认报价标的：电池ETF（561160.SH）。\n"
                    "标的产品画像：近20日收益 3.2%；20日实现波动率 18.6%。"
                ),
                "constraints": {"期限": "3个月", "最大损失": "100%"},
                "product_profile": {"name": "电池ETF", "return_20d": 3.2},
                "candidates": [{"product_name": "看涨期权", "reason": "方向匹配"}],
            }))
            text = path.read_text(encoding="utf-8")
            self.assertIn("结构推荐**：已完成", text)
            self.assertIn("正式参考报价**：尚未发起", text)
            self.assertIn("报价阶段逐标的实际输入记录", text)
            self.assertIn("电池ETF（561160.SH）", text)
            self.assertIn("近20日收益 3.2%", text)
            self.assertIn("期限=3个月", text)
            self.assertNotIn("**① 实际发送给 OptionHelper 的内容**", text)

    def test_formal_quote_updates_actual_target_and_product_profile(self) -> None:
        from render.gaps import refresh_optionhelper_result

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.md"
            path.write_text(
                "## 二、给 OptionHelper 的观点包\n\n"
                "**① 实际发送给 OptionHelper 的内容**（自然语言原文，逐字如下）：\n\n"
                "旧研究 ETF：159755.SZ\n\n---\n\n"
                "## 三、OptionHelper 正式参考报价调用结果\n\n（本次未调用）\n\n"
                "---\n\n## 四、缺口\n",
                encoding="utf-8",
            )
            result = SimpleNamespace(
                ok=True, product_name="看涨期权", product_id="call", reason="方向匹配",
                client_constraints={"期限": "3个月"}, main_risks=["期权费损失"],
                status="completed", coverage_status="complete", module_failures={},
                report_path="C:/quote.html", designer_input_path="C:/frozen.json",
                quote_groups=[], assumptions=[],
            )
            record = {
                "underlying": "561160.SH", "underlying_name": "电池ETF",
                "status": "正式报价已完成", "constraints": {"期限": "3个月"},
                "product_profile": {"name": "电池ETF"},
                "market_prompt": "本轮已确认报价标的：电池ETF（561160.SH）。\n近1年最大回撤 -16.8%。",
            }
            self.assertTrue(refresh_optionhelper_result(path, result, input_record=record))
            text = path.read_text(encoding="utf-8")
            self.assertIn("本次正式报价标的**：电池ETF（561160.SH）", text)
            self.assertIn("近1年最大回撤 -16.8%", text)
            self.assertIn("正式参考报价**：已完成", text)
            self.assertIn("旧版共同观点快照", text)
            self.assertNotIn("**① 实际发送给 OptionHelper 的内容**", text)

    def test_selected_multi_underlying_quotes_keep_each_identity_and_frozen_columns(self) -> None:
        html = multi_quote_block([
            {
                "underlying": "515880.SH", "product_name": "看涨期权", "product_id": "call",
                "reason": "看涨观点", "quote_date": "2026-09-02",
                "groups": [{"title": "参考报价", "columns": [
                    {"key": "term", "label": "期限"}, {"key": "price", "label": "期权费"},
                ], "rows": [{"term": "3个月", "price": "5%"}]}],
            },
            {
                "underlying": "300750.SZ", "product_name": "看跌期权", "product_id": "put",
                "reason": "风险对冲", "quote_date": "2026-09-02",
                "groups": [{"title": "参考报价", "columns": [
                    {"key": "term", "label": "期限"}, {"key": "price", "label": "期权费"},
                ], "rows": [{"term": "1个月", "price": "3%"}]}],
            },
        ])
        self.assertIn("已选产品 · 参考报价", html)
        self.assertIn("515880.SH｜看涨期权（call）", html)
        self.assertIn("300750.SZ｜看跌期权（put）", html)
        self.assertIn("<th scope=\"col\">期权费</th>", html)

    def test_multi_quote_gap_records_all_analyst_selected_quotes(self) -> None:
        from render.gaps import refresh_optionhelper_multi_result

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "draft.md"
            path.write_text(
                "## 三、OptionHelper 正式参考报价调用结果\n\n（本次未调用）\n\n---\n\n## 四、缺口\n",
                encoding="utf-8",
            )
            entries = [
                {"underlying": "515880.SH", "product_name": "看涨期权", "groups": [{"rows": [{}, {}]}],
                 "report_path": "C:/quote-a.html"},
                {"underlying": "300750.SZ", "product_name": "看跌期权", "groups": [{"rows": [{}]}],
                 "report_path": "C:/quote-b.html"},
            ]
            self.assertTrue(refresh_optionhelper_multi_result(path, entries, html_path="C:/onepager.html"))
            text = path.read_text(encoding="utf-8")
            self.assertIn("分析师选择 2 份写入一页通", text)
            self.assertIn("515880.SH", text)
            self.assertIn("300750.SZ", text)
