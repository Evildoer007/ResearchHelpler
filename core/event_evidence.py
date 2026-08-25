"""事件驱动报告的证据门。

事件本体、传导关系与市场响应是三件不同的事。行情数据只能回答最后一件；
没有可溯源的事件事实和传导依据时，不能把一次海外公司业绩与 A 股板块表现写成
因果关系，更不能照常生成一份看似完整的“事件影响”报告。

本模块只校验分析师提供或从受控材料整理出的证据，不调用 LLM，也不推断事实。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from . import genres
from .fetcher import FieldValue


@dataclass(frozen=True)
class EvidenceItem:
    content: str
    source: str
    link: str = ""
    relation: str = ""

    def display(self) -> str:
        suffix = f"；来源：{self.source}"
        if self.link:
            suffix += f"；链接：{self.link}"
        if self.relation:
            return f"{self.relation}：{self.content}{suffix}"
        return f"{self.content}{suffix}"


@dataclass
class EventEvidence:
    event_facts: list[EvidenceItem] = field(default_factory=list)
    transmission_links: list[EvidenceItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.errors and bool(self.event_facts) and bool(self.transmission_links)


@dataclass(frozen=True)
class GateResult:
    required: bool
    ready: bool
    missing: tuple[str, ...] = ()

    def message(self, entity_name: str = "该事件主体") -> str:
        if not self.required or self.ready:
            return ""
        return (f"无法生成事件影响报告：{entity_name}的事件事实与传导证据尚未齐备。"
                f"缺少：{'；'.join(self.missing)}。")


def required_for(brief) -> bool:
    """仅对“有具体触发实体”的事件驱动需求启用硬门。

    普通板块报告不应因没有一家公司业绩材料而被挡住；反之，事件在混合类型中只要
    占一个角色，就不能把事件影响写成没有事实来源的联想。
    """
    entity = getattr(brief, "触发实体", None)
    if entity is None or not getattr(entity, "可用", False):
        return False
    kinds = [str(getattr(brief, "主导类型", "") or "")]
    kinds.extend(str(item or "") for item in (getattr(brief, "附加类型", []) or []))
    return genres.TYPE_EVENT in kinds


def assess(brief, evidence: EventEvidence) -> GateResult:
    required = required_for(brief)
    if not required:
        return GateResult(False, True)
    missing: list[str] = []
    if evidence.errors:
        missing.extend(evidence.errors)
    if not evidence.event_facts:
        missing.append("至少 1 条事件事实（业绩实际值、指引或其他已披露事实，且必须注明来源）")
    if not evidence.transmission_links:
        missing.append("至少 1 条传导证据（事件主体如何影响本次 A 股行业/ETF，且必须注明来源）")
    return GateResult(True, not missing, tuple(dict.fromkeys(missing)))


def parse(raw: object) -> EventEvidence:
    """读取 overrides 顶层的 ``事件证据``；字段不合格不能悄悄当作有效。"""
    out = EventEvidence()
    if raw in (None, ""):
        return out
    if not isinstance(raw, Mapping):
        out.errors.append("「事件证据」必须是对象，含「事件事实」与「传导关系」列表")
        return out

    def items(key: str, *, relation_required: bool) -> list[EvidenceItem]:
        value = raw.get(key) or []
        if not isinstance(value, list):
            out.errors.append(f"「事件证据.{key}」必须是列表")
            return []
        result: list[EvidenceItem] = []
        for index, item in enumerate(value, 1):
            if not isinstance(item, Mapping):
                out.errors.append(f"「事件证据.{key}」第 {index} 项必须是对象")
                continue
            content = str(item.get("内容") or item.get("事实") or item.get("说明") or "").strip()
            source = str(item.get("来源") or "").strip()
            link = str(item.get("链接") or "").strip()
            relation = str(item.get("关系") or "").strip()
            if not content or not source or (relation_required and not relation):
                required_text = "内容、来源、关系" if relation_required else "内容、来源"
                out.errors.append(f"「事件证据.{key}」第 {index} 项必须填写{required_text}")
                continue
            result.append(EvidenceItem(content=content, source=source, link=link, relation=relation))
        return result

    out.event_facts = items("事件事实", relation_required=False)
    out.transmission_links = items("传导关系", relation_required=True)
    return out


def template() -> dict[str, Any]:
    """可直接放进覆盖文件的最小证据模板。"""
    return {
        "事件证据": {
            "事件事实": [
                {
                    "内容": "例如：SK海力士本季 DRAM/HBM 相关业绩或管理层指引的已披露事实",
                    "来源": "公司 IR 业绩公告 / 已上传研报名称及页码",
                    "链接": "可选：官方公告或材料链接",
                }
            ],
            "传导关系": [
                {
                    "关系": "直接竞争 / 供应链 / 客户需求 / 技术替代 / 估值情绪映射",
                    "内容": "例如：该事实为何会影响本次确认的 A 股存储芯片行业或芯片ETF",
                    "来源": "公司披露、产业链研报或分析师可核验材料",
                    "链接": "可选：材料链接",
                }
            ],
        }
    }


def attach_to_analysis(ma, brief, evidence: EventEvidence) -> None:
    """将已核验的事件证据作为可溯源字段交给 writer。

    事件事实使用 ``触发标的_`` 前缀，确保写作层把它与板块数据分开；传导关系则
    单列，避免把“同属半导体”这类未经依据的联想写成因果。
    """
    if not evidence.complete:
        return
    entity = getattr(brief, "触发实体", None)
    if entity is None:
        return
    for index, item in enumerate(evidence.event_facts, 1):
        ma.field_values[f"触发标的_事件事实{index}"] = FieldValue(
            field=f"触发标的_事件事实{index}", value=item.display(), ok=True,
            source=item.source, status="ok",
            note="分析师提供的事件本体事实；不得当作板块数据", display=item.display(),
        )
    for index, item in enumerate(evidence.transmission_links, 1):
        ma.field_values[f"事件传导证据{index}"] = FieldValue(
            field=f"事件传导证据{index}", value=item.display(), ok=True,
            source=item.source, status="ok",
            note="分析师提供的事件到本次研究口径的传导依据", display=item.display(),
        )
    ma.事件证据 = {
        "事件主体": f"{getattr(entity, '名称', '')} {getattr(entity, '代码', '')}".strip(),
        "事件事实": [item.display() for item in evidence.event_facts],
        "传导关系": [item.display() for item in evidence.transmission_links],
    }
