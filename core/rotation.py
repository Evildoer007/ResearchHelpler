"""轮动与相对强弱计算（论点库第九类 R1~R6）。

来源：范例《科技回调期的消费板块轮动机会》——其核心论证正是
"科创50 -28.1% 同期 消费ETF +13.7%"这种**跨板块相对表现**，
以及"历史上同类情形反复出现"的规律验证。

这类论点的数据全部是**历史行情统计**，无需研报口径，是当前性价比最高的一块。

风格基准（用指数而非个股，避免个股噪声）：
  大盘=沪深300 · 中盘=中证500 · 小盘=中证1000
  成长=创业板指 · 价值=上证50 · 科技=科创50 · 宽基基准=沪深300
"""

from __future__ import annotations

from dataclasses import dataclass, field as dfield

from . import history
from .provider import DataProvider

# 风格/宽基基准（代码已在 instruments.py 校验过）
BENCH = {
    "大盘": "000300.SH", "中盘": "000905.SH", "小盘": "000852.SH",
    "成长": "399006.SZ", "价值": "000016.SH", "科技": "000688.SH",
}
MARKET = "000300.SH"          # 相对大盘超额的基准


@dataclass
class RelPerf:
    """两个标的在同一区间的相对表现。"""

    a_code: str
    b_code: str
    window: int = 20
    a_ret: float | None = None      # %
    b_ret: float | None = None
    spread: float | None = None     # a - b
    ok: bool = False
    error: str = ""


_ret_cache: dict[tuple, float | None] = {}


def _ret(code: str, window: int, years: int, provider) -> float | None:
    """区间涨跌幅（%）。进程内缓存——轮动扫描会反复取同一标的，不缓存很慢。

    ⚠ 用**日涨跌幅累乘**而非"价格首尾相除"：后者会被除权/份额拆分污染。
    实测芯片ETF(159995.SZ) 价格由 3.009→1.039，价格法得 -65.5%，
    而真实区间收益仅 -23.53%——差异全部来自除权跳变。
    涨跌幅序列本身已处理除权，故为正确口径。
    """
    key = (code, window, years)
    if key in _ret_cache:
        return _ret_cache[key]

    chg = history.series(code, "ths_chg_ratio_stock", years=years, provider=provider,
                         drop_nonpositive=False)
    val = None
    if len(chg) >= window:
        cum = 1.0
        for x in chg[-window:]:
            cum *= 1 + x / 100
        val = (cum - 1) * 100
    _ret_cache[key] = val
    return val


def relative(a_code: str, b_code: str, *, window: int = 20, years: int = 1,
             provider: DataProvider | None = None) -> RelPerf:
    """A 相对 B 的区间表现差。"""
    r = RelPerf(a_code=a_code, b_code=b_code, window=window)
    ra = _ret(a_code, window, years, provider)
    rb = _ret(b_code, window, years, provider)
    if ra is None or rb is None:
        r.error = "价格样本不足"
        return r
    r.a_ret, r.b_ret, r.spread = ra, rb, ra - rb
    r.ok = True
    return r


@dataclass
class RotationScan:
    """轮动扫描：找出与本标的走势背离的板块（一涨一跌）。"""

    base_code: str
    window: int = 20
    base_ret: float | None = None
    diverged: list[dict] = dfield(default_factory=list)   # 反向的候选
    扫描数: int = 0        # 本次比对了多少个标的——**多重比较的规模，必须记录**：
                          # 在几十个标的里找"跟本标的反向的"，任何时候都能找到几个，
                          # 不记下分母就看不出这几个到底是信号还是巧合。
    ok: bool = False
    error: str = ""


def scan_rotation(base_code: str, candidates: dict[str, str] | None = None, *,
                  window: int = 20, years: int = 1, min_spread: float = 8.0,
                  provider: DataProvider | None = None) -> RotationScan:
    """以 base 为参照，扫描候选标的中**走势相反且差距显著**者。

    对应 R1 风格轮动——范例中"科技退→消费进"即此形态。
    min_spread：相对差需超过此值（百分点）才算显著轮动，避免噪声。
    """
    s = RotationScan(base_code=base_code, window=window)
    base = _ret(base_code, window, years, provider)
    if base is None:
        s.error = "基准标的价格样本不足"
        return s
    s.base_ret = base

    from . import instruments as inst
    pool = candidates or {i.简称: i.代码 for i in inst.INSTRUMENTS
                          if i.类型 in ("宽基指数", "行业ETF")}
    for name, code in pool.items():
        if code == base_code:
            continue
        r = _ret(code, window, years, provider)
        if r is None:
            continue
        s.扫描数 += 1
        spread = r - base
        # 走势相反（符号不同）且差距显著
        if base * r < 0 and abs(spread) >= min_spread:
            s.diverged.append({"名称": name, "代码": code, "涨跌": r, "相对差": spread})
    s.diverged.sort(key=lambda x: -abs(x["相对差"]))
    s.ok = True
    return s


