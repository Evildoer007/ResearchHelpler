"""市场分析编排：数据先行 → planner → fetcher 补漏。

2026-07-29 顺序调整（原"先定逻辑、后查数据"改为"先摸底数据、后定逻辑"）：
旧流程下 planner 在完全不知道"哪些数据真的能查到"的情况下就先选好了三条逻辑，
经常选中数据支撑为零的逻辑（如事件驱动型的历史规律统计），导致成品"诚实但空洞"
（正文写满"数据缺失，无法判断"）。

新流程：
  ① fetch_profile  —— 先把**论点库判定所需的全部字段**查一遍，不预设最后选哪条论点
  ② planner        —— 从"被这份真实数据触发的论点"里挑 2~3 条（DESIGN §7.2/§7.3）
  ③ 补充取数        —— 仅为规划中出现、摸底没覆盖的字段（多是自由槽新字段）再查一次
  ④ 回填            —— 把数据挂到每条被选中的逻辑上

产出正是后续 ③人工补充面板 与 ④writer 的输入：
  每条逻辑清楚地列出——哪些数据已自动取到、哪些待人工补。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dfield

from llm.client import DeepSeekClient

from . import fetcher, planner
from . import genres as gr
from .fetcher import FieldValue
from .planner import DOC_FIELD_PREFIX, ArgumentPlan, PlanLogic
from .provider import DataProvider, get_provider


@dataclass
class LogicWithData:
    logic: PlanLogic
    fields: list[FieldValue] = dfield(default_factory=list)

    @property
    def auto(self) -> list[FieldValue]:
        return [f for f in self.fields if f.ok]

    @property
    def gaps(self) -> list[FieldValue]:
        return [f for f in self.fields if not f.ok]


@dataclass
class MarketAnalysis:
    plan: ArgumentPlan
    rep_code: str
    logics: list[LogicWithData] = dfield(default_factory=list)
    field_values: dict[str, FieldValue] = dfield(default_factory=dict)
    外部事实待补: list[str] = dfield(default_factory=list)  # 我方数据源查不到，且尚未人工填
    # 分析师经覆盖文件填好的外部事实（待补事项 → 内容）。与上一项互斥：
    # 填了就从"待补"移到这里，否则 writer 仍被告知"不得编造"而回避它，等于白填。
    外部事实已填: dict = dfield(default_factory=dict)
    # 事件驱动报告的可溯源事实与传导证据；仅在通过证据门后存在。
    事件证据: dict = dfield(default_factory=dict)
    # 挂钩标的择优结果（`selection.Proposal`）。**只在板块→ETF 映射给不出答案时**
    # 才有值——产业趋势与事件驱动这两类的分析对象本身不可交易（"AI产业景气"、
    # "IPO的流动性冲击"都挂不了），必须映射到一只有该暴露的可交易标的上（§9.2②）。
    # 板块类需求走 `underlying_for()` 的确定答案，不必也不该再择一次优。
    挂钩择优: object = None
    # 研报观点自带的、已通过校验的配图数列：{逻辑id: 图表规格}。
    # 由 writer 直接采用而**不让 LLM 自拟**——图比文字更难被读者核对，
    # 一个编造的数列看起来和真的一模一样，故只画从研报里逐字校验过的数。
    doc_charts: dict = dfield(default_factory=dict)
    # 研报观点的类别（doc_id → 论点库九大类之一）。没有它，writer 的 _chart_type
    # 拿 "doc_21" 去查图型会既不在 _THESIS_CHART、也不是类别名，**回落到数字卡**——
    # 实测一份成品的研报逻辑因此配了张挤成一团的数字卡。类别本来就在 DocClaim 上，
    # 只是传到 writer 时丢了。
    doc_cats: dict = dfield(default_factory=dict)
    # 被人工选入正文的研报观点及其证据边界。writer 需要它阻止公司级证据外推。
    doc_claims: list = dfield(default_factory=list)
    # 系统按字段自动配的**历史序列图**：{字段名: 图表规格}。
    # 分位/波动率类字段传给 writer 的只有一个标量（"9.4%分位"），
    # 它手上没有序列，能画的就只有数字卡——这是数据可得性问题，不是提示词问题，
    # 调两轮提示词都没用（#65/#67）。#69 已经把板块级 726 天序列算出来了，
    # 这里把它做成图规格挂到字段上，**不经过 LLM**：几百个点让模型转写既浪费又是编造风险，
    # 同 doc_charts 的道理（图比文字更难核对，只画机器取来的真数）。
    auto_charts: dict = dfield(default_factory=dict)
    # 代表标的简称。只给代码（600030.SH）读者认不出是谁，而正文首次提及
    # 要写"以中信证券为代表"才读得通；成品的分析对象栏同样要用它。
    rep_name: str = ""
    # brief 给的"为什么是这个板块"。此前成品从不交代分析对象是怎么选出来的，
    # 而板块确实选错过（芯片需求取到医疗服务成分股，#61），读者却无从察觉。
    板块理由: str = ""
    # 仅供底稿与 OptionHelper 独立交接；planner/writer 不读取。
    客户产品诉求: str = ""
    市场确认: dict | None = None
    确认挂钩标的: str = ""
    仅研究: bool = False
    tokens: int = 0
    data_vol: int = 0
    ok: bool = False
    error: str = ""

    @property
    def gap_fields(self) -> list[str]:
        return [f for f, v in self.field_values.items()
                if not f.startswith("__") and not getattr(v, "ok", False)]


def _all_fields() -> list[str]:
    """摸底取数的字段清单 —— **由论点库决定**（DESIGN §7.3）。

    = 全部可判定论点的字段依赖 ∪ 标的画像字段（供 writer 写正文）。
    """
    from . import thesis as th

    return th.required_fields()


def fetch_profile(
    rep_code: str, sector: str | None = None,
    provider: DataProvider | None = None, *,
    trigger_code: str = "", trigger_name: str = "",
    analysis_etf: str = "",
    overrides=None,
) -> dict[str, FieldValue]:
    """摸底取数：先把论点库判定所需 + 画像字段查一遍，不预设最后选哪条论点。

    **主题名在这里解析一次，下游全部拿已解析的行业名。** 不在各模块里各自解析——
    取成分股的调用方有 9 处（aggregate/fundamentals/unlock/peers/signals），
    逐个改必然漏；而且只有这里同时握有 rep_code 与 sector，能做动态解析。

    analysis_etf：分析对象是某只 ETF 时的**显式指定**（用户需求里点名的、已过代码
    校验的 ETF）。给了它就优先于板块名反推的挂钩 ETF——修的是"用户明明给了
    酒ETF 512690.SH，板块名却被 LLM 飘成食品饮料、最终分析了另一个篮子"这个 bug。
    """
    from . import instruments as _inst
    from . import universe

    provider = provider or get_provider()
    confirmed_type = str(getattr(_inst.get(analysis_etf), "类型", "") or "") if analysis_etf else ""
    commodity_etf = confirmed_type == "商品ETF"

    # 用户点名了某只 ETF：先把板块名对齐到这只 ETF 规范代表的那一级（#85）。
    # 否则 PB/波动率用对了 ETF 真实成分篮子，但板块级信号字段（资金净流入等，
    # 按板块名走 iwencai）仍量的是 LLM 自由生成的宽口径板块——又是一处张冠李戴。
    if analysis_etf:
        aligned = _inst.sector_of_etf(analysis_etf)
        if aligned:
            sector = aligned
    if sector and not commodity_etf:
        sector = universe.resolve_sector(sector, rep_code=rep_code, provider=provider)

    # 分析 ETF：显式指定优先；否则按板块名反推（#85）。它一旦确定，本次所有板块级
    # 聚合（PB/波动率/分位/PE/ROE/净利同比 + 板块快照）都改用这只 ETF **真实跟踪
    # 指数的成分股**，而不是 iwencai 按行业名模糊匹配的近似篮子——"分析的东西"与
    # "挂钩的东西"从此是同一个篮子。取不到真实成分就退回原行为（iwencai 行业篮子）。
    etf_code = (analysis_etf or "").strip()
    etf_note = ""
    if etf_code:
        etf_note = "用户需求指定的挂钩 ETF"
    elif sector:
        _i, _note = _inst.resolve_analysis_etf(sector, provider=provider)
        etf_code = _i.代码 if _i else ""
        etf_note = _note
    basket = universe.etf_constituents(etf_code, provider=provider) if etf_code and not commodity_etf else []

    fields = _all_fields()
    # 已确认 ETF 却暂取不到真实成分时，宁可把股票整体法字段标为缺口，
    # 也不能退回“通信/电子”等宽行业篮子冒充这只 ETF 的分析。商品 ETF 同理。
    # ETF 自身的价格、波动率、成交额等字段仍会通过 analysis_etf 正常取数。
    fetch_sector = None if (commodity_etf or (etf_code and not basket)) else sector
    with universe.analysis_basket(fetch_sector or "", basket):
        results, _gaps, _prov = fetcher.fetch_fields(
            fields, rep_code, provider, fetch_sector, analysis_etf=etf_code,
            asset_type=confirmed_type,
        )
        profile = {fv.field: fv for fv in results}
        if sector and not commodity_etf:
            for fv in (_components_detail(sector, provider),
                       _subsector_detail(sector, provider)):
                if fv is not None:
                    profile[fv.field] = fv
    # 轮动/相对强弱类论点（R）需要标的代码与板块名本身（不是某个字段值），
    # 以双下划线键随 profile 传递，判定函数按需取用。
    profile["__code__"] = rep_code
    profile["__sector__"] = sector or ""

    # 触发实体（#74）：需求提到的事件本体，可能是境外标的，跟 rep_code 是两个角色——
    # rep_code 是 A股响应板块的数据锚点，这里取的是"这家公司自己发生了什么"。
    # 此前完全不取，报告因此从不分析事件本身，直接跳到"A股板块该怎么样"
    # （用户实测发现："全程没有分析海力士本身"）。
    if trigger_code:
        profile["__trigger_code__"] = trigger_code
        profile["__trigger_name__"] = trigger_name or trigger_code
        for fv in _fetch_trigger_entity(trigger_code, provider).values():
            profile[fv.field] = fv

    # 分析ETF：上面已确定（显式指定优先，否则板块名反推），这里落进 profile 供
    # writer/viewpoint/内部底稿引用。#85 起它同时是本次板块聚合的**真实成分来源**：
    # `__etf_成分数__` 记录实际拿到几只真实成分（0 表示没取到、聚合退回了行业篮子），
    # 内部底稿据此如实标注这份报告的板块口径到底来自 ETF 真实成分还是行业近似。
    profile["__etf__"] = etf_code
    profile["__etf_note__"] = etf_note
    profile["__etf_成分数__"] = len(basket)
    profile["__etf_basket_status__"] = (
        "真实跟踪指数成分" if basket else ("商品 ETF（无股票成分）" if commodity_etf else
                                           "未取得真实成分；不使用行业近似替代")
    )
    profile["__asset_type__"] = confirmed_type

    # 人工覆盖放在**最后**：先让机器尽力取，取不到的才由人补，
    # 人工值不会挡住本来能自动取到的数据。判定字段不可覆盖（见 core/overrides.py），
    # 合法性在 load() 时已校验，到这里的都是允许覆盖的字段。
    if overrides is not None and not overrides.为空:
        from . import overrides as _ov
        profile["__overridden__"] = _ov.apply_fields(profile, overrides)
    return profile


def _components_detail(sector: str, provider: DataProvider | None = None) -> FieldValue | None:
    """成分股明细，做成**可溯源的字段**供 writer 举例说明板块内分化。

    模板里"中信 +69%、华泰 +52%、广发 +41%"这类句子靠的就是个股级数据。
    `aggregate.SectorAggregate.明细` 一直带着这些（市值/PB/PE/ROE/净利同比），
    但从未传给 writer——聚合值进了报告，明细一直躺着没用。

    为什么包成 FieldValue 而不是直接塞进 prompt：validator 的 `_allowed_values`
    只认 `field_values` 里的数，直接塞会导致 writer 一引用个股数字就被判"可疑"。
    做成字段后，display 里的每个数都自动进入允许集，与研报原文走同一条路子。
    """
    from . import aggregate as ag

    try:
        agg = ag.sector_aggregate(sector, provider=provider)
    except Exception:
        return None
    if not agg.ok or not agg.明细:
        return None

    parts = []
    for r in agg.明细[:8]:                      # 只取前 8 大，够举例且不淹没提示词
        seg = [str(r.get("简称") or "")]
        mv, pb, yoy = r.get("市值"), r.get("PB"), r.get("净利同比")
        if mv:
            seg.append(f"市值{mv / 1e8:.0f}亿")
        if pb:
            seg.append(f"PB{pb:.2f}倍")
        if yoy is not None:
            seg.append(f"净利同比{yoy:+.1f}%")
        parts.append(" ".join(seg))
    text = "；".join(parts)
    return FieldValue(
        field="成分股明细", value=text, ok=True,
        source=f"iFinD·{sector}板块前{len(parts)}大", status="ok",
        note="个股级数据，用于举例说明板块内分化", display=text,
    )


def _subsector_detail(sector: str, provider: DataProvider | None = None) -> FieldValue | None:
    """宽口径主题的**子行业分解**：各成分一级行业各自的 PB/ROE/净利同比。

    没有这个字段时，"消费板块"报告只有一个合并数字加前 8 大个股，
    而前 8 大按市值排下来清一色是食品饮料的白酒股——读者看到的仍是食品饮料，
    "消费"这个口径名等于虚设（用户原话："消费板块覆盖面不止食品饮料，不能以偏概全"）。
    按子行业拆开后，家电/商贸零售/社会服务/纺织服饰/美容护理才各自可见，
    且这一组数天然适合柱状图，正好补上宽口径报告最该有的那张图。

    只对宽口径主题算；单一行业返回 None（它没有"子行业"这一层）。
    """
    from . import aggregate as ag
    from . import universe

    parts = universe.broad_parts(sector)
    if not parts:
        return None

    segs = []
    for sub in parts:
        try:
            agg = ag.sector_aggregate(sub, top=20, provider=provider)
        except Exception:
            continue
        if not agg.ok:
            continue
        d = agg.指标 or {}
        seg = [sub]
        if agg.合计市值:
            seg.append(f"合计市值{agg.合计市值 / 1e8:.0f}亿")
        if d.get("PB"):
            seg.append(f"PB{d['PB']:.2f}倍")
        if d.get("ROE") is not None:
            seg.append(f"ROE{d['ROE']:.2f}%")
        if d.get("净利同比") is not None:
            seg.append(f"净利同比{d['净利同比']:+.1f}%")
        if len(seg) > 1:
            segs.append(" ".join(seg))
    if not segs:
        return None

    text = "；".join(segs)
    return FieldValue(
        field="子行业明细", value=text, ok=True,
        source=f"iFinD·{sector}下{len(segs)}个一级行业（各自整体法）", status="ok",
        note="子行业级数据，用于展示宽口径板块内部结构", display=text,
    )


def _fetch_trigger_entity(code: str, provider: DataProvider | None = None) -> dict[str, FieldValue]:
    """触发实体（事件本体，可能是境外标的）自身的市场表现。

    与板块聚合、代表标的（rep_code）都无关——这里只对 `code` 自己算，
    结果全部挂 `触发标的_` 前缀，跟板块口径的字段分得清清楚楚，写正文时
    不会被误当成"板块整体表现"引用（同 #73 分析ETF自己数据 vs 板块聚合的道理）。

    覆盖面**因市场而异，逐字段独立尝试**，不预设"境外=只有价格没有基本面"——
    实测：韩股（SK海力士 000660.KS）基本面科目基本为空，但港股（腾讯0700.HK
    PE15.6倍、阿里9988.HK PE19.5倍）、美股（英伟达/苹果/台积电ADR）连PE都是真数。
    取不到的字段就不出现，不占位、不报错，不阻塞取到的那些。
    """
    from . import history

    provider = provider or get_provider()
    out: dict[str, FieldValue] = {}

    # 价格行为：直接对该代码算，不经过任何板块聚合，函数本就是给单一代码设计的
    v = history.volatility(code, provider=provider)
    if v.ok:
        out["触发标的_年化波动率"] = FieldValue(
            "触发标的_年化波动率", v.当前, True, f"iFinD·{code}近{v.窗口}日", "", "", "ok",
            "触发实体自身波动率，非板块数据", display=f"{v.当前:.1f}%")
        out["触发标的_波动率历史分位"] = FieldValue(
            "触发标的_波动率历史分位", v.分位, True, f"iFinD·{code}近3年", "", "", "ok",
            "触发实体自身波动率分位，非板块数据", display=f"{v.分位:.1f}%分位")

    rp = history.return_percentile(code, provider=provider)
    if rp.ok:
        out["触发标的_区间涨跌幅"] = FieldValue(
            "触发标的_区间涨跌幅", rp.当前值, True, f"iFinD·{code}{rp.起始}", "", "", "ok",
            "触发实体自身近20日涨跌，非板块数据", display=f"{rp.当前值:+.2f}%")

    # 基本面：能拿到就拿，逐个尝试、各自独立报告成败
    for field, ind, unit, scale in (
        ("触发标的_PE", "ths_pe_ttm_stock", "倍", 1),
        ("触发标的_PB", "ths_pb_latest_stock", "倍", 1),
        ("触发标的_归母净利同比", "ths_np_atsopc_yoy_stock", "%", 1),
        ("触发标的_总市值", "ths_market_value_stock", "亿元", 1e-8),
    ):
        try:
            r = provider.get_basic([code], [ind], "")
        except Exception:
            continue
        if not (r.ok and r.value not in (None, "", "--")):
            continue
        try:
            val = float(r.value) * scale
        except (TypeError, ValueError):
            continue
        # 0 当无效值处理：实测 000660.KS 的 ths_market_value_stock 返回
        # errorcode=0、值=0.0——不是报错，是"取到了却是空的"，PE/PB/市值/净利同比
        # 这几个字段业务上都不可能真为 0，当真实数据展示出去会误导人
        # （显示"总市值 0.00亿元"，读者会以为这是真数据，而不是取数失败）。
        if val == 0:
            continue
        out[field] = FieldValue(
            field, val, True, f"iFinD·{code}", "", ind, "ok",
            "触发实体自身数据，非板块数据",
            display=f"{val:.2f}{unit}" if unit != "%" else f"{val:+.2f}{unit}")
    return out


# 字段 → (序列名, 图标题, y轴标签, 缩放, 该字段的分位是否就是"所画序列当前点"的分位)
#
# 最后一项不能省。`波动率历史分位 82.2%` 说的是**波动率**在历史波动率序列里的分位，
# 而图上画的是价格指数——把 82.2% 标到指数点位旁边，等于宣称"当前指数点位处 82.2% 分位"，
# 是个看着合理、实则错误的数字。图比文字更难被读者核对，这种错最危险，
# 故只有分位与所画序列同源时才标注，否则图只讲走势、不写分位。
#
# `换手率近期高分位` 不单独配图：它与 `换手率历史分位` 共用同一条序列，
# 但它的分位描述的是"近三个月峰值"而非当前点，标上去同样是错位。
_AUTO_SERIES = {
    "PB历史分位": ("PB", "板块PB三年走势与当前分位", "PB(倍)", 1.0, True),
    "换手率历史分位": ("换手率", "板块换手率三年走势与当前分位", "换手率(%)", 1.0, True),
    "成交额历史分位": ("成交额", "板块成交额三年走势与当前分位", "成交额(亿元)", 1e-8, True),
    "区间涨跌幅分位": ("价格指数", "板块合成价格指数三年走势（市值加权，基点1000）",
                 "指数", 1.0, False),
    "波动率历史分位": ("价格指数", "板块合成价格指数三年走势（市值加权，基点1000）",
                 "指数", 1.0, False),
}


def _auto_series_charts(sector: str, fields: dict,
                        provider: DataProvider | None = None) -> dict[str, dict]:
    """给分位类字段配上真实历史序列图。取不到就不配，绝不用占位数据凑。

    降采样到约 240 点：一页通里图宽只有正文的六成，726 个点画上去挤成一团墨，
    而形状信息在 240 点上已经完全保留。
    """
    from . import history as h

    out: dict[str, dict] = {}
    需要 = [f for f in _AUTO_SERIES if f in fields and getattr(fields[f], "ok", False)]
    if not sector or not 需要:
        return out
    try:
        fr = h.sector_frames(sector, years=3, provider=provider)
    except Exception:
        return out
    if not fr.ok:
        return out

    step = max(1, len(fr.日期) // 240)
    for f in 需要:
        key, title, ylabel, scale, 分位同源 = _AUTO_SERIES[f]
        vals = getattr(fr, key, None)
        if not vals:
            continue
        pts = [{"标签": d, "值": v * scale}
               for d, v in list(zip(fr.日期, vals))[::step] if v]
        if len(pts) < 60:
            continue
        # 末点必须是真实最新值，降采样不能把它抽掉——图上标的"当前"就是它
        if pts[-1]["标签"] != fr.日期[-1]:
            pts.append({"标签": fr.日期[-1], "值": vals[-1] * scale})
        out[f] = {
            "类型": "hist_band", "标题": title, "y轴": ylabel,
            "数据点": pts,
            "当前值": vals[-1] * scale,
            "分位": getattr(fields[f], "value", None) if 分位同源 else None,
        }
    return out


def _structure_charts(sector: str, rep_code: str,
                      provider: DataProvider | None = None) -> dict[str, dict]:
    """板块结构类自动配图：成分股散点 + 子行业分组柱 + 波动率分布。

    这三张都从**已经取到的结构化数据**里直接生成，不经过 LLM——
    `成分股明细`/`子行业明细` 传给 writer 时是压扁的一句话，
    模型只能从里面挑三五个数写进正文，整体形态它既看不到也画不出。
    数据本来就是结构化的（`SectorAggregate.明细`），压扁再让模型还原本身就是浪费。
    """
    from . import aggregate as ag
    from . import history as h
    from . import universe

    out: dict[str, dict] = {}
    if not sector:
        return out

    # ① 成分股散点：PB × 净利同比。文字只能举三五只，散点能显示整批的形态与离群者。
    try:
        agg = ag.sector_aggregate(sector, provider=provider)
    except Exception:
        agg = None
    if agg is not None and agg.ok and agg.明细:
        pts = [{"标签": r.get("简称") or "", "x": r.get("PB"), "y": r.get("净利同比")}
               for r in agg.明细
               if r.get("PB") and r.get("净利同比") is not None]
        # 代表标的的简称从成分明细里反查——本函数调用时 ma.rep_name 还没赋值，
        # 依赖调用顺序拿名字迟早会拿到空串。
        rep_name = next((r.get("简称") or "" for r in agg.明细
                         if r.get("代码") == rep_code), "")
        if len(pts) >= 8:
            out["成分股明细"] = {
                "类型": "scatter", "标题": f"{sector}板块成分股：估值与盈利分布",
                "x轴": "PB(倍)", "y轴": "净利同比(%)", "高亮": rep_name or "",
                "数据点": pts,
            }

    # ② 子行业分组柱：宽口径板块专有，直接回答"哪个子行业强、哪个拖后腿"。
    parts = universe.broad_parts(sector)
    if parts:
        labels, pb, roe = [], [], []
        for sub in parts:
            try:
                a = ag.sector_aggregate(sub, top=20, provider=provider)
            except Exception:
                continue
            if not a.ok or not a.指标:
                continue
            labels.append(sub)
            pb.append(a.指标.get("PB"))
            roe.append(a.指标.get("ROE"))
        if len(labels) >= 3:
            # 用**双轴** bar_line 而非同轴分组柱：PB(倍) 与 ROE(%) 量纲不同，
            # 挤在一根轴上柱高的相对关系没有意义，却看着像有意义——
            # 与"数字卡升级须同量纲"是同一条原则，这里是我方自拟的规格，同样要守。
            out["子行业明细"] = {
                "类型": "bar_line", "标题": f"{sector}板块各子行业：盈利能力与估值",
                "柱标签": "ROE(%)", "线标签": "PB(倍)",
                "数据点": [{"标签": x, "值": r, "值2": p}
                        for x, r, p in zip(labels, roe, pb)
                        if r is not None and p is not None],
            }

    # ③ 波动率分布：厚尾变量，"处 82% 分位"在集中分布与长尾分布里含义完全不同，
    #    而波动率正是期权定价的核心输入，只给一个分位数字不够。
    try:
        v = h.sector_volatility(sector, provider=provider)
    except Exception:
        v = None
    if v is not None and v.ok and getattr(v, "序列", None):
        out["年化波动率"] = {
            "类型": "histogram", "标题": f"{sector}板块年化波动率三年分布",
            "x轴": "年化波动率(%)",
            "数据点": [{"值": x} for x in v.序列],
            "当前值": v.当前, "分位": v.分位,
        }
    return out


@dataclass
class Prepared:
    """①摸底 + ①b触发（+ 可选的文档抽取）的结果。人工勾选流程要先看到候选清单，
    再决定选哪几条，故把这几步单独暴露出来——**触发引擎含 peers/rotation/unlock
    等联网判定、文档抽取还要调 LLM，都很贵**，勾选后调 run() 时把这份结果原样
    传回去复用，保证一次生成只跑一遍。"""

    profile: dict[str, FieldValue]
    fired: list                      # list[thesis.Trigger]
    rep_code: str
    sector: str | None = None
    claims: list = dfield(default_factory=list)   # list[docs.DocClaim]，仅 with_docs 时有值


@dataclass
class Candidate:
    """统一编号的候选论证角度——数据触发的论点与研报提炼的观点并列。

    两者的可证伪性来源不同：`thesis` 由阈值机械判定，`doc` 靠分析师点开原文核对。
    故 doc 类候选**只在人工勾选模式下出现**，不参与自动挑选（DESIGN §7.5）。
    """

    kind: str                # "thesis" | "doc"
    id: str                  # V1 / doc_1
    名称: str = ""
    类别: str = ""
    方向: str = ""
    依据: str = ""           # 触发说明 或 研报原文
    出处: str = ""           # 仅 doc：机构《标题》日期 pN · 时效
    证据范围: str = ""       # 仅 doc：公司级 / 行业级 / 市场级
    证据主体: str = ""
    trigger: object = None
    claim: object = None


def prepare(rep_code: str, sector: str | None = None,
            provider: DataProvider | None = None, *,
            with_docs: bool = False, topic: str = "",
            trigger_code: str = "", trigger_name: str = "",
            analysis_etf: str = "",
            overrides=None) -> Prepared:
    """摸底取数 + 跑触发引擎（+ 可选抽取 sources/ 的研报），返回候选清单原料。

    with_docs 默认关闭：文档抽取要调 LLM、按篇计费，而自动模式根本不会用到
    文档观点（它们必须人工确认），跑了纯属浪费。只有 --pick 才打开。

    overrides：人工覆盖（`core.overrides.Overrides`）。**不参与触发判定**——
    它填的都是判定引擎不消费的字段（两者交集为空），只供 writer 引用。
    """
    from . import thesis as th

    provider = provider or get_provider()
    profile = fetch_profile(rep_code, sector, provider,
                            trigger_code=trigger_code, trigger_name=trigger_name,
                            analysis_etf=analysis_etf, overrides=overrides)
    try:
        fired = th.triggered_theses(profile)
    except Exception:
        fired = []

    claims: list = []
    if with_docs:
        from . import docs as dc
        from . import filings as fl

        # 先把代表标的最新公告下载进 sources/，再一并抽取。公告是交易所官方披露文件，
        # 合规风险远低于转载研报/新闻（详见 DESIGN §7.5）——但接口本身限制返回条数，
        # 每次只能拿到 1~3 条，故只当"最新动态"的补充来源，不指望覆盖历史公告。
        # 下载失败（无权限/无网络/无新公告）不阻塞后续研报抽取。
        try:
            fl.download_latest(rep_code, provider=provider)
        except Exception:
            pass

        # 传入主题让抽取只留相关观点。不传的话，每日通讯这类汇编里
        # 几十家不相干公司的业绩预告会一并抽出来污染候选清单（#56 实测）。
        主题串 = " ".join(x for x in [topic, sector or ""] if x).strip()
        try:
            for r in dc.extract_all(topic=主题串):
                claims.extend(r.通过)
        except Exception:
            claims = []
        for i, c in enumerate(claims, 1):
            c.id = f"doc_{i}"

        # C5/C6：研报里"事实陈述"型的板块联动观点，能翻译成 R2 结构的
        # 自动升级为机器验证过的论点，直接汇入 fired（不再是仅供人工勾选、
        # 原样引用的 doc 候选）。失败（含没有可用的联动观点）不阻塞主流程。
        try:
            fired.extend(th.doc_pattern_triggers(claims, provider=provider))
        except Exception:
            pass

    # sector 以 profile 里的为准——fetch_profile 已把主题名解析成行业名，
    # 存回来供 run() 的补充取数复用，避免两处各解析一次（结果可能不一致）
    return Prepared(profile=profile, fired=fired, rep_code=rep_code,
                    sector=profile.get("__sector__") or sector, claims=claims)


def candidates(prepared: Prepared) -> list[Candidate]:
    """把数据触发的论点与研报提炼的观点合成一份**统一编号**的候选清单。

    编号即列表下标+1，渲染与选号共用这一个顺序，避免两处各自排序而错位
    （与 thesis.group_for_pick 同一个教训）。
    """
    from . import thesis as th

    out: list[Candidate] = []
    for t in th.group_for_pick(prepared.fired):
        out.append(Candidate(
            kind="thesis", id=t.thesis.id, 名称=t.thesis.名称,
            类别=t.thesis.类别, 方向=t.thesis.方向 or t.thesis.特征.get("方向", ""),
            依据=t.说明, trigger=t,
        ))
    by_cat: dict[str, list] = {}
    for c in prepared.claims:
        by_cat.setdefault(c.类别, []).append(c)
    for items in by_cat.values():
        for c in items:
            out.append(Candidate(
                kind="doc", id=c.id, 名称=c.观点, 类别=c.类别, 方向=c.方向,
                依据=c.原文, 出处=f"{c.来源} p{c.页码} · {c.时效}",
                证据范围=c.证据范围, 证据主体=c.证据主体, claim=c,
            ))
    return out


_NUM_RE = __import__("re").compile(r"\d+(?:\.\d+)?\s*(?:%|亿|万亿|倍|pct|bp|pcts?|万|元)")


def _quality(c: Candidate) -> dict:
    """一条候选的**论据质量信号**（客观可数，不含对观点本身的判断）。"""
    txt = f"{c.名称} {c.依据}"
    nums = len(set(_NUM_RE.findall(txt)))
    stale = "⚠" in (c.出处 or "")
    has_chart = bool(getattr(getattr(c, "claim", None), "图表", None))
    return {"nums": nums, "stale": stale, "chart": has_chart}


def recommend(cands: list[Candidate], n: int = 3) -> list[int]:
    """给一份**建议组合**（返回序号）。

    ⚠ 排序依据只有**论据质量与结构**——带几个可引用数字、有无配图数列、
    时效是否过期、是否跨类别、方向是否平衡。**不判断观点本身对不对**，
    那是分析师的专业判断，系统替他判会挡掉真实的判断空间（DESIGN §7.4 同一原则）。
    故这里叫"建议"不叫"筛选"：全部候选照常列出，一条都不隐藏。
    """
    scored = []
    for i, c in enumerate(cands, 1):
        # 公司级材料可供分析师作为案例手动选择，但不能被“证据厚度”算法误推为
        # 行业报告的主轴。缺少行业/市场级证据时，系统宁可只建议数据触发项。
        if c.kind == "doc" and c.证据范围 == "公司级":
            continue
        q = _quality(c)
        s = min(q["nums"], 6) * 2                   # 可引用数字越多，正文越写得实
        s += 4 if c.kind == "thesis" else 0          # 自有数据可机械溯源，权重更高
        s += 3 if q["chart"] else 0                  # 带数列＝这条能配图
        s -= 6 if q["stale"] else 0                  # 过期研报降权
        scored.append((s, i, c))
    scored.sort(key=lambda x: (-x[0], x[1]))

    picked, cats, dirs = [], set(), []
    for s, i, c in scored:                           # 第一轮：每类至多一条，保证跨类别
        if len(picked) >= n or c.类别 in cats:
            continue
        picked.append((i, c)); cats.add(c.类别); dirs.append(c.方向 or "")
    # 方向平衡：若选中的全是同一方向，用一条反向的替掉分数最低的那条。
    # 卖方研报与模型都天然偏多，而这个产品最怕看漏下行风险（雪球敲入）。
    if picked and len({("看跌" in d) for d in dirs}) == 1:
        同向 = "看跌" in dirs[0]
        for s, i, c in scored:
            if i in {p[0] for p in picked}:
                continue
            if ("看跌" in (c.方向 or "")) != 同向:
                picked[-1] = (i, c)
                break
    return [i for i, _ in picked]


def render_candidates(cands: list[Candidate], *, with_hint: bool = True) -> str:
    """候选清单，按来源分两段、各自按类别分组，并给出概览与建议组合。"""
    if not cands:
        return "本次没有可用的论证角度（数据未触发任何论点，sources/ 也没有可用研报）。"

    lines: list[str] = []
    if with_hint:
        n_t = sum(1 for c in cands if c.kind == "thesis")
        n_d = len(cands) - n_t
        bull = sum(1 for c in cands if "看涨" in (c.方向 or ""))
        bear = sum(1 for c in cands if "看跌" in (c.方向 or ""))
        stale = sum(1 for c in cands if "⚠" in (c.出处 or ""))
        chart = sum(1 for c in cands if _quality(c)["chart"])
        lines.append(f"共 {len(cands)} 条候选（数据触发 {n_t} · 研报提炼 {n_d}）　"
                     f"方向：看涨 {bull} / 看跌 {bear} / 其它 {len(cands)-bull-bear}")
        extra = [f"{chart} 条自带配图数列"] if chart else []
        if stale:
            extra.append(f"{stale} 条研报已过 90 天（标 ⚠）")
        if extra:
            lines.append("　" + "；".join(extra))
        lines.append("")

    for kind, title in (("thesis", "数据触发（行情数据阈值判定）"),
                        ("doc", "研报提炼（请点开原文核对后再选）")):
        group = [(i, c) for i, c in enumerate(cands, 1) if c.kind == kind]
        if not group:
            continue
        lines.append(f"── {title} " + "─" * max(0, 46 - len(title) * 2))
        last = ""
        for i, c in group:
            if c.类别 != last:
                lines.append(f"【{c.类别}】")
                last = c.类别
            tag = f"　[{c.方向}]" if c.方向 else ""
            q = _quality(c)
            marks = []
            if q["chart"]:
                marks.append("📊可配图")
            if q["nums"]:
                marks.append(f"🔢{q['nums']}个数")
            if q["stale"]:
                marks.append("⚠已过期")
            mk = f"　{' '.join(marks)}" if marks else ""
            head = f"{c.id} {c.名称}" if kind == "thesis" else c.名称
            lines.append(f"  {i:>2}. {head}{tag}{mk}")
            if c.出处:
                lines.append(f"      {c.出处}")
            if kind == "doc" and c.证据范围:
                note = "仅可作公司案例，不可外推为板块结论" if c.证据范围 == "公司级" else "可作行业/市场层证据"
                lines.append(f"      证据边界：{c.证据范围}（{c.证据主体 or '主体待核'}）｜{note}")
            if c.依据:
                lines.append(f"      {'原文' if kind == 'doc' else '依据'}：{c.依据[:100]}")
        lines.append("")

    if with_hint:
        rec = recommend(cands)
        if rec:
            lines.append("── 建议组合（仅按论据质量与结构排序，未判断观点对错）──────")
            for i in rec:
                c = cands[i - 1]
                q = _quality(c)
                why = []
                if c.kind == "thesis":
                    why.append("自有数据可机械溯源")
                if q["nums"] >= 3:
                    why.append(f"含 {q['nums']} 个可引用数字")
                if q["chart"]:
                    why.append("自带配图数列")
                lines.append(f"  {i:>2}. {c.名称[:40]}　[{c.类别}·{c.方向 or '—'}]")
                if why:
                    lines.append(f"      理由：{'、'.join(why)}")
            lines.append("  ↑ 这只是按论据厚度给的起点。**哪几条论证成立是你的判断**，")
            lines.append("    系统不做筛选，上面 %d 条候选一条都没被隐藏。" % len(cands))
            lines.append("")
    return "\n".join(lines)


def select_candidates(cands: list[Candidate], picks: list[int]) -> list[Candidate]:
    """按序号取出被勾选的候选（1 起，越界与重复自动忽略）。"""
    out, seen = [], set()
    for p in picks:
        if 1 <= p <= len(cands) and p not in seen:
            seen.add(p)
            out.append(cands[p - 1])
    return out


def run(
    topic: str,
    topic_type: str,
    rep_code: str,
    *,
    client: DeepSeekClient | None = None,
    provider: DataProvider | None = None,
    genre: dict | None = None,
    context: dict | None = None,
    sector: str | None = None,
    analysis_etf: str = "",
    prepared: Prepared | None = None,
    chosen: list[str] | None = None,
    overrides=None,
) -> MarketAnalysis:
    """prepared: 已跑过的摸底+触发结果，传入即复用（不重复取数）。
    chosen:   人工勾选的论点 id；不传则由 planner 自动挑（保持原行为）。
    overrides: 人工覆盖；仅在 prepared 为空（本函数自行摸底）时生效，
              否则覆盖已在生成那份 prepared 时并入。"""
    client = client or DeepSeekClient()
    provider = provider or get_provider()
    g = genre or gr.get_genre(topic_type)   # 类型非法会报错

    # ①【数据先行】摸底：先把论点库判定所需的全部字段查一遍，不预设最后选哪条论点
    if prepared is None:
        prepared = prepare(rep_code, sector, provider,
                           analysis_etf=analysis_etf, overrides=overrides)
    profile = prepared.profile

    # 被勾选的研报观点（chosen 里 doc_ 打头的那些）
    doc_chosen = [c for c in prepared.claims
                  if chosen and c.id in set(chosen)]

    # ②【顺序已调整】规划：触发引擎按这份真实数据筛出候选论点
    #   —— 无 chosen 时 planner 自己挑 2~3 条；有 chosen 时挑选权归分析师，planner 只措辞
    #   （planner 内部仍守铁律：论点文本不得引用画像里的具体数字）
    plan = planner.plan(topic, topic_type, client, genre=g, context=context,
                        profile=profile, fired=planner.triggers_to_spec(prepared.fired),
                        chosen=chosen, doc_claims=doc_chosen)
    if not plan.ok:
        return MarketAnalysis(plan=plan, rep_code=rep_code, ok=False, error=plan.error,
                              tokens=client.total_tokens, data_vol=getattr(provider, "total_data_vol", 0))

    # ③ 补充取数：规划中出现、但摸底没覆盖的字段（多来自自由槽新拟的字段名）
    missing = [f for f in plan.取数清单
               if f not in profile and not f.startswith("__")
               and not f.startswith(DOC_FIELD_PREFIX)]      # 研报字段不走取数层
    extra: list[FieldValue] = []
    if missing:
        # 用 profile 里已解析的行业名，别用入参的原始主题名——否则补充取数会绕开解析
        extra, _gaps, _prov = fetcher.fetch_fields(
            missing, rep_code, provider, profile.get("__sector__") or sector,
            analysis_etf=str(profile.get("__etf__") or ""),
        )
    fv_map = {**profile, **{fv.field: fv for fv in extra}}

    # ③a 触发实测数据：把判定时算出的证据包装成 FieldValue 交给下游。
    #
    # 60 条已接判定的论点里，**36 条不声明 schema 字段**——M/R/F/V/E 类中那些
    # 自己调 macro / rotation / unlock / flows / peers / fundamentals 取数的判定。
    # 它们算出的真实数据（10Y国债收益率变动、未来3月解禁、板块毛利率、机构持股比例、
    # 同业PE偏离、FY1预测PE、历史胜率…）只存在于 Trigger.证据 里，
    # 而这一项此前在 pipeline/planner/writer 中**一次都没被读取过**，整个丢掉。
    #
    # 后果：选中这类论点时，writer 手上关于该条逻辑的结构化数据是空的——
    # 正文只能写虚，`_THESIS_CHART` 给 M1 指定的 line 图也没有数据点可画。
    # #44 加"板块全景"补数据时只覆盖了 fetcher 取到的字段，这 36 条一条都没覆盖到。
    #
    # 只接入**被选中论点**的证据，不是全部触发结果——否则 fv_map 与板块全景会被
    # 十几条未入选论点的数据稀释，反而冲淡正文焦点。
    选中 = {lg.逻辑id for lg in plan.logics}
    for t in prepared.fired:
        if t.thesis.id not in 选中:
            continue
        for k, v in (t.证据 or {}).items():
            v = str(v).strip()
            # 已有同名真实取数时不覆盖：fetcher 的值口径更明确（带 source/as_of）
            if not v or (k in fv_map and getattr(fv_map[k], "ok", False)):
                continue
            fv_map[k] = FieldValue(
                field=k, value=v, ok=True, source=f"论点{t.thesis.id}判定",
                status="ok", note=t.说明[:80], display=v,
            )

    # 研报观点：把**逐字原文**包装成 FieldValue，走与自动取数完全相同的下游通道。
    # 这样 writer 能像引用行情数据一样引用研报里的数字，validator 也照常溯源——
    # 它的 _allowed_values 会把 display 里的所有数字收进允许集，一行都不用改。
    for c in doc_chosen:
        fname = f"{DOC_FIELD_PREFIX}{c.id}"
        # 所引表格行必须一并进 display：validator 的 _allowed_values 是从 display
        # 收数字的，表格数若不在里面，writer 一引用就会被判成无出处而拦下——
        # 而它其实是有出处的（已逐字回查过文档）。
        text = c.原文 + (f"\n{c.表格原文}" if getattr(c, "表格原文", "") else "")
        fv_map[fname] = FieldValue(
            field=fname, value=text, ok=True, source=c.来源,
            as_of=c.文档日期, status="ok",
            note=f"p{c.页码} · {c.时效}", display=text,
        )

    # ③b 给每条研报逻辑配上可佐证的自有数据。
    # 研报给的是**产业机制**（渗透率、技术路线、海外业绩），我方数据给的是
    # **市场状态**（估值、盈利、资金、波动）；只有前者，正文里就是一段孤立引用，
    # 读者无从判断它与本标的当下状态的关系。两条通道此前只在勾选清单层面汇合，
    # 进了正文各写各的——这里把它们在**单条逻辑内**接起来。
    # 注意只挂已取到的字段：writer 那边"缺失数据不得作为论据"的铁律仍然适用。
    from . import thesis as th

    # 触发实测数据要挂到**该条逻辑自己的**所需字段上。只进 fv_map 的话，
    # 它们只会落进"板块全景"那个公共池（标着"勿偏题"），writer 不会把它们
    # 当成这条逻辑的论据来用——数据在手边却用不上，等于没修。
    ev_keys = {t.thesis.id: [k for k in (t.证据 or {})] for t in prepared.fired}
    for lg in plan.logics:
        for f in ev_keys.get(lg.逻辑id, []):
            if f in fv_map and getattr(fv_map[f], "ok", False) \
                    and f not in lg.所需数据字段:
                lg.所需数据字段.append(f)

    有值 = {k for k, v in fv_map.items() if getattr(v, "ok", False)}
    id2claim = {c.id: c for c in doc_chosen}
    for lg in plan.logics:
        c = id2claim.get(lg.逻辑id)
        if c is None:
            continue
        for f in th.companion_fields(c.类别, available=有值):
            if f not in lg.所需数据字段:
                lg.所需数据字段.append(f)

    # ④ 回填到每条逻辑
    logics = [
        LogicWithData(logic=lg, fields=[fv_map[f] for f in lg.所需数据字段 if f in fv_map])
        for lg in plan.logics
    ]

    ma = MarketAnalysis(
        plan=plan, rep_code=rep_code, logics=logics, field_values=fv_map,
        doc_charts={c.id: c.图表 for c in doc_chosen if c.图表},
        doc_cats={c.id: c.类别 for c in doc_chosen if c.类别},
        doc_claims=list(doc_chosen),
        auto_charts={
            **_auto_series_charts(profile.get("__sector__") or sector or "",
                                  fv_map, provider),
            **_structure_charts(profile.get("__sector__") or sector or "",
                                rep_code, provider),
        },
        tokens=client.total_tokens, data_vol=getattr(provider, "total_data_vol", 0),
        ok=True,
    )
    ma.挂钩择优 = _pick_underlying(ma, topic, topic_type, context, plan, client, provider)
    return ma


def _pick_underlying(ma, topic, topic_type, context, plan, client, provider):
    """挂钩标的择优（B4 / §9.2②），**仅在板块→ETF 映射给不出答案时**才跑。

    板块类需求（消费/半导体/券商…）由 `underlying_for()` 给出确定答案，
    此时分析对象与挂钩标的本就是同一只 ETF，再择一次优只会引入不一致。
    而产业趋势/事件驱动这两类的分析对象本身不可交易，映射是必需的一步——
    参考模板 AI→科创50/双创50、长鑫IPO→中证500 都专门用一节讲这个映射的理由。

    失败不阻塞报告：这一节空着是 A1 的既有状态，不该因为择优失败就整份报告失败。
    """
    if ma.field_values.get("__etf__"):
        return None
    from . import selection as sel

    try:
        return sel.propose(topic, topic_type, context=context,
                           direction=getattr(plan, "整体方向", "") or "",
                           client=client, provider=provider)
    except Exception:
        return None


def _explicit_etf_from_brief(b) -> str:
    """仅当用户原文**实际点名**时，返回可交易 ETF 作为显式分析标的。

    `候选标的`由需求解析器提出，不能反过来被当成用户指令。否则用户只说“消费板块”
    时，模型若把酒ETF放进候选池，就会错误覆盖“消费 → 消费ETF”的既有映射。名称或
    代码必须出现在原始需求中，才可优先于板块映射；代表标的仍优先选个股供基本面取数。
    """
    from . import universe

    confirmed = str(getattr(b, "确认挂钩标的", "") or "").strip()
    confirmed_type = str(getattr(b, "确认挂钩标的类型", "") or "")
    if confirmed and "ETF" in confirmed_type.upper():
        return confirmed
    raw = str(getattr(b, "原始需求", "") or "").upper()
    for t in getattr(b, "候选标的", []) or []:
        code = str(getattr(t, "代码", "") or "").upper()
        name = str(getattr(t, "名称", "") or "").strip()
        explicitly_named = (code and code in raw) or (name and name.upper() in raw)
        if getattr(t, "可用", False) and explicitly_named and universe._is_fund(code):
            return t.代码
    return ""


def prepare_from_brief(b, *, provider: DataProvider | None = None,
                       with_docs: bool = False, overrides=None) -> Prepared | None:
    """从需求解析结果做摸底+触发，供人工勾选。代表标的不可用时返回 None。"""
    explicit_etf = _explicit_etf_from_brief(b)
    t = b.代表标的
    # 分析师确认的 ETF 是本次研究对象与定价标的；不再暗中换回一只行业龙头作“数据锚点”。
    if explicit_etf:
        from .brief import TargetRef
        from . import instruments as _inst
        item = _inst.get(explicit_etf)
        t = TargetRef(item.简称 if item else explicit_etf, explicit_etf, "ok:分析师确认ETF")
    if t is None or not t.可用:
        return None
    # 主题串给文档抽取做相关性过滤：主题 + 全部涉及板块，比只给一个板块名更全
    # （"半导体、芯片、存储芯片"三个都带上，避免研报里说"存储"就被判为不相关）
    主题 = " ".join(x for x in [getattr(b, "研究主题", ""), *(b.涉及板块 or [])] if x)
    trig = getattr(b, "触发实体", None)
    trigger_code = trig.代码 if trig is not None and trig.可用 else ""
    trigger_name = trig.名称 if trig is not None and trig.可用 else ""
    return prepare(t.代码, (b.涉及板块[0] if b.涉及板块 else None), provider,
                   with_docs=with_docs, topic=主题,
                   trigger_code=trigger_code, trigger_name=trigger_name,
                   analysis_etf=explicit_etf,
                   overrides=overrides)


def run_from_brief(
    b, *, client: DeepSeekClient | None = None, provider: DataProvider | None = None,
    prepared: Prepared | None = None, chosen: list[str] | None = None,
    overrides=None,
) -> MarketAnalysis:
    """从需求解析结果（core.brief.Brief）直接跑：混合体裁 + 需求背景 + 真实代表标的。"""
    # 当前取数/ETF 白名单均为 A 股口径。港股与跨市场必须由分析师在确认页明确
    # 指定研究口径和可交易工具，绝不能悄悄退化为代表个股所属的 A 股行业。
    confirmation = getattr(b, "市场确认", None)
    if getattr(b, "市场范围", "A股") != "A股":
        return MarketAnalysis(plan=None, rep_code="", ok=False,
            市场确认=confirmation,
            error=(f"已确认按{b.市场范围}研究，但当前研究层尚无该市场的行业基本面数据链。"
                   "系统已停止，不会静默映射为 A 股行业；如需继续，请在确认页明确选择 A 股研究口径。"))
    from . import market_confirmation
    if confirmation is None and market_confirmation.needs_confirmation(b):
        return MarketAnalysis(plan=None, rep_code="", ok=False,
            error="该需求属于高风险口径，必须先完成分析师确认，不能直接进入研究链。")

    # 事件影响报告不能只拿“事件名称 + A 股板块行情”拼接。此处再做一次后端硬校验，
    # 即使未来 GUI/脚本绕过 main.py，也无法生成一份没有事件本体与传导依据的成品。
    from . import event_evidence
    evidence = getattr(overrides, "事件证据", None) or event_evidence.parse(None)
    gate = event_evidence.assess(b, evidence)
    if gate.required and not gate.ready:
        entity = getattr(getattr(b, "触发实体", None), "名称", "该事件主体")
        return MarketAnalysis(plan=None, rep_code="", ok=False,
            error=gate.message(entity))
    explicit_etf = _explicit_etf_from_brief(b)
    target = b.代表标的
    if explicit_etf:
        from .brief import TargetRef
        item = __import__("core.instruments", fromlist=["get"]).get(explicit_etf)
        target = TargetRef(item.简称 if item else explicit_etf, explicit_etf, "ok:分析师确认ETF")
    if target is None or not target.可用:
        ma = MarketAnalysis(plan=None, rep_code="", ok=False,
                            error="需求中没有可用的代表标的（代码待确认）")
        return ma

    # 人工已填的外部事实从"待补"移到"可引用"两侧——留在待补里 writer 会被告知
    # "不得编造"而回避它，那就白填了；移过去才真正进入可用数据。
    填好的 = dict(getattr(overrides, "外部事实", {}) or {})
    仍缺 = [x for x in b.外部事实待补 if x not in 填好的]

    ctx = {
        # 禁止把原始口语整段送入研究链：其中可能包含客户点名的产品结构。
        "研究需求": getattr(b, "研究主题", "") or b.主题,
        "研究主题（细分对象）": getattr(b, "研究主题", "") or b.主题,
        "研究篮子口径": getattr(b, "研究篮子口径", "") or "、".join(b.涉及板块 or []),
        "触发事件": b.触发事件,
        "用户关注点": b.关注点,
        "涉及板块": b.涉及板块,
        "市场判断查证": [
            {"说法": c.说法, "查证": c.查证, "依据": c.依据} for c in b.市场判断
        ],
        "外部事实待补(不得编造,人工填写)": 仍缺,
    }
    if 填好的:
        ctx["外部事实_分析师已人工填写_可直接引用"] = 填好的
    research_topic = getattr(b, "研究主题", "") or b.主题
    ma = run(research_topic, b.主导类型, target.代码, client=client, provider=provider,
             genre=b.混合体裁, context=ctx,
             sector=(b.涉及板块[0] if b.涉及板块 else None),
             analysis_etf=explicit_etf,
             prepared=prepared, chosen=chosen, overrides=overrides)
    ma.外部事实待补 = 仍缺
    ma.外部事实已填 = 填好的
    event_evidence.attach_to_analysis(ma, b, evidence)
    ma.rep_name = target.名称          # 正文首次提及要写名称，只有代码读者认不出
    ma.板块理由 = getattr(b, "板块理由", "")
    ma.客户产品诉求 = getattr(b, "客户产品诉求", "")
    ma.市场确认 = confirmation
    ma.确认挂钩标的 = getattr(b, "确认挂钩标的", "")
    ma.仅研究 = bool((confirmation or {}).get("research_only"))
    if ma.确认挂钩标的 or ma.仅研究:
        ma.挂钩择优 = None
    return ma


if __name__ == "__main__":  # python -m core.pipeline
    TOPIC, TYPE, CODE = "券商板块投资机会", gr.TYPE_SECTOR, "600030.SH"  # 中信证券代表券商板块
    ma = run(TOPIC, TYPE, CODE, sector="证券")
    print(f"主题: {ma.plan.主题} | 类型: {ma.plan.类型} | 整体方向: {ma.plan.整体方向}\n"
          f"叙事主轴: {ma.plan.叙事主轴}\n"
          f"代表标的: {ma.rep_code} | ok={ma.ok} {ma.error}\n")

    for lw in ma.logics:
        lg = lw.logic
        print(f"[{lg.来源}] {lg.逻辑id} · {lg.标题}  ({lg.结构方向倾向})")
        print(f"    论点: {lg.具体论点}")
        for fv in lw.fields:
            if fv.ok:
                print(f"      ✔ {fv.field}: {fv.display or str(fv.value)[:28]}")
            else:
                print(f"      … {fv.field}: 待人工补充")
        print()

    auto = sum(len(lw.auto) for lw in ma.logics)
    total = sum(len(lw.fields) for lw in ma.logics)
    print(f"字段引用合计 {total}（含跨逻辑重复）；缺口字段 {len(ma.gap_fields)} 个: {ma.gap_fields}")
    print(f"成本：DeepSeek {ma.tokens} tokens · iFinD {ma.data_vol} dataVol")
