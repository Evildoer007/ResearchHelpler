"""需求解析（brief intake）：把一段口语化需求解析成结构化选题。

真实场景（2026-07-29 需求变更）：选题**以人工给定为主**，且给的往往不是一个主题词，
而是老板/销售/客户的一段话，例如：
  "昨晚SK海力士发了业绩，全球芯片科技板块异动明显，相较上半年涨幅最近明显回调。
   针对SK海力士业绩预期和发布落地后市场的影响，有无最近投资相关板块的建议？"

本模块把它解析成 Brief（主题/类型/事件/板块/标的/关注点），再喂给 planner→fetcher→writer。
`topics.py`（自动扫市场选题）由主路径降为**辅助**：无明确需求时给建议，或为本模块提供信号。

三条与防幻觉直接相关的处理：
1. **市场观察须用真实数据查证**：需求里的口头判断（"最近明显回调"）会对照信号中的
   区间涨跌与资金流，输出"支持/不支持/数据不足"，而不是照单全收。
2. **外部事实不编造**：海外公司业绩等我方数据源查不到的事实，列入"外部事实待补"，
   走人工填写（DESIGN §9），LLM 不得臆造任何数字。
3. **标的代码实测校验**：复用 topics 的校验（代码须真实且简称与名称相符）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dfield

from llm.client import DeepSeekClient

from . import signals as sg
from . import genres as gr
from . import topics
from .provider import DataProvider, get_provider


@dataclass
class MarketClaim:
    """需求中的市场判断 + 数据查证结果。"""

    说法: str
    查证: str = ""        # 支持 / 不支持 / 数据不足
    依据: str = ""


@dataclass
class TargetRef:
    名称: str
    代码: str = ""
    校验: str = ""

    @property
    def 可用(self) -> bool:
        return self.校验.startswith("ok")


@dataclass
class Brief:
    原始需求: str
    主题: str = ""
    主导类型: str = ""
    附加类型: list[str] = dfield(default_factory=list)
    触发事件: str = ""
    关注点: str = ""
    涉及板块: list[str] = dfield(default_factory=list)
    宽口径成分行业: list[str] = dfield(default_factory=list)  # 大类拆解，已逐个校验可取数
    宽口径弃用行业: list[str] = dfield(default_factory=list)  # 拆出来但校验没过的，须让人看见
    板块理由: str = ""      # 为什么选这个板块。印进报告，让读者知道分析对象怎么来的
    市场判断: list[MarketClaim] = dfield(default_factory=list)
    候选标的: list[TargetRef] = dfield(default_factory=list)
    外部事实待补: list[str] = dfield(default_factory=list)
    tokens: int = 0
    ok: bool = False
    error: str = ""

    @property
    def 代表标的(self) -> TargetRef | None:
        """优先选**个股**，基金/ETF 只作最后兜底。

        代表标的要承担两件事：一是给板块聚合当口径基准，二是给 `PB历史分位`、
        `年化波动率`、`换手率分位` 这类**个股级派生字段**提供取数对象。
        ETF 两件都干不了——基金没有 PB/ROE/净利同比，历史序列取回来是 0 个点，
        `resolve_sector` 拿它查行业还会返回垃圾值（实测芯片ETF 查出"医疗服务"，
        导致整份芯片报告的成分股全是医药股，见 #61）。

        此前只按"第一个可用"取，而候选顺序由 LLM 给出——本次恰好是
        [芯片ETF, 韦尔股份(代码校验失败), 兆易创新]，于是选中了 ETF，
        后面一连串字段跟着失效。
        """
        from .universe import _is_fund

        可用 = [t for t in self.候选标的 if t.可用]
        for t in 可用:                      # 个股优先
            if not _is_fund(t.代码):
                return t
        if 可用:
            return 可用[0]
        return self.候选标的[0] if self.候选标的 else None

    @property
    def 混合体裁(self) -> dict:
        return gr.merged_genre(self.主导类型, self.附加类型)


_SYSTEM = """你是券商研究部的"需求解析器"。用户（老板/销售/客户）会用口语提出一个投资研究需求，
你要把它解析成一份结构化选题，供后续自动生成《场外衍生品投资策略》一页通。
铁律：
1. **不得编造任何数字或事实**，不要凭记忆写出任何具体数值。
1.1 **"外部事实待补"只填我方数据源确实拿不到的**，例如：海外公司业绩、海外市场表现、
   研报口径的产业数据（渗透率、资本开支、算力对比）、政策细节。
   **以下均可由系统自动取得，禁止列入待补**：A股任意板块或个股的区间涨跌幅、资金流向、
   估值（PB/PE）、盈利（ROE/净利润同比）、分红（分红总额/分红率/股息率）、市值、成交额等标准字段
   ——即使今日信号中未列出该板块，取数阶段仍可精确获取。
