"""GUI 的独立产品画像 worker：stdout 只输出 JSON，避免阻塞桌面主线程。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from core.product_profile import collect


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
        print(json.dumps({"ok": True, "profile": profile}, ensure_ascii=False))
        return 0
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"ok": False, "message": f"标的产品画像生成失败：{type(error).__name__}: {error}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
