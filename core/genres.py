"""报告体裁（原 `skeletons.py`）。

**这个模块曾经是"论证骨架"的单一事实来源**——每类型写死 3 条主轴 + 2 条可选池，
同时决定"论证哪几条 / 取哪些字段 / 配什么图"。后果是同一类型的报告永远在论证
同样三件事，换主题只换数字，即用户所说"来来回回那几个观点"。

2026-08-04（DESIGN §7.2/§7.3、更新日志 #32）三项权威全部移交论点库：

  | 由谁决定 | 现在的出处 |
  |---|---|
  | 摸底取哪些字段 | `thesis.required_fields()` |
  | 正文主轴候选   | `thesis.triggered_theses()`（本次被真实数据触发的论点） |
  | 每条配什么图   | `thesis.chart_type_of()`（按论点类别） |

本模块因此瘦身为**体裁配置**，只剩两件事：
  ① 主题类型枚举 TYPE_*（brief/topics 判断需求属于哪一类）；
  ② 每类型一句 `叙事主轴` —— 给 planner 的**行文口吻参考**，不规定论证什么。

原来的 15 条逻辑定义已删除（历史留档见 DESIGN 更新日志 #01/#31/#32）。
"""

from __future__ import annotations

# ---- 选题类型 ----
TYPE_SECTOR = "板块机会"
TYPE_INDUSTRY = "产业趋势"
TYPE_EVENT = "事件驱动"

# ---- 结构方向（埋线给 OptionHelper；论点库的"方向"特征也用这套取值）----
DIR_BULL = "看涨"
DIR_RANGE = "震荡"
DIR_BEAR = "看跌"
DIR_NEUTRAL = "中性"

# 每类型的体裁说明。参考报告即 references/ 下的 0720 三份一页通。
# 「叙事主轴」是一句话的行文口吻示范，planner 会另行生成本篇真实的叙事主轴。
GENRES: dict[str, dict] = {
    TYPE_SECTOR: {
        "类型": TYPE_SECTOR,
        "参考": "券商板块的投资机会 0720",
        "叙事主轴": "超跌修复 + 盈利支撑 + 现金流安全垫 → 现在具备阶段性配置价值",
        "体裁说明": "围绕某板块当前是否具备配置价值展开，落点在估值/盈利/资金/分红等横截面特征。",
    },
    TYPE_INDUSTRY: {
        "类型": TYPE_INDUSTRY,
        "参考": "基于当前 AI 产业趋势及估值的投资机会 0720",
        "叙事主轴": "景气验证 + 空间弹性 + 估值未泡沫 → 趋势尚未见顶，可配置",
        "体裁说明": "围绕一条产业趋势的持续性展开，落点在景气验证、成长空间与估值是否透支。"
                    "此类报告的产业口径数据（渗透率/中外产能对比等）多需人工补充。",
    },
    TYPE_EVENT: {
        "类型": TYPE_EVENT,
        "参考": "大型（长鑫科技）IPO 窗口期的投资机会 0720",
        "叙事主轴": "事件定性 + 规律/传导支撑 + 择优表达标的 → 窗口期博弈",
        "体裁说明": "围绕一个具体事件的窗口期展开。注意事件不一定是利空——"
                    "先给事件定性（利多/利空/扰动），再谈受益传导与标的择优。",
    },
}


def list_types() -> list[str]:
    """返回所有支持的选题类型。"""
    return list(GENRES)


def get_genre(topic_type: str) -> dict:
    """按类型取体裁配置；类型不存在时报错并列出可选类型。"""
    if topic_type not in GENRES:
        raise KeyError(f"未知选题类型: {topic_type!r}，可选: {list_types()}")
    return GENRES[topic_type]


def merged_genre(primary: str, extra: list[str] | None = None) -> dict:
    """混合体裁：真实需求常跨类型（如"某海外业绩事件后的板块配置机会"）。

    现在合并的只是**体裁标签**——论点候选一律来自触发引擎，与类型无关，
    故不再需要合并逻辑池（那正是旧版"叠加式混合"导致 6 条逻辑、每条变浅的原因）。
    """
    base = get_genre(primary)
    mixed = [primary] + [t for t in (extra or []) if t != primary and t in GENRES]
    g = dict(base, 混合类型=mixed)
    if len(mixed) > 1:
        others = "；".join(GENRES[t]["叙事主轴"] for t in mixed[1:])
        g["叙事主轴"] = f"{base['叙事主轴']}（兼及：{others}）"
    return g


if __name__ == "__main__":  # python -m core.genres
    for t in list_types():
        g = get_genre(t)
        print(f"[{t}] 参考《{g['参考']}》\n    叙事主轴：{g['叙事主轴']}\n    {g['体裁说明']}")
    print("\n混合示例：", merged_genre(TYPE_EVENT, [TYPE_SECTOR])["叙事主轴"])
