"""搜索设置连接测试子进程；API Key 仅经 stdin 传入，不出现在命令行或日志。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.evidence_discovery import test_search_connection


RESULT_PREFIX = "SEARCH_CONNECTION_RESULT="


def main() -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        settings = payload if isinstance(payload, dict) else {}
        result = test_search_connection(settings)
    except Exception as error:  # noqa: BLE001 - worker must always return structured status
        result = {"ok": False, "provider": "未知", "error": f"{type(error).__name__}: {error}"}
    print(RESULT_PREFIX + json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
