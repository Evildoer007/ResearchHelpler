"""为 OptionHelper 正式报价提供进程级超时保护。

GUI 的 Qt 定时器只能在事件循环可调度时生效；若 Qt/子进程状态异常，不能让
“报价中”永久留在界面。这个小包装器由同一解释器启动真实 worker，并在独立进程
内以 wall-clock 时间强制收束，不接触任何报价内容或凭证。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKER = ROOT / "core" / "optionhelper_quote_worker.py"
TIMEOUT_SECONDS = 180


def main() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    raw_request = sys.stdin.buffer.read()
    process = subprocess.Popen(
        [sys.executable, str(WORKER)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = process.communicate(raw_request, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        _stdout, stderr = process.communicate()
        response = {
            "ok": False,
            "message": (
                f"OptionHelper 正式报价超过 {TIMEOUT_SECONDS} 秒已停止；"
                "请检查 iFinD 凭证、网络或 OptionHelper 运行日志后重试。"
            ),
        }
        sys.stdout.write(json.dumps(response, ensure_ascii=False))
        if stderr:
            sys.stderr.buffer.write(stderr[-2000:])
        return
    # 子 worker 的 stdout 是唯一 JSON 协议，原样转发；stderr 仅供 GUI 诊断。
    sys.stdout.buffer.write(stdout)
    if stderr:
        sys.stderr.buffer.write(stderr)


if __name__ == "__main__":
    main()
