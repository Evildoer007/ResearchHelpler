"""以 Research Helper 已配置的 LLM 实现 OptionHelper Recommender 的 AgentPort。

此文件必须由 ``OPTIONHELPER_PYTHON`` 启动：推荐状态机、Knowledger 与产品目录均来自
用户指定的最新版 OptionHelper Skill；本项目只提供该 Skill 要求的语言 AgentPort。
标准输入/输出均为单个 JSON 对象，避免凭证或中间提示写入运行日志。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须在导入 Skill 前固定本项目的 core 包；Skill 会把自身 core/src 插到 sys.path 首位。
from core import config as research_config


def _utf8_safe(value: Any) -> Any:
    """丢弃孤立代理字符，避免个别模型输出的残缺 emoji 破坏跨进程 JSON。"""
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, list):
        return [_utf8_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_utf8_safe(item) for item in value]
    if isinstance(value, Mapping):
        return {_utf8_safe(key): _utf8_safe(item) for key, item in value.items()}
    return value


def _configure_utf8_stdio() -> None:
    """GUI 的 QProcess 协议固定为 UTF-8，不服从 Windows 控制台代码页。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


class DeepSeekAgentPort:
    """将严格角色输入交给本项目配置的 LLM；字段合法性仍由 Skill 二次校验。"""

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self.last_error = ""

    def capability(self):
        from modules.recommender.models import ModelCapability

        return ModelCapability(
            model_id=self.model_id, structured_output=True, tool_calling=False,
            multi_agent=False, max_parallel_agents=1,
        )

    def run_step(self, role: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        from llm.client import DeepSeekClient

        client = DeepSeekClient(model=self.model_id, timeout=120)
        system = (
            "你是 OptionHelper Recommender 的受控单Agent步骤。严格遵守 role_rule，"
            "仅输出 required_output 指定的 JSON 对象，字段名不得增删。"
            "Research 只能从 evidence 中选择产品并引用已有 evidence_id；"
            "Critic 只能审阅 Research 已提出的 product_id，不能新增产品。"
            "不得编造市场价格、产品条款、目录资料或未给出的客户事实。"
        )
        result = client.chat_json(
            system,
            json.dumps(_utf8_safe({"role": role, **dict(payload)}), ensure_ascii=False),
            temperature=0.1, max_tokens=2200,
        )
        if not result.ok or not isinstance(result.data, Mapping):
            self.last_error = result.error or "LLM 未返回合法的 Recommender 步骤结果。"
            raise RuntimeError(self.last_error)
        return _utf8_safe(dict(result.data))


def main() -> None:
    _configure_utf8_stdio()
    try:
        body = json.loads(sys.stdin.read())
        if not isinstance(body, Mapping):
            raise ValueError("请求必须为 JSON 对象")
        skill_root = Path(str(body["skill_root"])).resolve()
        project_root = Path(str(body["project_root"])).resolve()
        if not (skill_root / "scripts" / "tool_entry.py").is_file():
            raise ValueError("OptionHelper Skill 根目录无效")
        scripts = skill_root / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        # 与正式报价桥接使用同一组安装目录外 Store。
        os.environ["OPTIONHELPER_RUNTIME_ROOT"] = str(project_root / ".optionhelper" / "runtime")
        os.environ["OPTIONHELPER_DATA_ROOT"] = str(project_root / "data")
        os.environ["OPTIONHELPER_RESULT_ROOT"] = str(project_root / "result")
        import tool_entry

        request_body = _utf8_safe({
            "prompt": str(body.get("prompt") or ""), "constraints": dict(body.get("constraints") or {}),
        })
        agent = DeepSeekAgentPort(research_config.DEEPSEEK_MODEL)
        # OptionHelper 及其依赖可输出进度信息；本 Worker 的 stdout 是 GUI 的
        # 机器协议，只能保留最后一份 JSON。进度输出在此被隔离，绝不混入协议。
        progress = io.StringIO()
        try:
            with contextlib.redirect_stdout(progress):
                result = tool_entry.run_recommendation_request(
                    request_body, project_root=project_root, agent_port=agent,
                )
        except Exception as error:
            if agent.last_error:
                raise RuntimeError("OptionHelper Recommender 的 DeepSeek 调用失败：" + agent.last_error) from error
            raise
        if progress.tell():
            print("[OptionHelper] 已隔离内部进度输出。", file=sys.stderr)
        print(json.dumps(_utf8_safe({"ok": True, "result": result}), ensure_ascii=False))
    except Exception as error:  # 只输出已截断的可理解错误，绝不回显请求正文或凭证。
        response = {"ok": False, "message": f"{type(error).__name__}: {str(error)[:500]}"}
        if os.environ.get("RESEARCH_HELPER_DEBUG_RECOMMENDER") == "1":
            response["debug_trace"] = _utf8_safe(traceback.format_exc())
        print(json.dumps(_utf8_safe(response), ensure_ascii=False))


if __name__ == "__main__":
    main()
