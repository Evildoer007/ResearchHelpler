"""取数层（v1，方案A：代表标的）。

把"业务字段 → iFinD 指标 → 真实数值"接起来：
  planner 给字段清单 → fetcher 按 ifind_indicators 映射调 provider 取数 →
  取到的返回 {值,来源,参数日期}；取不到/MANUAL/DERIVED未实现的 → 标"需人工补充"，
  统一流向后续的人工补充面板（不开天窗，见 DESIGN §9）。

v1 范围（方案A）：
  - 只对"一个代表标的"取数（如券商板块用中信证券/板块ETF代表）。
  - AUTO 标量字段（PB/ROE/股息率/归母净利同比/现金分红等）直接取。
  - DERIVED（分红率/PB历史分位/板块股价vs营收对比）v1 暂标待实现→人工。
  待改进 → 方案C：龙头代表 + 板块成分股聚合（DESIGN §15）。

参数按指标的 param_type 在运行时解析：
  none/trade_date(交易日)/report_period(报告期,季度末)/date_range(起止日,币种)。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from . import ifind_indicators as im
from . import schema
from .provider import DataProvider, get_provider

# 能自动取的映射状态
_AUTO_STATUSES = {im.VERIFIED, im.UNCERTAIN}


@dataclass
class FieldValue:
    """单个字段的取数结果。ok=False 即需人工补充。"""

    field: str
    value: Any = None
    ok: bool = False
    source: str = "-"        # iFinD / DERIVED / -
    as_of: str = ""          # 使用的参数（日期/报告期/区间）
    indicator: str = ""      # 用到的 iFinD 指标
    status: str = ""         # ok / 需人工补充 / 待实现
    note: str = ""
    display: str = ""        # 格式化后的可读串（供 writer 照抄，防 LLM 自己换算出错）


# ---------------- 参数解析 ----------------

def latest_trade_date(d: dt.date | None = None) -> str:
    """最近交易日（简化：非工作日回退到周五）。"""
    d = d or dt.date.today()
    while d.weekday() >= 5:  # 5=周六 6=周日
        d -= dt.timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def latest_report_period(d: dt.date | None = None) -> str:
    """最近已披露的报告期（季度末）。启发式按 A 股披露截止日估计，可能偏保守。"""
    d = d or dt.date.today()
    y, m = d.year, d.month
    if m >= 11:
        return f"{y}-09-30"   # 三季报(10/31截止)已出
    if m >= 9:
        return f"{y}-06-30"   # 中报(8/31)已出
    if m >= 5:
        return f"{y}-03-31"   # 一季报(4/30)已出
    return f"{y - 1}-09-30"   # 年初：年报未出，取上年三季报


def last_full_year_range(d: dt.date | None = None) -> str:
    """上一个完整年度区间 + 币种，用于区间现金分红。"""
    d = d or dt.date.today()
    y = d.year - 1
    return f"{y}-01-01,{y}-12-31,CNY"


def _resolve_param(param_type: str) -> str:
    return {
        "none": "",
        "trade_date": latest_trade_date(),
        "report_period": latest_report_period(),
        "date_range": last_full_year_range(),
    }.get(param_type, "")


# ---------------- 数值格式化（前置，供 writer 照抄，避免 LLM 换算出错）----------------

def _fmt_money(v: float) -> str:
    """iFinD 金额原始多为「元」，自动缩放到易读单位。"""
    a = abs(v)
    if a >= 1e12:
        return f"{v / 1e12:.2f}万亿元"
    if a >= 1e8:
        return f"{v / 1e8:.2f}亿元"
    if a >= 1e4:
        return f"{v / 1e4:.2f}万元"
    return f"{v:.2f}元"


def format_value(field: str, value: Any) -> str:
    """按 schema 单位把原始值格式化成最终可读串。writer 只照抄，不再换算。"""
    if value in (None, "", "--"):
        return ""
    unit = (schema.FIELDS.get(field) or {}).get("单位", "")
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)             # 非数值（名称/日期）原样
    # 有方向的资金字段：把"流入/流出"写进文字，避免模型抄成正数而颠倒方向
    # （实测：真值 -425.3 亿被写成 "424.77亿元"，读起来像净流入——方向错在投研里是致命的）
    if "净流入" in field:
        return f"净流{'入' if v >= 0 else '出'}{_fmt_money(abs(v))}"
    if "%" in unit:                   # "%" 或 "%分位"
        return f"{v:.2f}%" if unit == "%" else f"{v:.1f}{unit}"
    if unit == "倍":
        return f"{v:.2f}倍"
    if unit in ("元", "亿元"):        # 金额：从「元」自动缩放
        return _fmt_money(v)
    return f"{v:.2f}"


# ---------------- 取数 ----------------

# 板块级字段：数据来自 signals（akshare 行业资金流），需要"板块名"而非个股代码。
# 这些是真实市场数据，接入后板块机会型逻辑（量价背离、资金面）才有据可依。
_SECTOR_SIGNAL_FIELDS = {
    "板块区间涨跌幅": "区间涨跌幅",
    "板块资金净流入": "主力净流入亿元",
    "板块领涨股": "领涨股",
}


def _fetch_sector_field(field: str, sector: str) -> FieldValue:
    from . import signals as sg

    key = _SECTOR_SIGNAL_FIELDS[field]
    try:
        snap = sg.sector_snapshot(sector)
    except Exception as e:  # noqa: BLE001
        return FieldValue(field, None, False, "-", status="需人工补充",
                          note=f"板块信号取数失败: {type(e).__name__}")
    if not snap or snap.get(key) is None:
        return FieldValue(field, None, False, "-", status="需人工补充",
                          note=f"未匹配到板块「{sector}」的{field}")
    val = snap[key]
    # 不变量：金额字段的 value 一律以「元」存储，由 format_value 统一缩放显示。
    # signals 为便于选题展示已折算成亿元，此处折回元，避免出现 "-188.99元" 这种错标。
    if key == "主力净流入亿元":
        val = float(val) * 1e8
    return FieldValue(field, val, True, f"akshare·{snap['板块']}", snap.get("周期", ""),
                      "", "ok", "", display=format_value(field, val))


# 可由板块成分股聚合得出的字段（方案C）：有板块名时优先用**板块整体法**值，
# 而非单一代表标的——后者会严重失真（长鑫分红0元 ≠ 板块分红改善）。
_AGGREGATABLE = {
    "PB": "PB", "PE_TTM": "PE", "ROE": "ROE",
    "A股股息率": "股息率", "分红率": "分红率",
    "现金分红总额": "现金分红总额", "归母净利同比": "归母净利同比",
}


def _fetch_aggregated(field: str, sector: str, provider: DataProvider) -> FieldValue | None:
    """尝试用板块聚合值。失败返回 None——#85 起调用方据此**报缺口，不再退回代表个股**。"""
    from . import aggregate as ag

    try:
        agg = ag.sector_aggregate(sector, provider=provider)
    except Exception:
        return None
    if not agg.ok:
        return None
    val = agg.get(_AGGREGATABLE[field])
    if val is None:
        return None
    return FieldValue(
        field, val, True, f"iFinD·板块整体法({agg.成分数}只)", "", "", "ok",
        f"{sector}板块前{agg.成分数}大成分股整体法聚合",
        display=format_value(field, val),
    )


