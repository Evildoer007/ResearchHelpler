"""历史序列与派生计算：估值分位、区间收益等。

用 iFinD 日期序列函数 `THS_DateSerial` 取长历史（正式用户可取"证券上市至今"），
用于计算 DERIVED 字段——首个落地的是 **PB/PE 历史分位**：
模板里"PB 仅 1.37x（2016 年以来约 27% 分位）"正是这类表述，而此前该字段一直缺失，
导致 writer 只能写"无法确认是否处于低分位"。

注意：
- 指数/板块的日期序列**单次跨度不得超过 1 年**（个股不限），故此处按个股取。
- 单只 10 年日频约 2500 条，属基本面额度（500万/周），成本可忽略；仍按天缓存以提速。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field as dfield

from . import config
from .provider import DataProvider, iFinDProvider

_GLOBAL_PARAM = "Days:Tradedays,Fill:Previous"   # 交易日 + 前值填充
LOOKBACK = 60                                   # "近期"= 约 3 个月交易日，用于回落判定


@dataclass
class Percentile:
    代码: str
    指标: str
    当前值: float | None = None
    分位: float | None = None        # 0~100，越小越便宜
    样本数: int = 0
    起始: str = ""
    近期高分位: float | None = None  # 近 LOOKBACK 个观测里出现过的最高分位（判断"是否从高位回落"）
    ok: bool = False
    error: str = ""


def _cache_path(code: str, indicator: str, years: int, drop_nonpositive: bool = True):
    config.DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tag = "" if drop_nonpositive else "_raw"      # 两种过滤口径分开缓存，避免互相污染
    safe = f"{code}_{indicator}_{years}y{tag}_{dt.date.today():%Y%m%d}".replace(".", "")
    return config.DATA_CACHE_DIR / f"hist_{safe}.json"


def series(code: str, indicator: str, *, years: int = 10,
           provider: DataProvider | None = None, use_cache: bool = True,
           drop_nonpositive: bool = True) -> list[float]:
    """取某标的某指标的历史日频序列（默认近 10 年）。

    drop_nonpositive：估值类序列（PB/PE）应剔除非正值（无意义）；
    但**涨跌幅序列必然含负数**，取用时须传 False，否则会把下跌日全部丢弃。
    """
    path = _cache_path(code, indicator, years, drop_nonpositive)
    if use_cache and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass

    prov = provider if isinstance(provider, iFinDProvider) else iFinDProvider()
    if not prov.available():
        return []
    prov._ensure_login()
    import iFinDPy as ths

    end = dt.date.today()
    begin = end.replace(year=end.year - years)
    d = ths.THS_DateSerial(code, indicator, "", _GLOBAL_PARAM,
                           begin.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    if d.get("errorcode", -1) != 0:
        return []
    prov.total_data_vol += int(d.get("dataVol", 0) or 0)

    vals: list[float] = []
    for t in d.get("tables", []) or []:
        for _k, v in (t.get("table", {}) or {}).items():
            if isinstance(v, list):
                for x in v:
                    try:
                        f = float(x)
                        if f != f:                       # NaN
                            continue
                        if drop_nonpositive and f <= 0:  # 估值为负无意义；涨跌幅须保留负值
                            continue
                        vals.append(f)
                    except (TypeError, ValueError):
                        continue
    if vals and use_cache:
        path.write_text(json.dumps(vals), encoding="utf-8")
    return vals


def series_multi(codes: list[str], indicator: str, *, years: int = 3,
                 provider: DataProvider | None = None,
                 chunk: int = 20) -> tuple[dict[str, dict[str, float]], list[str]]:
    """批量取多只标的的**带日期**序列，返回 ({代码: {日期: 值}}, 没取到的代码)。

    与 `series()` 的两点不同，都是为了合成板块序列：
      - **保留日期**。`series()` 只返回裸浮点列表，多只股票没法对齐——
        各股停牌日不同，直接按下标配对会把不同日子的数算到一起。
      - **一次多只**。实测 `THS_DateSerial` 支持逗号分隔的多代码，
        返回里每只一个 table 条目（带 thscode 与 time）。dataVol 按点数线性计费，
        批量不省额度，但把 30 只的调用数从 30 次压到 2 次，省的是时间。

    ⚠ **必须把"哪些没取到"返回给调用方，不能静默少几只。** 实测踩过：
    一次 20 只的分块偶发失败（重试即好，非容量上限），当时的实现只是 `continue`，
    于是 30 只里少了市值最大的 20 只，板块 PB 用剩下 10 只算出 3.46（真值 3.37），
    却照样标着"30只成分股整体法"——偏 2.6%，肉眼绝对看不出来，而且会被写进当天缓存。
    分块失败先自动退化成小块重试，仍失败的如实报回，由调用方决定是否放弃。
    """
    out: dict[str, dict[str, float]] = {}
    codes = [c for c in codes if c]
    if not codes:
        return out, []

    prov = provider if isinstance(provider, iFinDProvider) else iFinDProvider()
    if not prov.available():
        return out, list(codes)
    prov._ensure_login()
    import iFinDPy as ths

    end = dt.date.today()
    begin = end.replace(year=end.year - years)

    def _pull(part: list[str]) -> bool:
        try:
            d = ths.THS_DateSerial(",".join(part), indicator, "", _GLOBAL_PARAM,
                                   begin.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        except Exception:
            return False
        if d.get("errorcode", -1) != 0:
            return False
        prov.total_data_vol += int(d.get("dataVol", 0) or 0)
        for t in d.get("tables", []) or []:
            code = str(t.get("thscode") or "").strip()
            times = t.get("time") or []
            tab = t.get("table") or {}
            vals = next((v for v in tab.values() if isinstance(v, list)), None)
            if not code or not times or not vals:
                continue
            m = out.setdefault(code, {})
            for day, x in zip(times, vals):
                try:
                    f = float(x)
                except (TypeError, ValueError):
                    continue
                if f != f:                      # NaN
                    continue
                m[str(day)] = f
        return True

    for i in range(0, len(codes), chunk):
        part = codes[i:i + chunk]
        if _pull(part):
            continue
        for j in range(0, len(part), 5):        # 整块失败 → 拆小重试
            small = part[j:j + 5]
            if _pull(small):
                continue
            for c in small:                     # 小块仍失败 → 逐只，定位到具体标的
                _pull([c])

    missing = [c for c in codes if not out.get(c)]
    return out, missing


@dataclass
class SectorFrames:
    """板块级日频序列（整体法合成，非任何单一标的）。"""

    板块: str
    成分数: int = 0
    日期: list[str] = dfield(default_factory=list)
    PB: list[float] = dfield(default_factory=list)          # Σ市值 / Σ(市值/PB)
    价格指数: list[float] = dfield(default_factory=list)     # 市值加权收益累乘，基点 1000
    成交额: list[float] = dfield(default_factory=list)       # Σ成交额
    换手率: list[float] = dfield(default_factory=list)       # Σ成交额 / Σ市值 × 100
    ok: bool = False
    error: str = ""


def _sector_cache(sector: str, years: int, top: int):
    config.DATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in sector if ch.isalnum())[:24]
    return config.DATA_CACHE_DIR / f"secser_{safe}_{years}y_top{top}_{dt.date.today():%Y%m%d}.json"


def sector_frames(sector: str, *, years: int = 3, top: int = 30,
                  provider: DataProvider | None = None,
                  use_cache: bool = True) -> SectorFrames:
    """用成分股合成**板块自己的**日频序列。

    为什么要它：`PB历史分位`/`年化波动率`/`换手率分位`/`成交额分位` 这些字段此前
    全部取自**单一代表标的**，却在报告里被当作板块指标写（"板块波动率处 87% 分位"
    实际是那一只标的的）。13 条论点（V1/V1b/V8 + S1~S8b）骑在上面。

    为什么不用行业指数：实测申万指数（801120.SI 等）本账号取不到（-4210）；
    中证行业指数（000932.SH）行情类可取但 **PB 序列为空**，且指数序列单次跨度
    ≤1 年需分段。更要命的是它需要再维护一张"板块→指数代码"表——
    表里没有的板块就退化，与 #67 那个"手工表覆盖不全导致以偏概全"是同一个病。
    用成分股合成则任意板块通用，宽口径合并板块（消费=6个一级行业）同样适用。

    口径与快照保持一致：成分股取 `sector_leaders(top=30)`，与 `sector_aggregate`
    默认同一批，否则"当前 PB"与"历史 PB 序列"算的不是同一个篮子，分位没有意义。

    ⚠ 用**今天的成分股**回溯整段历史，存在幸存者偏差（当年的成分与现在不同）。
    快速板块代理普遍这么做，但据此说"处历史 X% 分位"时该知道这个前提。
    """
    from . import universe

    f = SectorFrames(板块=sector)
    path = _sector_cache(sector, years, top)
    # 分析篮子被 ETF 真实成分覆盖时（#85）绕过缓存：与 aggregate 同理，
    # ETF 真实篮子的历史序列不能与 iwencai 行业篮子共用按板块名的缓存键。
    override = universe.has_basket_override(sector)
    if use_cache and not override and path.exists():
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            return SectorFrames(**d)
        except Exception:
            pass

    leaders = universe.sector_leaders(sector, top=top, provider=provider)
    codes = [l.代码 for l in leaders if l.代码]
    if not codes:
        f.error = f"未取到板块「{sector}」成分股"
        return f

    fetched, missing = {}, {}
    for key, ind in (("mv", "ths_market_value_stock"), ("pb", "ths_pb_latest_stock"),
                     ("chg", "ths_chg_ratio_stock"), ("amt", "ths_amt_stock")):
        fetched[key], missing[key] = series_multi(codes, ind, years=years, provider=provider)
    # 少一只都不算数：整体法的分位是拿"同一个篮子"的今天比它自己的历史，
    # 篮子少了几只（尤其少的是权重股）算出来的分位没有意义，而数字看着完全正常。
    bad = {k: v for k, v in missing.items() if v}
    if bad:
        f.error = "成分股序列取数不全：" + "；".join(
            f"{k} 缺 {len(v)} 只（{'、'.join(v[:3])}{'…' if len(v) > 3 else ''}）"
            for k, v in bad.items())
        return f
    mv, pb, chg, amt = fetched["mv"], fetched["pb"], fetched["chg"], fetched["amt"]

    days = sorted({d for m in mv.values() for d in m})
    if len(days) < 60:
        f.error = f"板块序列样本不足({len(days)}点)"
        return f

    idx = 1000.0
    prev_day: str | None = None
    for day in days:
        mv_d = {c: m[day] for c, m in mv.items() if m.get(day)}
        if len(mv_d) < max(3, len(codes) * 0.5):     # 当日有效成分不足一半，跳过
            continue
        tot_mv = sum(mv_d.values())

        # 整体法 PB = Σ市值 / Σ净资产，而净资产 = 市值/PB，故只需市值与 PB 两个序列。
        # 同样要求当日 PB 覆盖过半：覆盖不足时宁可沿用前值，也不拿一个小篮子冒充板块。
        pb_ok = {c: v for c, v in mv_d.items() if c in pb and pb[c].get(day, 0) > 0}
        eq = sum(v / pb[c][day] for c, v in pb_ok.items())
        pb_d = (sum(pb_ok.values()) / eq
                if eq > 0 and len(pb_ok) >= len(mv_d) * 0.8 else None)

        # 市值加权日收益，权重用**前一日**市值（当日市值已含当日涨跌，用它会循环引用）
        r = None
        if prev_day is not None:
            w = {c: mv[c][prev_day] for c in mv_d
                 if c in chg and day in chg[c] and mv.get(c, {}).get(prev_day)}
            tw = sum(w.values())
            if tw > 0:
                r = sum(w[c] * chg[c][day] for c in w) / tw
        if r is not None:
            idx *= 1 + r / 100

        amt_d = sum(amt[c][day] for c in mv_d if c in amt and amt[c].get(day))

        f.日期.append(day)
        f.PB.append(pb_d if pb_d is not None else (f.PB[-1] if f.PB else 0.0))
        f.价格指数.append(idx)
        f.成交额.append(amt_d)
        f.换手率.append(amt_d / tot_mv * 100 if tot_mv else 0.0)
        prev_day = day

    f.成分数 = len(codes)
    f.ok = len(f.日期) >= 60
    if not f.ok:
        f.error = f"有效交易日不足({len(f.日期)}点)"
        return f
    if use_cache and not override:
        path.write_text(json.dumps(f.__dict__, ensure_ascii=False), encoding="utf-8")
    return f


def percentile(code: str, indicator: str = "ths_pb_latest_stock", *, years: int = 10,
               provider: DataProvider | None = None) -> Percentile:
    """当前值在近 N 年历史中的分位（%）。分位越低=越便宜。"""
    p = Percentile(代码=code, 指标=indicator)
    vals = series(code, indicator, years=years, provider=provider)
    if len(vals) < 60:                     # 样本太少不给结论
        p.error = f"历史样本不足({len(vals)}点)"
        return p
    cur = vals[-1]
    below = sum(1 for v in vals if v <= cur)
    p.当前值 = cur
    p.分位 = below / len(vals) * 100
    p.样本数 = len(vals)
    p.起始 = f"近{years}年"
    p.ok = True
    return p


def smoothed_percentile(code: str, indicator: str, *, window: int = 5, years: int = 3,
                        provider: DataProvider | None = None) -> Percentile:
    """近 window 日均值在历史同口径均值序列中的分位。

    换手率、成交额这类**日间噪声极大**的指标不能直接取当日值算分位——
    单日一笔大宗就能把分位打到 95%。先做 window 日滚动均值再比分位，
    衡量的才是"近期这段时间的热度"，与 S5/S6 拥挤度、S8/S8b 情绪冷热的语义一致。

    与 percentile() 的区别：那个用于 PB 这类本身平滑的存量指标，取当日值即可。
    """
    p = Percentile(代码=code, 指标=f"{indicator}近{window}日均值")
    vals = series(code, indicator, years=years, provider=provider)
    if len(vals) < window * 12:            # 至少要够算出 ~11 个独立窗口，否则分位无意义
        p.error = f"样本不足({len(vals)}点)"
        return p

    ma = [sum(vals[i - window:i]) / window for i in range(window, len(vals) + 1)]
    cur = ma[-1]
    p.当前值 = cur
    p.分位 = sum(1 for x in ma if x <= cur) / len(ma) * 100
    p.样本数 = len(ma)
    p.起始 = f"近{years}年"

    # 近 LOOKBACK 个观测中出现过的最高分位。S6「拥挤度已释放」问的是
    # "有没有从高位退下来"，只看当前分位答不了——低分位可能是一直低，不是退下来的。
    tail = ma[-LOOKBACK:]
    p.近期高分位 = max(sum(1 for y in ma if y <= x) / len(ma) * 100 for x in tail)
    p.ok = True
    return p


@dataclass
class Volatility:
    代码: str
    窗口: int = 20
    当前: float | None = None      # 年化波动率（%）
    分位: float | None = None      # 在历史波动率序列中的分位（%）
    均值: float | None = None
    样本数: int = 0
    # 滚动波动率全序列。直方图要画分布，只有一个当前值和一个分位画不了；
    # 而波动率是厚尾变量，分布形状本身就是信息（#71）。
    序列: list = dfield(default_factory=list)
    ok: bool = False
    error: str = ""


def volatility(code: str, *, window: int = 20, years: int = 3,
               provider: DataProvider | None = None) -> Volatility:
    """年化历史波动率及其历史分位。

    衍生品定价的核心输入，也是论点库第五类（情绪/交易）S1~S4 的判定依据。
    自算而非取现成指标的原因：S1/S2 需要的是**波动率的历史分位**，
    必须有滚动波动率序列才能算，单个当前值不够。

    口径：对数收益率的滚动标准差 × √244（A股年交易日）。
    """
    import math
    import statistics as st

    v = Volatility(代码=code, 窗口=window)
    px = series(code, "ths_close_price_stock", years=years, provider=provider)
    if len(px) < window * 3:
        v.error = f"价格样本不足({len(px)}点)"
        return v

    rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px)) if px[i - 1] > 0]
    ann = math.sqrt(244) * 100
    vols = [st.pstdev(rets[i - window:i]) * ann for i in range(window, len(rets) + 1)]
    if not vols:
        v.error = "波动率序列为空"
        return v

    cur = vols[-1]
    v.当前 = cur
    v.分位 = sum(1 for x in vols if x <= cur) / len(vols) * 100
    v.均值 = sum(vols) / len(vols)
    v.样本数 = len(vols)
    v.ok = True
    return v


def return_percentile(code: str, *, window: int = 20, years: int = 3,
                      provider: DataProvider | None = None) -> Percentile:
    """近 window 日涨跌幅在历史同长度区间涨跌幅中的分位。

    支撑 S7 超跌反弹 / S7b 超涨回调——判断"这次跌(涨)得算不算极端"，
    必须和该标的自身的历史波动幅度比，而非用固定的百分比阈值。
    """
    p = Percentile(代码=code, 指标=f"{window}日涨跌幅")
    px = series(code, "ths_close_price_stock", years=years, provider=provider)
    if len(px) < window * 3:
        p.error = f"价格样本不足({len(px)}点)"
        return p

    rets = [(px[i] / px[i - window] - 1) * 100
            for i in range(window, len(px)) if px[i - window] > 0]
    if not rets:
        p.error = "区间收益序列为空"
        return p
    cur = rets[-1]
    p.当前值 = cur
    p.分位 = sum(1 for x in rets if x <= cur) / len(rets) * 100
    p.样本数 = len(rets)
    p.起始 = f"近{years}年"
    p.ok = True
    return p


# ── 板块级版本：与上面四个同名函数算法完全一致，只把输入序列换成板块合成序列 ──
# 刻意复用同一套算法而不另写一套，是为了让"板块口径"与"个股口径"的分位可比，
# 也避免两套算法日后各改各的而悄悄产生差异。

def _pctl(vals: list[float]) -> tuple[float, float]:
    cur = vals[-1]
    return cur, sum(1 for x in vals if x <= cur) / len(vals) * 100


def sector_percentile(sector: str, kind: str = "PB", *, years: int = 3, top: int = 30,
                      provider: DataProvider | None = None) -> Percentile:
    """板块整体法指标的历史分位（对应个股版 percentile）。"""
    p = Percentile(代码=f"{sector}板块", 指标=kind)
    f = sector_frames(sector, years=years, top=top, provider=provider)
    if not f.ok:
        p.error = f.error
        return p
    vals = [x for x in getattr(f, kind, []) if x]
    if len(vals) < 60:
        p.error = f"板块{kind}样本不足({len(vals)}点)"
        return p
    p.当前值, p.分位 = _pctl(vals)
    p.样本数 = len(vals)
    p.起始 = f"近{years}年·{f.成分数}只成分股"
    p.ok = True
    return p


def sector_smoothed_percentile(sector: str, kind: str, *, window: int = 5, years: int = 3,
                               top: int = 30,
                               provider: DataProvider | None = None) -> Percentile:
    """板块换手率/成交额的平滑分位（对应个股版 smoothed_percentile）。"""
    p = Percentile(代码=f"{sector}板块", 指标=f"{kind}近{window}日均值")
    f = sector_frames(sector, years=years, top=top, provider=provider)
    if not f.ok:
        p.error = f.error
        return p
    vals = getattr(f, kind, [])
    if len(vals) < window * 12:
        p.error = f"板块{kind}样本不足({len(vals)}点)"
        return p
    ma = [sum(vals[i - window:i]) / window for i in range(window, len(vals) + 1)]
    p.当前值, p.分位 = _pctl(ma)
    p.样本数 = len(ma)
    p.起始 = f"近{years}年·{f.成分数}只成分股"
    tail = ma[-LOOKBACK:]
    p.近期高分位 = max(sum(1 for y in ma if y <= x) / len(ma) * 100 for x in tail)
    p.ok = True
    return p


def sector_volatility(sector: str, *, window: int = 20, years: int = 3, top: int = 30,
                      provider: DataProvider | None = None) -> Volatility:
    """板块合成价格指数的年化波动率及其历史分位（对应个股版 volatility）。"""
    import math
    import statistics as st

    v = Volatility(代码=f"{sector}板块", 窗口=window)
    f = sector_frames(sector, years=years, top=top, provider=provider)
    if not f.ok:
        v.error = f.error
        return v
    px = [x for x in f.价格指数 if x > 0]
    if len(px) < window * 3:
        v.error = f"板块价格序列不足({len(px)}点)"
        return v
    rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px)) if px[i - 1] > 0]
    ann = math.sqrt(244) * 100
    vols = [st.pstdev(rets[i - window:i]) * ann for i in range(window, len(rets) + 1)]
    if not vols:
        v.error = "板块波动率序列为空"
        return v
    v.当前, v.分位 = _pctl(vols)
    v.均值 = sum(vols) / len(vols)
    v.样本数 = len(vols)
    v.序列 = vols
    v.ok = True
    return v


def sector_return_percentile(sector: str, *, window: int = 20, years: int = 3, top: int = 30,
                             provider: DataProvider | None = None) -> Percentile:
    """板块近 window 日涨跌幅的历史分位（对应个股版 return_percentile）。"""
    p = Percentile(代码=f"{sector}板块", 指标=f"{window}日涨跌幅")
    f = sector_frames(sector, years=years, top=top, provider=provider)
    if not f.ok:
        p.error = f.error
        return p
    px = [x for x in f.价格指数 if x > 0]
    if len(px) < window * 3:
        p.error = f"板块价格序列不足({len(px)}点)"
        return p
    rets = [(px[i] / px[i - window] - 1) * 100
            for i in range(window, len(px)) if px[i - window] > 0]
    if not rets:
        p.error = "板块区间收益序列为空"
        return p
    p.当前值, p.分位 = _pctl(rets)
    p.样本数 = len(rets)
    p.起始 = f"近{years}年·{f.成分数}只成分股"
    p.ok = True
    return p


if __name__ == "__main__":  # python -m core.history
    for code, name in [("600030.SH", "中信证券"), ("688981.SH", "中芯国际")]:
        r = percentile(code)
        print(f"  {name}({code}) PB={r.当前值:.2f}倍 → 历史分位 {r.分位:.1f}% "
              f"（{r.起始}, {r.样本数}个交易日）" if r.ok else f"  {name} 失败: {r.error}")
