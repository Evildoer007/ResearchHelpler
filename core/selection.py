"""标的择优：对候选挂钩标的取真实数据做多维比较。

对应模板中"中证500 三维度最优"那一步——
  科技暴露 × 估值合理 × 市值适中 → 挂钩中证500 做雪球。

分工：
  instruments.py  提供**候选池**（人工维护、代码已校验）与标签
  本模块          对候选取真实比较数据（PE/PB/换手/区间涨跌/市值）
  planner/writer  基于池子与数据做发散推理与择优结论（不得自造标的代码）

指数与 ETF 的指标后缀不同（`_index` / `_fund`），此处按类型分派。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dfield

from . import fetcher as ft
from . import instruments as inst
from .provider import DataProvider, get_provider

# 指数类指标（已实测）
_IDX_INDS = {
    "PE": ("ths_pe_index", True),
    "PB": ("ths_pb_index", True),
    "换手率": ("ths_turnover_ratio_index", True),
    "区间涨跌幅": ("ths_chg_ratio_index", True),
    "总市值": ("ths_market_value_index", True),
}


@dataclass
class Candidate:
    代码: str
    简称: str
    类型: str
    标签: list[str] = dfield(default_factory=list)
    说明: str = ""
    指标: dict[str, float] = dfield(default_factory=dict)
    展示: dict[str, str] = dfield(default_factory=dict)


def _index_suffix(base: str) -> str:
    """跟踪指数代码补交易所后缀（实测规则）。

    iFinD 返回的跟踪指数代码不带后缀，不同指数公司后缀不同：
      H 开头（中证行业指数）→ .CSI ；980/399 开头（国证/深证）→ .SZ ；000 开头 → .SH
    """
    b = str(base).strip()
    if not b:
        return ""
    if b.upper().startswith("H"):
        return b + ".CSI"
    if b.startswith(("980", "399")):
        return b + ".SZ"
    if b.startswith("000"):
        return b + ".SH"
    return b + ".CSI"


def _auto_tracking(codes: list[str], provider: DataProvider) -> list[tuple[str, str]]:
    """自动解析 ETF 的跟踪指数代码（含后缀）。"""
    if not codes:
        return []
    r = provider.get_basic(codes, ["ths_tracking_index_code_fund"], "")
    if not r.ok:
        return []
    out = []
    for code, inds in r.data.items():
        for _ind, v in inds.items():
            val = v[0] if isinstance(v, list) and v else v
            if val:
                out.append((code, _index_suffix(val)))
    return out


def _pull_index_metrics(codes: list[str], provider: DataProvider) -> dict[str, dict]:
    """批量取指数类比较指标。"""
    if not codes:
        return {}
    inds = [v[0] for v in _IDX_INDS.values()]
    date = ft.latest_trade_date()
    params = [date if needs_date else "" for _n, (_i, needs_date) in _IDX_INDS.items()]
    r = provider.get_basic(codes, inds, params)
    if not r.ok:
        return {}
    out: dict[str, dict] = {}
    for code in codes:
        d = r.data.get(code) or {}
        vals = {}
        for name, (ind, _nd) in _IDX_INDS.items():
            v = d.get(ind)
            v = v[0] if isinstance(v, list) and v else v
            try:
                f = float(v)
                if f == f:
                    vals[name] = f
            except (TypeError, ValueError):
                pass
        if vals:
            out[code] = vals
    return out


def compare(
    codes: list[str] | None = None, *, tags: list[str] | None = None,
    provider: DataProvider | None = None,
) -> list[Candidate]:
    """对候选标的取比较数据。codes 指定则用之，否则按 tags 从候选池筛。"""
    provider = provider or get_provider()
    pool = ([inst.get(c) for c in codes] if codes else inst.by_tags(tags or []))
    pool = [p for p in pool if p]
    if not pool:
        return []

    # ETF 自身取不到 PE/PB，其估值等同于所跟踪的指数。跟踪指数优先用库里人工填的，
    # 未填的由 iFinD 自动解析（ths_tracking_index_code_fund），避免手工维护几十个指数代码。
    tracking = dict(_auto_tracking([p.代码 for p in pool if not p.跟踪指数], provider))
    for p in pool:
        if p.跟踪指数:
            tracking[p.代码] = p.跟踪指数

    query_codes = {p.代码 for p in pool} | {v for v in tracking.values() if v}
    metrics = _pull_index_metrics(sorted(query_codes), provider)

    out: list[Candidate] = []
    for p in pool:
        m = metrics.get(p.代码) or {}
        idx = tracking.get(p.代码)
        if idx:                             # 用跟踪指数补齐估值类指标
            base = metrics.get(idx) or {}
            m = {**base, **m}              # 自身有值优先（如涨跌幅取 ETF 自己的）
        c = Candidate(代码=p.代码, 简称=p.简称, 类型=p.类型, 标签=list(p.标签),
                      说明=p.说明, 指标=m)
        c.展示 = {
            "PE": f"{m['PE']:.2f}倍" if "PE" in m else "",
            "PB": f"{m['PB']:.2f}倍" if "PB" in m else "",
            "换手率": f"{m['换手率']:.2f}%" if "换手率" in m else "",
            "区间涨跌幅": f"{m['区间涨跌幅']:.2f}%" if "区间涨跌幅" in m else "",
            "总市值": ft._fmt_money(m["总市值"]) if "总市值" in m else "",
        }
        out.append(c)
    return out


def render(cands: list[Candidate]) -> str:
    lines = [f"{'标的':<12}{'代码':<12}{'PE':<10}{'PB':<9}{'换手':<8}{'涨跌':<9}标签"]
    for c in cands:
        d = c.展示
        lines.append(f"{c.简称:<12}{c.代码:<12}{d['PE']:<10}{d['PB']:<9}"
                     f"{d['换手率']:<8}{d['区间涨跌幅']:<9}{'/'.join(c.标签)}")
    return "\n".join(lines)


if __name__ == "__main__":  # python -m core.selection
    print("【场景】某事件冲击科技成长股，需选一个挂钩标的表达\n")
    cands = compare(tags=[inst.T_TECH, inst.T_GROWTH])
    print(render(cands))