def _fetch_holder_change(field: str, sector: str, provider: DataProvider) -> FieldValue | None:
    """减持规模：板块大股东近一年增减持（净额）。支撑 catalyst 的"卖压"侧。"""
    from . import aggregate as ag

    try:
        r = ag.sector_shareholder_change(sector, provider=provider)
    except Exception:
        return None
    if not r or r.get("净减持") is None:
        return None
    net = r["净减持"]
    # 方向写进文字：净减持=卖压，净增持=利好。避免模型抄成正数而误判方向
    word = "净减持" if net >= 0 else "净增持"
    disp = f"{word}{_fmt_money(abs(net))}"
    return FieldValue(
        field, net, True, f"iFinD·{sector}板块({r['涉及股票数']}只)", r.get("区间", ""), "", "ok",
        f"减持{_fmt_money(r['减持金额'])}、增持{_fmt_money(r['增持金额'])}；"
        f"减持前列：{'、'.join(x['简称'] for x in r.get('减持前列', [])[:3])}",
        display=disp,
    )


def _derived_failed(field: str, code: str, err: str) -> FieldValue:
    """DERIVED 字段**已实现但取数失败**时的返回。

    ⚠ 不能返回 None：调用方拿到 None 会落到通用兜底消息
    「DERIVED：v1 暂未实现，人工补充」——那句话在**说谎**，
    它说功能没做，而实际是功能有、这次的数据没取到（网络、样本不足、代码无历史…）。
    实测踩过：同一个 `PB历史分位` 在券商板块取得到、在电子板块取不到，
    缺口清单却都写"暂未实现"，把真正的原因（序列取数失败）整个吞掉，
    看的人会以为是待开发功能而不去排查。
    """
    return FieldValue(field, None, False, f"iFinD·{code}", "", "", "需人工补充",
                      err or "历史序列取数失败（非功能缺失）")


