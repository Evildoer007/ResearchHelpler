"""⓪选题：市场信号 → DeepSeek 归纳候选主题 → 人工勾选。

流程位置（DESIGN §3）：整条流水线的最前端。
  signals(真实市场信号) → 本模块归纳候选主题 → 【人工勾选】 → planner → ...

防幻觉延续（本模块的关键约束）：
  - LLM 只能基于喂进去的**真实信号**归纳主题，不得凭记忆声称"最近某某板块很热"；
  - 每个候选主题必须给出"信号依据"，引用信号包里的具体数据（板块、涨跌幅、资金流）；
  - 建议代表标的的证券代码会**用 iFinD 实际校验**（能否取到简称、是否与名称相符），
    校验不过的标为待确认，不直接拿去取数。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field as dfield

from llm.client import DeepSeekClient

from . import signals as sg
from . import genres as gr
from .provider import DataProvider, get_provider


@dataclass
class TopicCandidate:
    主题: str
    类型: str
    推荐理由: str
    信号依据: str
    建议标的: str = ""
    建议标的代码: str = ""
    代码校验: str = ""     # ok:简称 / 待确认:原因

    @property
    def 可直接取数(self) -> bool:
        return self.代码校验.startswith("ok")


@dataclass
class TopicSlate:
    date: str
    candidates: list[TopicCandidate] = dfield(default_factory=list)
    signal_summary: str = ""
    tokens: int = 0
    ok: bool = False
    error: str = ""


_SYSTEM = """你是券商研究部的"选题器"，负责从当日真实市场信号中提炼值得撰写《场外衍生品投资策略》一页通的候选主题。
铁律：
1. 只能基于给定的"市场信号"归纳主题，绝不能凭记忆或常识声称某板块近期表现如何——所有判断必须来自信号数据。
2. 每个候选主题必须给出"信号依据"，引用信号中的具体板块名与数值（如"数字芯片设计10日-18.39%但主力净流入163.34亿"）。
2.1 **"推荐理由"同样只能陈述信号所显示的事实**，严禁补充信号中不存在的因果解释、政策背景、
    供需判断或行业前景（例如信号里没有政策信息，就不得写"政策支持驱动景气回升"）。
    可以说"资金逆势流入"，不可以说"因为某某政策"。
2.2 优先使用**区间(10日)**信号提炼主题；单日涨跌属短期波动，除非幅度极端否则不足以支撑一篇研报选题。
3. 主题的"类型"必须是给定三类之一，且要选最贴合的：
   - 板块机会：某板块超跌/低估但基本面或资金面改善，适合修复配置
   - 产业趋势：某产业中期景气向上、有持续驱动
   - 事件驱动：具体事件（IPO、解禁、政策等）带来的窗口期机会
4. 建议代表标的：优先用信号中出现的龙头个股或该板块代表性ETF；给出名称与证券代码
   （A股代码格式如 600030.SH / 300750.SZ）。不确定就留空，不要编造代码。