@dataclass
class HistoryPattern:
    """R2 历史规律验证：历史上 A 跌超阈值时，B 的同期表现统计。"""

    a_code: str
    b_code: str
    window: int = 20
    threshold: float = -10.0        # A 的区间跌幅阈值(%)
    occurrences: int = 0            # 历史出现次数
    b_positive: int = 0             # 其中 B 上涨的次数
    b_avg: float | None = None      # B 同期平均涨跌(%)
    samples: list[dict] = dfield(default_factory=list)
    min_occurrences: int = 8        # 少于此次数则胜率无统计意义，不下结论
    # 样本外检验：把回看期切成前后两段，前段发现的规律在后段是否仍成立。
    # 这是"找不到机制解释时"的兜底——机制说不清但两段都成立，可信度仍高于
    # 只在全样本上跑一遍。两段胜率差距过大则判为过拟合。
    早期胜率: float | None = None
    晚期胜率: float | None = None
    ok: bool = False
    error: str = ""

    @property
    def 胜率(self) -> float | None:
        return self.b_positive / self.occurrences * 100 if self.occurrences else None

    @property
    def 样本充足(self) -> bool:
        return self.occurrences >= self.min_occurrences

    @property
    def 样本外一致(self) -> bool | None:
        """前后两段胜率是否一致（差距 ≤ 25pct）。样本不够切分时返回 None。

        不做统计检验，只做一个粗糙的稳定性判断——样本量本就只有十几次，
        再做显著性检验也没有意义，反而会给出虚假的精确感。
        """
        if self.早期胜率 is None or self.晚期胜率 is None:
            return None
        return abs(self.早期胜率 - self.晚期胜率) <= 25.0


def verify_pattern(a_code: str, b_code: str, *, window: int = 20,
                   threshold: float = -10.0, years: int = 5, min_gap: int = 20,
                   min_occurrences: int = 8,
                   provider: DataProvider | None = None) -> HistoryPattern:
    """统计历史上"A 区间涨跌达到 threshold"时，B 的同期表现。

    threshold 为负 → 统计 A 跌超该幅度的情形；为正 → 统计 A 涨超该幅度的情形。

    源于范例《消费板块轮动》逻辑二，但**用途是检验而非证实**：
    该范例手工列举 2024/1、2024/7、2025/4 三次以论证"科技退→消费进"是稳定规律；
    本函数全样本回测（科创50跌超10% 共 15 次）却得出消费上涨仅 3 次、胜率 20%、
    平均 -2.44% —— 说明范例可能存在**挑选支持性案例的幸存者偏差**。

    因此 R2 的正确用法是：先扫全样本，胜率高才作为论据；胜率低时应放弃该论点，
    而不是只摘取几个支持结论的时点。这与本项目"论断须可证伪"的原则一致。

    min_gap：两次事件间隔至少这么多交易日，避免同一轮下跌被重复计数。
    min_occurrences：样本量下限。3 次里 2 次上涨也是 67% 胜率，但毫无统计意义——
        没有这道闸，本函数会用小样本噪声生产出"高确定性"的论点，比不做验证更糟。
    """
    hp = HistoryPattern(a_code=a_code, b_code=b_code, window=window,
                        threshold=threshold, min_occurrences=min_occurrences)
    pa = history.series(a_code, "ths_close_price_stock", years=years, provider=provider)
    pb = history.series(b_code, "ths_close_price_stock", years=years, provider=provider)
    n = min(len(pa), len(pb))
    if n < window * 3:
        hp.error = f"样本不足(A={len(pa)}, B={len(pb)})"
        return hp
    pa, pb = pa[-n:], pb[-n:]

    down = threshold < 0            # 条件方向：跌超 or 涨超
    last_hit = -10**9
    for i in range(window, n):
        if pa[i - window] <= 0 or pb[i - window] <= 0:
            continue
        ra = (pa[i] / pa[i - window] - 1) * 100
        hit = ra <= threshold if down else ra >= threshold
        if not hit or i - last_hit < min_gap:
            continue
        rb = (pb[i] / pb[i - window] - 1) * 100
        hp.occurrences += 1
        if rb > 0:
            hp.b_positive += 1
        hp.samples.append({"位置": i, "A涨跌": round(ra, 2), "B涨跌": round(rb, 2)})
        last_hit = i

    if hp.occurrences:
        hp.b_avg = sum(s["B涨跌"] for s in hp.samples) / hp.occurrences
        # 样本外检验：以序列中点切分，两段各自算胜率。两段都有样本才有意义。
        mid = n / 2
        early = [s for s in hp.samples if s["位置"] < mid]
        late = [s for s in hp.samples if s["位置"] >= mid]
        if early and late:
            hp.早期胜率 = sum(1 for s in early if s["B涨跌"] > 0) / len(early) * 100
            hp.晚期胜率 = sum(1 for s in late if s["B涨跌"] > 0) / len(late) * 100
    hp.ok = True
    return hp