# 这七个字段此前一律取自**单一代表标的**，却在报告里被当板块指标写
# （"板块波动率处 87% 分位"实际是那一只标的的）。13 条论点骑在上面：
# V1/V1b/V8（PB分位）+ S1~S8b（波动率/涨跌/换手/成交额分位）。
# 有板块名时改走 `history.sector_*`（成分股整体法合成），没有才回落到个股口径。
_SECTOR_DERIVED = {
    "PB历史分位", "年化波动率", "波动率历史分位", "区间涨跌幅分位",
    "换手率历史分位", "换手率近期高分位", "成交额历史分位",
}


def _fetch_sector_derived(field: str, sector: str,
                          provider: DataProvider) -> FieldValue | None:
    """板块整体法口径的派生字段。取不到返回 None，由调用方回落到代表标的口径。"""
    from . import history

    src = f"iFinD·{sector}板块整体法"

    if field == "PB历史分位":
        p = history.sector_percentile(sector, "PB", provider=provider)
        if not p.ok:
            return None
        return FieldValue(field, p.分位, True, f"{src}·{p.起始}", p.起始, "", "ok",
                          f"板块整体法PB {p.当前值:.2f}倍，{p.起始}{p.样本数}个交易日中的分位",
                          display=f"{p.分位:.1f}%分位")

    if field in ("年化波动率", "波动率历史分位"):
        v = history.sector_volatility(sector, provider=provider)
        if not v.ok:
            return None
        if field == "年化波动率":
            return FieldValue(field, v.当前, True, f"{src}·近{v.窗口}日", "", "", "ok",
                              f"板块合成指数近3年均值 {v.均值:.1f}%", display=f"{v.当前:.1f}%")
        return FieldValue(field, v.分位, True, f"{src}·近3年", "", "", "ok",
                          f"板块波动率 {v.当前:.1f}%，近3年均值 {v.均值:.1f}%",
                          display=f"{v.分位:.1f}%分位")

    if field == "区间涨跌幅分位":
        r = history.sector_return_percentile(sector, provider=provider)
        if not r.ok:
            return None
        return FieldValue(field, r.分位, True, f"{src}·{r.起始}", "", "", "ok",
                          f"板块近20日涨跌 {r.当前值:+.1f}%", display=f"{r.分位:.1f}%分位")

    if field in ("换手率历史分位", "换手率近期高分位", "成交额历史分位"):
        kind = "成交额" if field.startswith("成交额") else "换手率"
        r = history.sector_smoothed_percentile(sector, kind, provider=provider)
        if not r.ok:
            return None
        if field == "换手率近期高分位":
            return FieldValue(field, r.近期高分位, True, f"{src}·{r.起始}", "", "", "ok",
                              f"板块近3个月换手率分位峰值（当前 {r.分位:.1f}%分位）",
                              display=f"{r.近期高分位:.1f}%分位")
        unit = "%" if kind == "换手率" else "亿元"
        cur = r.当前值 / 1e8 if unit == "亿元" else r.当前值
        return FieldValue(field, r.分位, True, f"{src}·{r.起始}", "", "", "ok",
                          f"板块近5日均值 {cur:.2f}{unit}，{r.起始}{r.样本数}个观测中的分位",
                          display=f"{r.分位:.1f}%分位")
    return None


