import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


@unittest.skipUnless(importlib.util.find_spec("pyecharts"), "pyecharts is optional")
class InternalInteractiveReviewTests(unittest.TestCase):
    def test_review_page_references_local_echarts_asset(self) -> None:
        from render.interactive import render_internal_review

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vendor = root / "assets" / "vendor"
            vendor.mkdir(parents=True)
            (vendor / "echarts.min.js").write_text("// test asset", encoding="utf-8")
            output = root / "output" / "review.html"
            analysis = SimpleNamespace(
                研究主题="光模块", 研究篮子口径="光模块主题研究篮子",
                研究篮子=["中际旭创", "新易盛", "天孚通信", "长飞光纤", "光迅科技"],
                plan=SimpleNamespace(主题="光模块"),
                field_values={},
                挂钩择优=None,
                auto_charts={
                    "成分股明细": {
                        "类型": "scatter", "标题": "光模块样本估值与盈利存在分化",
                        "x轴": "PB(倍)", "y轴": "净利同比(%)",
                        "数据点": [
                            {"标签": f"样本{i}", "x": i + 1, "y": i * 5 - 8}
                            for i in range(5)
                        ],
                    },
                },
            )
            report = SimpleNamespace(推荐方向="看涨")
            path = Path(render_internal_review(analysis, report, output))
            text = path.read_text(encoding="utf-8")
            self.assertIn("../assets/vendor/echarts.min.js", text)
            self.assertNotIn("https://assets.pyecharts.org", text)
            self.assertIn("内部交互复核", text)

    def test_internal_review_uses_the_shared_report_palette(self) -> None:
        from render.interactive import _PALETTE

        self.assertEqual(_PALETTE[:3], ("#BF3131", "#316FBF", "#31BF73"))
