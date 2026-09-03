"""挂钩标的的产品画像：在结构推荐前提供统一、可复核的事实输入。

画像服务于“这个标的在当前状态下适合再让 OptionHelper 审核哪些结构”，不产生产品、
条款或价格建议。正式报价仍由 OptionHelper 自行取得其定价所需的现价、波动率曲面、
利率、日历等数据。
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Mapping

from . import history, scenario_band
from .provider import DataProvider


def _number(value: float | None, digits: int = 2) -> float | None:
    return round(float(value), digits) if value is not None else None


def _max_drawdown(prices: list[float], window: int = 244) -> float | None:
    values = [float(item) for item in prices[-window:] if float(item) > 0]
    if len(values) < 2:
        return None
    peak = values[0]
    drawdowns: list[float] = []
    for price in values:
        peak = max(peak, price)
        drawdowns.append((price / peak - 1) * 100)
    return min(drawdowns)


def _scenario_dict(band: scenario_band.ScenarioBand) -> dict[str, Any]:
    return {
        "ok": band.ok,
        "return_state": band.return_state,
        "volatility_state": band.volatility_state,
        "sample_rule": band.sample_rule,
        "sample_count": band.sample_count,
        "bands": [
            {"horizon_days": item.horizon_days, "sample_count": item.sample_count,
             "q25": _number(item.q25), "median": _number(item.median), "q75": _number(item.q75)}
            for item in band.bands
        ],
        "error": band.error,
    }


def build_from_series(code: str, *, prices: list[float], amounts: list[float] | None = None,
                      name: str = "", source: str = "iFinD") -> dict[str, Any]:
    """纯计算入口，便于测试，也避免为情景收益带重复拉取收盘价。"""
    clean_prices = [float(item) for item in prices if item and float(item) > 0]
    clean_amounts = [float(item) for item in (amounts or []) if item and float(item) > 0]
    gaps: list[str] = []
    profile: dict[str, Any] = {
        "code": code.upper(), "name": name, "as_of": dt.date.today().isoformat(),
        "source": source, "current_price": None, "return_20d": None, "return_60d": None,
        "realized_volatility_20d": None, "volatility_percentile_3y": None,
        "max_drawdown_1y": None, "avg_daily_amount_20d_yi": None,
        "historical_scenarios": {}, "gaps": gaps, "ok": False,
    }
    if not clean_prices:
        gaps.append("未取得标的收盘价序列")
        return profile
    profile["current_price"] = _number(clean_prices[-1], 4)
    if len(clean_prices) > 20:
        profile["return_20d"] = _number((clean_prices[-1] / clean_prices[-21] - 1) * 100)
    else:
        gaps.append("收盘价不足，无法计算近20日收益")
    if len(clean_prices) > 60:
        profile["return_60d"] = _number((clean_prices[-1] / clean_prices[-61] - 1) * 100)
    else:
        gaps.append("收盘价不足，无法计算近60日收益")
    profile["max_drawdown_1y"] = _number(_max_drawdown(clean_prices))
    if profile["max_drawdown_1y"] is None:
        gaps.append("收盘价不足，无法计算近1年最大回撤")
    if clean_amounts:
        profile["avg_daily_amount_20d_yi"] = _number(sum(clean_amounts[-20:]) / len(clean_amounts[-20:]) / 1e8)
    else:
        gaps.append("未取得成交额序列")
    band = scenario_band.calculate_from_prices(code.upper(), clean_prices)
    profile["historical_scenarios"] = _scenario_dict(band)
    if not band.ok:
        gaps.append("历史相似情景收益带不可用：" + (band.error or "样本不足"))
    profile["ok"] = True
    return profile


def collect(code: str, *, name: str = "", provider: DataProvider | None = None) -> dict[str, Any]:
    """从 Research Helper/iFinD 的统一口径取得产品画像；缺口如实保留而不阻断推荐。"""
    code = str(code or "").strip().upper()
    if not name:
        try:
            from . import instruments

            item = instruments.get(code)
            name = item.官方名 if item else ""
        except Exception:
            pass
    prices = history.series(code, "ths_close_price_stock", years=3, provider=provider)
    amounts = history.series(code, "ths_amt_stock", years=1, provider=provider, drop_nonpositive=False)
    profile = build_from_series(code, prices=prices, amounts=amounts, name=name)
    vol = history.volatility(code, window=20, years=3, provider=provider)
    if vol.ok:
        profile["realized_volatility_20d"] = _number(vol.当前)
        profile["volatility_percentile_3y"] = _number(vol.分位)
    else:
        profile["gaps"].append("20日实现波动率及历史分位不可用：" + (vol.error or "取数失败"))
    return profile


def render_for_prompt(profile: Mapping[str, Any] | None) -> str:
    """仅把事实画像加入 Recommender prompt，不向正式定价阶段夹带自算参数。"""
    if not isinstance(profile, Mapping):
        return ""
    rows = [
        "【Research Helper 标的产品画像（结构推荐前的事实输入）】",
        f"- 标的：{profile.get('name') or '—'}（{profile.get('code') or '—'}）；数据查询日：{profile.get('as_of') or '—'}；来源：{profile.get('source') or '—'}。",
        "- 近20日收益：{}；近60日收益：{}；20日实现波动率：{}；近3年波动率分位：{}。".format(
            _format_pct(profile.get("return_20d")), _format_pct(profile.get("return_60d")),
            _format_pct(profile.get("realized_volatility_20d")), _format_pct(profile.get("volatility_percentile_3y"))),
        "- 近1年最大回撤：{}；近20日日均成交额：{}。".format(
            _format_pct(profile.get("max_drawdown_1y")), _format_yi(profile.get("avg_daily_amount_20d_yi"))),
    ]
    scenario = profile.get("historical_scenarios") if isinstance(profile.get("historical_scenarios"), Mapping) else {}
    bands = scenario.get("bands") if isinstance(scenario.get("bands"), list) else []
    if bands:
        parts = []
        for item in bands:
            if not isinstance(item, Mapping):
                continue
            parts.append("{}日后 P25/中位/P75：{} / {} / {}（样本{}）".format(
                item.get("horizon_days"), _format_pct(item.get("q25")), _format_pct(item.get("median")),
                _format_pct(item.get("q75")), item.get("sample_count") or "—"))
        rows.append("- 历史相似状态收益带（历史条件分布，非预测）：" + "；".join(parts))
    gaps = [str(item) for item in (profile.get("gaps") or []) if item]
    if gaps:
        rows.append("- 取数缺口：" + "；".join(gaps))
    rows.append("- 边界：此画像仅用于标的与结构的适配审核，不是产品、条款或价格建议；正式报价仍须由 OptionHelper 自行取得定价行情、波动率曲面、利率和交易日历。")
    return "\n".join(rows)


def _format_pct(value: Any) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _format_yi(value: Any) -> str:
    try:
        return f"{float(value):.2f}亿元"
    except (TypeError, ValueError):
        return "—"