def _fetch_derived(field: str, code: str, provider: DataProvider) -> FieldValue | None:
    """已实现的 DERIVED 字段（需在历史序列上加工）。

    **未实现**才返回 None（转通用兜底）；已实现但取数失败返回带真实原因的 FieldValue。
    """
    from . import history

    if field == "PB历史分位":
        p = history.percentile(code, "ths_pb_latest_stock", provider=provider)
        if not p.ok:
            return _derived_failed(field, code, p.error)
        return FieldValue(
            field, p.分位, True, f"iFinD·{code}{p.起始}", p.起始, "", "ok",
            f"当前PB {p.当前值:.2f}倍，{p.起始}{p.样本数}个交易日中的分位",
            display=f"{p.分位:.1f}%分位",
        )

    if field in ("年化波动率", "波动率历史分位"):
        v = history.volatility(code, provider=provider)
        if not v.ok:
            return _derived_failed(field, code, v.error)
        if field == "年化波动率":
            return FieldValue(field, v.当前, True, f"iFinD·{code}近{v.窗口}日", "", "", "ok",
                              f"近3年均值 {v.均值:.1f}%", display=f"{v.当前:.1f}%")
        return FieldValue(field, v.分位, True, f"iFinD·{code}近3年", "", "", "ok",
                          f"当前波动率 {v.当前:.1f}%，近3年均值 {v.均值:.1f}%",
                          display=f"{v.分位:.1f}%分位")

    if field == "区间涨跌幅分位":
        r = history.return_percentile(code, provider=provider)
        if not r.ok:
            return _derived_failed(field, code, r.error)
        return FieldValue(field, r.分位, True, f"iFinD·{code}{r.起始}", "", "", "ok",
                          f"近20日涨跌 {r.当前值:+.1f}%", display=f"{r.分位:.1f}%分位")

    # 拥挤度与情绪：换手率/成交额的**平滑后**分位（S5/S6/S8/S8b）
    if field in ("换手率历史分位", "换手率近期高分位", "成交额历史分位"):
        ind = "ths_amt_stock" if field.startswith("成交额") else "ths_turnover_ratio_stock"
        r = history.smoothed_percentile(code, ind, provider=provider)
        if not r.ok:
            return _derived_failed(field, code, r.error)
        if field == "换手率近期高分位":
            return FieldValue(field, r.近期高分位, True, f"iFinD·{code}{r.起始}", "", "", "ok",
                              f"近3个月换手率分位峰值（当前 {r.分位:.1f}%分位）",
                              display=f"{r.近期高分位:.1f}%分位")
        unit = "%" if field.startswith("换手率") else "亿元"
        cur = r.当前值 / 1e8 if unit == "亿元" else r.当前值
        return FieldValue(field, r.分位, True, f"iFinD·{code}{r.起始}", "", "", "ok",
                          f"近5日均值 {cur:.2f}{unit}，{r.起始}{r.样本数}个观测中的分位",
                          display=f"{r.分位:.1f}%分位")

    return None


