"""GUI 事件证据发现子进程，避免网络和 LLM 请求冻结桌面界面。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.evidence_discovery import discover


RESULT_PREFIX = "EVENT_EVIDENCE_DISCOVERY="
PROGRESS_PREFIX = "EVENT_EVIDENCE_PROGRESS="


def _progress(value: int, message: str) -> None:
    print(PROGRESS_PREFIX + json.dumps(
        {"value": value, "message": message}, ensure_ascii=False), flush=True)


def main() -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        result = discover(
            str(payload.get("topic") or ""),
            sources_dir=Path(str(payload.get("sources_dir") or "sources")),
            mode=str(payload.get("mode") or "full"),
            research_context=(payload.get("research_context")
                              if isinstance(payload.get("research_context"), dict) else None),
            existing_evidence=(payload.get("existing_evidence")
                               if isinstance(payload.get("existing_evidence"), dict) else None),
            progress=_progress,
        )
        print(RESULT_PREFIX + json.dumps(result.to_dict(), ensure_ascii=False))
        return 0
    except Exception as error:  # noqa: BLE001 - worker must always return a machine-readable result
        print(RESULT_PREFIX + json.dumps({
            "candidates": [], "queries": [], "searched_documents": 0,
            "warnings": [f"自动查找失败：{type(error).__name__}: {error}"],
        }, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
