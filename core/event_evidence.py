"""事件驱动报告的证据门。

事件本体、产业机制、A 股对象暴露与市场响应是不同层次。行情数据只能回答最后一件；
没有可溯源的事件事实、机制原文、暴露依据及经确认的组合链（或直接传导原文）时，
不能把一次海外公司业绩与 A 股板块表现写成因果关系。

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
    acquisition: str = ""
    evidence_id: str = ""
    translation: str = ""

    def display(self) -> str:
        prefix = f"[{self.evidence_id}] " if self.evidence_id else ""
        suffix = f"；来源：{self.source}"
        if self.link:
            suffix += f"；链接：{self.link}"
        if self.relation:
            return f"{prefix}{self.relation}：{self.content}{suffix}"
        return f"{prefix}{self.content}{suffix}"


@dataclass(frozen=True)
class EvidenceChain:
    fact_ids: tuple[str, ...]
    mechanism_ids: tuple[str, ...]
    exposure_ids: tuple[str, ...]
    conclusion: str
    direction: str = "不确定"
    confidence: str = "低"
    reason: str = ""

    def display(self) -> str:
        refs = "+".join((*self.fact_ids, *self.mechanism_ids, *self.exposure_ids))
        suffix = f"方向：{self.direction}；置信度：{self.confidence}"
        if self.reason:
            suffix += f"；边界：{self.reason}"
        return f"[{refs}] {self.conclusion}（{suffix}）"


@dataclass
class EventEvidence:
    event_facts: list[EvidenceItem] = field(default_factory=list)
    industry_mechanisms: list[EvidenceItem] = field(default_factory=list)
    ashare_exposures: list[EvidenceItem] = field(default_factory=list)
    inference_chains: list[EvidenceChain] = field(default_factory=list)
    # 兼容旧版：若一条原文已经直接说明“事件为何影响本次 A 股对象”，可走捷径。
    transmission_links: list[EvidenceItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        composed = bool(self.industry_mechanisms and self.ashare_exposures and self.inference_chains)
        return not self.errors and bool(self.event_facts) and bool(self.transmission_links or composed)


@dataclass(frozen=True)
class GateResult:
    required: bool
    ready: bool
    missing: tuple[str, ...] = ()

    def message(self, entity_name: str = "该事件主体") -> str:
        if not self.required or self.ready:
            return ""
        return (f"无法生成事件影响报告：{entity_name}的事件证据链尚未齐备。"
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
        if not evidence.industry_mechanisms:
            missing.append("至少 1 条产业机制原文（事件变量如何影响相关产业环节）")
        if not evidence.ashare_exposures:
            missing.append("至少 1 条 A 股暴露原文（公司业务、指数编制或ETF成分依据）")
        if evidence.industry_mechanisms and evidence.ashare_exposures and not evidence.inference_chains:
            missing.append("至少 1 条经分析师确认的组合传导链（事件事实＋产业机制＋A股暴露）")
    return GateResult(True, not missing, tuple(dict.fromkeys(missing)))


def parse(raw: object) -> EventEvidence:
    """读取 overrides 顶层的 ``事件证据``；字段不合格不能悄悄当作有效。"""
    out = EventEvidence()
    if raw in (None, ""):
        return out
    if not isinstance(raw, Mapping):
        out.errors.append("「事件证据」必须是对象，含事件事实及直接传导或三段式证据链")
        return out

    def items(key: str, *, relation_required: bool, prefix: str) -> list[EvidenceItem]:
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
            acquisition = str(item.get("取得方式") or "").strip()
            evidence_id = str(item.get("证据ID") or item.get("id") or "").strip()
            translation = str(item.get("译文") or "").strip()
            if not content or not source or (relation_required and not relation):
                required_text = "内容、来源、关系" if relation_required else "内容、来源"
                out.errors.append(f"「事件证据.{key}」第 {index} 项必须填写{required_text}")
                continue
            result.append(EvidenceItem(
                content=content, source=source, link=link, relation=relation,
                acquisition=acquisition, evidence_id=evidence_id or f"{prefix}{index}",
                translation=translation,
            ))
        return result

    out.event_facts = items("事件事实", relation_required=False, prefix="F")
    out.industry_mechanisms = items("产业机制", relation_required=False, prefix="M")
    out.ashare_exposures = items("A股暴露", relation_required=False, prefix="E")
    out.transmission_links = items("传导关系", relation_required=True, prefix="D")

    known = {
        item.evidence_id: kind
        for kind, values in (
            ("事件事实", out.event_facts), ("产业机制", out.industry_mechanisms),
            ("A股暴露", out.ashare_exposures), ("传导关系", out.transmission_links),
        )
        for item in values
    }
    raw_chains = raw.get("组合传导链") or raw.get("传导链") or []
    if not isinstance(raw_chains, list):
        out.errors.append("「事件证据.组合传导链」必须是列表")
        raw_chains = []
    for index, item in enumerate(raw_chains, 1):
        if not isinstance(item, Mapping):
            out.errors.append(f"「事件证据.组合传导链」第 {index} 项必须是对象")
            continue
        fact_ids = tuple(str(value).strip() for value in (item.get("事实证据ID") or item.get("fact_ids") or []) if str(value).strip())
        mechanism_ids = tuple(str(value).strip() for value in (item.get("机制证据ID") or item.get("mechanism_ids") or []) if str(value).strip())
        exposure_ids = tuple(str(value).strip() for value in (item.get("暴露证据ID") or item.get("exposure_ids") or []) if str(value).strip())
        conclusion = str(item.get("结论") or item.get("conclusion") or "").strip()
        if not (fact_ids and mechanism_ids and exposure_ids and conclusion):
            out.errors.append(f"「事件证据.组合传导链」第 {index} 项必须填写三类证据ID和结论")
            continue
        typed = ((fact_ids, "事件事实"), (mechanism_ids, "产业机制"), (exposure_ids, "A股暴露"))
        invalid = [value for values, expected in typed for value in values if known.get(value) != expected]
        if invalid:
            out.errors.append(f"「事件证据.组合传导链」第 {index} 项引用了不存在或类型不符的ID：{', '.join(invalid)}")
            continue
        out.inference_chains.append(EvidenceChain(
            fact_ids=fact_ids, mechanism_ids=mechanism_ids, exposure_ids=exposure_ids,
            conclusion=conclusion,
            direction=str(item.get("方向") or item.get("direction") or "不确定").strip(),
            confidence=str(item.get("置信度") or item.get("confidence") or "低").strip(),
            reason=str(item.get("边界") or item.get("reason") or "").strip(),
        ))
    return out


def template() -> dict[str, Any]:
    """可直接放进覆盖文件的最小证据模板。"""
    return {
        "事件证据": {
            "事件事实": [
                {
                    "证据ID": "F1",
                    "内容": "例如：SK海力士本季 DRAM/HBM 相关业绩或管理层指引的已披露事实",
                    "来源": "公司 IR 业绩公告 / 已上传研报名称及页码",
                    "链接": "可选：官方公告或材料链接",
                }
            ],
            "产业机制": [{
                "证据ID": "M1",
                "内容": "例如：需求、价格、产能或技术变化如何影响某一产业环节的原文",
                "来源": "产业报告 / 公司披露及页码",
                "链接": "可选",
            }],
            "A股暴露": [{
                "证据ID": "E1",
                "内容": "例如：本次A股公司、指数或ETF为何暴露于该产业环节的原文",
                "来源": "公司年报 / 指数编制方案 / 基金公告",
                "链接": "可选",
            }],
            "组合传导链": [{
                "事实证据ID": ["F1"], "机制证据ID": ["M1"], "暴露证据ID": ["E1"],
                "结论": "仅由上述原文组合出的传导结论", "方向": "正向/负向/双向/不确定",
                "置信度": "高/中/低", "边界": "可选：证据限制",
            }],
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


def to_dict(evidence: EventEvidence) -> dict[str, Any]:
    def item(value: EvidenceItem) -> dict[str, Any]:
        out = {"证据ID": value.evidence_id, "内容": value.content, "来源": value.source}
        if value.link:
            out["链接"] = value.link
        if value.relation:
            out["关系"] = value.relation
        if value.acquisition:
            out["取得方式"] = value.acquisition
        if value.translation:
            out["译文"] = value.translation
        return out

    return {
        "事件事实": [item(value) for value in evidence.event_facts],
        "产业机制": [item(value) for value in evidence.industry_mechanisms],
        "A股暴露": [item(value) for value in evidence.ashare_exposures],
        "组合传导链": [{
            "事实证据ID": list(value.fact_ids), "机制证据ID": list(value.mechanism_ids),
            "暴露证据ID": list(value.exposure_ids), "结论": value.conclusion,
            "方向": value.direction, "置信度": value.confidence, "边界": value.reason,
        } for value in evidence.inference_chains],
        "传导关系": [item(value) for value in evidence.transmission_links],
    }


def attach_to_analysis(ma, brief, evidence: EventEvidence) -> None:
    """将已核验的事件证据作为可溯源字段交给 writer。

    事件事实使用 ``触发标的_`` 前缀，确保写作层把它与板块数据分开；机制、暴露、
    组合链和直接传导分别单列，避免把“同属半导体”这类联想写成因果。
    """
    if not evidence.complete:
        return
    entity = getattr(brief, "触发实体", None)
    if entity is None:
        return
    for index, item in enumerate(evidence.event_facts, 1):
        note = ("自动检索原文并经分析师确认的事件本体事实"
                if item.acquisition else "分析师提供的事件本体事实")
        ma.field_values[f"触发标的_事件事实{index}"] = FieldValue(
            field=f"触发标的_事件事实{index}", value=item.display(), ok=True,
            source=item.source, status="ok",
            note=note + "；不得当作板块数据", display=item.display(),
        )
    for index, item in enumerate(evidence.transmission_links, 1):
        note = ("自动检索原文并经分析师确认的事件到研究口径传导依据"
                if item.acquisition else "分析师提供的事件到本次研究口径的传导依据")
        ma.field_values[f"事件传导证据{index}"] = FieldValue(
            field=f"事件传导证据{index}", value=item.display(), ok=True,
            source=item.source, status="ok",
            note=note, display=item.display(),
        )
    for label, values in (("事件产业机制", evidence.industry_mechanisms),
                          ("A股暴露依据", evidence.ashare_exposures)):
        for index, item in enumerate(values, 1):
            ma.field_values[f"{label}{index}"] = FieldValue(
                field=f"{label}{index}", value=item.display(), ok=True,
                source=item.source, status="ok",
                note="原文证据，经分析师确认；不得脱离组合传导链单独推导因果",
                display=item.display(),
            )
    for index, chain in enumerate(evidence.inference_chains, 1):
        ma.field_values[f"事件组合传导链{index}"] = FieldValue(
            field=f"事件组合传导链{index}", value=chain.display(), ok=True,
            source="分析师确认的受控证据组合", status="ok",
            note="结论只允许引用列示证据ID；不是新的外部事实来源", display=chain.display(),
        )
    ma.事件证据 = {
        "事件主体": f"{getattr(entity, '名称', '')} {getattr(entity, '代码', '')}".strip(),
        "事件事实": [item.display() for item in evidence.event_facts],
        "产业机制": [item.display() for item in evidence.industry_mechanisms],
        "A股暴露": [item.display() for item in evidence.ashare_exposures],
        "组合传导链": [item.display() for item in evidence.inference_chains],
        "传导关系": [item.display() for item in evidence.transmission_links],
    }
