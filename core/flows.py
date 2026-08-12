"""筹码流向类衍生指标：ETF 份额、两融余额、业绩预告——支撑 F7/F9/C3。

三者共同点：都不走板块整体法聚合（不是"板块口径的指标"），而是取某个具体标的
（板块代表 ETF 或代表个股）的市场微观结构信号，与 S1~S4 波动率/换手率用代表标的
的口径一致，不是 aggregate.py/fundamentals.py 那类"必须整体法聚合，否则失真"的字段。

数据源全部经 iwencai 验证（见 DESIGN #38 探测记录），无可靠的批量 ths_xxx 指标代码。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field as dfield

from . import config
from .provider import DataProvider, iFinDProvider, get_provider


def _wc_series(query: str, field_prefix: str, ty: str,
              provider: DataProvider | None = None) -> list[tuple[str, float]]:
    """iwencai 日频序列通用取数：[(日期, 值)]，按日期升序。"""
    import time

    prov = provider if isinstance(provider, iFinDProvider) else iFinDProvider()
    if not prov.available():
        return []
    prov._ensure_login()
    import iFinDPy as ths

    for attempt in range(2):
        d = ths.THS_iwencai(query, ty)
        if d.get("errorcode", -1) == 0:
            tables = d.get("tables") or []
            t = tables[0].get("table", {}) if tables else {}
            col = next((k for k in t if field_prefix in k), None)
            if col is not None:
                rows: list[tuple[str, float]] = []
                for k, vs in t.items():
                    if field_prefix not in k:
                        continue
                    # 列名形如 "融资余额[20260804]" 或 "基金@基金份额[20260804]"
                    date_part = k.rsplit("[", 1)[-1].rstrip("]")
                    v = vs[0] if isinstance(vs, list) and vs else vs
                    try:
                        f = float(v)
                        if f == f:
                            rows.append((date_part, f))
                    except (TypeError, ValueError):
                        continue
                if rows:
                    rows.sort()
                    return rows
        if attempt == 0:
            time.sleep(1)
    return []


@dataclass
class FlowTrend:
    """某标的的日频流向指标：当前值、近 N 天变动。支撑 F7/F9。"""
    指标: str
    代码: str = ""
    当前值: float | None = None
    当前日期: str = ""
    前值: float | None = None
    前值日期: str = ""
    窗口天: int = 0
    样本数: int = 0
    ok: bool = False
    error: str = ""

    @property
    def 变动(self) -> float | None:
        if self.当前值 is None or self.前值 is None:
            return None
        return self.当前值 - self.前值

    @property
    def 变动pct(self) -> float | None:
        if self.当前值 is None or self.前值 in (None, 0):
            return None
        return (self.当前值 / self.前值 - 1) * 100


def _trend_from_series(rows: list[tuple[str, float]], lookback_days: int,
                       name: str, code: str) -> FlowTrend:
    t = FlowTrend(指标=name, 代码=code, 窗口天=lookback_days)
    if len(rows) < 10:
        t.error = f"{name} 样本不足({len(rows)}点)"
        return t
    cur_d, cur = rows[-1]

    def to_date(s: str) -> dt.date:
        return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))

    target = to_date(cur_d) - dt.timedelta(days=lookback_days)
    past = [r for r in rows if to_date(r[0]) <= target]
    if not past:
        t.error = f"{name} 无 {lookback_days} 天前的观测"
        return t
    prev_d, prev = past[-1]
    t.当前值, t.当前日期 = cur, cur_d
    t.前值, t.前值日期 = prev, prev_d
    t.样本数 = len(rows)
    t.ok = True
    return t


def etf_share_trend(sector: str, *, lookback_days: int = 90,
                    provider: DataProvider | None = None) -> FlowTrend:
    """板块代表 ETF 的份额趋势（近 lookback_days 天变动）。支撑 F7/F7b。

    代表 ETF 取自 instruments.py 已校验的候选池（`find(sector)` 第一个匹配），
    该池已实测代码全部有效，不在本函数里再猜/编 ETF 代码。
    """
    from . import instruments as im

    cands = im.find(sector)
    if not cands:
        t = FlowTrend(指标="ETF份额")
        t.error = f"instruments 候选池未找到「{sector}」对应的 ETF"
        return t
    etf = cands[0]

    rows = _wc_series(f"{etf.代码} 基金份额 近{lookback_days + 30}天",
                      "基金份额", "fund", provider)
    return _trend_from_series(rows, lookback_days, "ETF份额", etf.代码)


def margin_balance_trend(code: str, *, lookback_days: int = 90,
                         provider: DataProvider | None = None) -> FlowTrend:
    """代表标的的两融余额趋势（近 lookback_days 天变动）。支撑 F9。

    走代表标的而非板块整体法聚合——两融余额本身是个股市场微观结构指标
    （与 S1~S4 波动率/换手率同类，不是"必须聚合才不失真"的估值/盈利类字段）。
    """
    rows = _wc_series(f"{code} 融资余额 近{lookback_days + 30}天", "融资余额", "stock", provider)
    return _trend_from_series(rows, lookback_days, "两融余额", code)


@dataclass
class Preannouncement:
    """业绩预告：类型 + 净利润变动幅度。支撑 C3/C3b。"""
    代码: str
    类型: str = ""
    净利润变动幅度: float | None = None
    报告期: str = ""
    ok: bool = False
    error: str = ""


# 业绩预告类型 → 方向。iFinD 常见取值集（实测中信证券得到"大幅上升"）。
_BULLISH_TYPES = {"预增", "略增", "扭亏", "续盈", "大幅上升", "上升"}
_BEARISH_TYPES = {"预减", "略减", "首亏", "续亏", "大幅下降", "下降", "略亏"}


def earnings_preannouncement(code: str, provider: DataProvider | None = None) -> Preannouncement:
    """代表标的最新一期业绩预告。无预告（多数时候）属正常状态，不算取数失败。"""
    import time

    out = Preannouncement(代码=code)
    prov = provider if isinstance(provider, iFinDProvider) else iFinDProvider()
    if not prov.available():
        out.error = "iFinD 不可用"
        return out
    prov._ensure_login()
    import iFinDPy as ths

    for attempt in range(2):
        d = ths.THS_iwencai(f"{code} 业绩预告类型 预告净利润变动幅度", "stock")
        if d.get("errorcode", -1) == 0:
            tables = d.get("tables") or []
            t = tables[0].get("table", {}) if tables else {}
            type_col = next((k for k in t if k.startswith("业绩预告类型")), None)
            chg_col = next((k for k in t if k.startswith("预告净利润变动幅度")), None)
            if type_col:
                v = t[type_col]
                v = v[0] if isinstance(v, list) and v else v
                if v not in (None, "", "--"):
                    out.类型 = str(v)
                    out.报告期 = type_col.rsplit("[", 1)[-1].rstrip("]")
                    if chg_col:
                        cv = t[chg_col]
                        cv = cv[0] if isinstance(cv, list) and cv else cv
                        try:
                            out.净利润变动幅度 = float(cv)
                        except (TypeError, ValueError):
                            pass
                    out.ok = True
                else:
                    out.ok = True   # 无预告是正常状态，不是取数失败
                return out
        if attempt == 0:
            time.sleep(1)
    out.error = "取数失败"
    return out


if __name__ == "__main__":  # python -m core.flows
    for sec in ["证券", "半导体"]:
        t = etf_share_trend(sec)
        print(f"[{sec}] ETF份额: ok={t.ok} {t.前值}->{t.当前值} 变动%={t.变动pct} {t.error}")

    for code in ["600030.SH"]:
        t = margin_balance_trend(code)
        print(f"[{code}] 两融余额: ok={t.ok} {t.前值}->{t.当前值} 变动%={t.变动pct} {t.error}")
        p = earnings_preannouncement(code)
        print(f"[{code}] 业绩预告: ok={p.ok} 类型={p.类型} 变动幅度={p.净利润变动幅度} {p.error}")
