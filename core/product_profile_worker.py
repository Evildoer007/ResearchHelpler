"""GUI 的独立产品画像 worker：stdout 只输出 JSON，避免阻塞桌面主线程。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from core.product_profile import collect


RESULT_PREFIX = "PRODUCT_PROFILE_RESULT="


def extract_result(output: str) -> dict:
    """从 worker stdout 中提取最终结果，忽略 iFinD 等依赖输出的启动信息。

    iFinD 首次加载时可能把安装路径或登录提示写到 stdout。GUI 与 worker 的协议因此
    不能假设 stdout 整体就是一个 JSON 文档；固定前缀是唯一可信的消息边界。末尾的
    逐行 JSON 回退用于兼容升级前只输出裸 JSON 的 worker。
    """
    lines = [line.strip() for line in str(output or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if not line.startswith(RESULT_PREFIX):
            continue
        try:
            payload = json.loads(line[len(RESULT_PREFIX):])
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
    for line in reversed(lines):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def main() -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        code = str(payload.get("underlying") or "").strip()
        if not code:
            raise ValueError("缺少挂钩标的代码")
        profile = collect(code, name=str(payload.get("name") or ""))
        print(RESULT_PREFIX + json.dumps({"ok": True, "profile": profile}, ensure_ascii=False), flush=True)
        return 0
    except Exception as error:  # noqa: BLE001
        print(RESULT_PREFIX + json.dumps(
            {"ok": False, "message": f"标的产品画像生成失败：{type(error).__name__}: {error}"},
            ensure_ascii=False,
        ), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