2. **用户的市场判断要用给定"市场信号"查证**：对每条判断给出 支持/不支持/数据不足，
   并引用信号中的板块与数值作依据。
   若该板块未出现在今日信号中，写"待取数验证"（而非"数据不足"）——取数阶段会精确获取；
   不要附和也不要否定。
3. 需求常同时涉及事件与板块机会：选一个**主导类型**，其余相关的放"附加类型"（可为空）。
4. "涉及板块"必须是**A股口径**的板块名，优先直接取自给定信号中出现的板块名称。
4.1 **按业务对口映射，不按概念联想。** 需求由海外公司或事件引发时，
   取**主营业务重叠最直接**的那个 A 股板块，不要跳到下游或配套环节。
   例：SK海力士主营存储芯片 → 应映射「半导体 / 存储芯片」；
   映射成「元件 / 印制电路板 / 被动元件」是错的——那是 AI 硬件的配套环节，
   与存储芯片没有直接业务重叠，据此取的成分股和估值都答非所问。
4.15 **需求问的是大类时，就填大类名，不要自作主张收窄到某个子行业。**
   例：问"最近消费板块如何" → 填「消费」，**不要**填「食品饮料」——
   消费还包含家用电器、商贸零售、社会服务、纺织服饰、美容护理，
   只取食品饮料就是以偏概全，而报告标题仍会写"消费板块"，口径与标题对不上。
   反过来，需求明确指向某个子行业时（"白酒还能买吗"）就填那个子行业，不要放大。
4.16 **填了大类名时，必须同时填 `宽口径成分行业`**，列出这个大类由哪几个
   **A股一级行业**构成（消费/周期/科技/金融/医药大健康 等都属大类）。
   - 只填**一级行业名**，不要填概念名、二级行业名或另一个大类名
     （对：食品饮料、家用电器、商贸零售；错：白酒、消费电子、必选消费）。
   - 宁缺勿滥：**只填你确定属于该大类的**。系统会逐个校验这些名字在数据源里
     取不取得到成分股，取不到的会被丢弃；但校验只能查出"名字不存在"，
     查不出"名字存在但归错类"——把汽车塞进医药不会报错，只会算出一个错的板块。
   - 不是大类时留空数组，不要为普通行业硬凑成分。
4.2 **必须填 `板块理由`**：一句话说明这个板块为何与需求直接相关
   （如"SK海力士为全球第二大 DRAM 厂商，A股存储芯片板块与其同处存储产业链"）。
   这句会印进报告，让读者知道分析对象是怎么选出来的；说不清理由就说明映射有问题。
