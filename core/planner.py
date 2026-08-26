"""planner：论点规划（第一个调 DeepSeek 的环节）。

输入：主题 + 类型 + **本次被真实数据触发的论点**，调 DeepSeek 产出结构化"论证计划"。
输出：每条逻辑 = {逻辑id, 标题, 来源, 具体论点(不含数字), 所需数据字段, 方向倾向}
      + 叙事主轴 + 整体方向 + 取数清单（去重，交给 fetcher）。

铁律（DESIGN 设计理念）：planner 只规划、不写正文、不碰任何数字。

候选来源（DESIGN §7.3）：**论点库触发结果**，不是体裁模板。
  旧版骨架（已删除的 skeletons.py）规定"每类型固定 3 条主轴必须全部保留"，
  其直接后果是同一类型的报告永远在论证同样三件事——"来来回回那几个观点"。现改为：
  - 主轴：从 113 条论点库中**被本次真实数据触发**的那些里选 2~3 条，尽量跨类别；
          不同标的的触发组合天然不同，报告视角随数据变化。
  - 可选池：未入选的已触发论点，最多留 1 条作补充。
  - 自由槽：触发结果覆盖不到的重要角度，LLM 可新增（来源标"自由槽"）。
  体裁（genres.py）在此只剩"类型 + 行文口吻参考"两个作用，不再决定论证什么。
  人工沉淀（自由槽好论点回库）留待带审核界面时做，本模块只负责产出、标注来源。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dfield

from llm.client import ChatResult, DeepSeekClient

from . import schema
from . import genres as gr

SRC_SPINE = "主轴"
SRC_POOL = "可选池"
SRC_FREE = "自由槽"

# C4：这些论点对同一次报告里的所有标的判定结果完全相同——M 类只看宏观数据、
# E4 只看法定披露日历，跟标的本身毫无关系。LLM 选主轴时按"尽量跨类别"择优，
# 这批论点因为总来自"新类别"而显得有吸引力，实测 M3b/M6 常驻触发、频繁双双入选，
# 挤占了真正体现标的差异化的角度。硬性限制自动模式下最多 1 条入选正文主轴。
_MARKET_LEVEL_IDS = {"E4", "M1", "M2", "M3", "M3b", "M4", "M4b", "M6", "M7"}

# 研报观点在取数清单里的字段名前缀。pipeline 据此把它们从取数层排除
# （值不是查出来的，是研报原文），并直接包装成 FieldValue 交给 writer。
DOC_FIELD_PREFIX = "研报依据·"


@dataclass
class PlanLogic:
    逻辑id: str
    标题: str
    来源: str
    具体论点: str
    所需数据字段: list[str]
    结构方向倾向: str


@dataclass
class ArgumentPlan:
    主题: str
    类型: str
    logics: list[PlanLogic] = dfield(default_factory=list)
    叙事主轴: str = ""      # 这几条论点串起来的整体故事，由 planner 按本篇实际选中的论点现场生成
    整体方向: str = ""
    取数清单: list[str] = dfield(default_factory=list)
    未知字段: list[str] = dfield(default_factory=list)  # 不在 schema 里的字段(多来自自由槽)
    ok: bool = False
    error: str = ""


_SYSTEM = """你是场外衍生品投资策略研报的"论点规划器"。
职责：从**已被真实数据触发的论点**中，挑选并组织出本篇报告的论证计划。
铁律：
1. 只做规划，不写正文，绝对不要编造或给出任何具体数字（数字后续由真实数据填充）。
2. **正文主轴从"已触发论点"中选 2~3 条**（不多不少），来源标"主轴"。挑选优先级：
   ① 与本主题/需求最相关；② **尽量跨类别**（如估值+资金+情绪 优于三条都是估值类），
   保证报告有多个观察视角；③ 相互之间能串成一条论证链。
   为每条写"具体论点"：本主题下这条论点要说明什么，一两句，**不含数字**。
3. 未选入主轴的已触发论点，最多再留 1 条标"可选池"作一句话补充；其余舍弃。
4. 若某条已触发论点方向与其它相反（如"估值低分位"看涨 + "盈利恶化"看跌），
   这是市场的真实状态：**不要只挑同向的**，应如实呈现分歧，在"整体方向"中给出权衡后的判断。
5. 若已触发论点少于 2 条，或本主题有重要角度是已触发论点未覆盖的，
   可新增"自由槽"论点补足（来源标"自由槽"），但必须是数据画像能支撑的角度，不得凭空杜撰。
6. "已知数据画像"是系统实际查到的真实数据，**只用于帮你判断挑哪几条、措辞轻重**，
   绝不能把其中的具体数字写进"具体论点"文本——数字由后续环节据真实数据填入正文。
