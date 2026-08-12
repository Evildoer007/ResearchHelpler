"""板块盈利的**多期趋势**（不是某一期的水平）。

支撑论点 E2（困境反转：增速由负转正）/ E2b（增速见顶：由高位连续回落）。

**为什么必须是序列而不是单期**：E1/E1b 只问"当期盈利是正是负"，
而困境反转与增速见顶问的是**形状**——拐点在哪、连了几期。单期数据回答不了。

**口径与 aggregate.py 一致：整体法**。
即 板块增速(t) = Σ成分股归母净利(t) / Σ成分股归母净利(t-1年) - 1，
而不是各成分股增速的算术平均——后者会让一只小盘股的 300% 增速淹没整个板块
（方案C 的同一个理由，见 DESIGN §18）。

**成分一致性**：每个 (本期, 去年同期) 配对只累加**两期都有数**的股票。
否则某只票今年新上市、去年无数据，会凭空抬高分子造出假增长。

季报为**累计口径**（Q1=年初至3月末），故本期与去年同期天然可比，无需再折算。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field as dfield

from . import config
from .provider import DataProvider, get_provider

_IND_PROFIT = "ths_np_atoopc_stock"     # 归母净利润（累计），经中信证券实测：
                                        # 2025年报 300.76亿，按 yoy 38.58% 反推 2024 为 217 亿，相符


@dataclass
class ProfitTrend:
    板块: str
    期数: int = 0
    序列: list[dict] = dfield(default_factory=list)   # [{报告期, 增速, 本期净利, 同期净利, 样本数}]，由近及远
    ok: bool = False
    error: str = ""

    @property
    def 增速序列(self) -> list[float]:
        """由近及远的增速（%）。"""
        return [r["增速"] for r in self.序列]

    @property
    def 最新增速(self) -> float | None:
        return self.增速序列[0] if self.序列 else None


def _prev_period(period: str) -> str:
    """上一个报告期（回退一个季度）。period 形如 '2026-03-31'。"""
    y, m, _d = (int(x) for x in period.split("-"))
    m -= 3
    if m <= 0:
        m += 12
        y -= 1
    return f"{y}-{m:02d}-{{}}".format({3: "31", 6: "30", 9: "30", 12: "31"}[m])


def _year_ago(period: str) -> str:
    y, rest = period.split("-", 1)
    return f"{int(y) - 1}-{rest}"


def _cache_path(sector: str, periods: int):
    config.DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in sector if ch.isalnum())[:24]
    return config.DATA_CACHE_DIR / f"ptrend_{safe}_{periods}_{dt.date.today():%Y%m%d}.json"


def sector_profit_trend(sector: str, *, periods: int = 4, top: int = 30,
                        provider: DataProvider | None = None,
                        use_cache: bool = True) -> ProfitTrend:
    """近 periods 个报告期的板块整体法归母净利同比序列（由近及远）。"""
    path = _cache_path(sector, periods)
    if use_cache and path.exists():
        try:
            return ProfitTrend(**json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass

    from . import fetcher as ft
    from . import universe

    t = ProfitTrend(板块=sector)
    provider = provider or get_provider()
    leaders = universe.sector_leaders(sector, top=top, provider=provider)
    codes = [x.代码 for x in leaders]
    if not codes:
        t.error = f"未取到板块「{sector}」成分股"
        return t

    # 本期序列 + 各自的去年同期，去重后一次性列出要取的报告期
    cur_periods: list[str] = []
    p = ft.latest_report_period()
    for _ in range(periods):
        cur_periods.append(p)
        p = _prev_period(p)
    need = list(dict.fromkeys(cur_periods + [_year_ago(x) for x in cur_periods]))

    profits: dict[str, dict[str, float]] = {}     # 报告期 → {代码: 归母净利}
    for per in need:
        r = provider.get_basic(codes, [_IND_PROFIT], [per])
        if not r.ok:
            continue
        got: dict[str, float] = {}
        for c in codes:
            v = (r.data.get(c) or {}).get(_IND_PROFIT)
            v = v[0] if isinstance(v, list) and v else v
            try:
                f = float(v)
                if f == f:                 # 排除 NaN
                    got[c] = f
            except (TypeError, ValueError):
                continue
        profits[per] = got

    for per in cur_periods:
        cur, ago = profits.get(per) or {}, profits.get(_year_ago(per)) or {}
        both = [c for c in codes if c in cur and c in ago]      # 成分一致性：两期都有才计入
        if len(both) < 5:
            continue
        s_cur = sum(cur[c] for c in both)
        s_ago = sum(ago[c] for c in both)
        if s_ago <= 0:                     # 去年同期亏损或为零 → 同比无意义，跳过而非硬算
            continue
        t.序列.append({
            "报告期": per, "增速": (s_cur / s_ago - 1) * 100,
            "本期净利": s_cur, "同期净利": s_ago, "样本数": len(both),
        })

    t.期数 = len(t.序列)
    t.ok = t.期数 >= 2                     # 至少两期才谈得上"趋势"
    if not t.ok and not t.error:
        t.error = f"有效期数不足（{t.期数} 期，需 ≥2）"
    if t.ok and use_cache:
        path.write_text(json.dumps(t.__dict__, ensure_ascii=False, default=str),
                        encoding="utf-8")
    return t


@dataclass
class SectorRatioYoY:
    """板块某比率的本期 vs 去年同期——支撑 E6/E6b（毛利率）、F8/F8b（机构持股）。

    两点对比而非多期序列：多期累计口径（Q1/中报/三季报/年报）的累计窗口长度不同，
    直接排成"序列"看连续几期回升/回落会被季节性污染（Q1 累计 3 个月 vs 年报累计 12 个月，
    数值天然不可比）。E2b 的净利同比序列之所以能连续多期比较，是因为它每个点本身
    已经是"同一累计窗口的同比增速"；这里没有现成的同比序列可用，故简化为
    本期与去年同期（累计窗口相同，天然可比）两点对比，牺牲"连续几期"的判断力度。
    """
    名称: str
    板块: str
    本期: float | None = None
    去年同期: float | None = None
    ok: bool = False
    error: str = ""

    @property
    def 变动(self) -> float | None:
        if self.本期 is None or self.去年同期 is None:
            return None
        return self.本期 - self.去年同期


def sector_gross_margin(sector: str, *, top: int = 15, provider: DataProvider | None = None,
                        use_cache: bool = True) -> SectorRatioYoY:
    """板块整体法毛利率：Σ(营收-成本)/Σ营收，本期 vs 去年同期。支撑 E6/E6b。

    ⚠ 已知限制：毛利率对金融类板块（银行/证券/保险）无意义。原以为该科目对这些
    行业会直接返回空值（如中信证券"毛利率"字段确实为空），实测发现"营业成本"
    这个科目本身**有值但经济含义不成立**——证券板块该科目合计仅 1.5 亿，
    对应营收 1159 亿，算出毛利率 99.9%（金融业成本主要是"业务及管理费"，
    不走"营业成本"这个记账口径，与制造业的 COGS 不是同一概念）。
    故加一道合理性护栏：毛利率超过 95%（已知最高的合法值——茅台约 92%）视为
    科目不适用，返回空而非假装是真数字。
    """
    date = dt.date.today().strftime("%Y%m%d")
    safe = "".join(ch for ch in sector if ch.isalnum())[:24]
    path = config.DATA_CACHE_DIR / f"gm_{safe}_{top}_{date}.json"
    if use_cache and path.exists():
        try:
            return SectorRatioYoY(**json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass

    from . import aggregate as ag
    from . import fetcher as ft
    from . import universe

    out = SectorRatioYoY(名称="毛利率", 板块=sector)
    provider = provider or get_provider()
    leaders = universe.sector_leaders(sector, top=top, provider=provider)
    codes = [x.代码 for x in leaders]
    if not codes:
        out.error = f"未取到板块「{sector}」成分股"
        return out

    cur_period = ft.latest_report_period()
    ago_period = f"{int(cur_period[:4]) - 1}{cur_period[4:]}"
    cur_ymd, ago_ymd = cur_period.replace("-", ""), ago_period.replace("-", "")

    rev_cur = ag.iwencai_field_sum(codes, "营业收入", cur_ymd, provider)
    cost_cur = ag.iwencai_field_sum(codes, "营业成本", cur_ymd, provider)
    rev_ago = ag.iwencai_field_sum(codes, "营业收入", ago_ymd, provider)
    cost_ago = ag.iwencai_field_sum(codes, "营业成本", ago_ymd, provider)

    GM_IMPLAUSIBLE = 95.0   # 高于此视为"营业成本"科目对该行业不适用，非真实毛利率
    if rev_cur and cost_cur is not None:
        gm = (rev_cur - cost_cur) / rev_cur * 100
        out.本期 = gm if gm <= GM_IMPLAUSIBLE else None
    if rev_ago and cost_ago is not None:
        gm = (rev_ago - cost_ago) / rev_ago * 100
        out.去年同期 = gm if gm <= GM_IMPLAUSIBLE else None

    out.ok = out.本期 is not None and out.去年同期 is not None
    if not out.ok and not out.error:
        out.error = "本期或去年同期毛利率取数不全，或超出合理区间（该板块可能不适用毛利率概念，如金融业）"
    if out.ok and use_cache:
        config.DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out.__dict__, ensure_ascii=False, default=str),
                        encoding="utf-8")
    return out


@dataclass
class InstitutionPercentile:
    """板块机构持股比例的当前值及其**历史分位**。支撑 F8/F8b。

    F8「机构低配」/F8b「机构超配」问的是"处于该板块自身历史什么水位"，
    不是"同比升降"——这两者不是一回事：一个常年机构持仓都很高的板块
    （如白酒、银行）即便本期同比只是小幅波动，仍应算"处于历史高位"，
    用同比方向判断会系统性判错方向。也不能用统一的绝对阈值（如>60%算超配）——
    实测银行 67.9%、白酒 71.5%、证券 43.1%，板块间天然水平差异巨大，
    绝对阈值对高机构偏好行业不公平（与 F5b 解禁"改用相对口径"是同一类教训）。
    故走真实历史分位，与 history.percentile 同一设计。

    样本仅 10 个季度（2.5 年，机构持股为季度披露频度，做不到 PB 分位那种
    十年日频样本量），分位的统计意义弱于 PB/波动率类分位，这一点需如实告知。
    """
    板块: str
    当前值: float | None = None
    分位: float | None = None
    样本数: int = 0
    ok: bool = False
    error: str = ""


def sector_institution_percentile(sector: str, *, top: int = 15, quarters: int = 10,
                                  provider: DataProvider | None = None,
                                  use_cache: bool = True) -> InstitutionPercentile:
    """板块机构持股比例（市值加权）在近 quarters 个季度中的分位。"""
    date = dt.date.today().strftime("%Y%m%d")
    safe = "".join(ch for ch in sector if ch.isalnum())[:24]
    path = config.DATA_CACHE_DIR / f"instpct_{safe}_{top}_{quarters}_{date}.json"
    if use_cache and path.exists():
        try:
            return InstitutionPercentile(**json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass

    import time

    from . import fetcher as ft
    from . import universe
    from .provider import iFinDProvider

    out = InstitutionPercentile(板块=sector)
    provider = provider or get_provider()
    leaders = universe.sector_leaders(sector, top=top, provider=provider)
    codes = [x.代码 for x in leaders]
    if not codes:
        out.error = f"未取到板块「{sector}」成分股"
        return out

    prov = provider if isinstance(provider, iFinDProvider) else iFinDProvider()
    if not prov.available():
        out.error = "iFinD 不可用"
        return out
    prov._ensure_login()
    import iFinDPy as ths

    # 由近及远列出 quarters 个季度末（报告期），每期一次批量查询
    period = ft.latest_report_period()
    period_ymds = []
    for _ in range(quarters):
        period_ymds.append(period.replace("-", ""))
        period = _prev_period(period)

    def wavg(date_ymd: str) -> float | None:
        q = " ".join(codes) + f" {date_ymd} 总市值 机构持股占流通股比例"
        for attempt in range(2):
            d = ths.THS_iwencai(q, "stock")
            if d.get("errorcode", -1) == 0:
                tabs = d.get("tables") or []
                t = tabs[0].get("table", {}) if tabs else {}
                mv_col = next((k for k in t if k.startswith("总市值")), None)
                r_col = next((k for k in t if k.startswith("机构持股")), None)
                if mv_col and r_col:
                    pairs = []
                    for m, r in zip(t[mv_col], t[r_col]):
                        try:
                            mf, rf = float(m), float(r)
                            if mf == mf and rf == rf:
                                pairs.append((mf, rf))
                        except (TypeError, ValueError):
                            continue
                    tot = sum(m for m, _ in pairs)
                    if tot:
                        return sum(m * r for m, r in pairs) / tot
            if attempt == 0:
                time.sleep(1)
        return None

    series = [v for ymd in period_ymds if (v := wavg(ymd)) is not None]
    if len(series) < 6:               # 样本太少不给结论（本身已比 PB 分位的门槛宽松很多）
        out.error = f"历史样本不足({len(series)}个季度)"
        return out

    cur = series[0]                    # period_ymds 由近及远，第一个即最新
    out.当前值 = cur
    out.分位 = sum(1 for v in series if v <= cur) / len(series) * 100
    out.样本数 = len(series)
    out.ok = True
    if use_cache:
        config.DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out.__dict__, ensure_ascii=False, default=str),
                        encoding="utf-8")
    return out


if __name__ == "__main__":  # python -m core.fundamentals
    for sec in ["证券", "半导体", "白酒"]:
        r = sector_profit_trend(sec, use_cache=False)
        if not r.ok:
            print(f"[{sec}] {r.error}")
            continue
        print(f"[{sec}] 整体法归母净利同比（由近及远）：")
        for x in r.序列:
            print(f"    {x['报告期']}  {x['增速']:+8.2f}%   "
                  f"({x['本期净利']/1e8:.0f}亿 vs {x['同期净利']/1e8:.0f}亿, {x['样本数']}只)")
        print()
