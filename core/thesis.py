"""论点库与触发引擎。

论点内容的**唯一事实来源是 `THESIS_LIBRARY.md`**（113 条，9 类），本模块负责：
  1. 解析该文档 → 结构化的 Thesis 对象
  2. 注册判定函数 → 按摸底数据自动算出"哪些论点被触发"
  3. 供 planner 从**被触发的论点**中挑选 2~3 条组成正文主轴（DESIGN §7.2）

为什么这样分工：论点是业务知识，改文档即可（无需动代码）；
判定是计算逻辑，写在代码里。避免文档与代码双份维护而失同步。

边界（DESIGN §7.3）：论点只产出**市场状态的客观特征**，不推导该用什么结构。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dfield
from pathlib import Path
from typing import Callable

_LIB_PATH = Path(__file__).resolve().parent.parent / "THESIS_LIBRARY.md"

# 可得性标记
AVAIL_OK = "✅"       # 数据已可取
AVAIL_WAIT = "⏳"     # 数据源有但未接
AVAIL_MANUAL = "📝"   # 需人工/研报口径


@dataclass
class Thesis:
    id: str
    名称: str
    类别: str
    触发条件: str
    所需数据: str
    可得性: str
    特征: dict[str, str] = dfield(default_factory=dict)
    对称面: str = ""

    @property
    def 方向(self) -> str:
        return self.特征.get("方向", "")

    @property
    def 可自动判定(self) -> bool:
        return self.id in _JUDGES


@dataclass
class Trigger:
    """一条论点的判定结果。triggered=None 表示数据不足，无法判定。"""

    thesis: Thesis
    triggered: bool | None
    说明: str = ""
    证据: dict[str, str] = dfield(default_factory=dict)


# ---------------- 解析 THESIS_LIBRARY.md ----------------

def _parse_features(cell: str) -> dict[str, str]:
    """把"方向=看涨；价格位置=低位；确定性=高"解析成字典。"""
    out: dict[str, str] = {}
    for part in re.split(r"[；;]", cell):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


_LIB_CACHE: dict[str, Thesis] | None = None   # 默认路径的解析结果，进程内复用


def load_library(path: Path | None = None) -> dict[str, Thesis]:
    """解析论点库文档。返回 {id: Thesis}，保持文档顺序。"""
    global _LIB_CACHE
    if path is None and _LIB_CACHE is not None:
        return _LIB_CACHE
    p = path or _LIB_PATH
    theses: dict[str, Thesis] = {}
    category = ""
    for line in p.read_text(encoding="utf-8").split("\n"):
        m_cat = re.match(r"^## [一二三四五六七八九]、(.+?)(?:\s*★.*)?$", line)
        if m_cat:
            category = m_cat.group(1).strip()
            continue
        if not re.match(r"^\|\s*\*?\*?[A-Z]\d+b?\*?\*?\s*\|", line):
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) < 5:
            continue
        tid = cells[0].replace("*", "").strip()
        name_cell = cells[1].replace("*", "")
        sym = ""
        if "↔" in name_cell:
            name, sym = name_cell.split("↔", 1)
            sym = sym.strip()
        else:
            name = name_cell
        avail = (AVAIL_OK if AVAIL_OK in cells[3] else
                 AVAIL_WAIT if AVAIL_WAIT in cells[3] else
                 AVAIL_MANUAL if AVAIL_MANUAL in cells[3] else "")
        theses[tid] = Thesis(
            id=tid, 名称=name.strip(), 类别=category,
            触发条件=cells[2].replace("*", "").strip(),
            所需数据=cells[3].replace("*", "").strip(),
            可得性=avail, 特征=_parse_features(cells[4]), 对称面=sym,
        )
    if path is None:
        _LIB_CACHE = theses
    return theses


# ---------------- 判定阈值（实测校准，集中管理便于调参）----------------

TH = {
    # ============ 主周期：全库时间口径的锚点 ============
    # 此前各判定的窗口散落在三处——TH、判定函数里的硬编码（window=20 共 5 处）、
    # 以及 signals 的函数默认参数（days=10）。三者互不相识，导致
    # **同一份报告里"区间涨跌幅"指两个不同周期**：V2/F1/F2 用的是 10 日，
    # 而 S7 超跌、R1 轮动、R3~R6 用的是 20 日。论点库文档又没有"窗口"这一列，
    # 读的人无从察觉。
    #
    # 定 20 个交易日（约 1 个月）为主周期，理由是产品端——挂钩的场外结构
    # 期限通常 1~3 个月，价格与资金类的观察窗口应与之匹配。
    # 其余窗口围绕它分层：盈利类随财报天然是季度；宏观类是季度到半年；
    # 估值分位需长样本，保持 10 年。
    "主周期_交易日": 20,

    "分位_低": 30.0,      # PB/PE 历史分位低于此 → 低估
    "分位_高": 70.0,      # 高于此 → 高估
    "分位_泡沫": 90.0,
    "涨跌_显著": 3.0,     # 区间涨跌幅绝对值超此值才算"显著"，避免噪声触发
    "资金_显著亿": 5.0,   # 主力净流入/流出超此值（亿元）才算显著
    # 大股东净增减持阈值。⚠ 绝对值阈值对不同规模板块并不公平：
    # 半导体净减持 334 亿与白酒净增持 7.4 亿，绝对额差 45 倍，但相对各自板块市值可能量级相近。
    # 实测白酒净增持 7.37 亿因低于 10 亿未触发 F4，判定偏严 → 暂降至 5 亿，
    # 后续宜改为"占板块市值比例"以消除规模偏差（见 §待办）。
    "减持_显著亿": 5.0,
    # 情绪/交易类阈值（S1~S4, S7）
    "波动率_高分位": 60.0,
    "波动率_低分位": 30.0,
    "波动率_极端高": 90.0,   # S4 均值回归：偏离到极端才有回归价值
    "波动率_极端低": 10.0,
    "涨跌分位_极低": 10.0,   # S7 超跌：近20日涨跌幅处历史最低 10%
    "涨跌分位_极高": 90.0,   # S7b 超涨
    # 轮动/相对强弱类（R）
    "超额_显著pct": 8.0,     # R6 相对大盘超额需超此百分点
    "轮动_显著pct": 10.0,    # R1 轮动：反向板块与本标的差距需超此值
    "规律_最低胜率": 60.0,   # R2 历史规律：胜率低于此则规律不成立，不应作为论据
    "规律_证伪胜率": 40.0,   # R2b：胜率低于此则主动指出该规律历史上不支持
    # R2 的"情形"条件。**必须是事先固定的条件，不能从数据里挑**——
    # 在 35 个板块里找"这次跟本板块反向的那个"，任何时候都能找到几个，
    # 那是多重比较（数据挖掘），不是假设检验。大盘是有先验依据的条件：
    # 任何板块对它都有 beta 暴露，"大盘跌时本板块如何"本身就是个有意义的问题。
    # 沪深300 近 window 日涨跌幅**绝对值**达此值 → 进入极端情形（急跌或急涨，双向对称）。
    # 两个方向都是有先验意义的问题："大盘急跌时本标的是否抗跌"与
    # "大盘急涨时本标的是否跟得上"同等重要，不因方向而只做一边。
    "规律_情形阈值pct": 10.0,
    # 样本量下限：3 次里 2 次上涨也是 67% 胜率，但没有统计意义。
    "规律_最小样本数": 8,
    # R1 的条件是**从几十个标的的扫描里挑出来的**，不像 R2 的"大盘"是事先固定的，
    # 因而更容易撞上巧合。故门槛比 R2 高，作为一次粗糙的多重比较校正。
    "轮动_扫描后胜率": 70.0,
    "切换_显著pct": 5.0,     # R3/R4 风格切换：短期相对差需超此值才算切换（非噪声）
    "背离_显著pct": 8.0,     # R5 龙头与板块背离阈值
    # 同业对比类（V3/V3b/E3/E3b）
    "同业偏离_显著pct": 25.0,  # PE 相对同业中位数偏离超此比例才算折价/溢价
    "增速差_显著pct": 15.0,    # 净利增速与同业中位数的绝对差（百分点）
    # 拥挤度与情绪（S5/S6/S8/S8b）——用换手率与成交额的历史分位
    "拥挤_过热分位": 80.0,     # 换手率近5日均值处历史 80% 以上 → 交易拥挤
    "拥挤_释放分位": 50.0,     # 退到 50% 以下才算"已释放"
    "拥挤_曾过热分位": 85.0,   # 且近3个月内确实到过 85% 以上（否则是一直不热，不叫释放）
    "情绪_冰点分位": 20.0,     # 成交额分位低于此 → 情绪冰点
    "情绪_过热分位": 85.0,
    # 盈利趋势（E2/E2b）
    "增速见顶_连降期数": 3,    # 连续回落几期才算见顶
    "增速见顶_累计降pct": 20.0,  # 且从最早一期到最新累计回落幅度（百分点）
    # 解禁（F5/F5b/C6）。规模一律用**占流通市值比例**，绝对额在大小板块间不可比。
    # 实测白酒未来3月解禁仅 0.1 亿却因后9个月近乎为零而算出 11x 倍数——
    # 故倍数必须配规模门槛使用，不能单独触发。
    "解禁_显著占比pct": 1.0,   # 未来3个月解禁额占板块市值比，超此才值得提示
    "解禁_高峰倍数": 1.5,      # 近端月均 / 远端月均，超此为高峰临近
    "解禁_已过倍数": 0.6,      # 低于此为高峰已过
    # 业绩披露窗口（E4）
    "业绩窗口_天": 30,         # 距法定披露截止日不足此天数 → 业绩验证期
    # 宏观利率（M1/M2）。阈值按 10Y 国债近 10 年的变动分布实测标定：
    # 90 天变动的中位绝对值为 13.3bp，取 20bp（约 1.5 倍）既能滤掉噪声，
    # 又能在历史上约 1/3 的时间触发（上行 14.4% / 下行 18.6%，两方向大致对称）。
    # 放宽到 15bp 则 44.6% 的时间都在触发，失去区分度。
    "利率_显著变动bp": 20.0,
    "利率_窗口天": 90,         # 与论点库 M1/M2 标注的"窗口=中期"一致
    # 以下（#38）阈值样本量明显小于 M1~M7（每板块仅 1~2 个历史观测点，
    # 非数百个交易日的分布），全部是务实拍定，比其余阈值更需要业务复核。
    "净利率_显著变动pct": 3.0,     # E5：板块整体法净利率同比变动超此才算显著
    "ROE_显著变动pct": 1.0,        # E5：板块整体法ROE同比变动超此才算显著
    "毛利率_显著变动pct": 2.0,     # E6：板块整体法毛利率同比变动超此才算显著
    "ETF份额_显著变动pct": 15.0,   # F7：代表ETF近90天份额变动超此才算显著
    "机构持仓_低配分位": 30.0,     # F8：机构持股比例历史分位低于此 → 低配
    "机构持仓_超配分位": 70.0,     # F8b：高于此 → 超配/拥挤
    "两融余额_显著变动pct": 10.0,  # F9：代表标的近90天两融余额变动超此才算显著
    "预测PE_显著折价pct": 10.0,    # V7：FY1预测PE 低于当前PE 超此比例才算估值切换
    # 流动性（M3/M3b）：M2同比 与 社融存量同比 均为**月频**，90 天只有 3 个观测，
    # 故窗口取 180 天（半年）。实测半年变动的中位绝对值 M2 0.80pct、社融 0.80pct，
    # 取 0.5pct 并**要求两者同向**——单看一个容易被单月扰动带偏，同向才配称"流动性环境"。
    "流动性_窗口天": 180,
    "流动性_显著变动pct": 0.5,
    # 汇率（M4/M4b）：90 天变动的中位绝对值 1.32%，取 2.0%（约 1.5 倍，与利率同一标定口径），
    # 历史触发 36.5%（贬值 20.5% / 升值 16.0%）。
    "汇率_窗口天": 90,
    "汇率_显著变动pct": 2.0,
    # 通胀（M6）：用绝对区间而非分位——"PPI 转负=工业品通缩"是投研通用口径，
    # 分位会随样本期漂移。实测近10年 PPI<0 占 50%、|PPI|>3 占比约 36%，CPI>3 占 7%。
    "通胀_PPI显著pct": 3.0,
    "通胀_CPI高pct": 3.0,
    # 景气（M7）：制造业 PMI 偏离荣枯线 50。实测偏离中位 0.80，取 1.0 → 历史触发 38.3%。
    "PMI_荣枯偏离": 1.0,
}

# A股法定定期报告披露截止日（月, 日）。年报与一季报同为 4/30。
_REPORT_DEADLINES = ((4, 30, "年报及一季报"), (8, 31, "半年报"), (10, 31, "三季报"))


# ---------------- 判定函数 ----------------
# 签名：fn(profile) -> (triggered|None, 说明, 证据)
# profile: {字段名: FieldValue}；返回 None 表示数据不足

def _val(profile, field):
    """取字段数值，取不到返回 None。"""
    fv = profile.get(field)
    if fv is None or not getattr(fv, "ok", False):
        return None
    try:
        return float(fv.value)
    except (TypeError, ValueError):
        return None


def _disp(profile, field) -> str:
    fv = profile.get(field)
    return (getattr(fv, "display", "") or "") if fv else ""


def _j_pb_low(p):
    q = _val(p, "PB历史分位")
    if q is None:
        return None, "缺 PB历史分位", {}
    ok = q < TH["分位_低"]
    return ok, f"PB分位 {q:.1f}% {'<' if ok else '≥'} {TH['分位_低']}%", {"PB历史分位": _disp(p, "PB历史分位")}


def _j_pb_high(p):
    q = _val(p, "PB历史分位")
    if q is None:
        return None, "缺 PB历史分位", {}
    ok = q > TH["分位_高"]
    return ok, f"PB分位 {q:.1f}% {'>' if ok else '≤'} {TH['分位_高']}%", {"PB历史分位": _disp(p, "PB历史分位")}


def _j_bubble(p):
    """V8 估值泡沫预警：**只判"分位极高"这一半**。

    论点库原始口径是"PE 分位 > 90% **且高于历次泡沫顶**"，后半句需要
    `历史泡沫顶PE对比`（schema 标 MANUAL，数据源结构性没有），从未参与判定。
    此前文档写"且高于历次泡沫顶"而代码只判分位，**说的和做的不一致**——
    读文档的人会以为触发时已做过历史对标，实际没有。
    现按实际能判的口径如实表述：触发即"分位处极端高位"，
    是否真的构成泡沫需人工用历史对标复核。
    """
    q = _val(p, "PB历史分位")
    if q is None:
        return None, "缺 PB历史分位", {}
    ok = q > TH["分位_泡沫"]
    return ok, (f"PB分位 {q:.1f}%（极端阈值 {TH['分位_泡沫']}%）"
                "；历次泡沫顶对标数据缺失，未参与判定"), {
        "PB历史分位": _disp(p, "PB历史分位")}


def _price_profit(p):
    return _val(p, "板块区间涨跌幅"), _val(p, "归母净利同比")


def _j_div_pos(p):
    chg, prof = _price_profit(p)
    if chg is None or prof is None:
        return None, "缺 区间涨跌幅或净利同比", {}
    ok = chg < -TH["涨跌_显著"] and prof > 0
    return ok, f"区间{chg:+.2f}%、净利同比{prof:+.2f}%", {
        "板块区间涨跌幅": _disp(p, "板块区间涨跌幅"), "归母净利同比": _disp(p, "归母净利同比")}


def _j_div_neg(p):
    chg, prof = _price_profit(p)
    if chg is None or prof is None:
        return None, "缺 区间涨跌幅或净利同比", {}
    ok = chg > TH["涨跌_显著"] and prof < 0
    return ok, f"区间{chg:+.2f}%、净利同比{prof:+.2f}%", {
        "板块区间涨跌幅": _disp(p, "板块区间涨跌幅"), "归母净利同比": _disp(p, "归母净利同比")}


def _j_profit_up(p):
    v = _val(p, "归母净利同比")
    if v is None:
        return None, "缺 归母净利同比", {}
    return v > 0, f"归母净利同比 {v:+.2f}%", {"归母净利同比": _disp(p, "归母净利同比")}


def _j_profit_down(p):
    v = _val(p, "归母净利同比")
    if v is None:
        return None, "缺 归母净利同比", {}
    return v < 0, f"归母净利同比 {v:+.2f}%", {"归母净利同比": _disp(p, "归母净利同比")}


def _flow_chg(p):
    """返回 (区间涨跌幅, 资金净流入亿元)。"""
    flow = _val(p, "板块资金净流入")
    return _val(p, "板块区间涨跌幅"), (flow / 1e8 if flow is not None else None)


def _j_flow_in_vs_fall(p):
    chg, flow = _flow_chg(p)
    if chg is None or flow is None:
        return None, "缺 区间涨跌幅或资金流", {}
    ok = chg < -TH["涨跌_显著"] and flow > TH["资金_显著亿"]
    return ok, f"区间{chg:+.2f}%、资金{flow:+.2f}亿", {
        "板块区间涨跌幅": _disp(p, "板块区间涨跌幅"), "板块资金净流入": _disp(p, "板块资金净流入")}


def _j_flow_out_vs_rise(p):
    chg, flow = _flow_chg(p)
    if chg is None or flow is None:
        return None, "缺 区间涨跌幅或资金流", {}
    ok = chg > TH["涨跌_显著"] and flow < -TH["资金_显著亿"]
    return ok, f"区间{chg:+.2f}%、资金{flow:+.2f}亿", {
        "板块区间涨跌幅": _disp(p, "板块区间涨跌幅"), "板块资金净流入": _disp(p, "板块资金净流入")}


def _j_flow_in_with_rise(p):
    chg, flow = _flow_chg(p)
    if chg is None or flow is None:
        return None, "缺 区间涨跌幅或资金流", {}
    ok = chg > TH["涨跌_显著"] and flow > TH["资金_显著亿"]
    return ok, f"区间{chg:+.2f}%、资金{flow:+.2f}亿", {
        "板块区间涨跌幅": _disp(p, "板块区间涨跌幅"), "板块资金净流入": _disp(p, "板块资金净流入")}


def _j_flow_out_with_fall(p):
    chg, flow = _flow_chg(p)
    if chg is None or flow is None:
        return None, "缺 区间涨跌幅或资金流", {}
    ok = chg < -TH["涨跌_显著"] and flow < -TH["资金_显著亿"]
    return ok, f"区间{chg:+.2f}%、资金{flow:+.2f}亿", {
        "板块区间涨跌幅": _disp(p, "板块区间涨跌幅"), "板块资金净流入": _disp(p, "板块资金净流入")}


def _j_holder_net_buy(p):
    """减持规模字段：正=净减持，负=净增持（见 schema）。"""
    v = _val(p, "减持规模")
    if v is None:
        return None, "缺 减持规模", {}
    ok = v < -TH["减持_显著亿"] * 1e8
    return ok, f"大股东{_disp(p, '减持规模')}", {"减持规模": _disp(p, "减持规模")}


def _j_holder_net_sell(p):
    """净减持规模显著。

    注：F3(卖压释放)/F3b(卖压加剧)/F4b(净减持转向) 的原始定义都含"较前期变化"
    （收窄/扩大/由增转减），需要**上期减持数据**才能严格判定。当前只有当期值，
    故仅以"净减持规模是否显著"作近似，并在说明中标注该局限，避免过度解读。
    """
    v = _val(p, "减持规模")
    if v is None:
        return None, "缺 减持规模", {}
    ok = v > TH["减持_显著亿"] * 1e8
    return ok, f"大股东{_disp(p, '减持规模')}（近一年，无上期数据故未判定趋势变化）", {
        "减持规模": _disp(p, "减持规模")}


def _holder_trend(p):
    """大股东增减持的**趋势**：近3月 vs 前9月，月均净减持额（元/月）。

    F3（卖压释放）/F4b（净减持转向）的原始定义都是"较前期变化"，
    此前只有近一年一个累计值，无从判定趋势，两条论点因此长期空着——
    而它们的对称面 F3b/F4 都已接判定，形成**方向偏倚**：
    系统能说"卖压加剧"却说不了"卖压释放"。对最怕敲入的雪球类产品，
    只会找看跌理由和只会找看涨理由同样危险。

    做法与解禁（F5/F5b）一致：取两个窗口相减得出前段。
    返回 (近3月月均净减持, 前9月月均净减持)，单位元；取不到返回 None。
    """
    sector = p.get("__sector__")
    if not sector:
        return None
    from . import aggregate as ag

    try:
        近 = ag.sector_shareholder_change(sector, months=3)
        年 = ag.sector_shareholder_change(sector, months=12)
    except Exception:
        return None
    if not 近 or not 年 or "净减持" not in 近 or "净减持" not in 年:
        return None
    近净 = float(近["净减持"])
    前净 = float(年["净减持"]) - 近净        # 前 9 个月 = 全年 − 近 3 月
    return 近净 / 3.0, 前净 / 9.0


def _j_holder_relief(p):
    """F3 卖压释放（减持尾声）：近3月月均净减持较前9月**显著收窄**。"""
    r = _holder_trend(p)
    if r is None:
        return None, "缺 大股东增减持趋势数据", {}
    近, 前 = r
    if 前 <= 0:                              # 前期本就没有净减持，谈不上"释放"
        return False, (f"前9个月月均为净增持 {-前/1e8:.2f}亿元，"
                       f"不存在待释放的卖压"), {}
    收窄 = 1 - 近 / 前 if 前 else 0.0
    ok = 近 < 前 * TH["解禁_已过倍数"]        # 沿用"高峰已过"的 0.6 倍口径
    return ok, (f"大股东净减持月均：前9个月 {前/1e8:.2f}亿元 → 近3个月 {近/1e8:.2f}亿元"
                f"（收窄 {收窄*100:.0f}%，阈值需降至 {TH['解禁_已过倍数']:.0%} 以下）"), {
        "近3月月均净减持": f"{近/1e8:.2f}亿元",
        "前9月月均净减持": f"{前/1e8:.2f}亿元",
        "净减持收窄幅度": f"{收窄*100:.0f}%"}


def _j_holder_turn_sell(p):
    """F4b 净减持转向：前期净增持，近期转为净减持。"""
    r = _holder_trend(p)
    if r is None:
        return None, "缺 大股东增减持趋势数据", {}
    近, 前 = r
    ok = 前 < 0 and 近 > TH["减持_显著亿"] * 1e8 / 3   # 前期净增持，近期月均净减持显著
    return ok, (f"大股东月均：前9个月净{'增持' if 前 < 0 else '减持'} {abs(前)/1e8:.2f}亿元"
                f" → 近3个月净{'减持' if 近 > 0 else '增持'} {abs(近)/1e8:.2f}亿元"), {
        "近3月月均净减持": f"{近/1e8:.2f}亿元",
        "前9月月均净减持": f"{前/1e8:.2f}亿元"}


def _j_vol_high(p):
    q = _val(p, "波动率历史分位")
    if q is None:
        return None, "缺 波动率历史分位", {}
    ok = q > TH["波动率_高分位"]
    return ok, f"波动率分位 {q:.1f}%（{_disp(p,'年化波动率')}）", {
        "年化波动率": _disp(p, "年化波动率"), "波动率历史分位": _disp(p, "波动率历史分位")}


def _j_vol_low(p):
    q = _val(p, "波动率历史分位")
    if q is None:
        return None, "缺 波动率历史分位", {}
    ok = q < TH["波动率_低分位"]
    return ok, f"波动率分位 {q:.1f}%（{_disp(p,'年化波动率')}）", {
        "年化波动率": _disp(p, "年化波动率"), "波动率历史分位": _disp(p, "波动率历史分位")}


def _j_crash_highvol(p):
    """S3 急跌后的高波动+低位组合——衍生品最关注的状态。"""
    vq = _val(p, "波动率历史分位")
    rq = _val(p, "区间涨跌幅分位")
    pq = _val(p, "PB历史分位")
    if vq is None or rq is None:
        return None, "缺 波动率分位或区间涨跌分位", {}
    # 只看"急跌 + 高波动"两个条件。曾附加"PB分位须<70%"，但实测中芯国际
    # (波动率92.6%分位 + 涨跌0.1%分位) 因 PB 79.1% 被否决——急跌高波动本身是
    # 独立的市场状态，不应被估值水平否决；估值高低由 V1/V1b 单独表达。
    ok = vq > TH["波动率_高分位"] and rq < TH["涨跌分位_极低"]
    ev = {"波动率历史分位": _disp(p, "波动率历史分位"),
          "区间涨跌幅分位": _disp(p, "区间涨跌幅分位")}
    if pq is not None:
        ev["PB历史分位"] = _disp(p, "PB历史分位")
    return ok, f"波动率分位{vq:.0f}%、区间涨跌分位{rq:.0f}%" + (f"、PB分位{pq:.0f}%" if pq is not None else ""), ev


def _j_vol_reversion(p):
    """S4 波动率均值回归：当前显著偏离历史均值（双向）。"""
    q = _val(p, "波动率历史分位")
    if q is None:
        return None, "缺 波动率历史分位", {}
    ok = q > TH["波动率_极端高"] or q < TH["波动率_极端低"]
    return ok, f"波动率分位 {q:.1f}%（极端区间 <{TH['波动率_极端低']}% 或 >{TH['波动率_极端高']}%）", {
        "波动率历史分位": _disp(p, "波动率历史分位"),
        "年化波动率": _disp(p, "年化波动率")}


def _j_oversold(p):
    q = _val(p, "区间涨跌幅分位")
    if q is None:
        return None, "缺 区间涨跌幅分位", {}
    ok = q < TH["涨跌分位_极低"]
    return ok, f"近20日涨跌分位 {q:.1f}%", {"区间涨跌幅分位": _disp(p, "区间涨跌幅分位")}


def _j_overbought(p):
    q = _val(p, "区间涨跌幅分位")
    if q is None:
        return None, "缺 区间涨跌幅分位", {}
    ok = q > TH["涨跌分位_极高"]
    return ok, f"近20日涨跌分位 {q:.1f}%", {"区间涨跌幅分位": _disp(p, "区间涨跌幅分位")}


# ---- 轮动/相对强弱类（R）：需标的代码而非仅 profile，故走独立入口 ----

def _rot_ctx(p):
    """从 profile 里取轮动计算所需的上下文（代码/板块）。"""
    return (p.get("__code__"), p.get("__sector__"))


def _j_excess_return(p):
    """R6 相对大盘超额：本标的相对沪深300 的区间表现差。"""
    code, _ = _rot_ctx(p)
    if not code:
        return None, "缺标的代码", {}
    from . import rotation as rot

    r = rot.relative(code, rot.MARKET, window=20)
    if not r.ok:
        return None, r.error, {}
    ok = abs(r.spread) >= TH["超额_显著pct"]
    return ok, (f"近20日 {r.a_ret:+.2f}% vs 沪深300 {r.b_ret:+.2f}%，"
                f"超额 {r.spread:+.2f}pct"), {"本标的近20日": f"{r.a_ret:+.2f}%", "沪深300近20日": f"{r.b_ret:+.2f}%",
                "相对大盘超额": f"{r.spread:+.2f}pct"}


def _j_rotation(p):
    """R1 风格轮动：本标的下跌而其它板块逆势上涨（或反之）。

    **扫描结果只是线索，不能直接当论点。** `scan_rotation` 会拿本标的去比对
    几十个指数/ETF，挑出走势相反的——在这么多标的里找反向的，任何时候都能找到几个，
    这是统计上的**多重比较**，挑出来的多半是巧合而非轮动关系
    （"创新药与金融科技ETF反向"就是这类）。

    故本判定要求线索再过一道历史检验：该反向关系在近5年里是否稳定存在。
    且因为这个条件是**从扫描里挑出来的**（不像 R2 的"大盘"是事先固定的），
    门槛比 R2 更严——用 `轮动_扫描后胜率`（更高）而非 `规律_最低胜率`，
    这相当于一次粗糙的多重比较校正。
    """
    code, _ = _rot_ctx(p)
    if not code:
        return None, "缺标的代码", {}
    from . import rotation as rot

    s = rot.scan_rotation(code, window=20, min_spread=TH["轮动_显著pct"])
    if not s.ok:
        return None, s.error, {}
    if not s.diverged:
        return False, f"扫描 {s.扫描数} 个标的，无走势反向者", {}

    top = s.diverged[0]                     # 相对差最大的那个
    hp = rot.verify_pattern(top["代码"], code, window=20,
                            threshold=abs(TH["轮动_显著pct"]),   # 该板块涨超此幅度
                            min_occurrences=int(TH["规律_最小样本数"]))
    head = (f"本标的近20日 {s.base_ret:+.2f}%，"
            f"扫描 {s.扫描数} 个标的中 {len(s.diverged)} 个反向，"
            f"最显著：{top['名称']}{top['涨跌']:+.1f}%")
    if not hp.ok:
        return None, f"{head}；历史检验失败：{hp.error}", {}
    if not hp.样本充足:
        return None, (f"{head}；但该关系历史上仅出现 {hp.occurrences} 次，"
                      f"不足 {int(TH['规律_最小样本数'])} 次，无法判断是轮动还是巧合"), {}

    # 稳定的轮动关系 = 对方涨时本标的**倾向下跌** → 胜率（本标的上涨占比）应当很低
    上限 = 100.0 - TH["轮动_扫描后胜率"]
    ok = hp.胜率 <= 上限 and hp.样本外一致 is not False
    判语 = "历史上该反向关系稳定" if ok else "历史上该反向关系不稳定，本次疑为巧合"
    return ok, (f"{head}；近5年 {top['名称']}涨超{abs(TH['轮动_显著pct']):.0f}% 共 "
                f"{hp.occurrences} 次，本标的其中 {hp.b_positive} 次同涨"
                f"（{hp.胜率:.0f}%，门槛 ≤{上限:.0f}%）——{判语}"), {
        "轮动对手": f"{top['名称']}{top['涨跌']:+.1f}%",
        "历史反向稳定度": f"同涨仅 {hp.胜率:.0f}%（{hp.occurrences} 次样本）"}


def _j_rotation_reverse(p):
    """R1b 轮动逆转：本标的相对大盘的强弱**发生反转**（原强转弱，或原弱转强）。

    与 R1 的关系：R1 判"此刻存在背离"，R1b 判"原有格局正在反转"。
    实现上比较两个窗口的超额——长窗（60日）代表原格局，短窗（20日）代表当下：
      长窗超额 > 0 而短窗超额 < 0 → 原强势转弱（对本标的看跌）
      长窗超额 < 0 而短窗超额 > 0 → 原弱势回流（对本标的看涨）
    两者都是"轮动逆转"，方向取决于本标的处在哪一侧，
    与论点库标注的「方向=看跌(B)/看涨(A)」一致。

    补这条的理由是**方向配平**：R1 早已接判定而 R1b 空着，
    系统只会说"别的板块在涨"，说不了"本板块正在被资金回补"。
    """
    code, _ = _rot_ctx(p)
    if not code:
        return None, "缺标的代码", {}
    from . import rotation as rot

    短 = rot.relative(code, rot.MARKET, window=TH["主周期_交易日"])
    长 = rot.relative(code, rot.MARKET, window=60, years=2)
    if not 短.ok or not 长.ok:
        return None, (短.error or 长.error or "相对表现取数失败"), {}
    th = TH["切换_显著pct"]
    反转 = 短.spread * 长.spread < 0 and abs(短.spread) >= th and abs(长.spread) >= th
    向 = "由弱转强（资金回流）" if 短.spread > 0 else "由强转弱（资金撤离）"
    return 反转, (f"相对沪深300超额：近60日 {长.spread:+.2f}pct → 近20日 {短.spread:+.2f}pct"
                  f"（阈值 ±{th:.0f}pct）"
                  + ("；" + 向 if 反转 else "；未发生反转")), {
        "近60日超额": f"{长.spread:+.2f}pct", "近20日超额": f"{短.spread:+.2f}pct",
        "轮动方向": 向 if 反转 else "未反转"}


def _j_size_switch(p):
    """R3 大小盘风格切换。"""
    from . import rotation as rot

    s = rot.style_switch("大盘", "小盘", min_spread=TH["切换_显著pct"])
    if not s.ok:
        return None, s.error, {}
    return s.切换, (f"大盘vs小盘 短期{s.短期差:+.2f}pct、长期{s.长期差:+.2f}pct，"
                   f"当前占优：{s.当前占优}"), {"大小盘相对": f"{s.短期差:+.2f}pct"}


def _j_growth_value_switch(p):
    """R4 成长/价值切换。"""
    from . import rotation as rot

    s = rot.style_switch("成长", "价值", min_spread=TH["切换_显著pct"])
    if not s.ok:
        return None, s.error, {}
    return s.切换, (f"成长vs价值 短期{s.短期差:+.2f}pct、长期{s.长期差:+.2f}pct，"
                   f"当前占优：{s.当前占优}"), {"成长价值相对": f"{s.短期差:+.2f}pct"}


def _j_leader_divergence(p):
    """R5 龙头与板块背离。板块涨跌直接用 profile 已有字段，避免重复取数。"""
    code, sector = _rot_ctx(p)
    sec_ret = _val(p, "板块区间涨跌幅")
    if not code or sec_ret is None:
        return None, "缺标的代码或板块区间涨跌幅", {}
    from . import rotation as rot

    d = rot.leader_divergence(code, sec_ret, sector=sector or "",
                              min_gap=TH["背离_显著pct"])
    if not d.ok:
        return None, d.error, {}
    return d.背离, (f"龙头{d.龙头涨跌:+.2f}% vs 板块{d.板块涨跌:+.2f}%，"
                   f"背离度{d.背离度:+.2f}pct"), {"龙头涨跌": f"{d.龙头涨跌:+.2f}%", "板块涨跌": f"{d.板块涨跌:+.2f}%",
                    "龙头板块背离": f"{d.背离度:+.2f}pct"}


def _pattern(p):
    """R2/R2b 的公共计算：先判当期情形，再做历史回测。

    **两个条件必须同时成立**，且顺序不能倒：

    ① 当期情形：现在确实处于"大盘下跌"这个情形中。
       不成立就直接返回，**不做任何历史回测**——一是逻辑上必须（历史规律说
       "大盘跌时本板块补涨"，可现在大盘没跌，这条论点根本用不上，不能拿一个
       当前不成立的前提去写正文）；二是成本上必须（回测要取两条 5 年日频序列）。
    ② 历史规律：该情形历史上出现过足够多次，且本板块表现方向稳定。

    返回 (HistoryPattern 或 None, 说明)。None 表示当期情形不成立或取数失败。
    """
    code, _ = _rot_ctx(p)
    if not code:
        return None, "缺标的代码"
    from . import rotation as rot

    thr = abs(TH["规律_情形阈值pct"])
    now = rot.relative(code, rot.MARKET, window=20)
    if not now.ok:
        return None, now.error
    if now.b_ret is None or abs(now.b_ret) < thr:
        got = f"{now.b_ret:+.2f}%" if now.b_ret is not None else "取不到"
        return None, f"当前沪深300近20日 {got}，未进入极端情形（阈值 ±{thr:.0f}%）"

    # 情形方向随当期实况定：急跌就用"大盘跌超"的历史样本，急涨就用"涨超"的。
    # 拿反方向的样本去验，统计的是另一件事，结论无效。
    下跌情形 = now.b_ret <= 0
    情形名 = "急跌" if 下跌情形 else "急涨"
    hp = rot.verify_pattern(rot.MARKET, code, window=20,
                            threshold=-thr if 下跌情形 else thr,
                            min_occurrences=int(TH["规律_最小样本数"]))
    if not hp.ok:
        return None, hp.error
    return hp, f"沪深300近20日 {now.b_ret:+.2f}%，已进入大盘{情形名}情形"


def _pattern_desc(hp, head: str) -> str:
    oos = ""
    if hp.样本外一致 is not None:
        oos = ("；前后两段胜率 "
               f"{hp.早期胜率:.0f}%/{hp.晚期胜率:.0f}%"
               f"（{'一致' if hp.样本外一致 else '不一致，疑过拟合'}）")
    return (f"{head}；近5年同类情形 {hp.occurrences} 次，"
            f"本标的 {hp.b_positive} 次上涨（胜率 {hp.胜率:.1f}%），"
            f"平均 {hp.b_avg:+.2f}%{oos}")


def _j_pattern_verified(p):
    """R2 历史规律反复验证：当期已进入该情形，且历史统计支持。"""
    hp, note = _pattern(p)
    if hp is None:
        return None, note, {}
    if not hp.样本充足:
        return None, (f"{note}；但历史仅 {hp.occurrences} 次，"
                      f"少于 {int(TH['规律_最小样本数'])} 次，胜率无统计意义"), {}
    ok = hp.胜率 >= TH["规律_最低胜率"] and hp.样本外一致 is not False
    return ok, _pattern_desc(hp, note), {
        "历史胜率": f"{hp.胜率:.1f}%（{hp.b_positive}/{hp.occurrences}）",
        "同类情形平均涨跌": f"{hp.b_avg:+.2f}%"}


def _j_pattern_falsified(p):
    """R2b 历史规律证伪：当期已进入该情形，但历史统计**不支持**常见说法。

    这条的存在理由：没有它，胜率低时只是"不触发"，静悄悄消失；
    而 R2b 会主动指出"这个听起来合理的规律，历史上其实不成立"。
    `verify_pattern` 的实测记录正是这么抓出范例《消费板块轮动》的幸存者偏差的
    （该范例从 15 次里挑了 3 次支持自己的时点来论证）。
    """
    hp, note = _pattern(p)
    if hp is None:
        return None, note, {}
    if not hp.样本充足:
        return None, (f"{note}；但历史仅 {hp.occurrences} 次，样本不足以证伪"), {}
    ok = hp.胜率 <= TH["规律_证伪胜率"] or hp.样本外一致 is False
    return ok, _pattern_desc(hp, note), {
        "历史胜率": f"{hp.胜率:.1f}%（{hp.b_positive}/{hp.occurrences}）",
        "同类情形平均涨跌": f"{hp.b_avg:+.2f}%"}


def _peer_cmp(p, metric: str):
    """同业对比的公共取数。返回 PeerCompare 或 None。"""
    _code, sector = _rot_ctx(p)
    if not sector:
        return None
    from . import peers

    r = peers.compare(sector, metric)
    return r if r.ok else None


def _j_peer_discount(p):
    """V3 相对估值折价：本板块 PE 显著低于同业中位数。"""
    r = _peer_cmp(p, "PE")
    if r is None:
        return None, "同业对比不可用（板块未配置可比组或样本不足）", {}
    ok = r.相对偏离 is not None and r.相对偏离 <= -TH["同业偏离_显著pct"]
    return ok, (f"本板块 PE {r.本值:.1f} vs 同业中位 {r.同业中位数:.1f}"
                f"（{r.样本数}个可比），偏离 {r.相对偏离:+.1f}%"), {
        "本板块PE": f"{r.本值:.1f}倍", "同业中位PE": f"{r.同业中位数:.1f}倍",
        "同业PE偏离": f"{r.相对偏离:+.1f}%", "可比样本数": f"{r.样本数}个"}


def _j_peer_premium(p):
    """V3b 相对估值溢价。"""
    r = _peer_cmp(p, "PE")
    if r is None:
        return None, "同业对比不可用（板块未配置可比组或样本不足）", {}
    ok = r.相对偏离 is not None and r.相对偏离 >= TH["同业偏离_显著pct"]
    return ok, (f"本板块 PE {r.本值:.1f} vs 同业中位 {r.同业中位数:.1f}"
                f"（{r.样本数}个可比），偏离 {r.相对偏离:+.1f}%"), {
        "本板块PE": f"{r.本值:.1f}倍", "同业中位PE": f"{r.同业中位数:.1f}倍",
        "同业PE偏离": f"{r.相对偏离:+.1f}%", "可比样本数": f"{r.样本数}个"}


def _j_growth_spread(p):
    """E3 盈利剪刀差走阔：本板块净利增速显著高于同业。"""
    r = _peer_cmp(p, "归母净利同比")
    if r is None:
        return None, "同业对比不可用（板块未配置可比组或样本不足）", {}
    # 增速用绝对差（百分点）而非相对偏离——同业中位接近 0 时相对偏离会失真
    ok = r.差值 is not None and r.差值 >= TH["增速差_显著pct"]
    return ok, (f"本板块净利同比 {r.本值:+.1f}% vs 同业中位 {r.同业中位数:+.1f}%"
                f"（{r.样本数}个可比），领先 {r.差值:+.1f}pct"), {
        "本板块净利增速": f"{r.本值:+.1f}%", "同业中位增速": f"{r.同业中位数:+.1f}%",
        "同业增速差": f"{r.差值:+.1f}pct", "可比样本数": f"{r.样本数}个"}


def _j_growth_spread_narrow(p):
    """E3b 盈利剪刀差收窄/逆转：本板块增速已落后于同业。"""
    r = _peer_cmp(p, "归母净利同比")
    if r is None:
        return None, "同业对比不可用（板块未配置可比组或样本不足）", {}
    ok = r.差值 is not None and r.差值 <= -TH["增速差_显著pct"]
    return ok, (f"本板块净利同比 {r.本值:+.1f}% vs 同业中位 {r.同业中位数:+.1f}%"
                f"（{r.样本数}个可比），落后 {r.差值:+.1f}pct"), {
        "本板块净利增速": f"{r.本值:+.1f}%", "同业中位增速": f"{r.同业中位数:+.1f}%",
        "同业增速差": f"{r.差值:+.1f}pct", "可比样本数": f"{r.样本数}个"}


# id → 判定函数。未注册者需 LLM 定性判断或等数据接入。
# ⚠ 同一判据不重复注册给多个论点——否则会产出语义重复的论点（实测 F3b/F4b 同时触发且说明相同）。
# ---- 拥挤度与情绪（S5/S6/S8/S8b）----
# 口径说明：换手率/成交额分位取自**代表标的**（与波动率、PB分位一致），
# 非板块整体。板块级换手需逐只聚合日频序列，成本与收益不成比例。

def _j_crowded(p):
    q = _val(p, "换手率历史分位")
    if q is None:
        return None, "缺 换手率历史分位", {}
    ok = q > TH["拥挤_过热分位"]
    return ok, f"换手率处历史 {q:.1f}% 分位（过热阈值 {TH['拥挤_过热分位']:.0f}%）", {
        "换手率历史分位": _disp(p, "换手率历史分位")}


def _j_crowd_released(p):
    """拥挤已释放：现在退到中低分位，**且近3个月确实到过高位**。

    只看"当前分位低"会把长期冷清的板块也判成"拥挤已释放"——那是两回事。
    """
    q = _val(p, "换手率历史分位")
    hi = _val(p, "换手率近期高分位")
    if q is None or hi is None:
        return None, "缺 换手率分位或近期高分位", {}
    ok = q < TH["拥挤_释放分位"] and hi > TH["拥挤_曾过热分位"]
    return ok, (f"换手率已回落至 {q:.1f}% 分位，近3个月峰值曾达 {hi:.1f}% 分位"), {
        "换手率历史分位": _disp(p, "换手率历史分位")}


def _j_sentiment_freeze(p):
    q = _val(p, "成交额历史分位")
    if q is None:
        return None, "缺 成交额历史分位", {}
    ok = q < TH["情绪_冰点分位"]
    return ok, f"成交额处历史 {q:.1f}% 分位（冰点阈值 {TH['情绪_冰点分位']:.0f}%）", {
        "成交额历史分位": _disp(p, "成交额历史分位")}


def _j_sentiment_hot(p):
    q = _val(p, "成交额历史分位")
    if q is None:
        return None, "缺 成交额历史分位", {}
    ok = q > TH["情绪_过热分位"]
    return ok, f"成交额处历史 {q:.1f}% 分位（过热阈值 {TH['情绪_过热分位']:.0f}%）", {
        "成交额历史分位": _disp(p, "成交额历史分位")}


# ---- 盈利趋势（E2/E2b）：看形状，不看单期水平 ----

def _profit_trend(p):
    sector = p.get("__sector__")
    if not sector:
        return None
    from . import fundamentals as fd
    try:
        t = fd.sector_profit_trend(sector)
    except Exception:
        return None
    return t if t.ok else None


def _j_turnaround(p):
    """困境反转：整体法增速由负转正。"""
    t = _profit_trend(p)
    if t is None:
        return None, "缺 板块盈利多期序列", {}
    g = t.增速序列                       # 由近及远
    if len(g) < 2:
        return None, "盈利序列不足2期", {}
    ok = g[0] > 0 and g[1] < 0
    return ok, (f"最新 {t.序列[0]['报告期']} 增速 {g[0]:+.1f}%，上期 {g[1]:+.1f}%"), {
        "板块盈利增速序列": " → ".join(f"{x:+.1f}%" for x in reversed(g))}


def _j_growth_peak(p):
    """增速见顶：连续 N 期回落，且累计回落幅度显著。

    只要"连降"不够——从 63.8% 降到 61.2% 也是连降，但那是噪声不是见顶。
    """
    t = _profit_trend(p)
    if t is None:
        return None, "缺 板块盈利多期序列", {}
    g = t.增速序列                       # 由近及远：g[0] 最新
    n = int(TH["增速见顶_连降期数"])
    if len(g) < n + 1:
        return None, f"盈利序列不足{n + 1}期", {}
    # 由近及远递增 == 由远及近递减 == 连续回落
    falling = all(g[i] < g[i + 1] for i in range(n))
    drop = g[n] - g[0]
    ok = falling and drop > TH["增速见顶_累计降pct"]
    return ok, (f"增速 {' → '.join(f'{x:+.1f}%' for x in reversed(g[:n + 1]))}"
                f"，累计回落 {drop:.1f}个百分点"), {
        "板块盈利增速序列": " → ".join(f"{x:+.1f}%" for x in reversed(g))}


# ---- 业绩披露窗口（E4）：纯日历，不依赖任何行情数据 ----

def _j_report_window(p):
    import datetime as _dt

    today = _dt.date.today()
    best, label = None, ""
    for mo, day, name in _REPORT_DEADLINES:
        d = _dt.date(today.year, mo, day)
        if d < today:
            d = _dt.date(today.year + 1, mo, day)
        gap = (d - today).days
        if best is None or gap < best:
            best, label = gap, f"{name}（{d.isoformat()}）"
    ok = best <= TH["业绩窗口_天"]
    return ok, f"距{label}还有 {best} 天（窗口阈值 {int(TH['业绩窗口_天'])} 天）", {
        "距披露截止日": f"{best}天", "披露节点": label}


# ---- 解禁（F5/F5b/C6）----

def _unlock(p):
    sector = p.get("__sector__")
    if not sector:
        return None
    from . import unlock as ul
    try:
        u = ul.sector_unlock(sector)
    except Exception:
        return None
    return u if u.ok else None


def _j_unlock_passed(p):
    u = _unlock(p)
    if u is None or u.倍数 is None:
        return None, "缺 解禁数据", {}
    ok = u.倍数 < TH["解禁_已过倍数"]
    return ok, (f"未来3个月解禁月均为后9个月的 {u.倍数:.2f}倍"
                f"（已过阈值 <{TH['解禁_已过倍数']}）"), {
        "未来3月解禁": f"{u.近端金额 / 1e8:.1f}亿元",
        "解禁占板块市值": f"{u.占流通市值:.2f}%",
        "解禁月均倍数": f"{u.倍数:.2f}倍"}


def _j_unlock_peak(p):
    """解禁高峰临近：**规模够大**且集中在近端。两个条件缺一不可——

    实测白酒未来3月解禁 0.1 亿（占市值 0.00%）却因后9个月近乎为零算出 11x 倍数，
    若只看倍数会误判成"高峰临近"。
    """
    u = _unlock(p)
    if u is None or u.倍数 is None or u.占流通市值 is None:
        return None, "缺 解禁数据", {}
    ok = u.占流通市值 >= TH["解禁_显著占比pct"] and u.倍数 > TH["解禁_高峰倍数"]
    top = u.明细[0] if u.明细 else None
    return ok, (f"未来3个月解禁 {u.近端金额 / 1e8:.1f}亿元，占板块市值 {u.占流通市值:.2f}%，"
                f"月均为后9个月的 {u.倍数:.2f}倍"
                + (f"；最大单笔 {top['简称']} {top['金额'] / 1e8:.1f}亿（{top['日期']}）"
                   if top else "")), {
        "未来3月解禁": f"{u.近端金额 / 1e8:.1f}亿元",
        "解禁占板块市值": f"{u.占流通市值:.2f}%",
        "解禁月均倍数": f"{u.倍数:.2f}倍"}


def _j_unlock_window(p):
    """解禁窗口（中性事件）：规模显著即提示，不预设方向。"""
    u = _unlock(p)
    if u is None or u.占流通市值 is None:
        return None, "缺 解禁数据", {}
    ok = u.占流通市值 >= TH["解禁_显著占比pct"]
    days = ""
    if u.明细:
        days = f"，最近节点 {min(x['日期'] for x in u.明细)}"
    return ok, (f"未来3个月解禁 {u.近端金额 / 1e8:.1f}亿元，"
                f"占板块市值 {u.占流通市值:.2f}%（提示阈值 {TH['解禁_显著占比pct']}%）{days}"), {
        "未来3月解禁": f"{u.近端金额 / 1e8:.1f}亿元",
        "解禁占板块市值": f"{u.占流通市值:.2f}%",
        "解禁月均倍数": f"{u.倍数:.2f}倍"}


# ---- 宏观利率（M1/M2）：市场级论点，与标的无关，故不依赖任何 profile 字段（同 E4）----

def _rate(p):
    """10Y 国债收益率走势。取数失败/身份核对不通过时返回 None，不硬撑。"""
    from . import macro
    try:
        r = macro.rate_10y(lookback_days=int(TH["利率_窗口天"]))
    except Exception:
        return None
    return r if r.ok else None


def _rate_evidence(r) -> dict[str, str]:
    return {
        "10Y国债收益率": f"{r.当前值:.2f}%",
        "10Y国债收益率变动": f"{r.变动bp:+.1f}bp",
        "10Y国债收益率分位": f"{r.分位:.1f}%",
    }


def _j_rate_down(p):
    """M1 利率下行利好估值——DDM 分母端下移。"""
    r = _rate(p)
    if r is None:
        return None, "缺 10Y国债收益率", {}
    th = TH["利率_显著变动bp"]
    ok = r.变动bp <= -th
    return ok, (f"10Y国债收益率近{r.窗口天}天 {r.前值:.2f}% → {r.当前值:.2f}%"
                f"（{r.变动bp:+.1f}bp，阈值 {th:.0f}bp）；当前处历史 {r.分位:.1f}% 分位"), \
        _rate_evidence(r)


def _j_rate_up(p):
    """M2 利率上行压制估值——成长股对分母端更敏感。"""
    r = _rate(p)
    if r is None:
        return None, "缺 10Y国债收益率", {}
    th = TH["利率_显著变动bp"]
    ok = r.变动bp >= th
    return ok, (f"10Y国债收益率近{r.窗口天}天 {r.前值:.2f}% → {r.当前值:.2f}%"
                f"（{r.变动bp:+.1f}bp，阈值 {th:.0f}bp）；当前处历史 {r.分位:.1f}% 分位"), \
        _rate_evidence(r)


# ---- 流动性 / 汇率 / 通胀 / 景气（M3~M7）----

def _mtrend(key, days):
    from . import macro
    try:
        r = macro.trend(key, lookback_days=int(days))
    except Exception:
        return None
    return r if r.ok else None


def _liquidity(p):
    """返回 (M2同比走势, 社融同比走势)，任一缺失则为 None。"""
    d = TH["流动性_窗口天"]
    m2, sf = _mtrend("M2同比", d), _mtrend("社融存量同比", d)
    return (m2, sf) if (m2 and sf) else (None, None)


def _j_liquidity_easing(p):
    """M3 流动性宽松：M2 与社融增速**同向回升**，且平均升幅达阈值。

    要求同向的原因：单看一个指标容易被单月扰动带偏（社融受地方债发行节奏影响尤甚），
    两者同向才配称"流动性环境"变化。
    """
    m2, sf = _liquidity(p)
    if m2 is None:
        return None, "缺 M2/社融同比", {}
    th = TH["流动性_显著变动pct"]
    avg = (m2.变动 + sf.变动) / 2
    ok = m2.变动 > 0 and sf.变动 > 0 and avg >= th
    return ok, (f"近{m2.窗口天}天 M2同比 {m2.前值:.1f}%→{m2.当前值:.1f}%（{m2.变动:+.1f}pct）、"
                f"社融同比 {sf.前值:.1f}%→{sf.当前值:.1f}%（{sf.变动:+.1f}pct），"
                f"均值 {avg:+.1f}pct（阈值 {th}pct 且需同向）"), {
        "M2同比": f"{m2.当前值:.1f}%", "M2同比变动": f"{m2.变动:+.1f}pct",
        "社融存量同比": f"{sf.当前值:.1f}%", "社融同比变动": f"{sf.变动:+.1f}pct",
        "流动性变动均值": f"{avg:+.1f}pct"}


def _j_liquidity_tightening(p):
    """M3b 流动性收紧：M2 与社融增速同向回落。"""
    m2, sf = _liquidity(p)
    if m2 is None:
        return None, "缺 M2/社融同比", {}
    th = TH["流动性_显著变动pct"]
    avg = (m2.变动 + sf.变动) / 2
    ok = m2.变动 < 0 and sf.变动 < 0 and avg <= -th
    return ok, (f"近{m2.窗口天}天 M2同比 {m2.前值:.1f}%→{m2.当前值:.1f}%（{m2.变动:+.1f}pct）、"
                f"社融同比 {sf.前值:.1f}%→{sf.当前值:.1f}%（{sf.变动:+.1f}pct），"
                f"均值 {avg:+.1f}pct（阈值 -{th}pct 且需同向）"), {
        "M2同比": f"{m2.当前值:.1f}%", "M2同比变动": f"{m2.变动:+.1f}pct",
        "社融存量同比": f"{sf.当前值:.1f}%", "社融同比变动": f"{sf.变动:+.1f}pct",
        "流动性变动均值": f"{avg:+.1f}pct"}


def _fx(p):
    return _mtrend("美元兑人民币", TH["汇率_窗口天"])


def _fx_pct(r) -> float:
    """美元兑人民币的百分比变动。上升 = 人民币贬值。"""
    return (r.当前值 / r.前值 - 1) * 100


def _j_fx_depreciation(p):
    """M4 汇率压力→外资流出：人民币贬值（美元兑人民币上行）。"""
    r = _fx(p)
    if r is None:
        return None, "缺 美元兑人民币", {}
    pct, th = _fx_pct(r), TH["汇率_显著变动pct"]
    ok = pct >= th
    return ok, (f"美元兑人民币近{r.窗口天}天 {r.前值:.4f}→{r.当前值:.4f}"
                f"（人民币{'贬值' if pct > 0 else '升值'} {abs(pct):.2f}%，阈值 {th}%）"), {
        "美元兑人民币": f"{r.当前值:.4f}", "美元兑人民币前值": f"{r.前值:.4f}",
        "人民币近期变动": f"{-pct:+.2f}%"}


def _j_fx_appreciation(p):
    """M4b 汇率企稳→外资回流：人民币升值（美元兑人民币下行）。"""
    r = _fx(p)
    if r is None:
        return None, "缺 美元兑人民币", {}
    pct, th = _fx_pct(r), TH["汇率_显著变动pct"]
    ok = pct <= -th
    return ok, (f"美元兑人民币近{r.窗口天}天 {r.前值:.4f}→{r.当前值:.4f}"
                f"（人民币{'升值' if pct < 0 else '贬值'} {abs(pct):.2f}%，阈值 {th}%）"), {
        "美元兑人民币": f"{r.当前值:.4f}", "美元兑人民币前值": f"{r.前值:.4f}",
        "人民币近期变动": f"{-pct:+.2f}%"}


def _j_inflation_regime(p):
    """M6 通胀/通缩环境：CPI/PPI 离开中性区间。方向=分化，故说明里必须点明是哪一种。"""
    ppi, cpi = _mtrend("PPI当月同比", 90), _mtrend("CPI当月同比", 90)
    if ppi is None or cpi is None:
        return None, "缺 CPI/PPI 同比", {}
    tp, tc = TH["通胀_PPI显著pct"], TH["通胀_CPI高pct"]
    label = ""
    if ppi.当前值 >= tp:
        label = "上游涨价（PPI 高位）—— 利好上游资源，压制中下游毛利"
    elif ppi.当前值 <= -tp:
        label = "工业品通缩（PPI 深负）—— 上游承压，下游成本改善"
    elif cpi.当前值 >= tc:
        label = "消费通胀显性（CPI 高位）"
    elif cpi.当前值 < 0:
        label = "消费端通缩（CPI 转负）"
    ok = bool(label)
    return ok, (f"PPI同比 {ppi.当前值:+.1f}%、CPI同比 {cpi.当前值:+.1f}%"
                + (f"；{label}" if label else
                   f"；均处中性区间（PPI 阈值±{tp}%、CPI 阈值 {tc}%/0%）")), {
        "PPI当月同比": f"{ppi.当前值:+.1f}%", "CPI当月同比": f"{cpi.当前值:+.1f}%"}


def _j_pmi_deviation(p):
    """M7 经济景气偏离（**近似**：以制造业 PMI 偏离荣枯线 50 代替"偏离一致预期"）。

    ⚠ 口径妥协：论点原义是"经济数据超预期/不及预期"，而"超预期"需要**市场一致预期**，
    iFinD EDB 无此数据。故退而判定"PMI 相对荣枯线的偏离"——它是景气扩张/收缩的客观刻画，
    但**不等于**超预期。已在论点库该行注明，待投研复核是否接受该近似（同 F3b 的处理方式）。
    """
    r = _mtrend("制造业PMI", 90)
    if r is None:
        return None, "缺 制造业PMI", {}
    dev, th = r.当前值 - 50.0, TH["PMI_荣枯偏离"]
    ok = abs(dev) >= th
    zone = "扩张" if dev > 0 else "收缩"
    return ok, (f"制造业PMI {r.当前值:.1f}（{r.当前日期}），偏离荣枯线 {dev:+.1f}"
                f"，处{zone}区间（阈值 ±{th}）"), {
        "制造业PMI": f"{r.当前值:.1f}", "PMI偏离荣枯线": f"{dev:+.1f}"}


# ---- E5/E5b ROE杜邦改善/恶化 ----

def _dupont(p):
    sector = p.get("__sector__")
    if not sector:
        return None
    from . import aggregate as ag
    try:
        d = ag.sector_dupont(sector)
    except Exception:
        return None
    return d if d.ok else None


def _j_dupont_up(p):
    """E5 ROE杜邦改善：ROE同比上行 且 净利率同向上行（改善可归因于盈利能力，非纯杠杆）。

    ⚠ 简化：不做完整三项杜邦分解（净利率×周转率×权益乘数）——权益乘数/周转率
    所需的"总资产"科目反复实测取不到可靠的批量指标代码。用 ROE+净利率两维
    仍能忠实原意的核心诉求："能否归因于盈利能力"，只是无法进一步区分是
    净利率还是周转率驱动。
    """
    d = _dupont(p)
    if d is None:
        return None, "缺 板块杜邦分项数据", {}
    dn, dr = d.ROE变动, d.净利率变动
    if dn is None or dr is None:
        return None, "ROE或净利率变动缺失", {}
    ok = dn > TH["ROE_显著变动pct"] and dr > 0
    return ok, (f"ROE同比 {d.ROE_去年同期:.2f}%→{d.ROE_本期:.2f}%（{dn:+.2f}pct），"
                f"净利率同比 {d.净利率_去年同期:.2f}%→{d.净利率_本期:.2f}%（{dr:+.2f}pct）"), {
        "板块ROE": f"{d.ROE_本期:.2f}%", "板块ROE去年同期": f"{d.ROE_去年同期:.2f}%",
        "板块ROE变动": f"{dn:+.2f}pct",
        "板块净利率": f"{d.净利率_本期:.2f}%", "板块净利率去年同期": f"{d.净利率_去年同期:.2f}%",
        "板块净利率变动": f"{dr:+.2f}pct"}


def _j_dupont_down(p):
    """E5b ROE恶化：ROE同比下行。（"或仅靠加杠杆维持"这一更细粒度诊断因缺权益乘数数据未实现）"""
    d = _dupont(p)
    if d is None:
        return None, "缺 板块杜邦分项数据", {}
    dn = d.ROE变动
    if dn is None:
        return None, "ROE变动缺失", {}
    ok = dn < -TH["ROE_显著变动pct"]
    # 证据须与 E5 对称给全两期值与变动量。此前只给"板块ROE"一个当期值，
    # 说明串里明明写着 23.62%→21.34%（-2.27pct），writer 却只收到 21.34%——
    # 于是它如实写下"缺少同比数据，无法证实盈利质量恶化"，整条逻辑自我否定，
    # 而数据其实一直都在（#67，与 #60 同类：判定用得到、下游收不到）。
    ev = {
        "板块ROE": f"{d.ROE_本期:.2f}%", "板块ROE去年同期": f"{d.ROE_去年同期:.2f}%",
        "板块ROE变动": f"{dn:+.2f}pct",
    }
    dr = d.净利率变动
    if dr is not None:
        ev.update({
            "板块净利率": f"{d.净利率_本期:.2f}%",
            "板块净利率去年同期": f"{d.净利率_去年同期:.2f}%",
            "板块净利率变动": f"{dr:+.2f}pct",
        })
    return ok, f"ROE同比 {d.ROE_去年同期:.2f}%→{d.ROE_本期:.2f}%（{dn:+.2f}pct）", ev


# ---- E6/E6b 毛利率拐点 ----

def _margin(p):
    sector = p.get("__sector__")
    if not sector:
        return None
    from . import fundamentals as fd
    try:
        m = fd.sector_gross_margin(sector)
    except Exception:
        return None
    return m if m.ok else None


def _j_margin_up(p):
    m = _margin(p)
    if m is None:
        return None, "缺 板块毛利率数据（或该板块不适用毛利率概念，如金融业）", {}
    chg = m.变动
    if chg is None:
        return None, "毛利率变动缺失", {}
    ok = chg > TH["毛利率_显著变动pct"]
    return ok, f"毛利率同比 {m.去年同期:.2f}%→{m.本期:.2f}%（{chg:+.2f}pct）", {
        "板块毛利率": f"{m.本期:.2f}%", "板块毛利率去年同期": f"{m.去年同期:.2f}%",
        "板块毛利率变动": f"{chg:+.2f}pct"}


def _j_margin_down(p):
    m = _margin(p)
    if m is None:
        return None, "缺 板块毛利率数据（或该板块不适用毛利率概念，如金融业）", {}
    chg = m.变动
    if chg is None:
        return None, "毛利率变动缺失", {}
    ok = chg < -TH["毛利率_显著变动pct"]
    # 与 E6 对称给全两期值（同 E5b 的 #67 修复）：只给当期值时，
    # writer 拿不到基期，"同比恶化"这条论证就没法落到数字上。
    return ok, f"毛利率同比 {m.去年同期:.2f}%→{m.本期:.2f}%（{chg:+.2f}pct）", {
        "板块毛利率": f"{m.本期:.2f}%", "板块毛利率去年同期": f"{m.去年同期:.2f}%",
        "板块毛利率变动": f"{chg:+.2f}pct"}


# ---- F7/F7b ETF份额 ----

def _etf(p):
    sector = p.get("__sector__")
    if not sector:
        return None
    from . import flows as fl
    try:
        t = fl.etf_share_trend(sector)
    except Exception:
        return None
    return t if t.ok else None


def _j_etf_grow(p):
    t = _etf(p)
    if t is None:
        return None, "缺 ETF份额数据", {}
    pct = t.变动pct
    if pct is None:
        return None, "ETF份额变动缺失", {}
    ok = pct > TH["ETF份额_显著变动pct"]
    return ok, f"{t.代码} 近{t.窗口天}天份额 {t.前值/1e8:.1f}亿份→{t.当前值/1e8:.1f}亿份（{pct:+.1f}%）", {
        "代表ETF份额": f"{t.当前值/1e8:.1f}亿份",
        "代表ETF份额前值": f"{t.前值/1e8:.1f}亿份", "代表ETF份额变动": f"{pct:+.1f}%"}


def _j_etf_shrink(p):
    t = _etf(p)
    if t is None:
        return None, "缺 ETF份额数据", {}
    pct = t.变动pct
    if pct is None:
        return None, "ETF份额变动缺失", {}
    ok = pct < -TH["ETF份额_显著变动pct"]
    return ok, f"{t.代码} 近{t.窗口天}天份额 {t.前值/1e8:.1f}亿份→{t.当前值/1e8:.1f}亿份（{pct:+.1f}%）", {
        "代表ETF份额": f"{t.当前值/1e8:.1f}亿份",
        "代表ETF份额前值": f"{t.前值/1e8:.1f}亿份", "代表ETF份额变动": f"{pct:+.1f}%"}


# ---- F8/F8b 机构持仓历史分位 ----

def _inst(p):
    sector = p.get("__sector__")
    if not sector:
        return None
    from . import fundamentals as fd
    try:
        r = fd.sector_institution_percentile(sector)
    except Exception:
        return None
    return r if r.ok else None


def _j_inst_underweight(p):
    r = _inst(p)
    if r is None:
        return None, "缺 板块机构持仓历史序列", {}
    ok = r.分位 < TH["机构持仓_低配分位"]
    return ok, (f"机构持股比例 {r.当前值:.1f}%，处近{r.样本数}个季度 {r.分位:.1f}% 分位"
                f"（低配阈值 <{TH['机构持仓_低配分位']:.0f}%）"), {
        "机构持股比例": f"{r.当前值:.1f}%", "机构持仓历史分位": f"{r.分位:.1f}%",
        "机构持仓样本期数": f"{r.样本数}个季度"}


def _j_inst_overweight(p):
    r = _inst(p)
    if r is None:
        return None, "缺 板块机构持仓历史序列", {}
    ok = r.分位 > TH["机构持仓_超配分位"]
    return ok, (f"机构持股比例 {r.当前值:.1f}%，处近{r.样本数}个季度 {r.分位:.1f}% 分位"
                f"（超配阈值 >{TH['机构持仓_超配分位']:.0f}%）"), {
        "机构持股比例": f"{r.当前值:.1f}%", "机构持仓历史分位": f"{r.分位:.1f}%",
        "机构持仓样本期数": f"{r.样本数}个季度"}


# ---- F9 两融余额 ----

def _j_margin_balance(p):
    """F9 融资余额变化：双向，不预设方向（论点库原文"方向=按变化方向；确定性=低"）。"""
    code = p.get("__code__")
    if not code:
        return None
    from . import flows as fl
    try:
        t = fl.margin_balance_trend(code)
    except Exception:
        return None
    if not t.ok:
        return None, "缺 两融余额数据", {}
    pct = t.变动pct
    if pct is None:
        return None, "两融余额变动缺失", {}
    ok = abs(pct) > TH["两融余额_显著变动pct"]
    direction = "上升" if pct > 0 else "下降"
    return ok, (f"{code} 两融余额近{t.窗口天}天 {t.前值/1e8:.1f}亿→{t.当前值/1e8:.1f}亿"
                f"（{direction} {abs(pct):.1f}%，阈值 {TH['两融余额_显著变动pct']:.0f}%）"), {
        "两融余额": f"{t.当前值/1e8:.1f}亿元", "两融余额前值": f"{t.前值/1e8:.1f}亿元",
        "两融余额变动": f"{pct:+.1f}%"}


# ---- C3/C3b 业绩预告 ----

def _preann(p):
    code = p.get("__code__")
    if not code:
        return None
    from . import flows as fl
    try:
        r = fl.earnings_preannouncement(code)
    except Exception:
        return None
    return r if r.ok else None


def _j_preann_beat(p):
    r = _preann(p)
    if r is None:
        return None, "缺 业绩预告数据", {}
    if not r.类型:
        return False, "本期无业绩预告", {}
    from . import flows as fl
    ok = r.类型 in fl._BULLISH_TYPES
    chg = f"，变动幅度 {r.净利润变动幅度:+.1f}%" if r.净利润变动幅度 is not None else ""
    ev = {"业绩预告类型": r.类型}
    if r.净利润变动幅度 is not None:      # 幅度是这条论点唯一的量化依据，不能只留在说明串里
        ev["业绩预告净利变动幅度"] = f"{r.净利润变动幅度:+.1f}%"
    return ok, f"业绩预告类型「{r.类型}」（{r.报告期}）{chg}", ev


def _j_preann_miss(p):
    r = _preann(p)
    if r is None:
        return None, "缺 业绩预告数据", {}
    if not r.类型:
        return False, "本期无业绩预告", {}
    from . import flows as fl
    ok = r.类型 in fl._BEARISH_TYPES
    chg = f"，变动幅度 {r.净利润变动幅度:+.1f}%" if r.净利润变动幅度 is not None else ""
    ev = {"业绩预告类型": r.类型}
    if r.净利润变动幅度 is not None:
        ev["业绩预告净利变动幅度"] = f"{r.净利润变动幅度:+.1f}%"
    return ok, f"业绩预告类型「{r.类型}」（{r.报告期}）{chg}", ev


# ---- V7 估值切换（预测PE） ----

def _j_valuation_switch(p):
    """V7 估值切换：FY1 预测PE 显著低于当前PE(TTM)，两者同一整体法口径(Σ市值/Σ净利润)。

    ⚠ 简化：论点原文含"临近年末"这一时点限定，本判定未加日历门槛——
    任何时点只要预测折价显著都触发，理由是该信号本身价值不因季节而失效，
    "临近年末"更多是研报讨论惯例而非数据必要条件。如需还原，可仿 E4 加天数窗口。
    """
    sector = p.get("__sector__")
    if not sector:
        return None
    from . import aggregate as ag
    try:
        r = ag.sector_forward_pe(sector)
    except Exception:
        return None
    if not r.ok:
        return None, r.error or "缺 预测PE数据", {}
    disc = r.折价pct
    if disc is None:
        return None, "折价计算失败", {}
    ok = disc > TH["预测PE_显著折价pct"]
    return ok, (f"当前PE(TTM,整体法) {r.当前PE:.2f}倍，FY1预测PE {r.FY1预测PE:.2f}倍"
                f"（折价 {disc:.1f}%，阈值 {TH['预测PE_显著折价pct']:.0f}%）"), {
        "当前PE": f"{r.当前PE:.2f}倍", "FY1预测PE": f"{r.FY1预测PE:.2f}倍",
        # 折价率必须进证据。只给两个 PE 值时，writer 想表达"差多少"就只能自己算——
        # 实测它把 29.87 与 23.21 写成"相差逾6倍"：既是编造（6 不在任何字段里），
        # 又是**表述错误**（投研语境下"相差6倍"指倍数关系，实际只差 6.66 个单位）。
        # 算好了就一并给出去，别让下游去推。
        "FY1较当前折价": f"{disc:.1f}%"}


_JUDGES: dict[str, Callable] = {
    "V1": _j_pb_low, "V1b": _j_pb_high, "V8": _j_bubble,
    "V2": _j_div_pos, "V2b": _j_div_neg,
    "E1": _j_profit_up, "E1b": _j_profit_down,
    "F1": _j_flow_in_vs_fall, "F1b": _j_flow_out_vs_rise,
    "F2": _j_flow_in_with_rise, "F2b": _j_flow_out_with_fall,
    "F3b": _j_holder_net_sell,   # 卖压加剧（近似：净减持显著）
    "F3": _j_holder_relief,      # 卖压释放（近3月 vs 前9月）
    "F4": _j_holder_net_buy,     # 净增持信号
    "F4b": _j_holder_turn_sell,  # 净减持转向（前增持→近减持）
    "S1": _j_vol_high, "S2": _j_vol_low,
    "S3": _j_crash_highvol, "S4": _j_vol_reversion,
    "S7": _j_oversold, "S7b": _j_overbought,
    "R1": _j_rotation, "R6": _j_excess_return,
    "R1b": _j_rotation_reverse,
    "R2": _j_pattern_verified, "R2b": _j_pattern_falsified,
    "R3": _j_size_switch, "R4": _j_growth_value_switch, "R5": _j_leader_divergence,
    "V3": _j_peer_discount, "V3b": _j_peer_premium,
    "E3": _j_growth_spread, "E3b": _j_growth_spread_narrow,
    # ↓ 本批新增（#34）：补空白类别与"看形状"类论点
    "S5": _j_crowded, "S6": _j_crowd_released,
    "S8": _j_sentiment_freeze, "S8b": _j_sentiment_hot,
    "E2": _j_turnaround, "E2b": _j_growth_peak,
    "E4": _j_report_window,
    "F5": _j_unlock_passed, "F5b": _j_unlock_peak,
    "C6": _j_unlock_window,       # 事件/催化类由此破零
    # ↓ #36：宏观/流动性类由此破零
    "M1": _j_rate_down, "M2": _j_rate_up,
    # ↓ #37：拿到 EDB 指标 ID 后补齐。M5 宏观传导链路不做（定性推理链，不适合规则化）
    "M3": _j_liquidity_easing, "M3b": _j_liquidity_tightening,
    "M4": _j_fx_depreciation, "M4b": _j_fx_appreciation,
    "M6": _j_inflation_regime,
    "M7": _j_pmi_deviation,       # 近似：PMI 偏离荣枯线，非"偏离一致预期"
    # ↓ #38：补齐"数据源有未接"的剩余论点（E5/E6/F7/F8/F9/C3/V7）
    "E5": _j_dupont_up, "E5b": _j_dupont_down,
    "E6": _j_margin_up, "E6b": _j_margin_down,
    "F7": _j_etf_grow, "F7b": _j_etf_shrink,
    "F8": _j_inst_underweight, "F8b": _j_inst_overweight,
    "F9": _j_margin_balance,
    "C3": _j_preann_beat, "C3b": _j_preann_miss,
    "V7": _j_valuation_switch,
}


# ---------------- 字段依赖与渲染属性（本模块是这两项的唯一权威，见 DESIGN §7.3）----------------

# 论点 → 判定所需的 **schema 字段名**。
# 为什么在代码里声明而非从 THESIS_LIBRARY.md 解析：文档"所需数据"列是给人看的自然语言
# （如"PB/PE 及历史分位"），而判定函数读的是精确字段名（如"PB历史分位"），两者对不上。
# 判定函数自己才知道真正依赖什么，故与判定函数并列维护。
# 空列表表示该论点不依赖 profile 字段（走 peers/rotation，只需 __sector__/__code__）。
_TRIGGER_FIELDS: dict[str, list[str]] = {
    "V1": ["PB历史分位"], "V1b": ["PB历史分位"], "V8": ["PB历史分位"],
    "V2": ["板块区间涨跌幅", "归母净利同比"], "V2b": ["板块区间涨跌幅", "归母净利同比"],
    "V3": [], "V3b": [],                       # 走 peers（需 __sector__）
    "E1": ["归母净利同比"], "E1b": ["归母净利同比"],
    "E3": [], "E3b": [],                       # 走 peers
    "F1": ["板块区间涨跌幅", "板块资金净流入"], "F1b": ["板块区间涨跌幅", "板块资金净流入"],
    "F2": ["板块区间涨跌幅", "板块资金净流入"], "F2b": ["板块区间涨跌幅", "板块资金净流入"],
    "F3b": ["减持规模"], "F4": ["减持规模"],
    # F3/F4b 走 __sector__ 自取两窗口趋势，R1b 走 __code__，均不声明 schema 字段
    "S1": ["波动率历史分位", "年化波动率"], "S2": ["波动率历史分位", "年化波动率"],
    "S3": ["波动率历史分位", "区间涨跌幅分位", "PB历史分位"],
    "S4": ["波动率历史分位", "年化波动率"],
    "S7": ["区间涨跌幅分位"], "S7b": ["区间涨跌幅分位"],
    "R1": [], "R3": [], "R4": [], "R6": [],    # 走 rotation（需 __code__）
    "R5": ["板块区间涨跌幅"],                   # 龙头 vs 板块，板块侧取自 profile
    "S5": ["换手率历史分位"],
    "S6": ["换手率历史分位", "换手率近期高分位"],
    "S8": ["成交额历史分位"], "S8b": ["成交额历史分位"],
    "E2": [], "E2b": [],                       # 走 fundamentals（需 __sector__）
    "E4": [],                                  # 纯日历，无数据依赖
    "F5": [], "F5b": [], "C6": [],             # 走 unlock（需 __sector__）
    "M1": [], "M2": [],                        # 走 macro（市场级，与标的无关）
    "M3": [], "M3b": [], "M4": [], "M4b": [], "M6": [], "M7": [],
    # #38：均为板块级（走 aggregate/fundamentals/flows，需 __sector__）或
    # 代表标的级（走 flows，需 __code__），不依赖 profile 字段。
    "E5": [], "E5b": [], "E6": [], "E6b": [],
    "F7": [], "F7b": [], "F8": [], "F8b": [], "F9": [],
    "C3": [], "C3b": [], "V7": [],
}

# 标的画像字段：不参与触发判定，但 writer 写正文需要（估值/盈利/分红的具体数值）。
# 这是"数据先行"里"标的画像"概念的落地——保证报告有血有肉，而不只有触发结论。
PROFILE_FIELDS: list[str] = [
    "PB", "ROE", "现金分红总额", "分红率", "A股股息率", "板块领涨股",
]

# 论点类别 → 默认图表类型（渲染用图的唯一出处）。
# 只映射到 charts.py 已实现的三种（number_cards/table/bar），未实现的图型不产出规格。
# 类别默认图型 —— 只在 `_THESIS_CHART` 没有显式覆盖时生效。
#
# ⚠ **默认值宁可选"有对比关系"的图，不要选 number_cards/table**。
# 早期默认值大量用 number_cards（估值/盈利/情绪三类）与 table（其余四类），
# 理由是"它们能接受任意 {标签,值} 列表，最安全"。实测代价是：
# 一份成品三条逻辑三张图**全是数字卡**——把"当前 PE 29.87 vs 预测 PE 23.14"
# 这种对比关系抹平成两个孤立数字，读者看不出差距，图等于白配。
#
# `bar` 接受的数据形态与 `number_cards` 完全相同（都是 {标签,值} 列表），
# 但两根柱子的高低差是**看得见**的。故默认一律改 `bar`，
# 仅业绩预告类（值是"预增/略减"这类文本，画不成柱）保留 table。
_CATEGORY_CHART: dict[str, str] = {
    "估值类": "bar",
    "盈利/基本面类": "bar",
    "供需/产业格局类": "bar",
    "资金/筹码类": "bar",
    "情绪/交易类": "bar",
    "事件/催化类": "table",      # 业绩预告类型是文本，无法成柱
    "宏观/流动性类": "line",     # 宏观指标天然是时间序列
    "扩散/映射类": "bar",
    "轮动/相对强弱类": "bar",
}


# 论点级图型覆盖：有些论点的**论证形状**本身就指向特定图型，按类别给默认反而不对。
# 例：V2 量价背离论证的就是"两个方向相反的量"，双轴柱线一张图说清；
#     E2b 增速见顶论证的是"连续几期怎么走下来的"，非折线不可。
_THESIS_CHART: dict[str, str] = {
    "V2": "bar_line", "V2b": "bar_line",          # 涨跌 vs 盈利
    "F1": "bar_line", "F1b": "bar_line",          # 涨跌 vs 资金流
    "F2": "bar_line", "F2b": "bar_line",
    "R5": "bar_line",                             # 龙头 vs 板块
    # R2/R2b 用柱状图逐次列出历史同类情形下本标的的涨跌——
    # "15 次里只有 3 次为正"一眼可见，比任何文字都难以辩驳，也是证伪时最有力的呈现。
    "R2": "bar", "R2b": "bar",
    "R1b": "bar",                                 # 长短窗超额对比
    "F3": "bar", "F4b": "bar",                    # 前9月 vs 近3月月均
    "E2": "line", "E2b": "line",                  # 净利同比序列
    "M1": "line", "M2": "line",                   # 利率走势
    "M3": "line", "M3b": "line",                  # M2/社融同比走势
    "M4": "line", "M4b": "line",                  # 汇率走势
    "M7": "line",                                 # PMI 相对荣枯线
    # 分位类：这些论点的**整个论证就是"当前值处在历史什么位置"**，
    # 而 number_cards 只印一个"28.5%"，恰好把「位置」这个核心信息丢了。
    # gauge 用一条历史区间横条 + 当前位置 + 阈值线，把论据完整摆出来。
    "V1": "gauge", "V1b": "gauge", "V8": "gauge",          # 估值分位
    "S1": "gauge", "S2": "gauge", "S4": "gauge",           # 波动率分位
    # S3 同时用波动率分位与区间涨跌分位，两根标尺并排最能说清"急跌+高波动"这个组合，
    # 是全库最该用 gauge 的一条。首版遗漏，实测选中它时落回 number_cards 才发现。
    "S3": "gauge",
    "S5": "gauge", "S6": "gauge",                          # 拥挤度分位
    "S7": "gauge", "S7b": "gauge",                         # 区间涨跌分位
    "S8": "gauge", "S8b": "gauge",                         # 成交额分位
    "F8": "gauge", "F8b": "gauge",                         # 机构持仓分位
    # 同业对比类：论证的是"本板块 vs 同业"，两列并排比最直观
    "V3": "two_col", "V3b": "two_col",                     # 估值折溢价
    "E3": "two_col", "E3b": "two_col",                     # 盈利剪刀差
    # ↓ 以下这批原先落在**类别默认值**上（估值/盈利→number_cards、宏观/事件→table），
    #   而它们的证据几乎全是"两期对比"或"两值对比"——正是柱状/双轴图的典型场景，
    #   塞进数字卡等于把对比关系抹平成两个孤立数字。实测一份成品三张图全是数字卡，
    #   根因就在这里：显式映射只覆盖了想到的那些，没覆盖到的一律掉进无信息量的默认值。
    "V7": "two_col",                       # 当前 PE vs FY1 预测 PE，两值对比
    "E5": "bar_line", "E5b": "bar_line",   # ROE（柱）+ 净利率（线），各两期
    "E6": "bar_line", "E6b": "bar_line",   # 毛利率两期 + 同期净利同比作对照
    "E1": "bar", "E1b": "bar",             # 净利同比，可与同业/上期并列成柱
    "M6": "bar",                           # PPI 与 CPI 两根柱，一眼看出剪刀差
    "C6": "bar",                           # 解禁规模按月分布
}


def chart_type_of(key) -> str:
    """取默认图表类型。key 可以是 Thesis 对象、论点 id（如 "V1"）或类别名。

    id 与类别名不会撞——论点 id 形如 V1/S3b，类别名都以"类"结尾。
    优先级：论点级覆盖（`_THESIS_CHART`）> 类别默认 > 数字卡片。
    未知 key（如自由槽的 free_1）返回数字卡片。
    """
    tid = getattr(key, "id", None) or str(key)
    if tid in _THESIS_CHART:
        return _THESIS_CHART[tid]

    cat = getattr(key, "类别", None)
    if cat is None:
        s = str(key)
        if s in _CATEGORY_CHART:
            cat = s
        else:
            t = load_library().get(s)
            cat = t.类别 if t else ""
    return _CATEGORY_CHART.get(str(cat), "number_cards")


def required_fields(include_profile: bool = True) -> list[str]:
    """全部可自动判定论点所需字段的并集（去重保序），供 pipeline 摸底取数。

    "该查哪些数据"由论点库决定——这样每个标的都按同一套论点做全面摸底，
    最后哪几条论点成立交给数据说话，而不是预先框定只查某三条逻辑要用的字段。
    """
    seen: dict[str, None] = {}
    for tid in _JUDGES:
        for f in _TRIGGER_FIELDS.get(tid, []):
            seen.setdefault(f, None)
    if include_profile:
        for f in PROFILE_FIELDS:
            seen.setdefault(f, None)
    return list(seen)


def fields_of(thesis_id: str) -> list[str]:
    """某条论点的判定字段依赖（供回填数据到该论点时使用）。"""
    return list(_TRIGGER_FIELDS.get(thesis_id, []))


# 研报观点配套的自有数据。研报给的是**产业机制**（渗透率、技术路线、海外业绩），
# 我们能给的是**市场状态**（估值、盈利、资金、波动）——两者合起来才是完整论证：
# 研报说明"为什么会这样"，我方数据佐证"它正在发生"。
# 没有这一步时，研报观点在正文里是孤立的一段引用，读者无从判断它与本标的当下状态的关系。
_DOC_CORE_FIELDS: list[str] = [
    "PB历史分位", "归母净利同比", "板块区间涨跌幅", "板块资金净流入", "年化波动率",
]


def companion_fields(category: str, available: set[str] | None = None,
                     limit: int = 5) -> list[str]:
    """给一条研报观点配几个可佐证的自有字段。

    先取**同类别已判定论点用到的字段**（最贴题），再用板块核心画像补足。
    ⚠ 供需/产业格局类与扩散/映射类的同类别字段是**空的**——那两类一条判定都没接，
    而研报观点恰恰最常落在这两类。故核心画像的兜底不是可选项，是主力来源。
    """
    seen: dict[str, None] = {}
    for tid in _JUDGES:
        t = _LIB_CACHE.get(tid) if _LIB_CACHE else None
        if t is not None and t.类别 == category:
            for f in _TRIGGER_FIELDS.get(tid, []):
                seen.setdefault(f, None)
    for f in _DOC_CORE_FIELDS:
        seen.setdefault(f, None)
    out = [f for f in seen if available is None or f in available]
    return out[:limit]


# ---------------- 触发引擎 ----------------

def evaluate(profile: dict, library: dict[str, Thesis] | None = None) -> list[Trigger]:
    """对摸底数据跑一遍全部可自动判定的论点，返回判定结果。"""
    lib = library or load_library()
    out: list[Trigger] = []
    for tid, judge in _JUDGES.items():
        th = lib.get(tid)
        if th is None:
            continue
        try:
            ok, why, ev = judge(profile)
        except Exception as e:  # noqa: BLE001 判定失败不应中断整体
            ok, why, ev = None, f"判定异常: {type(e).__name__}", {}
        out.append(Trigger(thesis=th, triggered=ok, 说明=why, 证据=ev))
    return out


# 语义包含关系：键触发时，抑制值中的论点（避免同时输出重复论点）
# 例：PB分位 95% 会同时满足 V1b(>70%) 与 V8(>90%)，此时只保留更具体的 V8。
_SUPPRESS: dict[str, tuple[str, ...]] = {
    "V8": ("V1b",),          # 泡沫预警 蕴含 高分位
    "S3": ("S1", "S7"),      # 急跌+高波动组合 蕴含 高波动环境 与 超跌
    "S4": ("S1", "S2"),      # 波动率极端(>90%或<10%分位) 蕴含 高/低波动环境
    "S5": ("S8b",),          # 拥挤度过热(换手) 与 情绪过热(成交额) 高度相关，留更具投研含义的前者
    "F5b": ("C6",),          # 解禁高峰临近 已包含 解禁窗口提示，不必并列
}


def triggered_theses(profile: dict, library: dict[str, Thesis] | None = None) -> list[Trigger]:
    """只返回**确定触发**的论点，并消除语义重叠（保留更具体者）。"""
    fired = [t for t in evaluate(profile, library) if t.triggered is True]
    ids = {t.thesis.id for t in fired}
    suppressed: set[str] = set()
    for tid in ids:
        suppressed |= set(_SUPPRESS.get(tid, ()))
    return [t for t in fired if t.thesis.id not in suppressed]


# ---------------- 人工勾选（供 CLI / 未来 GUI 使用）----------------

def group_for_pick(fired: list[Trigger]) -> list[Trigger]:
    """把触发结果按类别聚拢，作为人工勾选的**唯一展示顺序**。

    渲染与选号必须共用这一个顺序，否则编号会与实际选中的论点错位——
    故调用方先拿到这个列表，再拿它去 render_triggers / select，两处不各自排序。
    """
    by_cat: dict[str, list[Trigger]] = {}
    for t in fired:
        by_cat.setdefault(t.thesis.类别, []).append(t)
    return [t for items in by_cat.values() for t in items]


def render_triggers(fired: list[Trigger]) -> str:
    """把触发结果排成**可人工勾选**的清单：按类别分组，每条带方向与实测依据。

    传入的列表须已过 group_for_pick（编号即列表下标+1，与 select 对齐）。

    为什么按类别分组显示：分析师容易习惯性地总挑估值、资金那几类，
    那样"来来回回那几个观点"会以人的惯性再回来一次。分组后"三条全在同一类"
    一眼可见，让偷懒的代价显形——靠呈现方式提示，而不是靠规则限制人的判断。
    """
    if not fired:
        return "本次没有论点被真实数据触发（数据缺失较多），将由 planner 用自由槽补足。"

    cats = {t.thesis.类别 for t in fired}
    lines = [f"本次被真实数据触发的论点（共 {len(fired)} 条，分属 {len(cats)} 个类别）：", ""]
    last = ""
    for i, t in enumerate(fired, 1):
        if t.thesis.类别 != last:
            if last:
                lines.append("")
            lines.append(f"【{t.thesis.类别}】")
            last = t.thesis.类别
        d = t.thesis.方向 or t.thesis.特征.get("方向", "")
        lines.append(f"  {i:>2}. {t.thesis.id:<4} {t.thesis.名称}" + (f"　[{d}]" if d else ""))
        if t.说明:
            lines.append(f"      依据：{t.说明}")
    return "\n".join(lines)


def select(fired: list[Trigger], picks: list[int]) -> list[Trigger]:
    """按序号取出被勾选的论点（1 起，越界与重复自动忽略）。"""
    out, seen = [], set()
    for p in picks:
        if 1 <= p <= len(fired) and p not in seen:
            seen.add(p)
            out.append(fired[p - 1])
    return out


def categories_of(triggers: list[Trigger]) -> list[str]:
    """这批论点覆盖的类别（去重保序）——用于提示"是否视角过于集中"。"""
    seen: dict[str, None] = {}
    for t in triggers:
        seen.setdefault(t.thesis.类别, None)
    return list(seen)


if __name__ == "__main__":  # python -m core.thesis
    lib = load_library()
    from collections import Counter
    print(f"论点库载入 {len(lib)} 条")
    print("  按类别:", dict(Counter(t.类别 for t in lib.values())))
    print("  按可得性:", dict(Counter(t.可得性 for t in lib.values())))
    print(f"  已注册判定函数: {len(_JUDGES)} 条 → {sorted(_JUDGES)}")
    miss = [i for i in _JUDGES if i not in lib]
    if miss:
        print("  ⚠ 判定函数对应的论点在库中不存在:", miss)
    print()
    sample = lib.get("V1")
    print("样例 V1:", sample.名称, "|", sample.类别, "|", sample.触发条件, "|", sample.特征)