7. "叙事主轴"：用一句话概括这 2~3 条论点串起来的整体故事（如"超跌修复+盈利支撑"）。
8. 逻辑id 直接用已触发论点的 id（如 V1/S3/R1）；自由槽自拟为 free_1、free_2。
只输出一个 JSON 对象，不要输出任何多余文字。"""


# 人工勾选模式：选哪几条由分析师定，planner 只负责**措辞与串联**。
# 与自动模式的唯一差别是铁律 2/3/5——挑选权已交出，不得增删主轴。
_SYSTEM_PICKED = """你是场外衍生品投资策略研报的"论点规划器"。
本次**正文主轴已由分析师人工选定**，你的职责不是挑选，而是把选定的论点组织成论证计划。
铁律：
1. 只做规划，不写正文，绝对不要编造或给出任何具体数字（数字后续由真实数据填充）。
2. **"人工选定的正文主轴"里的每一条都必须输出，且来源标"主轴"；不得增加、删除或替换。**
   即使你认为某条不合适，也要如实写出——分析师的判断优先。
3. 为每条写"具体论点"：本主题下这条论点要说明什么，一两句，**不含数字**。
4. 若选定的几条方向相反（如"估值低分位"看涨 + "盈利恶化"看跌），
   这是市场的真实状态：**不要圆场**，应如实呈现分歧，在"整体方向"中给出权衡后的判断。
5. 不要新增自由槽论点，也不要补可选池——本次论点范围已由人工圈定。
6. "已知数据画像"是系统实际查到的真实数据，**只用于帮你把握措辞轻重**，
   绝不能把其中的具体数字写进"具体论点"文本。
7. "叙事主轴"：用一句话概括这几条论点串起来的整体故事（如"超跌修复+盈利支撑"）。
8. 逻辑id 必须与人工选定的 id 完全一致。
9. 选定项里 id 形如 `doc_N` 的是**研报观点**，它带有一段研报原文。
   为它写"具体论点"时，**只能依据那段原文**，不得引入原文之外的任何信息、
   不得推理演绎、不得补充你自己知道的行业知识；同样不得写具体数字。
   若其“证据范围”为**公司级**，它只能作为明确点名该公司的案例，不能写成
   “行业盈利改善”“板块景气上行”等行业结论，也不能据此决定整体方向。
