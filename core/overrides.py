"""人工覆盖：把分析师手上有、而系统取不到的数据填进本次报告。

## 为什么需要它

内部底稿一直在产出"需要人工补充"的清单，但**没有任何回填入口**——
分析师看到"缺 SK海力士最新季度营收"，即使手上就有这个数（来自公司公告、
Wind 终端、内部投研库），也没地方敲进去。唯一的间接路径是研报通道
（把 PDF 放进 `sources/` 加 `--pick` 重跑），但那要求恰好有一份写了它的研报，
且抽出来的是"候选观点"而不是"这个字段的值"。

于是清单长得像待办事项，实际却无法执行。本模块补上这个入口。

## 铁律：覆盖值只喂 writer，不喂判定引擎

`thesis.required_fields()` 那 17 个字段是**论点触发的依据**，一律不许覆盖。
手填一个 `PB历史分位=15%`，V1「估值历史低分位」就会触发，而触发依据那行
写的是"PB分位 15.0% < 30.0%"——与机器算出来的一模一样，**看不出它是人敲的**。
整套"论点由真实数据触发"的前提，会在这一条上悄悄变成"论点由一次手工输入触发"，
与 DESIGN §9.1 要防的"数字真、论断假"是同一类问题，只是入口换了。

这条限制几乎不花代价：11 个 MANUAL 字段与 17 个判定字段**交集为空**
（`python -m core.overrides` 可自查），分析师真正想填的东西本来就不在
判定引擎的取数范围内。代价只落在"判定字段取数失败能不能手工救回来"，
而那个场景的正确解法本就是重跑或修取数链路。

## 违规一律报错，不静默忽略

覆盖文件里出现判定字段时**直接失败退出**，而不是当没看见照常跑完——
后者会让分析师以为自己补上了，实际报告里那条依然是缺的，而他不会再去检查。
同 `universe.resolve_sector` 里 `n == 0` 保留原板块名的道理：
宁可显式失败，也不要静默换掉一个东西而成品上看不出。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field as dfield
from pathlib import Path

from . import schema
from .fetcher import FieldValue, format_value

# 人工来源在 FieldValue.source 上的前缀。成品与底稿据此把"人查的"与"机器取的"
# 区分开——不做这个标记，两种数混在一起，事后没人说得清哪个是查的、哪个是敲的。
SOURCE_PREFIX = "人工填写"


@dataclass
class Overrides:
    字段覆盖: dict[str, dict] = dfield(default_factory=dict)   # 字段名 → {值, 来源, 说明}
    外部事实: dict[str, str] = dfield(default_factory=dict)     # 待补事项原文 → 分析师填的内容
    # 事件驱动报告的事实与传导证据。它和通用“外部事实”分开，后者只是给 planner
    # 的背景，前者会作为可溯源字段交给 writer，并受 event_evidence 的硬门校验。
    事件证据: object = None
    path: str = ""
    errors: list[str] = dfield(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def 为空(self) -> bool:
        evidence = self.事件证据
        return (not self.字段覆盖 and not self.外部事实
                and not getattr(evidence, "event_facts", [])
                and not getattr(evidence, "transmission_links", []))


def judged_fields() -> set[str]:
    """判定引擎依赖的字段（不可覆盖）。延迟 import：thesis 会反向 import 本模块之外的东西。"""
    from . import thesis as th

    return set(th.required_fields())


def overridable_fields() -> list[str]:
    """允许覆盖的字段清单 = 全部 schema 字段 − 判定字段。"""
    judged = judged_fields()
    return [f for f in schema.FIELDS if f not in judged]


def load(path: str | Path | None) -> Overrides:
    """读覆盖文件并校验。文件不存在返回空 Overrides（不算错——多数报告不需要覆盖）。"""
    ov = Overrides()
    if not path:
        return ov
    p = Path(path)
    ov.path = str(p)
    if not p.exists():
        ov.errors.append(f"覆盖文件不存在：{p}")
        return ov

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        ov.errors.append(f"覆盖文件不是合法 JSON：{type(e).__name__}: {e}")
        return ov
    if not isinstance(raw, dict):
        ov.errors.append("覆盖文件顶层必须是一个 JSON 对象")
        return ov

    judged = judged_fields()
    fields = raw.get("字段覆盖") or {}
    if not isinstance(fields, dict):
        ov.errors.append("「字段覆盖」必须是一个对象（字段名 → {值, 来源}）")
        fields = {}

    for name, spec in fields.items():
        name = str(name).strip()
        if name in judged:
            ov.errors.append(
                f"「{name}」是判定引擎依赖的字段，不允许手工覆盖。"
                f"该字段取数失败时请重跑或排查取数链路——判定必须建立在机器取到的真数据上。"
            )
            continue
        if name not in schema.FIELDS:
            ov.errors.append(f"「{name}」不在 schema 字段目录里（拼写错误？）")
            continue
        if not isinstance(spec, dict) or "值" not in spec:
            ov.errors.append(f"「{name}」的内容必须是 {{\"值\": ..., \"来源\": \"...\"}}")
            continue
        if not str(spec.get("来源", "")).strip():
            ov.errors.append(f"「{name}」必须填「来源」——人工值同样要可追溯到出处")
            continue
        ov.字段覆盖[name] = dict(spec)

    facts = raw.get("外部事实") or {}
    if not isinstance(facts, dict):
        ov.errors.append("「外部事实」必须是一个对象（待补事项 → 你填的内容）")
        facts = {}
    for k, v in facts.items():
        k, v = str(k).strip(), str(v).strip()
        if k and v:
            ov.外部事实[k] = v
    from . import event_evidence
    ov.事件证据 = event_evidence.parse(raw.get("事件证据"))
    ov.errors.extend(ov.事件证据.errors)
    return ov


def apply_fields(profile: dict, ov: Overrides) -> list[str]:
    """把字段覆盖并进 profile，返回实际生效的字段名。

    在 `fetch_profile` **末尾**调用——先让机器尽力取，取不到的才由人补，
    人工值不会挡住本来能自动取到的数据。
    """
    applied = []
    for name, spec in ov.字段覆盖.items():
        val = spec.get("值")
        src = str(spec.get("来源", "")).strip()
        note = str(spec.get("说明", "")).strip()
        profile[name] = FieldValue(
            field=name, value=val, ok=True,
            source=f"{SOURCE_PREFIX}·{src}", as_of="", indicator="", status="ok",
            note=note or "分析师人工填写，非数据源自动取得",
            display=format_value(name, val) or str(val),
        )
        applied.append(name)
    return applied


def is_manual(fv) -> bool:
    """该字段值是否来自人工填写（供底稿/成品区分展示）。"""
    return str(getattr(fv, "source", "")).startswith(SOURCE_PREFIX)


def template(gap_fields: list[str], external: list[str],
             field_values: dict | None = None) -> str:
    """按本次实际缺口生成覆盖文件模板，供分析师复制后填写。

    只列**允许覆盖**的缺口字段：判定字段即使缺了也不放进模板，
    否则等于引导人去填一个填了就会被拒的东西。
    """
    judged = judged_fields()
    fields = {}
    for f in gap_fields:
        if f in judged or f not in schema.FIELDS:
            continue
        d = schema.FIELDS.get(f) or {}
        fields[f] = {"值": None, "来源": "如 Wind终端·2026-08-17",
                     "说明": f"{d.get('含义', '')}（单位：{d.get('单位', '—')}）"}
    facts = {x: "" for x in external}
    if not fields and not facts:
        return ""
    return json.dumps({"字段覆盖": fields, "外部事实": facts},
                      ensure_ascii=False, indent=2)


if __name__ == "__main__":  # python -m core.overrides
    import io
    import sys

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    judged = judged_fields()
    manual = {f for f, d in schema.FIELDS.items() if d.get("取数方式") == schema.MANUAL}
    print(f"schema 字段 {len(schema.FIELDS)} 个｜判定引擎依赖 {len(judged)} 个"
          f"｜MANUAL {len(manual)} 个")
    print(f"判定字段 ∩ MANUAL = {judged & manual or '空集'}"
          f"  ← 空集即证明「禁止覆盖判定字段」这条限制不妨碍人工补数")
    print(f"\n允许覆盖 {len(overridable_fields())} 个字段：")
    for f in overridable_fields():
        print(f"  - {f}")