@dataclass
class StyleSwitch:
    """R3/R4 风格切换：两种风格的相对强弱是否发生**反转**。"""

    风格A: str
    风格B: str
    短窗: int = 20
    长窗: int = 60
    短期差: float | None = None      # A - B（短窗，百分点）
    长期差: float | None = None      # A - B（长窗）
    切换: bool = False               # 短长期符号相反 = 发生反转
    当前占优: str = ""
    ok: bool = False
    error: str = ""


def style_switch(a: str, b: str, *, short: int = 20, long: int = 60,
                 min_spread: float = 5.0, years: int = 1,
                 provider: DataProvider | None = None) -> StyleSwitch:
    """判断风格 A 相对 B 是否发生切换。

    切换 ≠ 谁更强，而是**相对强弱反转**：长窗里 A 弱于 B，但短窗里 A 强于 B（或反之）。
    故须比较两个时间尺度的相对差符号，仅看单一窗口会把"持续占优"误判成"切换"。
    a/b 传 BENCH 的键（如 "大盘"/"小盘"）或直接传证券代码。
    """
    s = StyleSwitch(风格A=a, 风格B=b, 短窗=short, 长窗=long)
    ca, cb = BENCH.get(a, a), BENCH.get(b, b)
    rs = relative(ca, cb, window=short, years=years, provider=provider)
    rl = relative(ca, cb, window=long, years=years, provider=provider)
    if not (rs.ok and rl.ok):
        s.error = rs.error or rl.error
        return s

    s.短期差, s.长期差 = rs.spread, rl.spread
    s.当前占优 = a if rs.spread > 0 else b
    # 符号相反 且 短期差够显著 → 认定为切换
    s.切换 = (rs.spread * rl.spread < 0) and abs(rs.spread) >= min_spread
    s.ok = True
    return s


@dataclass
class LeaderDivergence:
    """R5 龙头与板块整体的走势背离。"""

    龙头代码: str
    板块: str = ""
    window: int = 20
    龙头涨跌: float | None = None
    板块涨跌: float | None = None
    背离度: float | None = None       # 龙头 - 板块（百分点）
    背离: bool = False
    ok: bool = False
    error: str = ""


def leader_divergence(code: str, sector_ret: float | None, *, sector: str = "",
                      window: int = 20, years: int = 1, min_gap: float = 8.0,
                      provider: DataProvider | None = None) -> LeaderDivergence:
    """龙头个股 vs 板块整体的表现背离。

    sector_ret 由调用方从 signals 的板块区间涨跌幅传入（板块口径与个股口径来源不同，
    不在此处重复取数）。背离 = 走势方向相反，或同向但差距超过阈值。
    """
    d = LeaderDivergence(龙头代码=code, 板块=sector, window=window)
    if sector_ret is None:
        d.error = "缺板块区间涨跌幅"
        return d
    r = _ret(code, window, years, provider)
    if r is None:
        d.error = "龙头价格样本不足"
        return d

    d.龙头涨跌, d.板块涨跌 = r, sector_ret
    d.背离度 = r - sector_ret
    d.背离 = (r * sector_ret < 0) or abs(d.背离度) >= min_gap
    d.ok = True
    return d


if __name__ == "__main__":  # python -m core.rotation
    print("【R6 相对大盘超额】科创50 vs 沪深300")
    r = relative("000688.SH", MARKET, window=20)
    print(f"   科创50 {r.a_ret:+.2f}% vs 沪深300 {r.b_ret:+.2f}% → 超额 {r.spread:+.2f}pct\n")

    print("【R1 轮动扫描】以科创50为参照，找走势相反的板块")
    s = scan_rotation("000688.SH", window=20, min_spread=5.0)
    print(f"   科创50 近20日 {s.base_ret:+.2f}%；反向且显著的候选 {len(s.diverged)} 个")
    for d in s.diverged[:5]:
        print(f"     {d['名称']:<10} {d['涨跌']:+6.2f}%  相对差 {d['相对差']:+.2f}pct")

    print("\n【R2 历史规律验证】科创50 20日跌超10% 时，消费ETF 同期表现")
    hp = verify_pattern("000688.SH", "159928.SZ", threshold=-10.0, years=5)
    if hp.ok and hp.occurrences:
        print(f"   历史出现 {hp.occurrences} 次，消费上涨 {hp.b_positive} 次"
              f"（胜率 {hp.胜率:.0f}%），平均 {hp.b_avg:+.2f}%")
        for s_ in hp.samples[:5]:
            print(f"     A {s_['A涨跌']:+.1f}% → B {s_['B涨跌']:+.1f}%")
    else:
        print("  ", hp.error or "历史上未出现该情形")