只输出一个 JSON 对象，不要输出任何多余文字。"""


def _format_profile(profile: dict | None) -> dict[str, str]:
    """把摸底数据格式化成给 planner 看的展示串（仅供判断，不进入论点文本）。"""
    if not profile:
        return {}
    return {field: (fv.display if fv.ok and fv.display else "暂无数据")
            for field, fv in profile.items()
            if not field.startswith("__")}   # 跳过 __code__/__sector__ 等内部键


def _format_triggers(profile: dict | None) -> list[dict]:
    """跑论点库触发引擎，把**已被真实数据触发**的论点整理给 planner 优先选用。

    这是"论点多样性"的核心（DESIGN §7.3）：不给 planner 固定的槽位去填，
    而是从数据实际支持的论点中挑——不同标的触发组合不同，报告自然不再千篇一律。
    """
    if not profile:
        return []
    from . import thesis as th

    try:
        fired = th.triggered_theses(profile)
    except Exception:
        return []
    return triggers_to_spec(fired)


def triggers_to_spec(fired) -> list[dict]:
    """thesis.Trigger 列表 → planner 输入格式。

    单独抽出来是为了让 pipeline 能把**已算好**的触发结果直接喂进来——
    触发引擎含联网判定，人工勾选流程下不能为了规划再触发一次。
    """
    from . import thesis as th

    return [
        {"论点id": t.thesis.id, "名称": t.thesis.名称, "类别": t.thesis.类别,
         "方向": t.thesis.方向, "触发依据": t.说明,
         "市场状态特征": t.thesis.特征, "所需数据字段": th.fields_of(t.thesis.id),
         # 判定时算出的实测数据。60 条已接判定的论点里有 36 条**不声明 schema 字段**
         # （M/R/F/V/E 类中自己调 macro/rotation/unlock/flows/peers/fundamentals 取数的那些），
         # 它们的数据只存在于这里。此前这一项被整个丢掉，导致选中这类论点时
         # writer 手上没有任何结构化数据可用，正文只能写虚、图表没有数据点可画。
         "实测数据": dict(t.证据 or {})}
        for t in fired
    ]


def _build_user_prompt(topic: str, genre: dict, vocab: list[str], ctx: dict | None = None,
                       profile: dict | None = None, fired: list[dict] | None = None,
                       chosen: list[str] | None = None,
                       doc_claims: list | None = None) -> str:
    """构造 planner 输入 —— **以"已触发论点"为唯一候选来源**（DESIGN §7.3）。

    体裁（genres.py）在此只剩两个作用：给出主题类型、以及一句行文口吻参考；
    它不规定"必须论证哪三条"——那正是"来来回回那几个观点"的根源。
    候选来自 113 条论点库中被本次真实数据触发的那些，不同标的触发组合不同。
    """
    # 触发引擎含 peers/rotation 等联网判定，由调用方算一次传进来，勿在此重复触发
    fired = _format_triggers(profile) if fired is None else fired
    spec = {
        "主题": topic,
        "类型": genre["类型"],
        "体裁参考_仅供行文口吻参考_不规定必须论证什么": genre["叙事主轴"],
        **({"需求背景": ctx} if ctx else {}),
        **({"已知数据画像_仅供判断选谁_论点文本中不得引用其中数字": _format_profile(profile)}
           if profile else {}),
        "可用字段词表": vocab,
    }
    if chosen:
        picked = [f for f in fired if f["论点id"] in set(chosen)]
        for c in (doc_claims or []):
            picked.append({
                "论点id": c.id, "名称": c.观点, "类别": c.类别, "方向": c.方向,
                "来源": "研报提炼（非数据触发）",
                "研报原文_具体论点只能依据这段": c.原文,
                "出处": f"{c.来源} p{c.页码}",
                "证据范围": getattr(c, "证据范围", "公司级"),
                "证据主体": getattr(c, "证据主体", ""),
            })
        spec["人工选定的正文主轴_必须全部输出_不得增删"] = picked
        spec["候选说明"] = (
            f"分析师已选定以上 {len(picked)} 条作为正文主轴"
            f"（其中研报提炼 {len(doc_claims or [])} 条）。"
            "请为每条写出本主题下的具体论证表述，并串成叙事主轴、给出整体方向。"
        )
    else:
        spec["已被真实数据触发的论点_正文主轴只能从这里选"] = fired
        spec["候选说明"] = (
            f"以上 {len(fired)} 条论点的触发条件均已由本次查得的真实数据校验通过。"
            "请按铁律2从中挑 2~3 条组成正文主轴，尽量分属不同类别以保证视角多元。"
            if fired else
            "本次没有论点被真实数据触发（数据缺失较多），请按铁律5用自由槽补足，"
            "并在措辞上保持克制、不预设结论。"
        )
    spec["输出格式"] = {
        "论证计划": [
            {
                "逻辑id": "直接用已触发论点的id（如 V1/S3/R1）；自由槽自拟如 free_1",
                "标题": "字符串",
                "来源": f"{SRC_SPINE}|{SRC_POOL}|{SRC_FREE}",
                "具体论点": "本主题下这条论点要论证什么，一两句，不含数字",
                "所需数据字段": ["字段名", "..."],
                "结构方向倾向": "看涨|震荡|看跌|中性",
            }
        ],
        "叙事主轴": "一句话概括这几条论点串起来的整体故事",
        "整体方向": "看涨|震荡|看跌|中性（各论点方向权衡后的汇总）",
    }
    return json.dumps(spec, ensure_ascii=False, indent=2)


def _postprocess(topic: str, genre: dict, raw: dict,
                 fired: list[dict] | None = None,
                 chosen: list[str] | None = None,
                 doc_claims: list | None = None) -> ArgumentPlan:
    """把 LLM 输出规整成 ArgumentPlan，并把主轴条数约束在 2~3 条。

    体裁不参与兜底（DESIGN §7.3）：缺条数时从**本次已触发的论点**里补，
    而不是补回旧版那三条固定主轴——否则"来来回回那几个观点"会从后门回来。
    """
    plan = ArgumentPlan(主题=topic, 类型=genre["类型"])
    items = raw.get("论证计划", []) if isinstance(raw, dict) else []
    fired = fired or []
    fired_map = {f["论点id"]: f for f in fired}

    logics: list[PlanLogic] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        lid = str(it.get("逻辑id", "")).strip()
        src = str(it.get("来源", "")).strip() or SRC_SPINE
        # 纠偏：论点库里没有的 id 一律算自由槽——LLM 偶尔会自拟 id 却标成"主轴"，
        # 那样它就绕过了"主轴必须有真实数据触发"这道闸。
        if src == SRC_SPINE and lid not in fired_map:
            src = SRC_FREE
        logics.append(PlanLogic(
            逻辑id=lid,
            标题=str(it.get("标题", "")).strip(),
            来源=src,
            具体论点=str(it.get("具体论点", "")).strip(),
            所需数据字段=[str(f).strip() for f in it.get("所需数据字段", []) if str(f).strip()],
            结构方向倾向=str(it.get("结构方向倾向", "")).strip(),
        ))

    if chosen:
        # 人工勾选模式：主轴范围已由分析师圈定，代码只做**对齐**，不做增删裁决。
        # ① 选中的一律标主轴；② planner 违反铁律5多写的一律丢弃（不降级为可选池——
        #    降级等于把分析师没选的论点又塞回正文）；③ 漏写的从触发结果补出来。
        want = list(dict.fromkeys(chosen))
        doc_map = {c.id: c for c in (doc_claims or [])}
        kept = {lg.逻辑id: lg for lg in logics if lg.逻辑id in set(want)}
        for lg in kept.values():
            lg.来源 = SRC_SPINE
        logics = []
        for cid in want:
            lg = kept.get(cid)
            if lg is None:                      # planner 漏写的，从候选补出来
                if cid in doc_map:
                    c = doc_map[cid]
                    lg = PlanLogic(逻辑id=cid, 标题=c.观点, 来源=SRC_SPINE,
                                   具体论点=c.观点, 所需数据字段=[],
                                   结构方向倾向=c.方向)
                else:
                    f = fired_map.get(cid)
                    if f is None:
                        continue
                    lg = PlanLogic(逻辑id=cid, 标题=f["名称"], 来源=SRC_SPINE,
                                   具体论点=f.get("触发依据", ""),
                                   所需数据字段=list(f.get("所需数据字段", [])),
                                   结构方向倾向=f.get("方向", ""))
            # 研报观点的证据就是那段原文，字段名固定，不容 LLM 自拟
            if cid in doc_map:
                lg.所需数据字段 = [f"{DOC_FIELD_PREFIX}{cid}"]
            logics.append(lg)
        spine_items = logics
    else:
        spine_items = [lg for lg in logics if lg.来源 == SRC_SPINE]
    if not chosen and len(spine_items) > 3:
        # 超过3条：保留前3(LLM输出顺序即优先级)，其余降级为可选池，不丢弃内容
        for extra in spine_items[3:]:
            extra.来源 = SRC_POOL
        spine_items = spine_items[:3]

    if not chosen and len(spine_items) < 2:
        # 不足2条：从已触发论点按触发顺序补足。**跨类别优先**——
        # 补进来的若与现有主轴同类别，报告仍是单一视角，达不到"多样性"的目的。
        have = {lg.逻辑id for lg in logics}
        cats = {fired_map[lg.逻辑id]["类别"] for lg in spine_items if lg.逻辑id in fired_map}
        pending = [f for f in fired if f["论点id"] not in have]
        for f in sorted(pending, key=lambda x: x["类别"] in cats):
            if len(spine_items) >= 2:
                break
            new_lg = PlanLogic(
                逻辑id=f["论点id"], 标题=f["名称"], 来源=SRC_SPINE,
                具体论点=f.get("触发依据", ""), 所需数据字段=list(f.get("所需数据字段", [])),
                结构方向倾向=f.get("方向", ""),
            )
            logics.append(new_lg)
            spine_items.append(new_lg)
            cats.add(f["类别"])

    if not chosen:
        # C4：市场级论点最多 1 条入选正文主轴，多出的降级为可选池，
        # 并尽量用非市场级的已触发论点补回原有条数（跨类别优先），
        # 补不到就少一条——好过让"同类论点"顶替真正区分标的的角度。
        market_in_spine = [lg for lg in spine_items if lg.逻辑id in _MARKET_LEVEL_IDS]
        if len(market_in_spine) > 1:
            target = len(spine_items)
            demoted = market_in_spine[1:]     # 保留 LLM 排在最前的一条
            for lg in demoted:
                lg.来源 = SRC_POOL
            spine_items = [lg for lg in spine_items if lg not in demoted]
            have = {lg.逻辑id for lg in spine_items} | {lg.逻辑id for lg in demoted}
            # ① 优先从已有的可选池/自由槽里捞非市场级候选，不凭空多算一次触发
            for lg in logics:
                if len(spine_items) >= target:
                    break
                if lg.来源 == SRC_SPINE or lg.逻辑id in have or lg.逻辑id in _MARKET_LEVEL_IDS:
                    continue
                lg.来源 = SRC_SPINE
                spine_items.append(lg)
                have.add(lg.逻辑id)
            # ② 仍不够，从触发结果里补新的（同前面 <2 分支的补法：跨类别优先）
            if len(spine_items) < target:
                cats = {fired_map[lg.逻辑id]["类别"] for lg in spine_items if lg.逻辑id in fired_map}
                pending = [f for f in fired
                          if f["论点id"] not in have and f["论点id"] not in _MARKET_LEVEL_IDS]
                for f in sorted(pending, key=lambda x: x["类别"] in cats):
                    if len(spine_items) >= target:
                        break
                    new_lg = PlanLogic(
                        逻辑id=f["论点id"], 标题=f["名称"], 来源=SRC_SPINE,
                        具体论点=f.get("触发依据", ""), 所需数据字段=list(f.get("所需数据字段", [])),
                        结构方向倾向=f.get("方向", ""),
                    )
                    logics.append(new_lg)
                    spine_items.append(new_lg)
                    have.add(new_lg.逻辑id)
                    cats.add(f["类别"])

    # 主轴缺字段时用论点库声明的字段依赖补齐——LLM 有时只填一两个，
    # 缺了字段这条论点在正文里就没数字可摆。
    for lg in logics:
        if lg.逻辑id in fired_map and not lg.所需数据字段:
            lg.所需数据字段 = list(fired_map[lg.逻辑id].get("所需数据字段", []))

    plan.logics = logics
    plan.叙事主轴 = str(raw.get("叙事主轴", "")).strip()
    plan.整体方向 = str(raw.get("整体方向", "")).strip()

    # 汇总取数清单（去重保序），并标出不在 schema 的字段（多来自自由槽 → 后续走人工）
    seen_fields: dict[str, None] = {}
    for lg in logics:
        for f in lg.所需数据字段:
            seen_fields.setdefault(f, None)
    plan.取数清单 = list(seen_fields)
    plan.未知字段 = [f for f in plan.取数清单 if f not in schema.FIELDS]
    plan.ok = True
    return plan


def plan(topic: str, topic_type: str, client: DeepSeekClient | None = None,
         *, genre: dict | None = None, context: dict | None = None,
         profile: dict | None = None, fired: list[dict] | None = None,
         chosen: list[str] | None = None,
         doc_claims: list | None = None) -> ArgumentPlan:
    """对主题做论点规划。

    genre  : 体裁配置（可传混合体裁 gr.merged_genre），只影响行文口吻，不决定论证什么。
    context: 需求背景（来自 brief：触发事件/关注点/市场判断查证），供论点贴合真实诉求。
    profile: 摸底数据（DESIGN §3 数据先行）——{字段: FieldValue}，在规划前已实际查询，
             其中被论点库判定为"触发"的那些即本次的主轴候选。
    fired  : 已算好的触发结果（触发引擎含 peers/rotation 等联网判定，很贵）。
             传入即复用，不再重复触发；不传则内部现算。
    chosen : **人工勾选的论点 id**。给了就进"人工勾选模式"——挑哪几条由分析师定，
             planner 只负责措辞与串联，不得增删主轴（DESIGN §7.4）。
    """
    g = genre or gr.get_genre(topic_type)   # 类型非法会报错
    vocab = list(schema.FIELDS.keys())
    client = client or DeepSeekClient()
    if not client.available():
        return ArgumentPlan(主题=topic, 类型=topic_type, ok=False,
                            error="未配置 DeepSeek key，无法规划")

    if fired is None:
        fired = _format_triggers(profile)
    user = _build_user_prompt(topic, g, vocab, context, profile, fired, chosen, doc_claims)
    res: ChatResult = client.chat_json(_SYSTEM_PICKED if chosen else _SYSTEM,
                                       user, temperature=0.3)
    if not res.ok or not isinstance(res.data, dict):
        return ArgumentPlan(主题=topic, 类型=topic_type, ok=False,
                            error=res.error or "LLM 返回非预期结构")
    return _postprocess(topic, g, res.data, fired, chosen, doc_claims)


if __name__ == "__main__":  # python -m core.planner
    p = plan("券商板块投资机会", gr.TYPE_SECTOR)
    print(f"主题: {p.主题} | 类型: {p.类型} | 整体方向: {p.整体方向} | ok={p.ok} {p.error}")
    for lg in p.logics:
        print(f"\n[{lg.来源}] {lg.逻辑id} · {lg.标题}  ({lg.结构方向倾向})")
        print(f"    论点: {lg.具体论点}")
        print(f"    字段: {lg.所需数据字段}")
    print(f"\n取数清单({len(p.取数清单)}): {p.取数清单}")
    if p.未知字段:
        print(f"未知字段(不在schema,走人工): {p.未知字段}")
