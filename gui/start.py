"""桌面端启动器。

VS Code 可能把“运行 Python 文件”绑定到一个只装了基础 Python 的解释器；而桌面端
依赖 PySide6。此启动器优先使用当前可用解释器，否则只在本机已存在的解释器中寻找
PySide6 并重新拉起 ``app.py``，不安装依赖、不修改环境。
"""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP = Path(__file__).resolve().with_name("app.py")


def _has_pyside(python: Path) -> bool:
    try:
        result = subprocess.run(
            [str(python), "-c", "import PySide6"], cwd=ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _candidates() -> list[Path]:
    configured = os.environ.get("RESEARCH_HELPER_GUI_PYTHON", "").strip()
    values = [configured] if configured else []
    # 当前项目在此电脑上的已验证环境；不存在时自然跳过，项目仍可在其它电脑配置环境变量。
    values.append(str(Path.home() / "anaconda3" / "python.exe"))
    values.append(sys.executable)
    seen: set[Path] = set()
    out: list[Path] = []
    for value in values:
        if not value:
            continue
        try:
            path = Path(value).resolve()
        except OSError:
            continue
        if path not in seen and path.is_file():
            seen.add(path)
            out.append(path)
    return out


def main() -> None:
    current = Path(sys.executable).resolve()
    for python in _candidates():
        if not _has_pyside(python):
            continue
        if python == current:
            runpy.run_path(str(APP), run_name="__main__")
        else:
            subprocess.Popen([str(python), str(APP)], cwd=ROOT)
        return
    print("无法启动桌面端：未找到安装 PySide6 的 Python 解释器。")
    print("请安装 PySide6，或设置 RESEARCH_HELPER_GUI_PYTHON 指向可用 python.exe。")


if __name__ == "__main__":
    main()
