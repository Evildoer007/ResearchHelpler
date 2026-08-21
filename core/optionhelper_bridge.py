"""新版 OptionHelper Skill 的正式参考报价桥接层。

本模块不推荐结构、不拼价格，也不解析最终 HTML。当前对话 Agent 完成
Recommender 的 Intent/Research/Critic 后，把已验证的公开 selection 交给 Skill；
Skill 的受控链路完成合同冻结、取数、收益结构、定价、Reporter 与 Designer。
本模块只读取同一次 ReportRun 的 ``designer-input.json`` 冻结报价事实。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field as dfield
from pathlib import Path
from typing import Any, Mapping

from . import config
from .client_constraints import ClientConstraints
from .run_tracker import record_external
from .viewpoint import ViewPackage

_TIMEOUT_S = 600
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class QuoteColumn:
    key: str
    label: str
    value_format: str = "text"


@dataclass
class QuoteGroup:
    title: str
    columns: list[QuoteColumn] = dfield(default_factory=list)
    rows: list[dict[str, str]] = dfield(default_factory=list)


@dataclass
class OptionHelperResult:
    ok: bool = False
    status: str = ""
    stage: str = ""
    message: str = ""
    error: str = ""
    missing: list[str] = dfield(default_factory=list)
    product_id: str = ""
    product_name: str = ""
    reason: str = ""
    main_risks: list[str] = dfield(default_factory=list)
    report_path: str = ""
    report_format: str = ""
    coverage_status: str = ""
    module_failures: dict[str, str] = dfield(default_factory=dict)
    assumptions: list[str] = dfield(default_factory=list)
    quote_date: str = ""
    quote_note: str = ""
    quote_groups: list[QuoteGroup] = dfield(default_factory=list)
    designer_input_path: str = ""
    raw: dict[str, Any] = dfield(default_factory=dict)
    client_constraints: dict[str, Any] = dfield(default_factory=dict)
    client_product_intent: str = ""

    @property
    def recovery_action(self) -> str:
        """将已知的技术拒绝翻成不越权的下一步；绝不自动换产品。"""
        return recovery_action_for(self.stage, self.error)


def recovery_action_for(stage: str, error: str) -> str:
    text = f"{stage} {error}".lower()
    if ("日历" in text or "calendar" in text) and (
            "dataassetref" in text or "历史行情" in text or "覆盖" in text):
        return ("该候选需要每日观察日程，触发了 OptionHelper 的日历绑定校验；可重新完成"
                "Recommender 审阅并选择无需每日观察的候选，或由 OptionHelper 修复交易日边界后重试。")
    if stage == "configuration":
        return "按错误提示完成 OptionHelper 就绪检查、解释器或凭证配置后重试。"
    if stage == "recommender":
        return "请重新核验本次 selection、挂钩标的和客户约束后重试；不要复用上一份报告的选择。"
    if stage:
        return "保留研究报告；核对该阶段错误后重试正式报价。"
    return ""


def missing_setup() -> list[str]:
    """只做路径检查；依赖、Store、iFind 统一交给 Skill readiness。"""
    missing: list[str] = []
    skill_root = Path(config.OPTIONHELPER_SKILL_ROOT) if config.OPTIONHELPER_SKILL_ROOT else None
    if skill_root is None:
        missing.append("OPTIONHELPER_SKILL_ROOT（新版 OptionHelper Skill 根目录）")
    else:
        for relative in ("SKILL.md", "scripts/environment_check.py", "scripts/tool_entry.py"):
            if not (skill_root / relative).is_file():
                missing.append(f"新版 Skill 缺少 {relative}：{skill_root}")
    if not config.OPTIONHELPER_PYTHON:
        missing.append("OPTIONHELPER_PYTHON（须先明确选择一个 Python 解释器）")
    elif not Path(config.OPTIONHELPER_PYTHON).is_file():
        missing.append(f"OPTIONHELPER_PYTHON 指向的解释器不存在：{config.OPTIONHELPER_PYTHON}")
    return missing


def build_prompt(vp: ViewPackage, client_constraints: Mapping[str, Any] | None = None,
                 client_product_intent: str = "") -> str:
    """只描述已验证市场状态；不向 OptionHelper 夹带产品或条款建议。"""
    lines: list[str] = []
    if vp.标的名称:
        lines.append(f"拟挂钩标的是{vp.标的名称}（{vp.标的代码}）。")
    if vp.标的选择说明:
        lines.append("标的选择原因：" + vp.标的选择说明)
    views: list[str] = []
    if vp.整体方向:
        views.append(f"整体方向{vp.整体方向}")
    if vp.波动率看法:
        views.append(vp.波动率看法)
    if views:
        lines.append("当前市场判断：" + "，".join(views) + "。")
    if vp.市场事实:
        lines.append("已验证市场情况：")
        for item in vp.市场事实:
            trace = "，".join(part for part in (item.来源, item.截止日) if part)
            lines.append(f"- {item.标签}：{item.数值}" + (f"（{trace}）" if trace else ""))
    band = getattr(vp, "情景收益带", None)
    if band is not None and getattr(band, "ok", False):
        try:
            from .scenario_band import render_compact

            lines.append("历史相似状态收益带（仅为历史条件分布，非预测）：")
            lines.append("- 当前状态：" + "；".join(item for item in (
                getattr(band, "return_state", ""), getattr(band, "volatility_state", "")) if item))
            lines.append(f"- 样本规则：{band.sample_rule}；样本数 {band.sample_count}")
            lines.append("- " + render_compact(band))
        except Exception:
            pass
    outlook = vp.市场展望
    if any((outlook.方向, outlook.窗口, outlook.支持因素, outlook.制约因素, outlook.需验证风险)):
        lines.append("市场展望：")
        if outlook.方向:
            lines.append(f"- 预计方向：{outlook.方向}")
        if outlook.窗口:
            lines.append("- 观察窗口：" + "、".join(outlook.窗口))
        if outlook.支持因素:
            lines.append("- 主要支持因素：" + "、".join(outlook.支持因素))
        if outlook.制约因素:
            lines.append("- 主要制约因素：" + "、".join(outlook.制约因素))
        if outlook.需验证风险:
            lines.append("- 需持续验证的风险：" + "、".join(outlook.需验证风险))
    if client_constraints:
        # 这是客户已声明的独立约束，不属于研究观点，也不构成产品建议。
        parts = []
        if client_constraints.get("horizon"):
            parts.append(f"期限 {client_constraints['horizon']}")
        if client_constraints.get("max_loss"):
            parts.append(f"最大损失 {client_constraints['max_loss']}")
        if "principal_fluctuation" in client_constraints:
            parts.append("接受本金波动" if client_constraints["principal_fluctuation"] else "不接受本金波动")
        if client_constraints.get("return_preference"):
            parts.append(f"收益偏好 {client_constraints['return_preference']}")
        if parts:
            lines.append("客户已声明约束（不属于市场观点）：" + "；".join(parts) + "。")
    if client_product_intent:
        lines.append("客户已提出的产品诉求（独立于市场研究，须经 Recommender 与合规校验，不构成研究建议）："
                     + client_product_intent)
    lines.append("以上市场部分不包含产品、结构或执行价建议；客户约束单独列示，不由研究层推导。")
    return "\n".join(lines)


def _json_stdout(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        value = json.loads(proc.stdout) if proc.stdout and proc.stdout.strip() else {}
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _readiness(project_root: Path) -> tuple[bool, dict[str, Any], str]:
    skill_root = Path(config.OPTIONHELPER_SKILL_ROOT).resolve()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    command = [
        str(Path(config.OPTIONHELPER_PYTHON).resolve()),
        str(skill_root / "scripts" / "environment_check.py"),
        "--check-readiness",
        "--skill-root", str(skill_root),
        "--project-root", str(project_root),
    ]
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            command, cwd=str(project_root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        message = f"无法完成 OptionHelper 统一就绪检查：{error}"
        record_external("OptionHelper readiness", status="failed",
                        duration_seconds=time.perf_counter() - started, error=message)
        return False, {}, message
    report = _json_stdout(proc)
    if report.get("ok") is True:
        record_external("OptionHelper readiness", status="completed",
                        duration_seconds=time.perf_counter() - started)
        return True, report, ""
    guidance = str(report.get("guidance") or "").strip()
    action = str(report.get("next_action") or "").strip()
    detail = "；".join(item for item in (guidance, f"下一步：{action}" if action else "") if item)
    if not detail:
        detail = "OptionHelper 统一就绪检查未通过。"
    record_external("OptionHelper readiness", status="failed",
                    duration_seconds=time.perf_counter() - started, error=detail)
    return False, report, detail


def _selection_payload(project_root: Path, vp: ViewPackage,
                       client_constraints: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    """读取 Agent 的公开选择；绝不从旧模型网关自行生成一个选择。"""
    if config.OPTIONHELPER_HOST_URL:
        return {}, ""
    path = Path(config.OPTIONHELPER_SELECTION_PATH)
    if not path.is_absolute():
        path = project_root / path
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, ("新版 Skill 需要对话 Agent 已验证的 selection；未找到交接文件："
                    f"{path}")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return {}, f"OptionHelper selection 文件不可读取：{error}"
    if not isinstance(raw, Mapping):
        return {}, "OptionHelper selection 必须是 JSON 对象。"
    selection = raw.get("selection", raw)
    if not isinstance(selection, Mapping):
        return {}, "OptionHelper selection.selection 必须是 JSON 对象。"
    product_id = str(selection.get("product_id") or "").strip()
    underlyings = selection.get("underlyings")
    if isinstance(underlyings, str):
        underlyings = [item.strip() for item in underlyings.split(",") if item.strip()]
    if not product_id or not isinstance(underlyings, list) or not underlyings:
        return {}, "OptionHelper selection 缺少 product_id 或 underlyings。"
    normalized = {str(item).strip().upper() for item in underlyings}
    if vp.标的代码 and vp.标的代码.strip().upper() not in normalized:
        return {}, (f"selection 的挂钩标的 {sorted(normalized)} 与本页 {vp.标的代码} 不一致；"
                    "请重新完成 Recommender 审阅，不能复用旧选择。")
    allowed = ("product_id", "underlyings", "reason", "suitable_for",
               "not_suitable_for", "main_risks")
    body: dict[str, Any] = {"selection": {key: selection[key] for key in allowed if key in selection}}
    current_view = vp.市场展望.方向 or vp.整体方向
    body["constraints"] = _effective_constraints(
        raw,
        client_constraints,
        underlying=vp.标的代码,
        market_view=current_view,
    )
    # 执行价等受控合同覆盖只透传，不在桥接层推导条款或价格。
    for field in ("term_overrides", "pricing_config", "backtest_config"):
        value = raw.get(field)
        if isinstance(value, Mapping):
            body[field] = dict(value)
    variants = raw.get("quote_variants")
    if isinstance(variants, list) and variants:
        body["quote_variants"] = variants
    return body, ""


def _effective_constraints(selection_file: Mapping[str, Any],
                           client_constraints: Mapping[str, Any] | None = None,
                           *, underlying: str = "", market_view: str = "") -> dict[str, Any]:
    """返回本次有效约束；标的和市场方向永远来自当前观点包。

    selection 文件可以长期保留客户条件，但不能让上一份报告遗留的 ``underlying``
    或 ``market_view`` 污染当前报价。两者是本次研究结果，不是客户默认档案。
    """
    result = dict(config.OPTIONHELPER_DEFAULT_CONSTRAINTS)
    supplied = selection_file.get("constraints")
    if isinstance(supplied, Mapping):
        result.update({str(key): value for key, value in supplied.items()
                       if str(key) in ("horizon", "max_loss", "principal_fluctuation",
                                       "return_preference")
                       and value is not None and value != ""})
    if isinstance(client_constraints, Mapping):
        # CLI/GUI 是本次客户的最新明确输入，优先于本地旧 selection 的遗留条件。
        result.update({str(key): value for key, value in client_constraints.items()
                       if str(key) in ("horizon", "max_loss", "principal_fluctuation",
                                       "return_preference")
                       and value is not None and value != ""})
    if underlying:
        result["underlying"] = underlying
    if market_view:
        result["market_view"] = market_view
    result["output_type"] = "quote"
    result["format"] = "html"
    return result


def _quote_facts(report_path: str, project_root: Path) -> tuple[list[QuoteGroup], str, str, str]:
    """读取本次 ReportRun 的 Designer 冻结输入，不解析展示 HTML。"""
    if not report_path:
        return [], "", "", "正式报价没有返回报告路径。"
    report = Path(report_path).resolve()
    allowed_root = (project_root / "result").resolve()
    if allowed_root != report.parent and allowed_root not in report.parents:
        return [], "", "", f"正式报价路径不在本项目 Result Store：{report}"
    payload_path = report.parent / "designer-input.json"
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return [], "", "", f"无法读取本次冻结报价事实 {payload_path}：{error}"
    quote = payload.get("reference_quote") if isinstance(payload, Mapping) else None
    if not isinstance(quote, Mapping):
        return [], "", "", "designer-input.json 缺少 reference_quote。"
    groups: list[QuoteGroup] = []
    raw_groups = quote.get("groups") if isinstance(quote.get("groups"), list) else []
    for raw_group in raw_groups:
        if not isinstance(raw_group, Mapping):
            continue
        columns: list[QuoteColumn] = []
        raw_columns = raw_group.get("columns") if isinstance(raw_group.get("columns"), list) else []
        for raw_column in raw_columns:
            if not isinstance(raw_column, Mapping):
                continue
            key = str(raw_column.get("key") or "").strip()
            label = str(raw_column.get("label") or "").strip()
            if key and label:
                columns.append(QuoteColumn(key, label, str(raw_column.get("format") or "text")))
        rows: list[dict[str, str]] = []
        raw_rows = raw_group.get("rows") if isinstance(raw_group.get("rows"), list) else []
        for raw_row in raw_rows:
            if not isinstance(raw_row, Mapping) or not columns:
                continue
            rows.append({column.key: str(raw_row.get(column.key) or "—") for column in columns})
        if columns and rows:
            groups.append(QuoteGroup(str(raw_group.get("title") or "参考报价"), columns, rows))
    if not groups:
        return [], "", "", "reference_quote 没有可展示的 columns/rows。"
    return groups, str(quote.get("quote_date") or ""), str(quote.get("note") or ""), str(payload_path)


def run_full(
    vp: ViewPackage,
    *,
    output_type: str = "quote",
    project_root: str | Path | None = None,
    client_constraints: Mapping[str, Any] | ClientConstraints | None = None,
    client_product_intent: str = "",
) -> OptionHelperResult:
    """执行最新版受控 Quote 链路并返回一页通需要的冻结表格事实。"""
    if output_type != "quote":
        return OptionHelperResult(ok=False, stage="request", error="一页通仅接入新版 OptionHelper 的 quote 正式交付。")
    if not vp.ok or not vp.标的代码:
        return OptionHelperResult(ok=False, stage="request", error="观点包未就绪或挂钩标的未定。")
    missing = missing_setup()
    if missing:
        return OptionHelperResult(ok=False, stage="configuration", missing=missing,
                                  error="OptionHelper 新版 Skill 尚未就绪：" + "；".join(missing))

    root = Path(project_root).resolve() if project_root else _PROJECT_ROOT
    ready, readiness, readiness_error = _readiness(root)
    if not ready:
        next_action = str(readiness.get("next_action") or "完成统一就绪检查")
        return OptionHelperResult(ok=False, stage="configuration", error=readiness_error,
                                  missing=[next_action], raw=readiness)

    supplied_constraints = (client_constraints.supplied() if isinstance(client_constraints, ClientConstraints)
                             else dict(client_constraints or {}))
    selection_fields, selection_error = _selection_payload(root, vp, supplied_constraints)
    if selection_error:
        return OptionHelperResult(ok=False, stage="recommender", error=selection_error)
    prompt_constraints = dict(selection_fields.get("constraints") or {})
    if supplied_constraints.get("return_preference"):
        prompt_constraints["return_preference"] = supplied_constraints["return_preference"]
    body = {
        "prompt": build_prompt(vp, prompt_constraints, client_product_intent),
        "output_type": "quote",
        "format": "html",
        **selection_fields,
    }
    skill_root = Path(config.OPTIONHELPER_SKILL_ROOT).resolve()
    env = dict(os.environ)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    env["NO_PROXY"] = "*"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # 发行版 Skill 在 import 阶段就要求 Host 显式授予三个安装目录外的 Store；
    # 这里严格使用 readiness 已核验的当前项目目录，不沿用旧版嵌套 Store。
    env["OPTIONHELPER_RUNTIME_ROOT"] = str(root / ".optionhelper" / "runtime")
    env["OPTIONHELPER_DATA_ROOT"] = str(root / "data")
    env["OPTIONHELPER_RESULT_ROOT"] = str(root / "result")
    if config.OPTIONHELPER_HOST_URL:
        env["OPTIONHELPER_HOST_URL"] = config.OPTIONHELPER_HOST_URL

    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [str(Path(config.OPTIONHELPER_PYTHON).resolve()), str(skill_root / "scripts" / "tool_entry.py"),
             "--project-json", json.dumps(body, ensure_ascii=False)],
            cwd=str(root), env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        message = f"OptionHelper 运行超过 {_TIMEOUT_S}s 未完成。"
        record_external("OptionHelper formal quote", status="failed",
                        duration_seconds=time.perf_counter() - started, error=message)
        return OptionHelperResult(ok=False, stage="host", error=message,
                                  client_constraints=selection_fields.get("constraints", {}),
                                  client_product_intent=client_product_intent)
    except OSError as error:
        message = f"无法启动 OptionHelper：{error}"
        record_external("OptionHelper formal quote", status="failed",
                        duration_seconds=time.perf_counter() - started, error=message)
        return OptionHelperResult(ok=False, stage="host", error=message,
                                  client_constraints=selection_fields.get("constraints", {}),
                                  client_product_intent=client_product_intent)

    out = _json_stdout(proc)
    if not out:
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:]) if proc.stderr else "（无 stderr 输出）"
        message = f"OptionHelper 未返回可解析结果（退出码 {proc.returncode}）：{tail}"
        record_external("OptionHelper formal quote", status="failed",
                        duration_seconds=time.perf_counter() - started, error=message)
        return OptionHelperResult(ok=False, stage="host", error=message,
                                  client_constraints=selection_fields.get("constraints", {}),
                                  client_product_intent=client_product_intent)
    if out.get("ok") is not True:
        raw_missing = out.get("missing") if isinstance(out.get("missing"), list) else []
        message = str(out.get("message") or "")
        record_external("OptionHelper formal quote", status="failed",
                        duration_seconds=time.perf_counter() - started,
                        detail=str(out.get("stage") or ""), error=message)
        return OptionHelperResult(
            ok=False, status=str(out.get("status") or ""), stage=str(out.get("stage") or ""),
            error=message, missing=[str(item) for item in raw_missing], raw=out,
            client_constraints=selection_fields.get("constraints", {}),
            client_product_intent=client_product_intent,
        )

    summary = out.get("user_summary") if isinstance(out.get("user_summary"), Mapping) else {}
    rec = summary.get("recommendation") if isinstance(summary.get("recommendation"), Mapping) else {}
    rep = summary.get("report") if isinstance(summary.get("report"), Mapping) else {}
    report_path = str(rep.get("path") or "")
    groups, quote_date, quote_note, facts_path = _quote_facts(report_path, root)
    if not groups:
        record_external("OptionHelper formal quote", status="failed",
                        duration_seconds=time.perf_counter() - started,
                        detail="reporter", error=facts_path)
        return OptionHelperResult(ok=False, status=str(out.get("status") or ""), stage="reporter",
                                  error=facts_path, report_path=report_path, raw=out,
                                  client_constraints=selection_fields.get("constraints", {}),
                                  client_product_intent=client_product_intent)
    failures = summary.get("module_failures") if isinstance(summary.get("module_failures"), Mapping) else {}
    raw_assumptions = summary.get("assumptions") if isinstance(summary.get("assumptions"), list) else []
    raw_risks = rec.get("main_risks") if isinstance(rec.get("main_risks"), list) else []
    record_external("OptionHelper formal quote", status="completed",
                    duration_seconds=time.perf_counter() - started,
                    detail=f"product={rec.get('product_id') or ''}")
    return OptionHelperResult(
        ok=True, status=str(out.get("status") or ""), message=str(out.get("message") or ""),
        product_id=str(rec.get("product_id") or ""), product_name=str(rec.get("product_name") or ""),
        reason=str(rec.get("reason") or ""), main_risks=[str(item) for item in raw_risks],
        report_path=report_path, report_format=str(rep.get("format") or ""),
        coverage_status=str(rep.get("coverage_status") or ""), module_failures=dict(failures),
        assumptions=[str(item) for item in raw_assumptions], quote_date=quote_date,
        quote_note=quote_note, quote_groups=groups, designer_input_path=facts_path, raw=out,
        client_constraints=selection_fields.get("constraints", {}),
        client_product_intent=client_product_intent,
    )
