"""一页通 HTML → PDF（A2）。

## 为什么用 QtWebEngine 而不是别的

四条路实测比较过（#83）：

| 方案 | 依赖 | 打包代价 | 还原度 |
|---|---|---|---|
| **QtWebEngine（本模块）** | **零新增**——GUI 本就要 PySide6 | 与 GUI 共用，不额外增加 | Chromium 内核，完美 |
| WeasyPrint | 需 GTK 原生库（Pango/HarfBuzz DLL） | 要打包一堆 DLL，配置繁琐 | 无 JS；**flexbox 支持不完整** |
| Playwright | 需另下 ~150MB Chromium | 二进制默认不随 PyInstaller 打包 | 完美但与上者重复 |
| wkhtmltopdf | 独立 exe | 最易打包 | 旧 WebKit，flexbox 差；**项目已归档停维护** |

WeasyPrint 对本项目**尤其不合适**：#80 把图表并排改成 flexbox（那是把最坏组合
从 2.02 页压到 1.36 页的关键），而它的 flexbox 支持一直不完整——正好踩在我们
最依赖的那个特性上。

## 使用注意

`printToPdf` 是异步的，且需要一个 Qt 事件循环。本模块对**命令行调用**自建
一次性 QApplication；将来 GUI 里已有事件循环时，改调 `print_page_async()`，
不要再建第二个 QApplication（一个进程只能有一个）。
"""

from __future__ import annotations

import os
import pathlib

# GPU 在无显卡/远程会话下会刷一串 ERROR 且拖慢启动；导 PDF 不需要 GPU。
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")

# 页面加载完到开始打印之间的等待（毫秒）。
# 图是 base64 内嵌的、字体是系统字体，理论上 loadFinished 时已就绪，
# 但实测留一拍更稳——不留时偶发首图未绘制完就被截进 PDF。
_SETTLE_MS = 1200
_TIMEOUT_MS = 60000
_A4_CSS_HEIGHT = 1123  # A4 @ 96dpi；仅用于定位超页区块，正式判定仍以 PDF 页数为准。


def _layout_audit_script() -> str:
    """返回页面内区块的位置，供超页时给分析师定位；不改动 HTML。"""
    return f"""(() => {{
      const page = document.querySelector('.page');
      if (!page) return {{}};
      const top = page.getBoundingClientRect().top;
      const label = (el) => {{
        if (el.matches('h1')) return '标题';
        if (el.matches('.sub')) return '副标题';
        if (el.matches('.concl')) return '核心结论';
        if (el.matches('.under')) return '挂钩标的';
        if (el.matches('.quote')) return '推荐结构·参考报价';
        if (el.matches('.footer')) return '页脚与风险提示';
        if (el.matches('.logic')) return (el.querySelector('.ltitle')?.innerText || '策略逻辑').trim();
        return (el.className || el.tagName).toString();
      }};
      const sections = Array.from(page.children).map(el => {{
        const rect = el.getBoundingClientRect();
        return {{label: label(el), top: Math.round(rect.top - top), bottom: Math.round(rect.bottom - top)}};
      }});
      return {{page_height: Math.round(page.getBoundingClientRect().height), printable_height: {_A4_CSS_HEIGHT},
               overflow: sections.filter(s => s.bottom > {_A4_CSS_HEIGHT}), sections}};
    }})()"""


def _page_layout():
    from PySide6.QtCore import QMarginsF
    from PySide6.QtGui import QPageLayout, QPageSize

    # 零边距：版面自身的 padding 就是页边距（`.page` 已按 A4 内容宽 794px 设计），
    # 这里再加边距会导致二次内缩、右侧被裁。
    return QPageLayout(QPageSize(QPageSize.A4), QPageLayout.Portrait,
                       QMarginsF(0, 0, 0, 0))


def html_to_pdf(html_path: str | pathlib.Path,
                pdf_path: str | pathlib.Path | None = None, *, layout_audit: dict | None = None) -> str:
    """把一页通 HTML 转成 PDF，返回 PDF 路径。**自建事件循环，供命令行调用。**

    GUI 环境下不要用这个（QApplication 只能有一个），改用 `print_page_async`。
    """
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication

    src = pathlib.Path(html_path).resolve()
    if not src.exists():
        raise FileNotFoundError(f"找不到 HTML：{src}")
    out = pathlib.Path(pdf_path) if pdf_path else src.with_suffix(".pdf")

    app = QApplication.instance() or QApplication([])
    view = QWebEngineView()
    state: dict = {"err": "超时未完成"}

    def on_pdf(data: bytes) -> None:
        if data:
            out.write_bytes(data)
            state.pop("err", None)
        else:
            state["err"] = "printToPdf 返回空数据"
        app.quit()

    def on_load(ok: bool) -> None:
        if not ok:
            state["err"] = "页面加载失败"
            app.quit()
            return

        def after_audit(result) -> None:
            # Qt 把 JS object 映射为 Python dict；失败时保持空，页数校验仍照常执行。
            if layout_audit is not None and isinstance(result, dict):
                layout_audit.update(result)
            QTimer.singleShot(_SETTLE_MS,
                              lambda: view.page().printToPdf(on_pdf, _page_layout()))

        view.page().runJavaScript(_layout_audit_script(), after_audit)

    view.loadFinished.connect(on_load)
    view.setUrl(QUrl.fromLocalFile(str(src)))
    # 视口按 A4 比例给足：宽度不足时 flex 布局会提前换行，
    # 导出的版面就与浏览器里看到的不一致。
    view.resize(900, 1200)
    view.show()
    QTimer.singleShot(_TIMEOUT_MS, app.quit)
    app.exec()

    if "err" in state:
        raise RuntimeError(f"导出 PDF 失败：{state['err']}")
    return str(out)


def print_page_async(page, pdf_path: str | pathlib.Path, on_done=None) -> None:
    """在**已有事件循环**里把某个 QWebEnginePage 打印成 PDF（GUI 用）。

    page 必须已经加载完成；on_done(路径 或 None) 在写盘后回调。
    """
    out = pathlib.Path(pdf_path)

    def _cb(data: bytes) -> None:
        ok = bool(data)
        if ok:
            out.write_bytes(data)
        if on_done:
            on_done(str(out) if ok else None)

    page.printToPdf(_cb, _page_layout())


def page_count(pdf_path: str | pathlib.Path) -> int:
    """数 PDF 页数。用于校验"一页通"是否真的只有一页。"""
    import re

    data = pathlib.Path(pdf_path).read_bytes()
    return len(re.findall(rb"/Type\s*/Page[^s]", data))


if __name__ == "__main__":  # python -m render.pdf_out [xxx.html]
    import glob
    import io
    import sys

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    args = [a for a in sys.argv[1:] if a.endswith(".html")]
    src = args[0] if args else max(glob.glob("output/onepager_*.html"),
                                   key=os.path.getmtime)
    out = html_to_pdf(src)
    n = page_count(out)
    print(f"✓ {pathlib.Path(src).name}")
    print(f"  → {out}")
    print(f"  {os.path.getsize(out) / 1024:.0f} KB，共 {n} 页"
          + ("" if n == 1 else "　⚠ 一页通应为 1 页，请看内部底稿的「版面预算」"))
