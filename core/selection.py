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


# ---------------- 择优（B4：接入报告生成，§9.2②）----------------

@dataclass
class Pick:
    代码: str
    简称: str = ""
    理由: str = ""
    适合: str = ""          # 这个选项对应什么配置偏好（多选项时用于区分）
    指标: dict = dfield(default_factory=dict)
    展示: dict = dfield(default_factory=dict)


@dataclass
class Proposal:
    picks: list[Pick] = dfield(default_factory=list)
    择优维度: list[str] = dfield(default_factory=list)
    说明: str = ""
    候选数: int = 0
    tokens: int = 0
    ok: bool = False
    error: str = ""


# 择优这一步**必须给候选池**才能推理，否则模型只能凭记忆编代码
# （已两次踩坑：思源电气→中国石化、证券ETF代码给错）。故池子与真实指标一并给出，
# 且返回的代码逐个回查池子，不在池中的直接丢弃。
_SYSTEM_PICK = """你是场外衍生品的"挂钩标的择优器"。
给你一个**真实存在、代码已校验**的候选池（含实测指标），请为本次报告选出挂钩标的。

铁律（违反即作废）：
1. **只能从候选池里选**，代码必须与池中完全一致。绝对不许写池子里没有的代码或名称。
2. **理由只能引用候选池里给出的真实指标**，不得引入池外数字，更不得编造。
   指标缺失（显示为空）的维度不要拿来当理由。
3. 选 **1~3 个**。选多个时，必须说明每个各自**适合什么配置偏好**
   （如"集中硬科技暴露"vs"跨板均衡配置"），而不是罗列几个差不多的。
4. **择优维度要明说**，且应贴合本次主题。典型维度：主题暴露度（该标的多大比例
   落在本次分析的产业/板块上）、估值水平、成分股市值与流动性、波动弹性。
5. 若池中确实没有能表达本次主题的标的，`挂钩候选` 给空数组，并在 `说明` 里
   写清为什么——**宁可承认没有合适标的，也不要硬挑一个不相关的**。
6. 不要给期权结构、期限、报价建议——那由交易台决定，不在你的职责内。
只输出一个 JSON 对象，不要多余文字。"""


def _pick_prompt(topic: str, topic_type: str, cands: list[Candidate],
                 context: dict | None, direction: str) -> str:
    import json

    pool = []
    for c in cands:
        row = {"代码": c.代码, "简称": c.简称, "类型": c.类型,
               "标签": c.标签, "说明": c.说明}
        row.update({k: v for k, v in c.展示.items() if v})
        pool.append(row)
    spec = {
        "本次主题": topic,
        "报告类型": topic_type,
        **({"需求背景": context} if context else {}),
        **({"报告整体方向": direction} if direction else {}),
        "候选池_只能从这里选_指标为实测值": pool,
        "输出格式": {
            "择优维度": ["本次据以比较的维度，2~4 个"],
            "挂钩候选": [
                {"代码": "必须与候选池中完全一致",
                 "理由": "为什么是它，只引用候选池里的真实指标",
                 "适合": "这个选项对应什么配置偏好（只有一个候选时可留空）"}
            ],
            "说明": "一句话总结择优结论；池中无合适标的时在此说明原因",
        },
    }
    return json.dumps(spec, ensure_ascii=False, indent=1)


def propose(topic: str, topic_type: str = "", *, context: dict | None = None,
            direction: str = "", client=None,
            provider: DataProvider | None = None) -> Proposal:
    """从候选池择优出本次的挂钩标的（B4 / DESIGN §9.2②）。

    用途是板块类需求之外的那两类：产业趋势与事件驱动的**分析对象本身不可交易**
    （"AI 产业景气""IPO 的流动性冲击"都挂不了），必须映射到一只有该暴露的
    可交易标的上，而这一步映射正是报告的价值所在（参考模板 AI→科创50/双创50、
    长鑫IPO→中证500 都专门用一节讲这个映射的理由）。

    板块类需求不必走这里：`instruments.underlying_for()` 已经给出确定答案。

    实现上**一次 LLM 调用**：先把整个候选池的实测指标批量取回（配额可忽略，
    见 §11 额度说明），连池子一起喂给模型，避免"先让模型缩小范围、再取数、
    再让模型选"的两次调用。模型返回的代码逐个回查池子，编的直接丢弃。
    """
    from llm.client import DeepSeekClient

    p = Proposal()
    provider = provider or get_provider()
    client = client or DeepSeekClient()
    if not client.available():
        p.error = "未配置 DeepSeek key，无法择优"
        return p

    cands = compare(codes=[i.代码 for i in inst.INSTRUMENTS], provider=provider)
    p.候选数 = len(cands)
    if not cands:
        p.error = "候选池取数失败，无法比较"
        return p

    res = client.chat_json(_SYSTEM_PICK,
                           _pick_prompt(topic, topic_type, cands, context, direction),
                           temperature=0.3)
    p.tokens = client.total_tokens
    if not res.ok or not isinstance(res.data, dict):
        p.error = res.error or "LLM 返回非预期结构"
        return p

    by_code = {c.代码: c for c in cands}
    p.择优维度 = [str(x).strip() for x in (res.data.get("择优维度") or []) if str(x).strip()]
    p.说明 = str(res.data.get("说明", "")).strip()
    for it in (res.data.get("挂钩候选") or [])[:3]:
        if not isinstance(it, dict):
            continue
        code = str(it.get("代码", "")).strip()
        c = by_code.get(code)
        if c is None:              # 池子里没有 = 模型编的，丢弃（铁律1）
            continue
        p.picks.append(Pick(
            代码=code, 简称=c.简称,
            理由=str(it.get("理由", "")).strip(),
            适合=str(it.get("适合", "")).strip(),
            指标=dict(c.指标), 展示=dict(c.展示),
        ))
    p.ok = True
    return p


def render_proposal(p: Proposal) -> str:
    if not p.ok:
        return f"择优失败：{p.error}"
    if not p.picks:
        return f"候选池 {p.候选数} 个中未选出合适标的：{p.说明 or '（未说明）'}"
    out = [f"择优维度：{'、'.join(p.择优维度) or '—'}（候选池 {p.候选数} 个）"]
    for k in p.picks:
        seg = "、".join(f"{n}{v}" for n, v in k.展示.items() if v)
        out.append(f"  · {k.简称}（{k.代码}）{('｜' + k.适合) if k.适合 else ''}")
        if seg:
            out.append(f"      实测：{seg}")
        out.append(f"      理由：{k.理由}")
    if p.说明:
        out.append(f"  结论：{p.说明}")
    return "\n".join(out)


if __name__ == "__main__":  # python -m core.selection
    print("【场景】某事件冲击科技成长股，需选一个挂钩标的表达\n")
    cands = compare(tags=[inst.T_TECH, inst.T_GROWTH])
    print(render(cands))