5. 提炼 3~5 个候选，覆盖不同类型与不同板块，不要都挤在同一条产业链上。
只输出一个 JSON 对象，不要多余文字。"""


def _build_user_prompt(bundle: sg.SignalBundle) -> str:
    spec = {
        "日期": bundle.date,
        "市场信号": bundle.data,
        "可选类型": gr.list_types(),
        "输出格式": {
            "候选主题": [
                {
                    "主题": "如：半导体设备板块超跌修复机会（简洁、像研报副标题）",
                    "类型": "板块机会|产业趋势|事件驱动",
                    "推荐理由": "为什么值得现在写，一两句",
                    "信号依据": "引用信号中的具体板块与数值",
                    "建议标的": "标的名称",
                    "建议标的代码": "如 600030.SH，不确定留空",
                }
            ]
        },
    }
    return json.dumps(spec, ensure_ascii=False, default=str)


def _normalize_name(s: str) -> str:
    """归一化证券名称，便于比对：去空格与新股/风险警示前缀。"""
    s = str(s).strip().replace(" ", "").replace("　", "")
    for p in ("*ST", "ST", "C", "N", "U", "XD", "DR"):
        if s.startswith(p) and len(s) > len(p):
            s = s[len(p):]
    return s


def _etf_theme_alias_matches(actual: str, suggested: str) -> bool:
    """判断 ETF 的口语主题简称是否与官方基金名一致。

    动态发现的 ETF 不一定在 ``INSTRUMENTS`` 人工目录中，因此不能只依赖人工
    别名。例如“智能汽车ETF”对应的官方名可为“富国中证智能汽车主题ETF”。
    代码已经由数据源核验后，只要去掉 ETF 后的主题简称完整出现在官方名称中，
    就是同一工具的合理简称，不应误报“名称不符”。

    此规则只用于 ETF，且要求主题词至少 3 个字符；不能放宽到普通个股，避免
    “中信”之类短词把真实但无关的代码张冠李戴。
    """
    actual_normalized = _normalize_name(actual)
    suggested_normalized = _normalize_name(suggested)
    if not actual_normalized.endswith("ETF"):
        return False
    actual_theme = re.sub(r"ETF$", "", actual_normalized, flags=re.IGNORECASE)
    suggested_theme = re.sub(r"ETF$", "", suggested_normalized, flags=re.IGNORECASE)
    return len(suggested_theme) >= 3 and suggested_theme in actual_theme


def _verify_code(code: str, name: str, provider: DataProvider) -> str:
    """校验代码是否真实**且与建议标的名称相符**。

    只查"能否取到简称"是不够的——LLM 可能给出一个真实但张冠李戴的代码
    （实测：思源电气被写成 600028.SH，那其实是中国石化）。若不比对名称，
    就会拿错公司的数据去写整篇报告。
    """
    code = (code or "").strip()
    if not code:
        return "待确认:未给代码"
    r = provider.get_basic([code], ["ths_stock_short_name_stock"])
    if not (r.ok and r.value):
        return f"待确认:{r.error or '代码取不到简称'}"

    actual = str(r.value)
    a, b = _normalize_name(actual), _normalize_name(name)
    if not b:
        return f"待确认:未给标的名称(该代码为{actual})"
    if a == b or a in b or b in a:
        return f"ok:{actual}"
    if _etf_theme_alias_matches(actual, name):
        return f"ok:{actual}（ETF主题简称已核验）"
    # ETF 同时有交易简称和基金全称。iFinD 往往返回后者，例如 512000.SH 的
    # “华宝中证全指证券公司ETF”，而用户/LLM自然会写交易简称“券商ETF”。
    # 两者并不矛盾；若代码在已校验的工具目录中，必须拿目录别名做第二次比对，
    # 不能把真实的显式 ETF 错误拦下，再悄悄退回板块默认 ETF。
    try:
        from . import instruments
        instrument = instruments.get(code)
    except Exception:  # noqa: BLE001 - 校验目录不可用时保留原有严格行为
        instrument = None
    if instrument is not None:
        aliases = [instrument.简称, instrument.官方名]
        if any((normalized := _normalize_name(alias)) and
               (normalized == b or normalized in b or b in normalized) for alias in aliases):
            return f"ok:{actual}（交易简称/基金全称别名已核验）"
    return f"名称不符:代码{code}实为「{actual}」，与建议标的「{name}」不一致"


def propose(
    bundle: sg.SignalBundle | None = None,
    *,
    client: DeepSeekClient | None = None,
    provider: DataProvider | None = None,
    verify_codes: bool = True,
) -> TopicSlate:
    """从市场信号提炼候选主题（含代码校验）。"""
    bundle = bundle or sg.collect_signals(with_news=False)
    slate = TopicSlate(date=bundle.date, signal_summary=bundle.summary())

    client = client or DeepSeekClient()
    if not client.available():
        slate.error = "未配置 DeepSeek key，无法选题"
        return slate
    if not bundle.data:
        slate.error = f"未采集到任何市场信号（{bundle.errors}）"
        return slate

    res = client.chat_json(_SYSTEM, _build_user_prompt(bundle), temperature=0.5)
    slate.tokens = client.total_tokens
    if not res.ok or not isinstance(res.data, dict):
        slate.error = res.error or "LLM 返回非预期结构"
        return slate

    cands: list[TopicCandidate] = []
    for it in res.data.get("候选主题", []):
        if not isinstance(it, dict):
            continue
        t = str(it.get("类型", "")).strip()
        cands.append(TopicCandidate(
            主题=str(it.get("主题", "")).strip(),
            类型=t if t in gr.list_types() else gr.TYPE_SECTOR,
            推荐理由=str(it.get("推荐理由", "")).strip(),
            信号依据=str(it.get("信号依据", "")).strip(),
            建议标的=str(it.get("建议标的", "")).strip(),
            建议标的代码=str(it.get("建议标的代码", "")).strip(),
        ))

    if verify_codes and cands:
        provider = provider or get_provider()
        for c in cands:
            c.代码校验 = _verify_code(c.建议标的代码, c.建议标的, provider)

    slate.candidates = cands
    slate.ok = True
    return slate


def render_slate(slate: TopicSlate) -> str:
    """把候选主题渲染成可读清单，供人工勾选。"""
    lines = [f"【{slate.date} 候选主题】{slate.signal_summary}", ""]
    if not slate.ok:
        return f"选题失败：{slate.error}"
    for i, c in enumerate(slate.candidates, 1):
        mark = "✔可取数" if c.可直接取数 else "⚠需人工确认标的"
        lines += [
            f"  [{i}] {c.主题}   〔{c.类型}〕",
            f"      理由：{c.推荐理由}",
            f"      依据：{c.信号依据}",
            f"      标的：{c.建议标的} {c.建议标的代码}  {mark}（{c.代码校验}）",
            "",
        ]
    return "\n".join(lines)


def select(slate: TopicSlate, indices: list[int]) -> list[TopicCandidate]:
    """人工勾选：按 1 起的序号选出主题（数量不固定，DESIGN §2）。"""
    out = []
    for i in indices:
        if 1 <= i <= len(slate.candidates):
            out.append(slate.candidates[i - 1])
    return out


if __name__ == "__main__":  # python -m core.topics
    s = propose()
    print(render_slate(s))
    print(f"tokens: {s.tokens}")