def _fetch_one(field: str, code: str, provider: DataProvider,
               sector: str | None = None, etf_code: str | None = None,
               asset_type: str = "") -> FieldValue:
    # 已实现的 DERIVED 计算字段。
    #
    # 行情类（波动率/涨跌幅分位/换手率/成交额分位）优先级（#73）：
    #   ① 有一只流动性够格的 ETF 能代表这个板块 → 直接用它**自己的**真实价格数据。
    #      六份参考模板没有一份为"板块"单独造聚合篮子，分析的就是最终要挂钩的那只
    #      ETF 自身——这样"分析对象"与"挂钩标的"天然是同一个东西，不再有基差。
    #   ② 没有合格 ETF（或成交太薄）→ 退回板块整体法聚合（#69）。
    # PB历史分位不参与①：ETF 和它跟踪的指数都不直接提供 PB 历史序列
    # （实测中证消费指数 PB 序列 0 点），这是数据源的硬限制，永远走②。
    # 商品 ETF（黄金、原油等）没有股票行业的 PB/ROE/净利润/板块主力资金口径。
    # 它们的研究对象就是已确认 ETF 本身；不应把“贵金属”股票板块当替身。
    commodity = asset_type == "商品ETF"
    if commodity and field in (_AGGREGATABLE | _SECTOR_SIGNAL_FIELDS.keys() | {"减持规模", "PB历史分位"}):
        return FieldValue(field, None, True, "不适用·商品ETF", "", "", "不适用",
                          "商品 ETF 不采用股票板块基本面、资金流或 PB 口径")

    if (sector or commodity) and field in _SECTOR_DERIVED:
        if commodity and etf_code:
            # 商品 ETF 的行情、波动率、成交额均严格使用它自身的历史序列。
            return _fetch_derived(field, etf_code, provider)
        if field != "PB历史分位" and etf_code:
            # #73：分析对象是 ETF 时，行情类分位就用这只 ETF **自身**的真实价格序列。
            # 成功或失败都以 ETF 口径返回——失败也是 ETF 代码来源的诚实缺口，
            # 绝不退回代表个股冒充（_fetch_derived 对已实现字段不会返回 None）。
            return _fetch_derived(field, etf_code, provider)
        fv = _fetch_sector_derived(field, sector, provider)
        if fv is not None:
            return fv
        # 板块整体法（#85 起用 ETF 真实成分）也取不到：如实缺口。
        # **不以代表个股数据冒充板块**——分析的是板块/ETF，拿一只成分股的
        # PB/波动率顶替是张冠李戴（用户实测点名的正是这条）。
        return FieldValue(field, None, False, f"iFinD·{sector}板块", status="需人工补充",
                          note="板块/ETF口径历史序列取数失败；按口径要求不以个股数据替代")
    fv = _fetch_derived(field, code, provider)   # 无板块名：纯个股分析才走到这
    if fv is not None:
        return fv

    # 板块级信号字段优先（需板块名）
    if field in _SECTOR_SIGNAL_FIELDS:
        if sector:
            return _fetch_sector_field(field, sector)
        return FieldValue(field, None, False, "-", status="需人工补充",
                          note="板块级字段，但未提供板块名")

    if field == "减持规模" and sector:
        fv = _fetch_holder_change(field, sector, provider)
        if fv is not None:
            return fv

    # 可聚合字段：有板块名时用板块整体法（口径与研报一致，#85 起用 ETF 真实成分）。
    # 失败**不再回退代表个股**——分析 ETF 却写茅台的 PB/ROE 是张冠李戴（用户点名）。
    if sector and field in _AGGREGATABLE:
        fv = _fetch_aggregated(field, sector, provider)
        if fv is not None:
            return fv
        return FieldValue(field, None, False, f"iFinD·{sector}板块", status="需人工补充",
                          note="板块整体法取数失败；按口径要求不以个股数据替代")

    mapping = im.get_mapping(field)

    # 有可用 iFinD 指标 → 直接取
    if mapping and mapping.get("indicator") and mapping.get("status") in _AUTO_STATUSES:
        param = _resolve_param(mapping.get("param_type", "none"))
        r = provider.get_basic([code], [mapping["indicator"]], param)
        val = r.value
        if r.ok and val not in (None, "", "--"):
            return FieldValue(field, val, True, r.source, param, mapping["indicator"], "ok",
                              mapping.get("note", ""), display=format_value(field, val))
        return FieldValue(field, None, False, r.source, param, mapping["indicator"],
                          "需人工补充", r.error or "返回空值")

    # 无可用指标：按 schema 取数方式说明原因，转人工
    try:
        method = schema.method_of(field)
    except KeyError:
        method = "未定义"
    reason = {
        schema.MANUAL: "MANUAL：数据源拿不到，人工补充",
        schema.DERIVED: "DERIVED：v1 暂未实现，人工补充",
        schema.AUTO: "映射未就绪(待面板/待确认)，暂人工",
    }.get(method, f"字段未在 schema 定义({method})")
    return FieldValue(field, None, False, "-", status="需人工补充", note=reason)


