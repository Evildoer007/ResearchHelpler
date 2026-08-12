"""板块聚合（方案C）：用成分股聚合出**板块级**指标，替代"单一代表标的"。

为什么必须做（DESIGN §9.1 实测教训）：
  用一只股票代表整个板块会严重失真——长鑫科技（次新股）的分红 0 元被写成"板块分红改善"，
  中芯国际的 PB 6.14 倍代表不了半导体板块。而模板里"735亿分红""33%分红率""板块PB 1.37x"
  全都是**板块聚合值**。iFinD 正式版额度（基本面 500万条/周）足以支撑，20只×7指标仅 140 条。

聚合口径采用研报惯例的**整体法**（总量法），而非算术平均：
  PB整体   = Σ市值 / Σ净资产        = Σmv / Σ(mv/pb)      （调和加权）
  PE整体   = Σ市值 / Σ净利润        = Σmv / Σ(mv/pe)
  ROE整体  = Σ净利润 / Σ净资产      = Σ(mv/pe) / Σ(mv/pb)
  分红率   = Σ现金分红 / Σ净利润
  股息率   = Σ现金分红 / Σ市值
  净利同比 = Σ本期净利 / Σ上期净利 - 1，其中 上期 = 本期/(1+同比)
算术平均会让小盘股与龙头同权，明显偏离研报口径，故不采用。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field as dfield

from . import config
from . import fetcher as ft
from . import universe
from .provider import DataProvider, get_provider

# 聚合所需的原始指标（一次批量取回）
_IND = {
    "市值": ("ths_market_value_stock", ""),
    "PB": ("ths_pb_latest_stock", ""),
    "PE": ("ths_pe_ttm_stock", ""),
    "ROE": ("ths_roe_ttm_stock", ""),
    "股息率": ("ths_dividend_yield_ttm_ex_sd_stock", None),          # 交易日
    "净利同比": ("ths_np_atsopc_yoy_stock", None),                    # 报告期
    "现金分红": ("ths_unit_total_cash_dividend_stock", None),         # 年度区间
}


@dataclass
class SectorAggregate:
    板块: str
    成分数: int = 0
    合计市值: float = 0.0
    指标: dict[str, float] = dfield(default_factory=dict)
    明细: list[dict] = dfield(default_factory=list)   # 成分股逐只数据，供龙头列举/对比表
    ok: bool = False
    error: str = ""

    def get(self, key: str):
        return self.指标.get(key)


def _f(x):
    try:
        v = float(x)
        return v if v == v else None      # 排除 NaN
    except (TypeError, ValueError):
        return None


def _aggregate(rows: list[dict]) -> dict[str, float]:
    """按整体法聚合。rows 每项含 市值/PB/PE/ROE/股息率/净利同比/现金分红。"""
    out: dict[str, float] = {}
    mv_sum = sum(r["市值"] for r in rows if r.get("市值"))
    if not mv_sum:
        return out
    out["合计市值"] = mv_sum

    # 净资产、净利润（由市值反推，避免额外取数）
    equity = sum(r["市值"] / r["PB"] for r in rows if r.get("市值") and r.get("PB"))
    profit = sum(r["市值"] / r["PE"] for r in rows if r.get("市值") and r.get("PE") and r["PE"] > 0)

    if equity:
        out["PB"] = mv_sum / equity
    if profit:
        out["PE"] = mv_sum / profit
    if equity and profit:
        out["ROE"] = profit / equity * 100

    div = sum(r["现金分红"] for r in rows if r.get("现金分红"))
    if div:
        out["现金分红总额"] = div
        out["股息率"] = div / mv_sum * 100
        if profit:
            out["分红率"] = div / profit * 100

    # 净利同比整体法：Σ本期 / Σ上期 - 1
    cur = prev = 0.0
    for r in rows:
        p, g = r.get("净利润"), r.get("净利同比")
        if p and g is not None and g > -100:
            cur += p
            prev += p / (1 + g / 100)
    if prev:
        out["归母净利同比"] = (cur / prev - 1) * 100
    return out


def sector_aggregate(
    sector: str, *, top: int = 30, provider: DataProvider | None = None,
    use_cache: bool = True,
) -> SectorAggregate:
    """取板块前 top 大成熟成分股（已剔次新股），按整体法聚合出板块级指标。"""
    date = dt.date.today().strftime("%Y%m%d")
    config.DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in sector if ch.isalnum())[:24]
    path = config.DATA_CACHE_DIR / f"agg_{safe}_{top}_{date}.json"

    if use_cache and path.exists():
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            return SectorAggregate(**d)
        except Exception:
            pass

    agg = SectorAggregate(板块=sector)
    provider = provider or get_provider()
    leaders = universe.sector_leaders(sector, top=top, provider=provider)
    if not leaders:
        agg.error = f"未取到板块「{sector}」成分股"
        return agg

    codes = [l.代码 for l in leaders]
    names = {l.代码: l.简称 for l in leaders}
    inds = [v[0] for v in _IND.values()]
    params = []
    for _k, (_ind, p) in _IND.items():
        if p is None:
            p = (ft.latest_trade_date() if _k == "股息率"
                 else ft.latest_report_period() if _k == "净利同比"
                 else ft.last_full_year_range())
        params.append(p)

    r = provider.get_basic(codes, inds, params)
    if not r.ok:
        agg.error = f"批量取数失败：{r.error}"
        return agg

    rows = []
    for code in codes:
        d = r.data.get(code) or {}
        row = {"代码": code, "简称": names.get(code, "")}
        for key, (ind, _p) in _IND.items():
            v = d.get(ind)
            row[key] = _f(v[0] if isinstance(v, list) and v else v)
        if row.get("市值") and row.get("PE") and row["PE"] > 0:
            row["净利润"] = row["市值"] / row["PE"]
        rows.append(row)

    agg.指标 = _aggregate(rows)
    agg.合计市值 = agg.指标.pop("合计市值", 0.0)
    agg.成分数 = len([r_ for r_ in rows if r_.get("市值")])
    agg.明细 = sorted(rows, key=lambda x: x.get("市值") or 0, reverse=True)
    agg.ok = bool(agg.指标)

    if agg.ok and use_cache:
        path.write_text(json.dumps(agg.__dict__, ensure_ascii=False, default=str),
                        encoding="utf-8")
    return agg


@dataclass
class SectorDupont:
    """板块整体法净利率 + ROE，本期与去年同期对比——支撑 E5/E5b。

    只用两个维度（净利率、ROE），不做完整三项杜邦分解（净利率×周转率×权益乘数）：
    权益乘数/总资产周转率所需的"营业收入""总资产"等原始科目在 iFinD 反复实测下
    取不到可靠的批量指标代码（金融股与工业股报表科目结构不同，猜测代码屡次 -209 或返回空）。
    退而求其次：ROE 下行本身已是 E5b（"ROE下行，或仅靠加杠杆维持"）的判据之一，
    不需要杠杆数据佐证也能忠实原意；只是无法识别"改善但改善质量存疑（靠杠杆撑）"这一细粒度情形。
    """
    板块: str
    净利率_本期: float | None = None
    净利率_去年同期: float | None = None
    ROE_本期: float | None = None
    ROE_去年同期: float | None = None
    ok: bool = False
    error: str = ""

    @property
    def 净利率变动(self) -> float | None:
        if self.净利率_本期 is None or self.净利率_去年同期 is None:
            return None
        return self.净利率_本期 - self.净利率_去年同期

    @property
    def ROE变动(self) -> float | None:
        if self.ROE_本期 is None or self.ROE_去年同期 is None:
            return None
        return self.ROE_本期 - self.ROE_去年同期


def iwencai_field_sum(codes: list[str], field: str, date_yyyymmdd: str,
                      provider: DataProvider | None = None) -> float | None:
    """板块某财务科目合计（iwencai，单字段+显式日期）。

    供 E5(营业收入)/E6(营业成本)/F8(机构持股占流通股比例的分子分母…) 等场景共用，
    凡是"官方无可靠 ths_xxx 批量指标代码，但 iwencai 能给出该科目真实数值"的情形都走这里。

    ⚠ 实测教训 1：iwencai 对"不加日期限定"的多股批量查询不可靠——会把该字段的
    全部历史期都返回而不仅是最新一期，若不察觉会重复累加算出离谱大的合计值。
    故调用方两个口径（本期/去年同期）都必须显式传 YYYYMMDD，不依赖默认行为。
    单次查询字段数须 ≤3（实测 5 个日期敏感字段时，日期限定符只会附着在第一个字段上）。

    ⚠ 实测教训 2：**必须显式确保登录**，不能只裸调 `iFinDPy`——
    漏了这步会表现为"看似随机"的间歇性失败：同一进程内若此调用排在其它已登录调用
    之前，会因会话未就绪而返回空，极容易被误判成"接口本身不稳定"（本模块头两版就踩了这个坑）。
    """
    import time

    from .provider import iFinDProvider

    prov = provider if isinstance(provider, iFinDProvider) else iFinDProvider()
    if not prov.available():
        return None
    prov._ensure_login()
    import iFinDPy as ths

    q = " ".join(codes) + f" {date_yyyymmdd} {field}"
    for attempt in range(2):        # 网络抖动兜底，非主要防线（主要防线是上面的登录修正）
        d = ths.THS_iwencai(q, "stock")
        if d.get("errorcode", -1) == 0:
            tables = d.get("tables") or []
            t = tables[0].get("table", {}) if tables else {}
            col = next((k for k in t if k.startswith(field)), None)
            if col is not None:
                vals = []
                for x in t[col]:
                    try:
                        f = float(x)
                        if f == f:
                            vals.append(f)
                    except (TypeError, ValueError):
                        continue
                if vals:
                    return sum(vals)
        if attempt == 0:
            time.sleep(1)
    return None


def _revenue_sum(codes: list[str], date_yyyymmdd: str,
                 provider: DataProvider | None = None) -> float | None:
    return iwencai_field_sum(codes, "营业收入", date_yyyymmdd, provider)


def _profit_sum(codes: list[str], report_period: str, provider: DataProvider) -> float | None:
    """板块归母净利润合计（已验证指标 ths_np_atoopc_stock，与 fundamentals.py 同源）。"""
    r = provider.get_basic(codes, ["ths_np_atoopc_stock"], [report_period])
    if not r.ok:
        return None
    total = 0.0
    got = False
    for c in codes:
        v = (r.data.get(c) or {}).get("ths_np_atoopc_stock")
        v = v[0] if isinstance(v, list) and v else v
        try:
            f = float(v)
            if f == f:
                total += f
                got = True
        except (TypeError, ValueError):
            continue
    return total if got else None


def _roe_snapshot(codes: list[str], trade_date: str, provider: DataProvider) -> float | None:
    """板块 ROE 整体法快照：Σ市值/Σ(市值/PE) ÷ Σ市值/Σ(市值/PB)，与 sector_aggregate 同口径。"""
    r = provider.get_basic(codes, ["ths_market_value_stock", "ths_pb_latest_stock",
                                   "ths_pe_ttm_stock"], [trade_date, trade_date, trade_date])
    if not r.ok:
        return None
    equity = profit = 0.0
    for c in codes:
        d = r.data.get(c) or {}
        mv = _f((d.get("ths_market_value_stock") or [None])[0])
        pb = _f((d.get("ths_pb_latest_stock") or [None])[0])
        pe = _f((d.get("ths_pe_ttm_stock") or [None])[0])
        if mv and pb:
            equity += mv / pb
        if mv and pe and pe > 0:
            profit += mv / pe
    if not equity:
        return None
    return profit / equity * 100


def sector_dupont(sector: str, *, top: int = 15, provider: DataProvider | None = None,
                  use_cache: bool = True) -> SectorDupont:
    """板块整体法净利率 + ROE：本期 vs 去年同期。支撑 E5/E5b。"""
    from . import fetcher as ft
    from . import universe

    date = dt.date.today().strftime("%Y%m%d")
    safe = "".join(ch for ch in sector if ch.isalnum())[:24]
    path = config.DATA_CACHE_DIR / f"dupont_{safe}_{top}_{date}.json"
    if use_cache and path.exists():
        try:
            return SectorDupont(**json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass

    out = SectorDupont(板块=sector)
    provider = provider or get_provider()
    leaders = universe.sector_leaders(sector, top=top, provider=provider)
    codes = [l.代码 for l in leaders]
    if not codes:
        out.error = f"未取到板块「{sector}」成分股"
        return out

    cur_period = ft.latest_report_period()          # 如 "2026-03-31"
    ago_period = f"{int(cur_period[:4]) - 1}{cur_period[4:]}"
    cur_yyyymmdd = cur_period.replace("-", "")
    ago_yyyymmdd = ago_period.replace("-", "")
    cur_trade = ft.latest_trade_date()
    y, m, d_ = cur_trade.split("-")
    ago_trade = f"{int(y) - 1}-{m}-{d_}"

    rev_cur = _revenue_sum(codes, cur_yyyymmdd, provider)
    rev_ago = _revenue_sum(codes, ago_yyyymmdd, provider)
    np_cur = _profit_sum(codes, cur_period, provider)
    np_ago = _profit_sum(codes, ago_period, provider)
    if rev_cur and np_cur is not None:
        out.净利率_本期 = np_cur / rev_cur * 100
    if rev_ago and np_ago is not None:
        out.净利率_去年同期 = np_ago / rev_ago * 100

    out.ROE_本期 = _roe_snapshot(codes, cur_trade, provider)
    out.ROE_去年同期 = _roe_snapshot(codes, ago_trade, provider)

    out.ok = out.净利率_本期 is not None and out.ROE_本期 is not None
    if not out.ok and not out.error:
        out.error = "净利率或ROE取数不全"
    if out.ok and use_cache:
        config.DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out.__dict__, ensure_ascii=False, default=str),
                        encoding="utf-8")
    return out


@dataclass
class ForwardPE:
    """板块当前PE(TTM) vs FY1一致预期PE，同一整体法口径(Σ市值/Σ净利润)。支撑 V7。"""
    板块: str
    当前PE: float | None = None
    FY1预测PE: float | None = None
    ok: bool = False
    error: str = ""

    @property
    def 折价pct(self) -> float | None:
        if self.当前PE is None or self.FY1预测PE is None or self.当前PE == 0:
            return None
        return (self.当前PE - self.FY1预测PE) / self.当前PE * 100


def sector_forward_pe(sector: str, *, top: int = 15, provider: DataProvider | None = None,
                      use_cache: bool = True) -> ForwardPE:
    """板块当前PE 与 FY1一致预期PE 的对比。支撑 V7 估值切换。"""
    import time

    date = dt.date.today().strftime("%Y%m%d")
    safe = "".join(ch for ch in sector if ch.isalnum())[:24]
    path = config.DATA_CACHE_DIR / f"fwdpe_{safe}_{top}_{date}.json"
    if use_cache and path.exists():
        try:
            return ForwardPE(**json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass

    from . import universe
    from .provider import iFinDProvider

    out = ForwardPE(板块=sector)
    provider = provider or get_provider()
    agg = sector_aggregate(sector, top=top, provider=provider, use_cache=use_cache)
    if not agg.ok or not agg.get("PE"):
        out.error = "缺板块当前PE"
        return out
    out.当前PE = agg.get("PE")

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

    fy1 = dt.date.today().year + 1
    q = " ".join(codes) + f" 总市值 {fy1}年预测净利润平均值"
    for attempt in range(2):
        d = ths.THS_iwencai(q, "stock")
        if d.get("errorcode", -1) == 0:
            t = (d.get("tables") or [{}])[0].get("table", {})
            mv_col = next((k for k in t if k.startswith("总市值")), None)
            np_col = next((k for k in t if k.startswith("预测净利润")), None)
            if mv_col and np_col:
                mv = [float(x) for x in t[mv_col] if x not in (None, "")]
                npv = [float(x) for x in t[np_col] if x not in (None, "")]
                if mv and npv and sum(npv) > 0:
                    out.FY1预测PE = sum(mv) / sum(npv)
                    break
        if attempt == 0:
            time.sleep(1)

    out.ok = out.当前PE is not None and out.FY1预测PE is not None
    if not out.ok and not out.error:
        out.error = "缺 FY1一致预期净利润"
    if out.ok and use_cache:
        config.DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out.__dict__, ensure_ascii=False, default=str),
                        encoding="utf-8")
    return out


def sector_shareholder_change(
    sector: str, *, months: int = 12, provider: DataProvider | None = None,
    use_cache: bool = True,
) -> dict:
    """板块大股东增减持规模（近 N 月）。

    支撑 catalyst 逻辑的"卖压"侧——此前该逻辑因无数据长期写作"无法验证"。
    iwencai 一次板块级查询即可取回全部相关成分股（按次计费，故不逐只查）。
    金额 = |变动股数| × 每股价格，其中每股价格 = 总市值 / 总股本。
    """
    date = dt.date.today().strftime("%Y%m%d")
    safe = "".join(ch for ch in sector if ch.isalnum())[:24]
    path = config.DATA_CACHE_DIR / f"holder_{safe}_{months}m_{date}.json"
    if use_cache and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass

    from .provider import iFinDProvider

    prov = provider if isinstance(provider, iFinDProvider) else iFinDProvider()
    if not prov.available():
        return {}
    prov._ensure_login()
    import iFinDPy as ths

    d = ths.THS_iwencai(
        f"{sector}板块 近{months}个月大股东增减持股数 总市值 总股本 所属行业", "stock")
    if d.get("errorcode", -1) != 0:
        return {}
    prov.total_data_vol += int(d.get("dataVol", 0) or 0)
    tables = d.get("tables") or []
    t = tables[0].get("table", {}) if tables else {}
    if not t:
        return {}

    def col(*prefixes):
        for p in prefixes:
            for k in t:
                if str(k).startswith(p):
                    return t[k]
        return None

    names = col("股票简称") or []
    chg = col("大股东变动股数") or []
    mv = col("总市值") or []
    shares = col("总股本") or []
    industry = col("所属同花顺行业", "所属行业") or []
    if not (chg and mv and shares):
        return {}

    # iwencai 对板块限定的理解不稳定（"半导体板块"曾返回全市场 2460 只），
    # 故用返回的行业列二次过滤，只保留确属该板块的标的。
    from .signals import _SECTOR_ALIAS

    keys = {sector, _SECTOR_ALIAS.get(sector, sector)}

    def in_sector(i: int) -> bool:
        if not industry or i >= len(industry):
            return True                      # 无行业列时不过滤（保持可用）
        s = str(industry[i])
        return any(k and k in s for k in keys)

    减持 = 增持 = 0.0
    减持股 = []
    for i in range(min(len(chg), len(mv), len(shares))):
        if not in_sector(i):
            continue
        c, m, s = _f(chg[i]), _f(mv[i]), _f(shares[i])
        if None in (c, m, s) or not s:
            continue
        amount = abs(c) * (m / s)          # 变动金额（元）
        if c < 0:
            减持 += amount
            减持股.append({"简称": names[i] if i < len(names) else "", "金额": amount})
        elif c > 0:
            增持 += amount

    out = {
        "板块": sector, "区间": f"近{months}个月",
        "减持金额": 减持, "增持金额": 增持, "净减持": 减持 - 增持,
        "涉及股票数": len(减持股),
        "减持前列": sorted(减持股, key=lambda x: -x["金额"])[:5],
    }
    if use_cache:
        config.DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


if __name__ == "__main__":  # python -m core.aggregate
    for sec in ["证券", "半导体"]:
        a = sector_aggregate(sec, top=30, use_cache=False)
        print(f"\n[{sec}] 成分{a.成分数}只 合计市值{a.合计市值/1e8:,.0f}亿 ok={a.ok} {a.error}")
        for k, v in a.指标.items():
            unit = "亿元" if "总额" in k else ("%" if k in ("ROE", "股息率", "分红率", "归母净利同比") else "倍")
            val = v / 1e8 if "总额" in k else v
            print(f"   {k:<10} {val:,.2f}{unit}")
        print("   前3大:", [f"{r['简称']}({r['市值']/1e8:.0f}亿)" for r in a.明细[:3]])
