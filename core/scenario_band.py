"""历史相似市场状态下的未来收益带。

它不是预测模型，也不生成结构建议：只用挂钩标的自身已验证的收盘价，按预先固定的
“近 20 日收益分位 × 近 20 日实现波动率分位”筛历史状态，再统计后续收益分位数。
所有样本筛选、回退与最小样本量都是代码规则，不交给 LLM。
"""

from __future__ import annotations

import math
import statistics as st
from dataclasses import dataclass, field as dfield

from . import history
from .provider import DataProvider


LOOKBACK_DAYS = 20
HORIZONS_DAYS = (20, 60)
MIN_JOINT_SAMPLES = 18
MIN_RETURN_SAMPLES = 24


@dataclass
class ReturnBand:
    horizon_days: int
    sample_count: int
    q25: float
    median: float
    q75: float


@dataclass
class ScenarioBand:
    code: str
    lookback_days: int = LOOKBACK_DAYS
    return_state: str = ""
    volatility_state: str = ""
    current_return: float | None = None
    current_volatility: float | None = None
    sample_rule: str = ""
    sample_count: int = 0
    bands: list[ReturnBand] = dfield(default_factory=list)
    source: str = ""
    ok: bool = False
    error: str = ""


def _percentile(values: list[float], q: float) -> float:
    if not values:
        raise ValueError("empty percentile input")
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low, high = int(pos), min(int(pos) + 1, len(ordered) - 1)
    weight = pos - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _rank_percentile(values: list[float], value: float) -> float:
    return sum(1 for item in values if item <= value) / len(values) * 100


def _return_bucket(rank: float) -> str:
    if rank <= 25:
        return "近20日收益处历史低四分位"
    if rank >= 75:
        return "近20日收益处历史高四分位"
    return "近20日收益处历史中间区间"


def _volatility_bucket(rank: float) -> str:
    if rank <= 33.333:
        return "近20日实现波动率处历史低三分位"
    if rank >= 66.667:
        return "近20日实现波动率处历史高三分位"
    return "近20日实现波动率处历史中三分位"


def _bucket(value: float, values: list[float], cuts: tuple[float, float]) -> int:
    low, high = (_percentile(values, cuts[0]), _percentile(values, cuts[1]))
    return 0 if value <= low else (2 if value >= high else 1)


def calculate_from_prices(code: str, prices: list[float], *, lookback_days: int = LOOKBACK_DAYS,
                          horizons_days: tuple[int, ...] = HORIZONS_DAYS) -> ScenarioBand:
    """纯计算入口，便于在不联网的测试中验证状态与回退规则。"""
    band = ScenarioBand(code=code, lookback_days=lookback_days)
    prices = [float(value) for value in prices if value and float(value) > 0]
    max_horizon = max(horizons_days, default=0)
    if len(prices) < lookback_days * 8 + max_horizon:
        band.error = f"价格样本不足（{len(prices)} 点，至少需 {lookback_days * 8 + max_horizon} 点）"
        return band

    states: list[tuple[int, float, float]] = []
    for index in range(lookback_days, len(prices)):
        trailing_return = (prices[index] / prices[index - lookback_days] - 1) * 100
        log_returns = [math.log(prices[pos] / prices[pos - 1])
                       for pos in range(index - lookback_days + 1, index + 1)
                       if prices[pos - 1] > 0]
        if len(log_returns) < lookback_days - 1:
            continue
        annual_volatility = st.pstdev(log_returns) * math.sqrt(244) * 100
        states.append((index, trailing_return, annual_volatility))
    anchors = [state for state in states if state[0] + max_horizon < len(prices)]
    if len(anchors) < MIN_JOINT_SAMPLES:
        band.error = f"可回测历史状态不足（{len(anchors)} 个）"
        return band

    current = states[-1]
    returns = [item[1] for item in anchors]
    volatilities = [item[2] for item in anchors]
    ret_bucket = _bucket(current[1], returns, (0.25, 0.75))
    vol_bucket = _bucket(current[2], volatilities, (1 / 3, 2 / 3))
    joint = [item for item in anchors
             if _bucket(item[1], returns, (0.25, 0.75)) == ret_bucket
             and _bucket(item[2], volatilities, (1 / 3, 2 / 3)) == vol_bucket]
    selected = joint
    rule = "收益四分位与实现波动率三分位同时匹配"
    if len(selected) < MIN_JOINT_SAMPLES:
        selected = [item for item in anchors if _bucket(item[1], returns, (0.25, 0.75)) == ret_bucket]
        rule = "收益四分位匹配（波动率条件因样本不足放宽）"
    if len(selected) < MIN_RETURN_SAMPLES:
        selected = anchors
        rule = "全历史滚动状态（匹配样本不足，已降级）"

    band.current_return = current[1]
    band.current_volatility = current[2]
    band.return_state = _return_bucket(_rank_percentile(returns, current[1]))
    band.volatility_state = _volatility_bucket(_rank_percentile(volatilities, current[2]))
    band.sample_rule = rule
    band.sample_count = len(selected)
    for horizon in horizons_days:
        outcomes = [(prices[index + horizon] / prices[index] - 1) * 100 for index, _ret, _vol in selected]
        if not outcomes:
            continue
        band.bands.append(ReturnBand(horizon, len(outcomes), _percentile(outcomes, .25),
                                     _percentile(outcomes, .50), _percentile(outcomes, .75)))
    band.ok = bool(band.bands)
    if not band.ok:
        band.error = "相似状态没有可用的未来收益样本"
    return band


def calculate(code: str, *, provider: DataProvider | None = None, years: int = 3) -> ScenarioBand:
    prices = history.series(code, "ths_close_price_stock", years=years, provider=provider)
    band = calculate_from_prices(code, prices)
    band.source = f"iFinD·{code}近{years}年收盘价"
    return band


def render_compact(band: ScenarioBand) -> str:
    """内部底稿/观点包用的简洁、非预测性表述。"""
    if not band.ok:
        return ""
    rows = []
    for item in band.bands:
        months = "约1个月" if item.horizon_days == 20 else ("约3个月" if item.horizon_days == 60 else f"{item.horizon_days}个交易日")
        rows.append(f"{months}后收益 P25/中位/P75：{item.q25:+.1f}% / {item.median:+.1f}% / {item.q75:+.1f}%")
    return "；".join(rows)
