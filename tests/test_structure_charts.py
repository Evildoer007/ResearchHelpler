import unittest
from types import SimpleNamespace
from unittest.mock import patch


class StructureChartsTests(unittest.TestCase):
    def test_same_verified_basket_generates_safe_cross_section_charts(self) -> None:
        from core import pipeline, universe

        basket = [universe.Leader(代码=f"00000{i}.SZ", 简称=f"样本{i}") for i in range(8)]
        detail = [
            {"代码": item.代码, "简称": item.简称, "市值": (i + 1) * 1e10,
             "PB": 1.0 + i, "ROE": 5.0 + i, "净利同比": -12.0 + i * 7}
            for i, item in enumerate(basket)
        ]
        aggregate = SimpleNamespace(ok=True, 明细=detail, 指标={})
        with patch("core.aggregate.sector_aggregate", return_value=aggregate):
            charts = pipeline._structure_charts("细分主题", basket[0].代码, basket=basket)

        self.assertEqual(charts["成分股气泡"]["类型"], "bubble")
        self.assertEqual(charts["成分股权重"]["类型"], "treemap")
        self.assertEqual(charts["主题篮子热力"]["类型"], "heatmap")
        self.assertTrue(charts["主题篮子热力"]["标准化"])
        self.assertTrue(all(0 <= value <= 100
                            for series in charts["主题篮子热力"]["系列"]
                            for value in series["值"]))
        self.assertEqual(charts["成分股气泡"]["数据点"][0]["大小"], 100.0)