def fetch_fields(
    fields: list[str], code: str, provider: DataProvider | None = None,
    sector: str | None = None, *, analysis_etf: str | None = None,
    asset_type: str = "",
) -> tuple[list[FieldValue], list[str], DataProvider]:
    """取一批字段：个股字段用代表标的 code（iFinD），板块级字段用 sector（signals）。

    返回 (结果列表, 缺口字段列表, provider)。
    """
    provider = provider or get_provider()

    # 分析ETF只解析一次（内含一次流动性查询），不要让 6 个 _SECTOR_DERIVED
    # 字段各自重复查一遍——那是同一个问题问 6 次。
    # 用户已点名 ETF 时，它就是本次行情字段的唯一分析对象。此前这里忽略了
    # pipeline 传入的显式 ETF，又按 sector 反查默认映射，导致 512000.SH 的报告
    # 悄悄读取了“证券”默认 ETF 512880.SH 的自身波动率/成交额等行情字段。
    etf_code = (analysis_etf or "").strip() or None
    if not etf_code and sector and any(f in _SECTOR_DERIVED for f in fields):
        from . import instruments as inst
        i, _note = inst.resolve_analysis_etf(sector, provider=provider)
        etf_code = i.代码 if i else None

    # 保持普通股票/行业 ETF 调用的既有参数形状；商品分支才显式带资产类型，
    # 方便外部调用和已有测试继续把最后一个位置理解为 analysis_etf。
    if asset_type:
        results = [_fetch_one(f, code, provider, sector, etf_code, asset_type=asset_type)
                   for f in fields]
    else:
        results = [_fetch_one(f, code, provider, sector, etf_code) for f in fields]
    gaps = [fv.field for fv in results if not fv.ok]
    return results, gaps, provider


if __name__ == "__main__":  # 端到端实测：python -m core.fetcher
    from . import thesis as th

    CODE = "600030.SH"  # 中信证券，作券商板块代表标的（方案A）
    fields = th.required_fields()
    print(f"论点库判定所需字段 {len(fields)} 个（代表标的 {CODE}）:\n")
    results, gaps, prov = fetch_fields(fields, CODE)
    for fv in results:
        flag = "✔" if fv.ok else "…"
        v = f"{fv.value}" if fv.ok else fv.note
        print(f"  {flag} {fv.field:<16} {str(v)[:40]:<40} {('['+fv.indicator+' @'+fv.as_of+']') if fv.ok else ''}")
    print(f"\n自动取到 {len(results)-len(gaps)}/{len(results)}，需人工补充 {len(gaps)} 个: {gaps}")
    if hasattr(prov, "total_data_vol"):
        print(f"本次 iFinD 消耗 dataVol: {prov.total_data_vol}")
        prov.close()