5. 候选标的给 A 股龙头个股或板块 ETF，附证券代码（如 600030.SH / 300750.SZ）；不确定就留空，不要编代码。
只输出一个 JSON 对象，不要多余文字。"""


def _build_prompt(text: str, bundle: sg.SignalBundle) -> str:
    ff = (bundle.data or {}).get("sector_fund_flow") or {}
    spec = {
        "用户需求原文": text,
        "今日市场信号": {
            "板块区间表现与资金流": ff,
            "行业当日异动": (bundle.data or {}).get("industry_movers"),
        },
        "可选类型": gr.list_types(),
        "输出格式": {
            "主题": "研报标题，简洁专业，体现事件与板块",
            "主导类型": "板块机会|产业趋势|事件驱动",
            "附加类型": ["可为空；需求同时涉及的其它类型"],
            "触发事件": "需求中的引发事件，一句话",
            "关注点": "用户真正想知道什么",
            "涉及板块": ["A股口径板块名，优先取自信号；按业务对口而非概念联想"],
            "宽口径成分行业": ["仅当涉及板块是大类（消费/周期/科技…）时填：它由哪几个A股一级行业构成；否则空数组"],
            "板块理由": "一句话：这个板块为何与需求直接相关（会印进报告）",
            "市场判断": [
                {"说法": "用户原话中的判断", "查证": "支持|不支持|数据不足",
                 "依据": "引用信号中的板块与数值"}
            ],
            "候选标的": [{"名称": "标的名", "代码": "600030.SH，不确定留空"}],
            "外部事实待补": ["我方数据源查不到、需人工提供的事实（如某海外公司业绩具体数据）"],
        },
    }
    return json.dumps(spec, ensure_ascii=False, default=str)


def _resolve_broad(b: Brief, d: dict, provider: DataProvider) -> None:
    """处理大类拆解：文件里已定义的优先，没定义的用 LLM 拆解 + 逐个校验后登记。

    为什么不是纯查表：大类聚合（消费/周期/科技/金融…）没有官方标准，
    穷举一张表是无底洞，表里没有的就退化成"下钻到某个一级行业"——
    正是 #67 那个以偏概全。让 LLM 拆、再拿数据源逐个校验，表就从
    **白名单**变成**缓存**：命中表走研究部认过的口径，没命中也能正确处理。

    校验能挡住的是"名字数据源不认"，挡不住"名字对但归错类"（把汽车塞进医药
    不会报错）。故自动拆解的口径在报告里标注为"自动拆解"，与文件里的既定口径
    区分开，并把成分行业全列出来让人一眼能否决。
    """
    from . import universe

    if not b.涉及板块:
        return
    name = b.涉及板块[0]
    if universe.is_broad(name):        # 文件里已有既定口径，不覆盖
        b.宽口径成分行业 = universe.broad_parts(name)
        return

    proposed = [str(x).strip() for x in (d.get("宽口径成分行业") or []) if str(x).strip()]
    proposed = [p for p in proposed if p != name]
    if len(proposed) < 2:              # 没给或只给一个，说明不是大类，按普通板块走
        return

    ok, bad = universe.validate_industries(proposed, provider=provider)
    b.宽口径弃用行业 = bad
    if len(ok) < 2:                    # 校验后不足两个，不成其为宽口径，退回普通板块
        return
    b.宽口径成分行业 = universe.register_broad(name, ok)


def parse(
    text: str,
    *,
    bundle: sg.SignalBundle | None = None,
    client: DeepSeekClient | None = None,
    provider: DataProvider | None = None,
    verify_codes: bool = True,
) -> Brief:
    """解析一段口语化需求为结构化选题。"""
    b = Brief(原始需求=text.strip())
    client = client or DeepSeekClient()
    if not client.available():
        b.error = "未配置 DeepSeek key，无法解析需求"
        return b

    bundle = bundle or sg.collect_signals(with_news=False)
    res = client.chat_json(_SYSTEM, _build_prompt(text, bundle), temperature=0.3)
    b.tokens = client.total_tokens
    if not res.ok or not isinstance(res.data, dict):
        b.error = res.error or "LLM 返回非预期结构"
        return b

    d = res.data
    b.主题 = str(d.get("主题", "")).strip()
    pt = str(d.get("主导类型", "")).strip()
    b.主导类型 = pt if pt in gr.list_types() else gr.TYPE_SECTOR
    b.附加类型 = [t for t in (d.get("附加类型") or []) if t in gr.list_types() and t != b.主导类型]
    b.触发事件 = str(d.get("触发事件", "")).strip()
    b.关注点 = str(d.get("关注点", "")).strip()
    b.涉及板块 = [str(x).strip() for x in (d.get("涉及板块") or []) if str(x).strip()]
    b.板块理由 = str(d.get("板块理由", "")).strip()
    b.外部事实待补 = [str(x).strip() for x in (d.get("外部事实待补") or []) if str(x).strip()]
    b.市场判断 = [
        MarketClaim(说法=str(c.get("说法", "")).strip(), 查证=str(c.get("查证", "")).strip(),
                    依据=str(c.get("依据", "")).strip())
        for c in (d.get("市场判断") or []) if isinstance(c, dict)
    ]
    b.候选标的 = [
        TargetRef(名称=str(t.get("名称", "")).strip(), 代码=str(t.get("代码", "")).strip())
        for t in (d.get("候选标的") or []) if isinstance(t, dict)
    ]

    if verify_codes:
        provider = provider or get_provider()
        _resolve_broad(b, d, provider)
        for t in b.候选标的:
            t.校验 = topics._verify_code(t.代码, t.名称, provider)

        # LLM 常留空或给错代码（守规矩不编，但不可用）。此时不靠模型记忆，
        # 改用 iFinD 按"涉及板块"取真实龙头（已剔除次新股，见 universe / DESIGN §9.1）。
        #
        # 触发条件是"**没有可用的个股**"而不是"没有可用标的"：LLM 给宽口径板块时
        # 常常只给得出 ETF（问"消费板块如何"，候选就一个消费ETF），ETF 本身校验能过，
        # 于是这条兜底不触发，代表标的落到基金上——而基金没有 PB/ROE/净利同比，
        # `PB历史分位` 这类个股级派生字段直接取不到（实测本次缺口正是它）。
        # 按板块取龙头个股才是这条兜底该管的事，见 `代表标的` 属性的同类说明（#61）。
        from .universe import _is_fund
        有可用个股 = any(t.可用 and not _is_fund(t.代码) for t in b.候选标的)
        if not 有可用个股 and b.涉及板块:
            from . import universe
            lead = universe.pick_representative(b.涉及板块[:3], provider=provider)
            if lead:
                b.候选标的.insert(0, TargetRef(
                    名称=lead.简称, 代码=lead.代码,
                    校验=f"ok:{lead.简称}（系统按板块选取·市值{lead.市值亿元}亿）",
                ))

    b.ok = True
    return b


def render(b: Brief) -> str:
    if not b.ok:
        return f"解析失败：{b.error}"
    types = "＋".join([b.主导类型] + b.附加类型) if b.附加类型 else b.主导类型
    lines = [
        f"【需求解析】{b.主题}",
        f"  类型：{types}（主导：{b.主导类型}）",
        f"  触发事件：{b.触发事件}",
        f"  关注点：{b.关注点}",
        f"  涉及板块：{'、'.join(b.涉及板块) or '—'}"
        + (f"（{b.板块理由}）" if b.板块理由 else ""),
    ]
    # 宽口径必须把成分行业摊开给人看——口径是这份报告最容易被质疑的地方，
    # 尤其自动拆解出来的，得让分析师一眼能否决。
    if b.宽口径成分行业:
        from . import universe
        名 = b.涉及板块[0] if b.涉及板块 else ""
        lines.append(f"  宽口径展开：{'、'.join(b.宽口径成分行业)}"
                     f"（{len(b.宽口径成分行业)}个一级行业合并）")
        lines.append(f"    依据：{universe.classification_basis(名)}")
    if b.宽口径弃用行业:
        lines.append(f"  ⚠ 拆解中被丢弃（数据源取不到成分股）：{'、'.join(b.宽口径弃用行业)}")
    lines.append("  市场判断查证：")
    for c in b.市场判断:
        lines.append(f"    · {c.说法}\n        → {c.查证}：{c.依据}")
    lines.append("  候选标的：")
    for t in b.候选标的:
        lines.append(f"    · {t.名称} {t.代码}  {'✔' if t.可用 else '⚠'}（{t.校验}）")
    if b.外部事实待补:
        lines.append("  外部事实待补（人工填写，不得由模型编造）：")
        lines += [f"    · {x}" for x in b.外部事实待补]
    return "\n".join(lines)


if __name__ == "__main__":  # python -m core.brief
    demo = ("昨天晚上SK海力士发了业绩，全球市场芯片科技板块异动明显，"
            "相较于上半年的涨幅最近明显回调。针对SK海力士业绩预期和发布落地后市场的影响，"
            "有无最近投资相关板块的建议？")
    print(render(parse(demo)))
