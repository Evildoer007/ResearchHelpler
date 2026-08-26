import unittest

from render.layout import _ordered_time_labels


class ChartAxisTests(unittest.TestCase):
    def test_metric_names_are_not_treated_as_a_trend_axis(self) -> None:
        self.assertFalse(_ordered_time_labels(["PB历史分位", "归母净利同比", "板块区间涨跌幅"]))

    def test_ordered_reporting_periods_are_valid_trend_axis(self) -> None:
        self.assertTrue(_ordered_time_labels(["2025Q2", "2025Q3", "2025Q4"]))

