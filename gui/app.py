"""最小可用桌面界面：客户输入、实时阶段、结果与人工补数入口。

界面不重写研究流程：它启动同一份 ``main.py``，并消费 ``output/runs`` 的结构化运行记录。
这样命令行、未来打包版和 GUI 对一次运行的状态解释完全一致。
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
import tempfile
import uuid
from datetime import datetime
from html import escape as _html_escape
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QProcess, QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow,
    QMessageBox, QPushButton, QPlainTextEdit, QProgressBar, QSplitter, QTabWidget,
    QScrollArea, QSizePolicy, QTextBrowser, QVBoxLayout, QWidget,
)

try:  # 预览是增强功能；少数精简 PySide6 安装不带 WebEngine 时仍可启动应用。
    from PySide6.QtWebEngineWidgets import QWebEngineView
except ImportError:  # pragma: no cover - 取决于终端用户安装的 Qt 组件
    QWebEngineView = None

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "output" / "runs"
LOCAL_CONFIG = ROOT / "config.local.json"


# 使用接近 Codex 桌面端的中性浅色界面：灰白画布、轻边框、深色主操作、
# 低饱和蓝色焦点。报告的酒红色板独立保留在 render/，不反向染色操作界面。
APPLE_STYLE = """
QMainWindow, QDialog {
    background: #f7f7f8;
}
QWidget {
    color: #202124;
    font-family: "Segoe UI Variable", "Segoe UI", "SF Pro Text", "Microsoft YaHei UI";
    font-size: 13px;
}
QFrame#card {
    background: #ffffff;
    border: 1px solid #e4e4e7;
    border-radius: 12px;
}
QLabel[kind="section"] {
    color: #202124;
    font-size: 15px;
    font-weight: 700;
    padding: 2px 0;
}
QLabel[kind="field-title"] {
    color: #303136;
    font-weight: 600;
}
QLabel[kind="caption"] {
    color: #6b7280;
    font-size: 12px;
}
QLabel[kind="callout"] {
    color: #374151;
    background: #f8fafc;
    border: 1px solid #e5e7eb;
    border-radius: 9px;
    padding: 7px 10px;
}
QLineEdit, QPlainTextEdit, QTextBrowser, QListWidget, QComboBox {
    background: #ffffff;
    border: 1px solid #d7d9de;
    border-radius: 8px;
    padding: 5px 8px;
    selection-background-color: #2563eb;
    selection-color: #ffffff;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextBrowser:focus, QListWidget:focus, QComboBox:focus {
    border: 2px solid #4f8cff;
    padding: 4px 7px;
}
QLineEdit:disabled, QPlainTextEdit:disabled, QListWidget:disabled, QComboBox:disabled {
    color: #9ca3af;
    background: #f5f5f6;
    border-color: #e5e7eb;
}
QComboBox {
    min-height: 20px;
    padding-right: 24px;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #d7d9de;
    border-radius: 8px;
    padding: 3px;
    selection-background-color: #e8f0ff;
    selection-color: #1d4ed8;
}
QPushButton {
    min-height: 20px;
    max-height: 28px;
    background: #ffffff;
    border: 1px solid #d7d9de;
    border-radius: 8px;
    padding: 2px 9px;
    font-weight: 600;
}
QPushButton:hover {
    background: #f4f4f5;
    border-color: #bfc3ca;
}
QPushButton:pressed {
    background: #e9eaec;
}
QPushButton:disabled {
    color: #a1a1aa;
    background: #f5f5f6;
    border-color: #e5e7eb;
}
QPushButton[role="primary"] {
    color: #ffffff;
    background: #2d2f33;
    border-color: #2d2f33;
}
QPushButton[role="primary"]:hover {
    background: #17181a;
    border-color: #17181a;
}
QPushButton[role="danger"] {
    color: #b42318;
    background: #fff8f7;
    border-color: #fecaca;
}
QPushButton[role="danger"]:hover {
    color: #991b1b;
    background: #fef2f2;
    border-color: #fca5a5;
}
QCheckBox {
    spacing: 6px;
}
QTabWidget::pane {
    background: #ffffff;
    border: 1px solid #e4e4e7;
    border-radius: 10px;
    top: -1px;
}
QTabBar::tab {
    color: #6b7280;
    background: transparent;
    border: none;
    padding: 6px 12px;
    margin-right: 2px;
}
QTabBar::tab:selected {
    color: #202124;
    background: #ececef;
    border-radius: 7px;
    font-weight: 600;
}
QProgressBar {
    height: 6px;
    background: #e5e7eb;
    border: none;
    border-radius: 3px;
    text-align: center;
}
QProgressBar::chunk {
    background: #4b5563;
    border-radius: 3px;
}
QScrollArea {
    background: transparent;
    border: none;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #c7c9ce;
    border-radius: 4px;
    min-height: 22px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QToolTip {
    color: #ffffff;
    background: #26272b;
    border: none;
    border-radius: 6px;
    padding: 5px;
}
"""


def _section_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("kind", "section")
    return label


def _purpose_label(title: str, purpose: str) -> QWidget:
    """确认页左栏同时解释字段含义和它影响的流程。"""
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 2, 12, 2)
    layout.setSpacing(2)
    heading = QLabel(title)
    heading.setProperty("kind", "field-title")
    caption = QLabel(purpose)
    caption.setProperty("kind", "caption")
    caption.setWordWrap(True)
    layout.addWidget(heading)
    layout.addWidget(caption)
    box.setMinimumWidth(225)
    return box

# 直接运行 ``gui/app.py`` 时，Python 会把 gui/ 而不是项目根目录放到 sys.path 首位；
# GUI 内的补数表单随后 import core.* 会因此失败。无论从 VS Code、启动器还是命令行启动，
# 都把项目根目录显式加入模块搜索路径。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


@dataclass
class QuoteJob:
    """GUI 内存中的正式报价任务。

    它刻意不落盘：真正的一次性 selection 只在任务开始时写入，报价结束即由桥接层消费归档。
    关闭应用或取消排队都不会遗留一份可被下一次运行误用的待报价选择。
    """

    job_id: str
    request: str
    underlying: str
    market_prompt: str
    selection_payload: dict
    overrides: dict
    export_pdf: bool
    underlying_name: str = ""
    underlying_note: str = ""
    source_run_id: str = ""
    # 多标的比较时，每只标的都产生独立的 OptionHelper 报价；不能让后一份报价
    # 覆盖主题研究报告中的前一份报价表。
    comparison_mode: bool = False
    # 多标的报价纳入一页通时使用的、已经冻结的事实；普通报价保持为空。
    inclusion_entries: list[dict] | None = None
    status: str = "queued"  # queued / running / completed / failed / cancelled / blocked
    run_id: str = ""
    message: str = ""
    forced_stop_reason: str = ""


class JsonEditor(QDialog):
    def __init__(self, parent: QWidget | None = None, initial_path: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("人工数据补充 JSON（仅自动取数缺口时使用）")
        self.resize(740, 520)
        self.path = Path(initial_path) if initial_path else None
        self.editor = QPlainTextEdit()
        template = {"字段覆盖": {}, "外部事实": {},
                    "事件证据": {"事件事实": [], "产业机制": [], "A股暴露": [],
                               "组合传导链": [], "传导关系": []}}
        if self.path and self.path.is_file():
            try:
                self.editor.setPlainText(self.path.read_text(encoding="utf-8"))
            except OSError:
                self.editor.setPlainText(json.dumps(template, ensure_ascii=False, indent=2))
        else:
            self.editor.setPlainText(json.dumps(template, ensure_ascii=False, indent=2))
        hint = QLabel("字段覆盖必须含“值”和“来源”；判定字段不可人工覆盖。事件驱动报告需填写“事件事实＋产业机制＋A股暴露＋组合传导链”，或提供一条可直接证明传导的原文。")
        hint.setWordWrap(True)
        save = QPushButton("保存…")
        add_field = QPushButton("添加字段覆盖…")
        add_fact = QPushButton("添加外部事实…")
        add_event_fact = QPushButton("添加事件事实…")
        add_transmission = QPushButton("添加传导证据…")
        close = QPushButton("关闭")
        save.clicked.connect(self.save)
        add_field.clicked.connect(self.add_field)
        add_fact.clicked.connect(self.add_fact)
        add_event_fact.clicked.connect(self.add_event_fact)
        add_transmission.clicked.connect(self.add_transmission)
        close.clicked.connect(self.reject)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(add_field)
        actions.addWidget(add_fact)
        actions.addWidget(add_event_fact)
        actions.addWidget(add_transmission)
        actions.addWidget(save)
        actions.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(self.editor)
        layout.addLayout(actions)

    def _data(self) -> dict:
        try:
            value = json.loads(self.editor.toPlainText())
            return value if isinstance(value, dict) else {
                "字段覆盖": {}, "外部事实": {},
                "事件证据": {"事件事实": [], "产业机制": [], "A股暴露": [],
                           "组合传导链": [], "传导关系": []},
            }
        except json.JSONDecodeError:
            QMessageBox.warning(self, "JSON 无效", "请先修正 JSON 后再使用表单添加。")
            return {}

    def _write_data(self, value: dict) -> None:
        value.setdefault("字段覆盖", {})
        value.setdefault("外部事实", {})
        self._event_evidence(value)
        self.editor.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))

    @staticmethod
    def _event_evidence(value: dict) -> dict:
        evidence = value.get("事件证据")
        if not isinstance(evidence, dict):
            evidence = {}
            value["事件证据"] = evidence
        if not isinstance(evidence.get("事件事实"), list):
            evidence["事件事实"] = []
        if not isinstance(evidence.get("产业机制"), list):
            evidence["产业机制"] = []
        if not isinstance(evidence.get("A股暴露"), list):
            evidence["A股暴露"] = []
        if not isinstance(evidence.get("组合传导链"), list):
            evidence["组合传导链"] = []
        if not isinstance(evidence.get("传导关系"), list):
            evidence["传导关系"] = []
        return evidence

    def add_field(self) -> None:
        from core.overrides import overridable_fields
        dialog = QDialog(self)
        dialog.setWindowTitle("添加字段覆盖")
        field, value, source, note = QComboBox(), QLineEdit(), QLineEdit(), QLineEdit()
        field.addItems(overridable_fields())
        source.setPlaceholderText("必填，例如 Wind终端·2026-08-21")
        ok, cancel = QPushButton("添加"), QPushButton("取消")
        ok.clicked.connect(dialog.accept); cancel.clicked.connect(dialog.reject)
        form = QFormLayout(dialog)
        form.addRow("字段", field); form.addRow("值", value); form.addRow("来源", source); form.addRow("说明", note)
        form.addRow("", ResearchHelperWindow._row(ok, cancel))
        if dialog.exec() and source.text().strip() and value.text().strip():
            data = self._data()
            if data:
                data["字段覆盖"][field.currentText()] = {"值": value.text().strip(), "来源": source.text().strip(), "说明": note.text().strip()}
                self._write_data(data)
        elif dialog.result() and (not source.text().strip() or not value.text().strip()):
            QMessageBox.information(self, "缺少信息", "字段覆盖必须填写值和来源。")

    def add_fact(self) -> None:
        dialog = QDialog(self); dialog.setWindowTitle("添加外部事实")
        name, value = QLineEdit(), QPlainTextEdit()
        ok, cancel = QPushButton("添加"), QPushButton("取消")
        ok.clicked.connect(dialog.accept); cancel.clicked.connect(dialog.reject)
        form = QFormLayout(dialog)
        form.addRow("事实名称/待补事项", name); form.addRow("内容（请含来源）", value); form.addRow("", ResearchHelperWindow._row(ok, cancel))
        if dialog.exec() and name.text().strip() and value.toPlainText().strip():
            data = self._data()
            if data:
                data["外部事实"][name.text().strip()] = value.toPlainText().strip()
                self._write_data(data)

    def add_event_fact(self) -> None:
        dialog = QDialog(self); dialog.setWindowTitle("添加事件事实")
        content, source, link = QPlainTextEdit(), QLineEdit(), QLineEdit()
        source.setPlaceholderText("必填，例如 SK hynix Q2 业绩公告 p4")
        link.setPlaceholderText("可选：官方公告或材料链接")
        ok, cancel = QPushButton("添加"), QPushButton("取消")
        ok.clicked.connect(dialog.accept); cancel.clicked.connect(dialog.reject)
        form = QFormLayout(dialog)
        form.addRow("已披露事实", content); form.addRow("来源", source); form.addRow("链接", link)
        form.addRow("", ResearchHelperWindow._row(ok, cancel))
        if dialog.exec() and content.toPlainText().strip() and source.text().strip():
            data = self._data()
            if data:
                self._event_evidence(data)["事件事实"].append(
                    {"内容": content.toPlainText().strip(), "来源": source.text().strip(),
                     "链接": link.text().strip()})
                self._write_data(data)
        elif dialog.result():
            QMessageBox.information(self, "缺少信息", "事件事实必须填写内容和来源。")

    def add_transmission(self) -> None:
        dialog = QDialog(self); dialog.setWindowTitle("添加事件传导证据")
        relation, content, source, link = QComboBox(), QPlainTextEdit(), QLineEdit(), QLineEdit()
        relation.addItems(["直接竞争", "供应链", "客户需求", "技术替代", "估值情绪映射", "其他"])
        source.setPlaceholderText("必填，例如产业链研报名称 p8 / 公司披露")
        link.setPlaceholderText("可选：材料链接")
        ok, cancel = QPushButton("添加"), QPushButton("取消")
        ok.clicked.connect(dialog.accept); cancel.clicked.connect(dialog.reject)
        form = QFormLayout(dialog)
        form.addRow("关系类型", relation); form.addRow("为何影响本次行业/ETF", content)
        form.addRow("来源", source); form.addRow("链接", link)
        form.addRow("", ResearchHelperWindow._row(ok, cancel))
        if dialog.exec() and content.toPlainText().strip() and source.text().strip():
            data = self._data()
            if data:
                self._event_evidence(data)["传导关系"].append(
                    {"关系": relation.currentText(), "内容": content.toPlainText().strip(),
                     "来源": source.text().strip(), "链接": link.text().strip()})
                self._write_data(data)
        elif dialog.result():
            QMessageBox.information(self, "缺少信息", "传导证据必须填写关系、内容和来源。")

    def save(self) -> None:
        try:
            json.loads(self.editor.toPlainText())
        except json.JSONDecodeError as error:
            QMessageBox.warning(self, "JSON 无效", f"无法保存：{error}")
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存人工补数 JSON", str(self.path or ROOT / "overrides.json"), "JSON (*.json)")
        if path:
            Path(path).write_text(self.editor.toPlainText(), encoding="utf-8")
            self.path = Path(path)
            self.accept()


class PastedMaterialDialog(QDialog):
    """把分析师粘贴的原文与其明确填写的来源保存为可追溯研究材料。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("粘贴补充材料")
        self.resize(760, 510)
        self.source = QLineEdit()
        self.source.setPlaceholderText("必填，例如：公司公告《2026 年半年报》p4 / 机构研报名称及日期")
        self.content = QPlainTextEdit()
        self.content.setPlaceholderText("粘贴需要作为研究依据的原文。系统会读取原文提炼候选逻辑，不会把来源说明改写成事实。")
        self.content.setMinimumHeight(320)
        save, cancel = QPushButton("保存到补充材料"), QPushButton("取消")
        save.clicked.connect(self._validate_and_accept)
        cancel.clicked.connect(self.reject)
        hint = QLabel("来源和原文均为必填。保存后会写入 sources/，与上传 PDF 一样在下一次研究中读取，并在候选及报告中保留来源。")
        hint.setWordWrap(True)
        form = QFormLayout(self)
        form.addRow(hint)
        form.addRow("资料来源", self.source)
        form.addRow("原文内容", self.content)
        form.addRow("", ResearchHelperWindow._row(save, cancel))

    def _validate_and_accept(self) -> None:
        if not self.source.text().strip():
            QMessageBox.information(self, "请填写资料来源", "粘贴材料必须填写可供复核的资料来源。")
            return
        if not self.content.toPlainText().strip():
            QMessageBox.information(self, "请粘贴原文", "请粘贴需要作为研究依据的原文内容。")
            return
        self.accept()


class MaterialCandidateDialog(QDialog):
    """展示本地/直链材料的原文片段；分类及导入始终由分析师点击完成。"""

    def __init__(self, parent: QWidget | None = None, *, topic: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("从材料导入事件证据")
        self.resize(900, 620)
        self.topic = topic
        self.candidates = []
        self.imported_facts: list[dict] = []
        self.imported_mechanisms: list[dict] = []
        self.imported_exposures: list[dict] = []
        self.imported_links: list[dict] = []

        self.material_label = QLabel("尚未选择材料。仅提取原文候选，不会调用 LLM 或自动判断事实。")
        self.material_label.setWordWrap(True)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.list.setMinimumHeight(360)
        self.relation = QComboBox()
        self.relation.addItems(["直接竞争", "供应链", "客户需求", "技术替代", "估值情绪映射", "其他"])
        choose = QPushButton("选择本地材料…")
        direct = QPushButton("从 HTTPS 直链获取…")
        import_fact = QPushButton("将选中原文作为事件事实")
        import_mechanism = QPushButton("作为产业机制")
        import_exposure = QPushButton("作为A股暴露")
        import_link = QPushButton("将选中原文作为传导证据")
        close = QPushButton("取消")
        choose.clicked.connect(self.choose_material)
        direct.clicked.connect(self.download_material)
        import_fact.clicked.connect(lambda: self.import_selected("事件事实"))
        import_mechanism.clicked.connect(lambda: self.import_selected("产业机制"))
        import_exposure.clicked.connect(lambda: self.import_selected("A股暴露"))
        import_link.clicked.connect(lambda: self.import_selected("传导证据"))
        close.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "选择公司 IR、业绩公告或产业链材料后，系统只展示逐字原文候选。请自行核对原件，"
            "再把选中内容明确归入事件事实、产业机制、A股暴露或直接传导证据。直链仅支持 HTTPS 的原始文件，不抓取网页。"))
        layout.itemAt(layout.count() - 1).widget().setWordWrap(True)
        layout.addWidget(self.material_label)
        layout.addWidget(ResearchHelperWindow._row(choose, direct))
        layout.addWidget(QLabel("候选原文（可多选）"))
        layout.addWidget(self.list)
        layout.addWidget(QLabel("作为传导证据时的关系类型"))
        layout.addWidget(self.relation)
        layout.addWidget(ResearchHelperWindow._row(import_fact, import_mechanism, import_exposure, import_link, close))

    def choose_material(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择事件材料", str(ROOT), "材料 (*.pdf *.txt *.md *.docx);;所有文件 (*)")
        if path:
            self.load_material(Path(path), reference=str(Path(path).resolve()))

    def download_material(self) -> None:
        url, accepted = QInputDialog.getText(
            self, "HTTPS 材料直链", "请输入官方 PDF/TXT/MD/DOCX 直链（不会抓取普通网页）：")
        if not accepted or not url.strip():
            return
        try:
            from core.material_evidence import download_direct_material
            path = download_direct_material(url.strip())
        except ValueError as error:
            QMessageBox.warning(self, "材料下载失败", str(error))
            return
        self.load_material(path, reference=url.strip())

    def load_material(self, path: Path, *, reference: str) -> None:
        try:
            from core.material_evidence import extract_candidates
            self.candidates = extract_candidates(path, query=self.topic, reference=reference)
        except (OSError, ValueError, RuntimeError) as error:
            QMessageBox.warning(self, "无法读取材料", str(error))
            return
        self.list.clear()
        for index, candidate in enumerate(self.candidates):
            item = QListWidgetItem(f"{candidate.source}\n{candidate.content}")
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.list.addItem(item)
        self.material_label.setText(
            f"已载入：{path.name}；抽取 {len(self.candidates)} 条原文候选。"
            "请对照原件核验后再导入。")
        if not self.candidates:
            QMessageBox.information(self, "没有候选", "材料没有可展示的文本；扫描版 PDF 可能需要 OCR。")

    def import_selected(self, evidence_type: str) -> None:
        selected = self.list.selectedItems()
        if not selected:
            QMessageBox.information(self, "尚未选择", "请选择至少一条原文候选。")
            return
        payload: list[dict] = []
        for widget in selected:
            candidate = self.candidates[int(widget.data(Qt.ItemDataRole.UserRole))]
            item = {"内容": candidate.content, "来源": candidate.source,
                    "链接": candidate.reference, "材料页码": candidate.page}
            if evidence_type in {"产业机制", "传导证据"}:
                item["关系"] = self.relation.currentText()
            payload.append(item)
        if evidence_type == "传导证据":
            self.imported_links = payload
        elif evidence_type == "产业机制":
            self.imported_mechanisms = payload
        elif evidence_type == "A股暴露":
            self.imported_exposures = payload
        else:
            self.imported_facts = payload
        self.accept()


class EvidenceDiscoveryReviewDialog(QDialog):
    """集中审核自动检索候选；默认全不选，分析师确认后才写入证据包。"""

    def __init__(self, parent: QWidget | None, payload: dict) -> None:
        super().__init__(parent)
        self.setWindowTitle("审核自动查找到的事件证据")
        self.resize(1040, 780)
        self.candidates = [dict(item) for item in (payload.get("candidates") or [])
                           if isinstance(item, dict)]
        self.chains = [dict(item) for item in (payload.get("chains") or []) if isinstance(item, dict)]
        self.list = QListWidget()
        self.list.setMinimumHeight(300)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setWordWrap(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._render_candidates()

        query_groups = payload.get("query_groups") or {}
        group_text = "；".join(
            f"{name}：{' / '.join(str(query) for query in values)}"
            for name, values in query_groups.items() if isinstance(values, list)
        )
        queries = group_text or "；".join(str(item) for item in (payload.get("queries") or [])) or "—"
        channel_counts = payload.get("searched_by_channel") or {}
        channel_text = "；".join(f"{name} {count} 份" for name, count in channel_counts.items()) or "—"
        hit_counts = payload.get("search_hits_by_channel") or {}
        hit_text = "；".join(f"{name} {count} 条" for name, count in hit_counts.items()) or "—"
        failure_counts = payload.get("fetch_failures_by_channel") or {}
        failure_text = "；".join(f"{name} {count} 条" for name, count in failure_counts.items()) or "0"
        entity_mismatch = int(payload.get("filtered_entity_mismatch_hits") or 0)
        provider_text = ("、".join(str(item) for item in (payload.get("search_providers") or []))
                         or str(payload.get("configured_search_provider") or "—"))
        call_text = "；".join(
            f"{name} {count} 次" for name, count in (payload.get("search_calls_by_provider") or {}).items()
        ) or "—"
        credit_text = "；".join(
            f"{name} {float(value or 0):g} credits"
            for name, value in (payload.get("search_credits_by_provider") or {}).items()
        ) or "0"
        fact_count = sum(item.get("evidence_type") == "事件事实" for item in self.candidates)
        mechanism_count = sum(item.get("evidence_type") == "产业机制" for item in self.candidates)
        exposure_count = sum(item.get("evidence_type") == "A股暴露" for item in self.candidates)
        link_count = sum(item.get("evidence_type") == "传导证据" for item in self.candidates)
        hint = QLabel(
            "自动检索不会自动采用任何内容。请核对原文并勾选。完整路径为“事件事实＋产业机制＋A股暴露＋组合传导链”；"
            "若某段原文已经直接说明事件怎样影响本次A股对象，也可归为“直接传导证据”走兼容捷径。"
        )
        hint.setWordWrap(True)
        diagnostics_text = (
            f"搜索服务：{provider_text}；调用：{call_text}；额度：{credit_text}"
            f"\n分阶段检索：{queries}\n读取原文：{payload.get('searched_documents') or 0} 份（{channel_text}）"
            f"\n搜索命中：{hit_text}；正文读取/质量失败：{failure_text}"
            f"；事件主体不匹配过滤：{entity_mismatch} 条"
            f"\n候选：事件事实 {fact_count}；产业机制 {mechanism_count}；A股暴露 {exposure_count}；"
            f"直接传导 {link_count}；组合链 {len(self.chains)}"
            + (f"\n提示：\n" + "\n".join(f"• {item}" for item in (payload.get("warnings") or []))
               if payload.get("warnings") else "")
        )
        diagnostics = QPlainTextEdit(diagnostics_text)
        diagnostics.setReadOnly(True)
        diagnostics.setMaximumHeight(160)
        diagnostics.setStyleSheet("color:#666; background:#f7f7f8;")
        open_source = QPushButton("打开当前候选来源")
        as_fact = QPushButton("改为事件事实")
        as_mechanism = QPushButton("改为产业机制")
        as_exposure = QPushButton("改为A股暴露")
        as_link = QPushButton("改为直接传导")
        select_all = QPushButton("全部勾选")
        clear = QPushButton("清空勾选")
        confirm, cancel = QPushButton("采用已勾选证据"), QPushButton("取消")
        open_source.clicked.connect(self.open_current_source)
        as_fact.clicked.connect(lambda: self._change_type("事件事实"))
        as_mechanism.clicked.connect(lambda: self._change_type("产业机制"))
        as_exposure.clicked.connect(lambda: self._change_type("A股暴露"))
        as_link.clicked.connect(lambda: self._change_type("传导证据"))
        select_all.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
        clear.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
        confirm.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(diagnostics)
        layout.addWidget(self.list)
        layout.addWidget(ResearchHelperWindow._row(open_source, as_fact, as_mechanism, as_exposure, as_link))
        self.chain_list = QListWidget()
        self.chain_list.setMinimumHeight(120)
        self.chain_list.setWordWrap(True)
        self.chain_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for index, chain in enumerate(self.chains):
            refs = "+".join([
                *(str(value) for value in (chain.get("fact_ids") or [])),
                *(str(value) for value in (chain.get("mechanism_ids") or [])),
                *(str(value) for value in (chain.get("exposure_ids") or [])),
            ])
            item = QListWidgetItem(
                f"{refs}｜方向 {chain.get('direction') or '不确定'}｜置信度 {chain.get('confidence') or '低'}\n"
                f"{chain.get('conclusion') or ''}\n边界：{chain.get('reason') or '—'}"
            )
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.chain_list.addItem(item)
        layout.addWidget(QLabel("组合传导链（勾选链时会自动采用其引用的三类原文）"))
        layout.addWidget(self.chain_list)
        layout.addWidget(ResearchHelperWindow._row(select_all, clear, confirm, cancel))

    def _render_candidates(self, checked: set[int] | None = None, current_index: int | None = None) -> None:
        """重新显示分析师改类后的候选，仍保持明确的逐条勾选边界。"""
        checked = checked or set()
        self.list.clear()
        for index, candidate in enumerate(self.candidates):
            kind = str(candidate.get("evidence_type") or "候选")
            relation = str(candidate.get("relation") or "")
            source_kind = str(candidate.get("source_kind") or "公开材料")
            prefix = f"{kind}｜{relation}｜" if relation else f"{kind}｜"
            text = (
                f"{candidate.get('evidence_id') or '—'}｜{prefix}{source_kind}\n"
                f"{candidate.get('content') or ''}\n"
                f"来源：{candidate.get('source') or ''}\n"
                f"模型相关性说明（不是证据原文）：{candidate.get('reason') or '—'}"
            )
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if index in checked else Qt.CheckState.Unchecked)
            self.list.addItem(item)
        if current_index is not None and 0 <= current_index < self.list.count():
            self.list.setCurrentRow(current_index)

    def _set_all(self, state: Qt.CheckState) -> None:
        for row in range(self.list.count()):
            self.list.item(row).setCheckState(state)

    def _change_type(self, evidence_type: str) -> None:
        item = self.list.currentItem()
        if item is None:
            QMessageBox.information(self, "尚未选择", "请先点选需要调整分类的候选。")
            return
        index = int(item.data(Qt.ItemDataRole.UserRole))
        checked = {
            int(self.list.item(row).data(Qt.ItemDataRole.UserRole))
            for row in range(self.list.count())
            if self.list.item(row).checkState() == Qt.CheckState.Checked
        }
        candidate = self.candidates[index]
        if str(candidate.get("evidence_type") or "") != evidence_type:
            # 分类改变后原来的 F/M/E/D 前缀不再可信；合并进证据包时按新类别重编号。
            candidate["evidence_id"] = ""
        candidate["evidence_type"] = evidence_type
        if evidence_type in {"事件事实", "A股暴露"}:
            candidate["relation"] = ""
        elif evidence_type in {"产业机制", "传导证据"} and not str(candidate.get("relation") or "").strip():
            relation, accepted = QInputDialog.getItem(
                self, "确认传导关系", "该原文体现的传导关系：",
                ["供应链", "客户需求", "直接竞争", "技术替代", "估值情绪映射", "其他"], 0, False,
            )
            candidate["relation"] = relation if accepted else "其他"
        self._render_candidates(checked, index)

    def open_current_source(self) -> None:
        item = self.list.currentItem()
        if item is None:
            QMessageBox.information(self, "尚未选择", "请先点选一条候选。")
            return
        candidate = self.candidates[int(item.data(Qt.ItemDataRole.UserRole))]
        raw = str(candidate.get("link") or "").strip()
        if not raw:
            QMessageBox.information(self, "没有来源链接", "该候选没有可打开的链接或文件路径。")
            return
        if raw.lower().startswith("https://"):
            QDesktopServices.openUrl(QUrl(raw))
        else:
            path = Path(raw)
            if path.is_file():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))
            else:
                QMessageBox.warning(self, "来源不可用", "找不到候选对应的本地材料。")

    def selected(self) -> dict:
        selected_ids: set[str] = set()
        selected_chains: list[dict] = []
        type_by_id = {str(item.get("evidence_id") or ""): str(item.get("evidence_type") or "")
                      for item in self.candidates if str(item.get("evidence_id") or "")}
        invalid_chains = 0
        for row in range(self.chain_list.count()):
            item = self.chain_list.item(row)
            if item.checkState() != Qt.CheckState.Checked:
                continue
            chain = self.chains[int(item.data(Qt.ItemDataRole.UserRole))]
            typed_refs = ((chain.get("fact_ids") or [], "事件事实"),
                          (chain.get("mechanism_ids") or [], "产业机制"),
                          (chain.get("exposure_ids") or [], "A股暴露"))
            if any(type_by_id.get(str(value)) != expected
                   for values, expected in typed_refs for value in values):
                invalid_chains += 1
                continue
            selected_chains.append({
                "事实证据ID": list(chain.get("fact_ids") or []),
                "机制证据ID": list(chain.get("mechanism_ids") or []),
                "暴露证据ID": list(chain.get("exposure_ids") or []),
                "结论": str(chain.get("conclusion") or "").strip(),
                "方向": str(chain.get("direction") or "不确定"),
                "置信度": str(chain.get("confidence") or "低"),
                "边界": str(chain.get("reason") or ""),
            })
            selected_ids.update(str(value) for key in ("fact_ids", "mechanism_ids", "exposure_ids")
                                for value in (chain.get(key) or []))
        chosen_indices = {
            int(self.list.item(row).data(Qt.ItemDataRole.UserRole))
            for row in range(self.list.count())
            if self.list.item(row).checkState() == Qt.CheckState.Checked
        }
        for index, candidate in enumerate(self.candidates):
            if str(candidate.get("evidence_id") or "") in selected_ids:
                chosen_indices.add(index)
        result = {"事件事实": [], "产业机制": [], "A股暴露": [],
                  "组合传导链": selected_chains, "传导关系": []}
        for index in sorted(chosen_indices):
            candidate = self.candidates[index]
            value = {
                "证据ID": str(candidate.get("evidence_id") or "").strip(),
                "内容": str(candidate.get("content") or "").strip(),
                "来源": str(candidate.get("source") or "").strip(),
                "链接": str(candidate.get("link") or "").strip(),
                "取得方式": "自动检索后经分析师确认",
            }
            if candidate.get("evidence_type") == "传导证据":
                value["关系"] = str(candidate.get("relation") or "其他")
                result["传导关系"].append(value)
            elif candidate.get("evidence_type") == "产业机制":
                value["关系"] = str(candidate.get("relation") or "其他")
                result["产业机制"].append(value)
            elif candidate.get("evidence_type") == "A股暴露":
                result["A股暴露"].append(value)
            else:
                result["事件事实"].append(value)
        if invalid_chains:
            QMessageBox.information(
                self, "组合链未采用",
                f"有 {invalid_chains} 条组合链引用的候选已被改类或移除，因此未写入证据包；请重新检索组合或手工建立链。",
            )
        return result


class EventEvidenceDialog(QDialog):
    """事件型研究的表单式证据包；运行时由主窗口自动写成临时 overrides。"""

    def __init__(self, parent: QWidget | None = None, evidence: dict | None = None,
                 *, topic: str = "", discovery_mode: str = "foundation",
                 research_context: dict | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("事件证据包")
        self.resize(860, 560)
        evidence = evidence or {}
        self.facts = [dict(item) for item in (evidence.get("事件事实") or []) if isinstance(item, dict)]
        self.mechanisms = [dict(item) for item in (evidence.get("产业机制") or []) if isinstance(item, dict)]
        self.exposures = [dict(item) for item in (evidence.get("A股暴露") or []) if isinstance(item, dict)]
        self.chains = [dict(item) for item in (evidence.get("组合传导链") or []) if isinstance(item, dict)]
        self.links = [dict(item) for item in (evidence.get("传导关系") or []) if isinstance(item, dict)]
        self.discovery_audit = [dict(item) for item in (evidence.get("检索审计") or [])
                                if isinstance(item, dict)]
        self.fact_list, self.mechanism_list = QListWidget(), QListWidget()
        self.exposure_list, self.chain_list, self.link_list = QListWidget(), QListWidget(), QListWidget()
        self.topic = topic
        self.discovery_mode = discovery_mode
        self.research_context = dict(research_context or {})
        self.discovery_process: QProcess | None = None
        self.discovery_output = ""
        self.discovery_error = ""
        self.discovery_line_buffer = ""
        self.discovery_cancelled = False
        phase_text = (
            "当前是研究对象确认后的第二阶段：系统将使用已确认的行业、公司篮子或主题ETF检索A股暴露，"
            "并与已有事实和产业机制组合传导链。"
            if discovery_mode in {"exposure", "complete"} else
            "当前是第一阶段：先检索事件事实和产业机制；A股暴露将在研究取数对象确认后自动进入第二阶段。"
        )
        self.hint = QLabel(
            phase_text + " 完整证据需包含事实、机制、A股暴露和经确认的组合链；"
            "直接传导原文可作为兼容捷径。任何候选都必须由分析师确认后才会采用。"
        )
        self.hint.setWordWrap(True)

        add_fact, remove_fact = QPushButton("添加事件事实…"), QPushButton("删除选中")
        add_mechanism, remove_mechanism = QPushButton("添加产业机制…"), QPushButton("删除选中")
        add_exposure, remove_exposure = QPushButton("添加A股暴露…"), QPushButton("删除选中")
        add_link, remove_link = QPushButton("添加直接传导…"), QPushButton("删除选中")
        add_chain, remove_chain = QPushButton("添加组合传导链…"), QPushButton("删除选中")
        import_material = QPushButton("从已上传材料导入原文")
        self.auto_discover = QPushButton("自动查找候选证据")
        self.discovery_status = QLabel(
            "自动查找会分别检索事件事实、产业机制和A股暴露原文，并尝试用证据ID组合传导链；各通道有独立配额，"
            "不会自动写入报告。"
        )
        self.discovery_status.setWordWrap(True)
        self.discovery_status.setStyleSheet("color:#666;")
        self.discovery_progress = QProgressBar()
        self.discovery_progress.setRange(0, 100)
        self.discovery_progress.setValue(0)
        self.discovery_progress.setFormat("尚未开始")
        add_fact.clicked.connect(self.add_fact); remove_fact.clicked.connect(self.remove_fact)
        add_mechanism.clicked.connect(self.add_mechanism); remove_mechanism.clicked.connect(self.remove_mechanism)
        add_exposure.clicked.connect(self.add_exposure); remove_exposure.clicked.connect(self.remove_exposure)
        add_link.clicked.connect(self.add_link); remove_link.clicked.connect(self.remove_link)
        add_chain.clicked.connect(self.add_chain)
        remove_chain.clicked.connect(self.remove_chain)
        import_material.clicked.connect(lambda: self.import_material(topic))
        self.auto_discover.clicked.connect(self.start_discovery)
        save, cancel = QPushButton("保存证据"), QPushButton("取消")
        save.clicked.connect(self.accept); cancel.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.hint)
        layout.addWidget(ResearchHelperWindow._row(self.auto_discover, import_material))
        layout.addWidget(self.discovery_status)
        layout.addWidget(self.discovery_progress)
        tabs = QTabWidget()
        for title, widget, actions in (
            ("1 事件事实", self.fact_list, (add_fact, remove_fact)),
            ("2 产业机制", self.mechanism_list, (add_mechanism, remove_mechanism)),
            ("3 A股暴露", self.exposure_list, (add_exposure, remove_exposure)),
            ("4 组合传导链", self.chain_list, (add_chain, remove_chain)),
            ("直接传导（兼容）", self.link_list, (add_link, remove_link)),
        ):
            page = QWidget(); page_layout = QVBoxLayout(page)
            widget.setMinimumHeight(260)
            page_layout.addWidget(widget)
            page_layout.addWidget(ResearchHelperWindow._row(*actions))
            tabs.addTab(page, title)
        layout.addWidget(tabs)
        layout.addWidget(ResearchHelperWindow._row(save, cancel))
        self.refresh()

    def start_discovery(self) -> None:
        if self.discovery_process is not None:
            self.discovery_cancelled = True
            self.discovery_status.setText("正在停止自动查找…")
            self.discovery_process.kill()
            return
        if not self.topic.strip():
            QMessageBox.information(self, "缺少客户需求", "请先填写客户需求，再自动查找事件证据。")
            return
        approved = QMessageBox.question(
            self, "确认外部检索",
            "自动查找会把根据当前客户需求生成的检索词发送到公开搜索服务，"
            "并把成功读取的公开原文发送给当前配置的 LLM 做分类。\n\n"
            "不会自动采用任何结果；是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if approved != QMessageBox.StandardButton.Yes:
            return
        self.discovery_output = ""
        self.discovery_error = ""
        self.discovery_line_buffer = ""
        self.discovery_cancelled = False
        process = QProcess(self)
        process.setWorkingDirectory(str(ROOT))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.readyReadStandardOutput.connect(self._read_discovery_output)
        process.readyReadStandardError.connect(self._read_discovery_error)
        process.finished.connect(self._finish_discovery)
        process.errorOccurred.connect(self._discovery_process_error)
        self.discovery_process = process
        self.auto_discover.setText("停止自动查找")
        self.discovery_status.setText(
            "正在启动自动证据查找；窗口不会冻结，可随时停止。"
        )
        self.discovery_progress.setRange(0, 100)
        self.discovery_progress.setValue(1)
        self.discovery_progress.setFormat("1%｜正在启动…")
        process.start(sys.executable, [str(ROOT / "core" / "event_evidence_worker.py")])
        process.write(json.dumps({
            "topic": self.topic, "sources_dir": str(ROOT / "sources"),
            "mode": self.discovery_mode,
            "research_context": self.research_context,
            "existing_evidence": self.payload(),
        }, ensure_ascii=False).encode("utf-8"))
        process.closeWriteChannel()

    def _read_discovery_output(self) -> None:
        if self.discovery_process:
            chunk = bytes(self.discovery_process.readAllStandardOutput()).decode("utf-8", errors="replace")
            self.discovery_output += chunk
            self.discovery_line_buffer += chunk
            lines = self.discovery_line_buffer.split("\n")
            self.discovery_line_buffer = lines.pop()
            for line in lines:
                if not line.startswith("EVENT_EVIDENCE_PROGRESS="):
                    continue
                try:
                    progress = json.loads(line.split("=", 1)[1])
                except json.JSONDecodeError:
                    continue
                value = max(0, min(100, int(progress.get("value") or 0)))
                message = str(progress.get("message") or "正在处理…")
                self.discovery_progress.setValue(value)
                self.discovery_progress.setFormat(f"{value}%｜{message}")
                self.discovery_status.setText(message)

    def _read_discovery_error(self) -> None:
        if self.discovery_process:
            self.discovery_error += bytes(
                self.discovery_process.readAllStandardError()
            ).decode("utf-8", errors="replace")

    def _discovery_process_error(self, _error) -> None:
        self.discovery_status.setText("自动查找进程无法启动；仍可从材料导入或手工添加。")

    def _finish_discovery(self, _exit_code: int, _status) -> None:
        self._read_discovery_output()
        self._read_discovery_error()
        self.discovery_process = None
        self.auto_discover.setText("自动查找候选证据")
        if self.discovery_cancelled:
            self.discovery_cancelled = False
            self.discovery_status.setText("自动查找已停止；现有证据包未改变。")
            self.discovery_progress.setFormat("已停止")
            return
        prefix = "EVENT_EVIDENCE_DISCOVERY="
        try:
            line = next(
                value for value in reversed(self.discovery_output.splitlines())
                if value.startswith(prefix)
            )
            payload = json.loads(line[len(prefix):])
        except (StopIteration, json.JSONDecodeError):
            detail = self.discovery_error.strip()[-1000:] or "子进程没有返回可解析结果。"
            self.discovery_status.setText("自动查找失败；仍可从材料导入或手工添加。")
            self.discovery_progress.setFormat("失败")
            QMessageBox.warning(self, "自动查找失败", detail)
            return
        candidates = payload.get("candidates") or []
        self.discovery_audit.append({
            "时间": datetime.now().astimezone().isoformat(timespec="seconds"),
            "阶段": self.discovery_mode,
            "配置的搜索服务": str(payload.get("configured_search_provider") or ""),
            "实际搜索服务": list(payload.get("search_providers") or []),
            "检索词": dict(payload.get("query_groups") or {}),
            "搜索调用": dict(payload.get("search_calls_by_provider") or {}),
            "额度消耗": dict(payload.get("search_credits_by_provider") or {}),
            "命中统计": dict(payload.get("search_hits_by_channel") or {}),
            "正文读取": dict(payload.get("searched_by_channel") or {}),
            "读取失败": dict(payload.get("fetch_failures_by_channel") or {}),
            "主体不匹配过滤": int(payload.get("filtered_entity_mismatch_hits") or 0),
            "搜索与读取明细": list(payload.get("search_audit") or []),
            "分类淘汰": list(payload.get("classification_rejections") or []),
            "提示": list(payload.get("warnings") or []),
        })
        fact_count = sum(item.get("evidence_type") == "事件事实" for item in candidates if isinstance(item, dict))
        mechanism_count = sum(item.get("evidence_type") == "产业机制" for item in candidates if isinstance(item, dict))
        exposure_count = sum(item.get("evidence_type") == "A股暴露" for item in candidates if isinstance(item, dict))
        link_count = sum(item.get("evidence_type") == "传导证据" for item in candidates if isinstance(item, dict))
        providers = ("、".join(str(item) for item in (payload.get("search_providers") or []))
                     or str(payload.get("configured_search_provider") or "未知搜索入口"))
        credits = sum(float(value or 0) for value in (payload.get("search_credits_by_provider") or {}).values())
        self.discovery_status.setText(
            f"{providers} 共命中 {sum((payload.get('search_hits_by_channel') or {}).values())} 条、"
            f"读取 {payload.get('searched_documents') or 0} 份原文、消耗 {credits:g} credits；"
            f"过滤主体不匹配 {int(payload.get('filtered_entity_mismatch_hits') or 0)} 条；"
            f"形成事件事实 {fact_count} 条、"
            f"产业机制 {mechanism_count} 条、A股暴露 {exposure_count} 条、直接传导 {link_count} 条，"
            f"组合链 {len(payload.get('chains') or [])} 条待审核。"
        )
        self.discovery_progress.setValue(100)
        self.discovery_progress.setFormat("100%｜查找完成，等待审核")
        if not candidates:
            warnings = "\n".join(str(item) for item in (payload.get("warnings") or []))
            QMessageBox.information(
                self, "未形成可确认候选",
                (warnings or "没有找到同时满足原文和来源要求的证据。")
                + "\n\n你仍可以上传材料、粘贴原文或手工添加；也可以调整客户问题后重新检索。"
                  "如果不需要验证具体事件影响，可将需求改为普通主题研究。",
            )
            return
        dialog = EvidenceDiscoveryReviewDialog(self, payload)
        if not dialog.exec():
            return
        selected = dialog.selected()
        if not any(selected.values()):
            QMessageBox.information(self, "没有采用候选", "你没有勾选任何证据，现有证据包未改变。")
            return
        self._merge_selected(selected)
        self.refresh()
        self.discovery_status.setText(
            f"已采用事件事实 {len(selected['事件事实'])}、产业机制 {len(selected['产业机制'])}、"
            f"A股暴露 {len(selected['A股暴露'])}、组合链 {len(selected['组合传导链'])}、"
            f"直接传导 {len(selected['传导关系'])}；保存后进入本次运行。"
        )

    @staticmethod
    def _extend_unique(target: list[dict], values: list[dict], *,
                       keys: tuple[str, ...] = ("内容", "来源")) -> None:
        existing = {
            tuple(str(item.get(key) or "").strip() for key in keys)
            for item in target
        }
        for value in values:
            key = tuple(str(value.get(name) or "").strip() for name in keys)
            if key not in existing:
                target.append(value)
                existing.add(key)

    def _merge_selected(self, selected: dict) -> None:
        """合并多次检索结果时重排证据ID，保证组合链始终引用唯一条目。"""
        groups = (("事件事实", self.facts, "F"), ("产业机制", self.mechanisms, "M"),
                  ("A股暴露", self.exposures, "E"), ("传导关系", self.links, "D"))
        used: set[str] = set()
        for _key, target, prefix in groups:
            for index, item in enumerate(target, 1):
                evidence_id = str(item.get("证据ID") or "").strip()
                if not evidence_id or evidence_id in used:
                    serial = index
                    while f"{prefix}{serial}" in used:
                        serial += 1
                    evidence_id = f"{prefix}{serial}"
                    item["证据ID"] = evidence_id
                used.add(evidence_id)
        remap: dict[str, str] = {}
        for key, target, prefix in groups:
            for value in selected.get(key) or []:
                item = dict(value)
                old_id = str(item.get("证据ID") or "").strip()
                duplicate = next((existing for existing in target
                                  if str(existing.get("内容") or "").strip() == str(item.get("内容") or "").strip()
                                  and str(existing.get("来源") or "").strip() == str(item.get("来源") or "").strip()), None)
                if duplicate is not None:
                    if old_id:
                        remap[old_id] = str(duplicate.get("证据ID") or "")
                    continue
                new_id = old_id
                serial = 1
                while not new_id or new_id in used:
                    new_id = f"{prefix}{serial}"
                    serial += 1
                item["证据ID"] = new_id
                if old_id:
                    remap[old_id] = new_id
                used.add(new_id)
                self._extend_unique(target, [item])
        chains: list[dict] = []
        for raw in selected.get("组合传导链") or []:
            chain = dict(raw)
            for key in ("事实证据ID", "机制证据ID", "暴露证据ID"):
                chain[key] = [remap.get(str(value), str(value)) for value in (chain.get(key) or [])]
            chains.append(chain)
        self._extend_unique(self.chains, chains, keys=("结论", "方向"))

    def import_material(self, topic: str) -> None:
        dialog = MaterialCandidateDialog(self, topic=topic)
        # 默认从项目 sources/ 选择材料：材料上传和证据导入因而是一条连续路径。
        # 对于不在 sources/ 的官方披露，弹窗内仍可选择本地文件或 HTTPS 直链。
        source_root = ROOT / "sources"
        source_files = (sorted([*source_root.rglob("*.pdf"), *source_root.rglob("*.txt"), *source_root.rglob("*.md")])
                        if source_root.is_dir() else [])
        if source_files:
            names = [path.name for path in source_files]
            name, accepted = QInputDialog.getItem(
                self, "选择已上传材料", "从 sources/ 选择需要核验的材料：", names, 0, False)
            if not accepted:
                return
            path = next((item for item in source_files if item.name == name), None)
            if path is not None:
                dialog.load_material(path, reference=str(path.resolve()))
        if dialog.exec():
            self.facts.extend(dialog.imported_facts)
            self.mechanisms.extend(dialog.imported_mechanisms)
            self.exposures.extend(dialog.imported_exposures)
            self.links.extend(dialog.imported_links)
            self.refresh()

    @staticmethod
    def _source_row(source: QLineEdit, link: QLineEdit, parent: QWidget) -> QWidget:
        browse = QPushButton("选择材料…")

        def choose() -> None:
            path, _ = QFileDialog.getOpenFileName(parent, "选择来源材料", str(ROOT),
                                                   "材料 (*.pdf *.docx *.txt);;所有文件 (*)")
            if path:
                link.setText(path)
                if not source.text().strip():
                    source.setText(Path(path).name)

        browse.clicked.connect(choose)
        row = QWidget(); layout = QHBoxLayout(row); layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(source, 2); layout.addWidget(link, 3); layout.addWidget(browse)
        return row

    def _edit_item(self, kind: str) -> dict | None:
        dialog = QDialog(self)
        dialog.setWindowTitle(f"添加{kind}")
        content, source, link = QPlainTextEdit(), QLineEdit(), QLineEdit()
        source.setPlaceholderText("来源（必填），例如 SK hynix 业绩公告 p4")
        link.setPlaceholderText("链接或本地材料路径（可选）")
        relation = QComboBox()
        relation.addItems(["直接竞争", "供应链", "客户需求", "技术替代", "估值情绪映射", "其他"])
        confirm, cancel = QPushButton("添加"), QPushButton("取消")
        confirm.clicked.connect(dialog.accept); cancel.clicked.connect(dialog.reject)
        form = QFormLayout(dialog)
        if kind in {"产业机制", "直接传导证据"}:
            form.addRow("关系类型", relation)
            form.addRow("原文内容", content)
        else:
            form.addRow("原文内容", content)
        form.addRow("来源 / 链接", self._source_row(source, link, dialog))
        form.addRow("", ResearchHelperWindow._row(confirm, cancel))
        if not dialog.exec():
            return None
        if not content.toPlainText().strip() or not source.text().strip():
            QMessageBox.information(self, "缺少信息", "内容和来源均为必填项。")
            return None
        item = {"内容": content.toPlainText().strip(), "来源": source.text().strip(),
                "链接": link.text().strip()}
        if kind in {"产业机制", "直接传导证据"}:
            item["关系"] = relation.currentText()
        return item

    def add_fact(self) -> None:
        if item := self._edit_item("事件事实"):
            self.facts.append(item); self.refresh()

    def add_mechanism(self) -> None:
        if item := self._edit_item("产业机制"):
            self.mechanisms.append(item); self.refresh()

    def add_exposure(self) -> None:
        if item := self._edit_item("A股暴露"):
            self.exposures.append(item); self.refresh()

    def add_link(self) -> None:
        if item := self._edit_item("直接传导证据"):
            self.links.append(item); self.refresh()

    def remove_fact(self) -> None:
        row = self.fact_list.currentRow()
        if row >= 0:
            self.facts.pop(row); self.refresh()

    def remove_link(self) -> None:
        row = self.link_list.currentRow()
        if row >= 0:
            self.links.pop(row); self.refresh()

    def remove_mechanism(self) -> None:
        row = self.mechanism_list.currentRow()
        if row >= 0:
            self.mechanisms.pop(row); self.refresh()

    def remove_exposure(self) -> None:
        row = self.exposure_list.currentRow()
        if row >= 0:
            self.exposures.pop(row); self.refresh()

    def remove_chain(self) -> None:
        row = self.chain_list.currentRow()
        if row >= 0:
            self.chains.pop(row); self.refresh()

    def add_chain(self) -> None:
        self._ensure_evidence_ids()
        if not self.facts or not self.mechanisms or not self.exposures:
            QMessageBox.information(
                self, "三类原文尚未齐备",
                "请先分别添加至少一条事件事实、产业机制和A股暴露原文，再组合传导链。",
            )
            return
        dialog = QDialog(self); dialog.setWindowTitle("添加组合传导链")
        fact, mechanism, exposure = QComboBox(), QComboBox(), QComboBox()
        for combo, values in ((fact, self.facts), (mechanism, self.mechanisms), (exposure, self.exposures)):
            for item in values:
                combo.addItem(f"{item.get('证据ID')}｜{str(item.get('内容') or '')[:60]}", item.get("证据ID"))
        conclusion, boundary = QPlainTextEdit(), QLineEdit()
        conclusion.setPlaceholderText("只根据所选三类原文说明事件如何传导至本次A股对象，不补充新事实或新数字。")
        direction, confidence = QComboBox(), QComboBox()
        direction.addItems(["不确定", "正向", "负向", "双向", "中性"])
        confidence.addItems(["低", "中", "高"])
        confirm, cancel = QPushButton("添加"), QPushButton("取消")
        confirm.clicked.connect(dialog.accept); cancel.clicked.connect(dialog.reject)
        form = QFormLayout(dialog)
        form.addRow("事件事实", fact); form.addRow("产业机制", mechanism); form.addRow("A股暴露", exposure)
        form.addRow("组合结论", conclusion); form.addRow("方向", direction); form.addRow("置信度", confidence)
        form.addRow("边界/风险", boundary); form.addRow("", ResearchHelperWindow._row(confirm, cancel))
        if not dialog.exec():
            return
        if not conclusion.toPlainText().strip():
            QMessageBox.information(self, "缺少组合结论", "请填写只基于所选原文的组合结论。")
            return
        self.chains.append({
            "事实证据ID": [str(fact.currentData())],
            "机制证据ID": [str(mechanism.currentData())],
            "暴露证据ID": [str(exposure.currentData())],
            "结论": conclusion.toPlainText().strip(), "方向": direction.currentText(),
            "置信度": confidence.currentText(), "边界": boundary.text().strip(),
        })
        self.refresh()

    def _ensure_evidence_ids(self) -> None:
        used: set[str] = set()
        for values, prefix in ((self.facts, "F"), (self.mechanisms, "M"),
                               (self.exposures, "E"), (self.links, "D")):
            serial = 1
            for item in values:
                value = str(item.get("证据ID") or "").strip()
                if not value or value in used:
                    while f"{prefix}{serial}" in used:
                        serial += 1
                    value = f"{prefix}{serial}"
                    item["证据ID"] = value
                used.add(value)
                serial += 1

    def refresh(self) -> None:
        self._ensure_evidence_ids()
        self.fact_list.clear(); self.mechanism_list.clear(); self.exposure_list.clear()
        self.chain_list.clear(); self.link_list.clear()
        for item in self.facts:
            self.fact_list.addItem(f"{item.get('证据ID', 'F?')}｜事实｜{item.get('内容', '')}\n来源：{item.get('来源', '')}")
        for item in self.links:
            self.link_list.addItem(f"{item.get('证据ID', 'D?')}｜{item.get('关系', '传导')}｜{item.get('内容', '')}\n来源：{item.get('来源', '')}")
        for item in self.mechanisms:
            self.mechanism_list.addItem(
                f"{item.get('证据ID', 'M?')}｜{item.get('关系', '机制')}｜{item.get('内容', '')}\n来源：{item.get('来源', '')}")
        for item in self.exposures:
            self.exposure_list.addItem(
                f"{item.get('证据ID', 'E?')}｜{item.get('内容', '')}\n来源：{item.get('来源', '')}")
        for item in self.chains:
            refs = "+".join([*(item.get("事实证据ID") or []), *(item.get("机制证据ID") or []),
                             *(item.get("暴露证据ID") or [])])
            self.chain_list.addItem(
                f"{refs}｜方向 {item.get('方向', '不确定')}｜置信度 {item.get('置信度', '低')}\n"
                f"{item.get('结论', '')}\n边界：{item.get('边界', '—')}")

    def payload(self) -> dict:
        return {"事件事实": list(self.facts), "产业机制": list(self.mechanisms),
                "A股暴露": list(self.exposures), "组合传导链": list(self.chains),
                "传导关系": list(self.links), "检索审计": list(self.discovery_audit)}


class LogicPickDialog(QDialog):
    """让分析师在 GUI 中选择数据/材料候选逻辑，而非让 LLM 静默决定。"""

    def __init__(self, parent: QWidget | None, payload: dict) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择本次报告逻辑")
        self.resize(900, 650)
        self._auto = False
        self.require_event_chain = bool(payload.get("require_event_chain"))
        self._payload_candidates = [
            dict(item) for item in (payload.get("candidates") or []) if isinstance(item, dict)
        ]
        suggested = {int(item) for item in (payload.get("suggested") or []) if str(item).isdigit()}
        self.suggested = suggested
        self.checks: list[tuple[int, QCheckBox]] = []
        hint_text = (
            "请选择 2–3 条作为报告正文主轴。数据触发项来自已核验行情；材料提炼项仅在您核对原文后才应勾选。"
            "“采用系统建议”只按证据质量和结构组合，不替代专业判断。")
        if self.require_event_chain:
            hint_text += " 本次是事件型报告，至少必须选择一条“已确认事件传导”。"
        hint = QLabel(hint_text)
        hint.setWordWrap(True)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        for raw in self._payload_candidates:
            try:
                index = int(raw.get("index"))
            except (TypeError, ValueError):
                continue
            source_kind = ({
                "thesis": "数据触发",
                "event": "已确认事件传导",
                "doc": "材料原文（请核对）",
            }).get(str(raw.get("kind") or ""), "其他")
            boundary = ""
            if raw.get("kind") == "doc" and raw.get("evidence_scope"):
                scope = str(raw.get("evidence_scope"))
                boundary = f"｜{scope}" + ("（仅案例）" if scope == "公司级" else "")
            title = f"{index}. {raw.get('name') or '未命名'}｜{raw.get('category') or '—'}｜{raw.get('direction') or '—'}｜{source_kind}{boundary}"
            check = QCheckBox(title)
            check.setChecked(index in suggested)
            check.setProperty("system_suggested", index in suggested)
            detail = "依据：" + str(raw.get("basis") or "—")
            if raw.get("source"):
                detail += "\n出处：" + str(raw.get("source"))
            if raw.get("kind") == "doc" and raw.get("evidence_scope") == "公司级":
                detail += "\n提示：公司级材料只能写为该公司的案例，不能作为整个行业盈利或景气的结论。"
            box = QWidget()
            layout = QVBoxLayout(box); layout.setContentsMargins(8, 6, 8, 6)
            layout.addWidget(check)
            label = QLabel(detail); label.setWordWrap(True); label.setStyleSheet("color:#555;")
            layout.addWidget(label)
            item = QListWidgetItem(); item.setSizeHint(box.sizeHint())
            self.list.addItem(item); self.list.setItemWidget(item, box)
            self.checks.append((index, check))

        use_suggested = QPushButton("采用系统建议")
        confirm = QPushButton("确认所选逻辑")
        auto = QPushButton("不选择，交由系统自动挑选")
        cancel = QPushButton("取消本次运行")
        use_suggested.clicked.connect(self._apply_suggested)
        confirm.clicked.connect(self._confirm)
        auto.clicked.connect(self._automatic)
        cancel.clicked.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(self.list)
        layout.addWidget(ResearchHelperWindow._row(use_suggested, confirm, auto, cancel))

    def _apply_suggested(self) -> None:
        for index, check in self.checks:
            check.setChecked(index in self.suggested)

    def _confirm(self) -> None:
        selected = self.selected_indices()
        if not selected:
            QMessageBox.information(self, "请选择逻辑", "请至少勾选一条逻辑，或选择“交由系统自动挑选”。")
            return
        if self.require_event_chain:
            selected_set = set(selected)
            has_event = any(
                int(raw.get("index") or 0) in selected_set and raw.get("kind") == "event"
                for raw in self._payload_candidates
            )
            if not has_event:
                QMessageBox.information(
                    self, "缺少事件传导主轴",
                    "事件型报告至少需要勾选一条“已确认事件传导”，否则正文无法回答事件如何影响A股对象。",
                )
                return
        self.accept()

    def _automatic(self) -> None:
        self._auto = True
        self.accept()

    def selected_indices(self) -> list[int]:
        return [] if self._auto else [index for index, check in self.checks if check.isChecked()]


class RecommendationReviewDialog(QDialog):
    """展示 OptionHelper 已验证候选；分析师只能确认候选或返回修改客户约束。"""

    def __init__(self, parent: QWidget | None, *, underlying: str, candidates: list[dict],
                 client_summary: str = "", product_profile: dict | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("确认 OptionHelper 推荐后发起正式报价")
        self.resize(800, 560)
        self.underlying = underlying
        self.candidates = candidates
        self.choice = QComboBox()
        for item in candidates:
            label = f"主候选" if int(item.get("rank") or 99) == 1 else f"备选 {item.get('rank', '')}"
            self.choice.addItem(f"{label}｜{item.get('product_name') or item.get('product_id')}", item)
        self.detail = QPlainTextEdit(); self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(220)
        self.profile_detail = QPlainTextEdit(); self.profile_detail.setReadOnly(True)
        self.profile_detail.setMaximumHeight(128)
        from core.product_profile import render_for_prompt
        self.profile_detail.setPlainText(render_for_prompt(product_profile))
        self.confirmed = QCheckBox("我已核对客户约束与挂钩标的，确认采用上述 OptionHelper 候选发起正式报价。")
        note = QLabel("产品编号、推荐理由、适配情形和风险均由 OptionHelper Recommender 生成；"
                      "如不采用，请关闭此窗口并修改客户约束后重新推荐。")
        note.setWordWrap(True); note.setStyleSheet("color:#8a5b14;")
        submit, cancel = QPushButton("确认候选并加入正式报价队列"), QPushButton("返回修改条件")
        submit.clicked.connect(self._submit); cancel.clicked.connect(self.reject)
        self.choice.currentIndexChanged.connect(self._refresh_detail)
        form = QFormLayout(self)
        form.addRow("已确认挂钩标的", QLabel(underlying))
        form.addRow("本次客户条件", QLabel(client_summary or "—"))
        form.addRow("标的产品画像（推荐前）", self.profile_detail)
        form.addRow("OptionHelper 推荐", self.choice)
        form.addRow("候选说明", self.detail)
        form.addRow("", self.confirmed)
        form.addRow("", note)
        form.addRow("", ResearchHelperWindow._row(submit, cancel))
        self._refresh_detail()

    def _refresh_detail(self, *_args) -> None:
        item = self.choice.currentData() or {}
        rows = [
            "推荐理由：" + str(item.get("reason") or "—"),
            "适合情形：" + "；".join(item.get("suitable_for") or []) or "—",
            "不适合情形：" + "；".join(item.get("not_suitable_for") or []) or "—",
            "主要风险：" + "；".join(item.get("main_risks") or []) or "—",
        ]
        self.detail.setPlainText("\n".join(rows))

    def _submit(self) -> None:
        if not self.confirmed.isChecked():
            QMessageBox.information(self, "尚未确认", "请确认采用 OptionHelper 推荐候选，或返回修改客户条件。")
            return
        self.accept()

    def selection(self) -> dict:
        item = dict(self.choice.currentData() or {})
        return {
            "product_id": str(item.get("product_id") or ""),
            "underlyings": [self.underlying.upper()],
            "reason": str(item.get("reason") or ""),
            "suitable_for": list(item.get("suitable_for") or []),
            "not_suitable_for": list(item.get("not_suitable_for") or []),
            "main_risks": list(item.get("main_risks") or []),
        }


class BatchRecommendationReviewDialog(QDialog):
    """汇总展示多标的结构推荐：成功项统一审核，失败项保留原因而不阻塞其它标的。"""

    def __init__(self, parent: QWidget | None, *, results: list[dict], client_summary: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("审核多标的 OptionHelper 推荐")
        self.resize(980, 680)
        self._rows: list[tuple[str, QCheckBox, QComboBox]] = []

        hint = QLabel(
            "所有勾选标的已先完成独立结构推荐。请在同一窗口选择要进入正式报价的标的及结构；"
            "“无候选”只表示该标的本轮未通过 OptionHelper 推荐门禁，不影响其它标的。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8a5b14;")
        conditions = QLabel("本次客户条件：" + (client_summary or "—"))
        conditions.setWordWrap(True)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(6, 6, 6, 6)
        for result in results:
            underlying = str(result.get("underlying") or "—").upper()
            candidates = [dict(item) for item in (result.get("candidates") or []) if isinstance(item, dict)]
            card = QFrame()
            card.setFrameShape(QFrame.Shape.StyledPanel)
            card_layout = QVBoxLayout(card)
            title = QLabel(f"<b>{underlying}</b>")
            card_layout.addWidget(title)
            from core.product_profile import render_for_prompt
            profile = QPlainTextEdit()
            profile.setReadOnly(True)
            profile.setMaximumHeight(110)
            profile.setPlainText(render_for_prompt(result.get("product_profile")))
            card_layout.addWidget(profile)
            if not candidates:
                reason = str(result.get("message") or "OptionHelper 未形成可验证的结构候选。")
                unavailable = QLabel("无候选：" + reason)
                unavailable.setWordWrap(True)
                unavailable.setStyleSheet("color:#b42318;")
                card_layout.addWidget(unavailable)
            else:
                include = QCheckBox("将此标的加入正式报价队列")
                include.setChecked(True)
                choice = QComboBox()
                for item in candidates:
                    rank = int(item.get("rank") or 99)
                    label = "主候选" if rank == 1 else f"备选 {rank}"
                    choice.addItem(f"{label}｜{item.get('product_name') or item.get('product_id')}", item)
                detail = QPlainTextEdit()
                detail.setReadOnly(True)
                detail.setMaximumHeight(96)

                def refresh_detail(*_args, combo: QComboBox = choice, target: QPlainTextEdit = detail) -> None:
                    item = dict(combo.currentData() or {})
                    target.setPlainText("\n".join([
                        "推荐理由：" + str(item.get("reason") or "—"),
                        "适合情形：" + "；".join(item.get("suitable_for") or []),
                        "不适合情形：" + "；".join(item.get("not_suitable_for") or []),
                        "主要风险：" + "；".join(item.get("main_risks") or []),
                    ]))

                choice.currentIndexChanged.connect(refresh_detail)
                refresh_detail()
                card_layout.addWidget(include)
                card_layout.addWidget(choice)
                card_layout.addWidget(detail)
                self._rows.append((underlying, include, choice))
            content_layout.addWidget(card)
        content_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)

        self.confirmed = QCheckBox("我已核对客户约束、挂钩标的及所选结构，确认将已勾选项加入正式报价队列。")
        submit, cancel = QPushButton("确认所选项并加入报价队列"), QPushButton("返回修改条件")
        submit.clicked.connect(self._submit)
        cancel.clicked.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(conditions)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.confirmed)
        layout.addWidget(ResearchHelperWindow._row(submit, cancel))

    def _submit(self) -> None:
        if not self._selected_rows():
            QMessageBox.information(self, "未选择报价项", "本轮没有可进入正式报价的候选；可关闭窗口后修改客户条件或标的。")
            return
        if not self.confirmed.isChecked():
            QMessageBox.information(self, "尚未确认", "请确认采用已勾选的 OptionHelper 候选。")
            return
        self.accept()

    def _selected_rows(self) -> list[tuple[str, QComboBox]]:
        return [(underlying, choice) for underlying, include, choice in self._rows if include.isChecked()]

    def selections(self) -> list[tuple[str, dict]]:
        values: list[tuple[str, dict]] = []
        for underlying, choice in self._selected_rows():
            item = dict(choice.currentData() or {})
            values.append((underlying, {
                "product_id": str(item.get("product_id") or ""),
                "underlyings": [underlying],
                "reason": str(item.get("reason") or ""),
                "suitable_for": list(item.get("suitable_for") or []),
                "not_suitable_for": list(item.get("not_suitable_for") or []),
                "main_risks": list(item.get("main_risks") or []),
            }))
        return values


class BatchQuoteInclusionDialog(QDialog):
    """在多标的正式报价全部返回后，由分析师决定哪些冻结表写入一页通。"""

    def __init__(self, parent: QWidget | None, *, entries: list[dict]) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择写入一页通的正式报价")
        self.resize(880, 560)
        self._rows: list[tuple[dict, QCheckBox]] = []
        hint = QLabel(
            "以下均为已完成的 OptionHelper 正式报价。请勾选需要写入一页通“已选产品·参考报价”表格的项目；"
            "未勾选项仍保留在报价比较中，不会丢失。系统不会计算或按胜率排序。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8a5b14;")
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(6, 6, 6, 6)
        for entry in entries:
            underlying = str(entry.get("underlying") or "—").upper()
            product = str(entry.get("product_name") or entry.get("product_id") or "—")
            date = str(entry.get("quote_date") or "—")
            reason = str(entry.get("reason") or "—")
            check = QCheckBox(f"{underlying}｜{product}｜报价日期：{date}")
            detail = QLabel("推荐理由：" + reason)
            detail.setWordWrap(True)
            card = QFrame()
            card.setFrameShape(QFrame.Shape.StyledPanel)
            card_layout = QVBoxLayout(card)
            card_layout.addWidget(check)
            card_layout.addWidget(detail)
            content_layout.addWidget(card)
            self._rows.append((entry, check))
        content_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        self.confirmed = QCheckBox("我已核对所选标的、产品结构和正式报价，确认写入本次一页通。")
        submit, cancel = QPushButton("写入所选报价并更新一页通"), QPushButton("暂不写入")
        submit.clicked.connect(self._submit)
        cancel.clicked.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(scroll, 1)
        layout.addWidget(self.confirmed)
        layout.addWidget(ResearchHelperWindow._row(submit, cancel))

    def _submit(self) -> None:
        if not self.selected_entries():
            QMessageBox.information(self, "尚未选择报价", "请至少勾选一份正式报价，或选择“暂不写入”。")
            return
        if not self.confirmed.isChecked():
            QMessageBox.information(self, "尚未确认", "请确认已核对所选正式报价。")
            return
        self.accept()

    def selected_entries(self) -> list[dict]:
        return [dict(entry) for entry, check in self._rows if check.isChecked()]


class QuoteUnderlyingPoolDialog(QDialog):
    """确认待报价标的；系统扩展候选只有在分析师主动展开后才显示。"""

    def __init__(self, parent: QWidget | None, *, candidates: list[dict],
                 alternatives: list[dict] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("确认待报价标的")
        self.resize(860, 500)
        self.checks: list[tuple[str, QCheckBox]] = []
        self._alternatives = [dict(item) for item in (alternatives or [])]
        candidate_provided = any(
            str(item.get("origin") or "") in {"系统推荐", "研究取数目标"}
            for item in candidates
        )
        hint = QLabel(
            ("系统根据本次研究主题、标准行业及候选流动性找到了下列 ETF。请选择需要进入正式报价审核的工具；"
             "这不是产品推荐结论，OptionHelper 仍会独立核验行情并生成结构候选。")
            if candidate_provided else
            ("客户点名了一个或多个 ETF/个股。Research Helper 的主题研究不替代产品选择；"
             "请勾选需要送入 OptionHelper 的标的。系统会对每一只标的分别生成结构推荐、"
             "等待你确认后再分别正式报价，绝不混成一份多标的报价。")
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8a5b14;")
        list_box = QWidget()
        self.list_layout = QVBoxLayout(list_box)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.addStretch()
        for item in candidates:
            self._add_candidate(item, checked=True)
        self.compare_button = QPushButton(f"比较其他同主题工具（{len(self._alternatives)}）")
        self.compare_button.setVisible(bool(self._alternatives))
        self.compare_button.clicked.connect(self._show_alternatives)
        self.compare_hint = QLabel(
            "其他工具只是系统发现的同主题候选，尚未形成与本次研究完全一致的标的级观点。"
            "只有主动展开并勾选后，才会进入多标的 OptionHelper 审核。"
        )
        self.compare_hint.setWordWrap(True)
        self.compare_hint.setProperty("kind", "caption")
        self.compare_hint.setVisible(bool(self._alternatives))
        confirm, cancel = QPushButton("进入逐标的报价审核"), QPushButton("取消")
        confirm.clicked.connect(self._submit)
        cancel.clicked.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(QLabel("待报价池"))
        layout.addWidget(list_box)
        layout.addWidget(self.compare_button)
        layout.addWidget(self.compare_hint)
        layout.addWidget(ResearchHelperWindow._row(confirm, cancel))

    def _add_candidate(self, item: dict, *, checked: bool) -> None:
        code = str(item.get("code") or "").strip().upper()
        name = str(item.get("name") or "").strip()
        origin = str(item.get("origin") or "客户指定")
        note = str(item.get("note") or "由 OptionHelper 独立核验、定价")
        check = QCheckBox(f"{code}｜{name or '名称待核验'}｜{origin}\n{note}")
        check.setChecked(checked)
        # stretch 始终位于末尾；扩展候选应插在它前面。
        self.list_layout.insertWidget(max(0, self.list_layout.count() - 1), check)
        self.checks.append((code, check))

    def _show_alternatives(self) -> None:
        for item in self._alternatives:
            self._add_candidate(item, checked=False)
        self._alternatives = []
        self.compare_button.hide()
        self.compare_hint.setText("已展开其他同主题工具；请只勾选确实需要进行标的级审核的项目。")

    def _submit(self) -> None:
        if not self.selected_codes():
            QMessageBox.information(self, "尚未选择标的", "请至少选择一只需要进入 OptionHelper 报价审核的标的。")
            return
        self.accept()

    def selected_codes(self) -> list[str]:
        return [code for code, check in self.checks if check.isChecked()]


class LlmSettingsDialog(QDialog):
    """只允许更新本地、被 git 忽略的分析模型设置；绝不回显既有 API key。"""
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("LLM 设置（仅本机）")
        config = _load_json(LOCAL_CONFIG)
        self.model = QComboBox()
        self.model.addItems(["deepseek-v4-flash", "deepseek-v4-pro"])
        current = str(config.get("DEEPSEEK_MODEL") or "deepseek-v4-flash")
        self.model.setCurrentText(current if current in [self.model.itemText(i) for i in range(self.model.count())] else "deepseek-v4-flash")
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("已配置；留空则不修改")
        hint = QLabel("API key 仅写入本机 config.local.json（Git 已忽略），保存后下一次运行生效。")
        hint.setWordWrap(True)
        save, cancel = QPushButton("保存设置"), QPushButton("取消")
        save.clicked.connect(self.save)
        cancel.clicked.connect(self.reject)
        form = QFormLayout(self)
        form.addRow("分析模型", self.model)
        form.addRow("DeepSeek API key", self.key)
        form.addRow("", hint)
        form.addRow("", ResearchHelperWindow._row(save, cancel))

    def save(self) -> None:
        config = _load_json(LOCAL_CONFIG)
        config["DEEPSEEK_MODEL"] = self.model.currentText()
        if self.key.text().strip():
            config["DEEPSEEK_API_KEY"] = self.key.text().strip()
        try:
            LOCAL_CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, "保存失败", str(error))
            return
        self.accept()


class SearchSettingsDialog(QDialog):
    """管理事件证据公开检索；秘密只写本机配置，连接测试在子进程执行。"""

    RESULT_PREFIX = "SEARCH_CONNECTION_RESULT="

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("搜索设置（仅本机）")
        self.resize(620, 390)
        self.config = _load_json(LOCAL_CONFIG)
        self.test_process: QProcess | None = None
        self.test_output = ""
        self.test_error = ""

        self.provider = QComboBox()
        self.provider.addItem("Tavily（推荐，清洗正文）", "tavily")
        self.provider.addItem("Bing RSS（免费搜索入口）", "bing")
        provider = str(self.config.get("SEARCH_PROVIDER") or "tavily").lower()
        self.provider.setCurrentIndex(max(0, self.provider.findData(provider)))

        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("已配置；留空则不修改")

        self.depth = QComboBox()
        self.depth.addItem("基础搜索（1 credit/次）", "basic")
        self.depth.addItem("高级搜索（2 credits/次）", "advanced")
        depth = str(self.config.get("SEARCH_DEPTH") or "basic").lower()
        self.depth.setCurrentIndex(max(0, self.depth.findData(depth)))

        self.country = QComboBox()
        self.country.addItem("不限定国家", "")
        self.country.addItem("中国", "china")
        self.country.addItem("美国", "united states")
        self.country.addItem("英国", "united kingdom")
        self.country.addItem("韩国", "south korea")
        self.country.addItem("日本", "japan")
        country = str(self.config.get("SEARCH_COUNTRY") or "china").lower()
        index = self.country.findData(country)
        self.country.setCurrentIndex(index if index >= 0 else 0)

        self.language = QComboBox()
        self.language.addItem("不限定语言", "")
        self.language.addItem("简体中文优先", "zh-cn")
        self.language.addItem("英文优先", "en")
        language = str(self.config.get("SEARCH_LANGUAGE") or "zh-cn").lower()
        index = self.language.findData(language)
        self.language.setCurrentIndex(index if index >= 0 else 0)

        self.fallback = QCheckBox("Tavily 不可用、额度不足或无结果时自动使用 Bing RSS")
        raw_fallback = self.config.get("SEARCH_BING_FALLBACK", True)
        enabled = (raw_fallback if isinstance(raw_fallback, bool) else
                   str(raw_fallback).lower() not in {"0", "false", "no", "off"})
        self.fallback.setChecked(bool(enabled))
        self.status = QLabel("尚未测试连接。测试 Tavily 固定使用基础搜索，预计消耗 1 credit。")
        self.status.setWordWrap(True)
        self.status.setProperty("kind", "caption")
        hint = QLabel(
            "Tavily API Key 仅写入本机 config.local.json（Git 已忽略），不会写入运行日志。"
            "环境变量 TAVILY_API_KEY 具有更高优先级。国家和语言仅用于结果排序偏好，不强制过滤其他来源。"
        )
        hint.setWordWrap(True)

        self.test_button = QPushButton("测试连接")
        self.save_button = QPushButton("保存设置")
        cancel = QPushButton("取消")
        self.test_button.clicked.connect(self.test_connection)
        self.save_button.clicked.connect(self.save)
        cancel.clicked.connect(self.reject)
        self.provider.currentIndexChanged.connect(self._sync_provider)

        form = QFormLayout(self)
        form.addRow("搜索提供商", self.provider)
        form.addRow("Tavily API Key", self.key)
        form.addRow("搜索深度", self.depth)
        form.addRow("国家偏好", self.country)
        form.addRow("语言偏好", self.language)
        form.addRow("失败兜底", self.fallback)
        form.addRow("", hint)
        form.addRow("连接状态", self.status)
        form.addRow("", ResearchHelperWindow._row(self.test_button, self.save_button, cancel))
        self._sync_provider()

    def _sync_provider(self) -> None:
        tavily = self.provider.currentData() == "tavily"
        self.key.setEnabled(tavily)
        self.depth.setEnabled(tavily)
        self.country.setEnabled(tavily)
        self.language.setEnabled(tavily)
        self.fallback.setEnabled(tavily)

    def _settings(self) -> dict:
        values = {
            "provider": str(self.provider.currentData() or "tavily"),
            "search_depth": str(self.depth.currentData() or "basic"),
            "country": str(self.country.currentData() or ""),
            "language": str(self.language.currentData() or ""),
            "bing_fallback": self.fallback.isChecked(),
        }
        if self.key.text().strip():
            values["api_key"] = self.key.text().strip()
        return values

    def test_connection(self) -> None:
        if self.test_process is not None:
            return
        from core import config as runtime_config
        if self.provider.currentData() == "tavily" and not (
                self.key.text().strip() or self.config.get("TAVILY_API_KEY")
                or runtime_config.TAVILY_API_KEY):
            QMessageBox.information(self, "缺少 Tavily API Key", "请先填写 API Key，再测试连接。")
            return
        self.test_output = ""
        self.test_error = ""
        process = QProcess(self)
        process.setWorkingDirectory(str(ROOT))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.readyReadStandardOutput.connect(
            lambda: self._read_test_stream(standard_output=True))
        process.readyReadStandardError.connect(
            lambda: self._read_test_stream(standard_output=False))
        process.finished.connect(self._finish_test)
        process.errorOccurred.connect(self._test_process_error)
        self.test_process = process
        self.test_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.status.setText("正在测试搜索入口，请稍候…")
        process.start(sys.executable, [str(ROOT / "core" / "search_test_worker.py")])
        process.write(json.dumps(self._settings(), ensure_ascii=False).encode("utf-8"))
        process.closeWriteChannel()

    def _read_test_stream(self, *, standard_output: bool) -> None:
        if not self.test_process:
            return
        raw = (self.test_process.readAllStandardOutput() if standard_output
               else self.test_process.readAllStandardError())
        value = bytes(raw).decode("utf-8", errors="replace")
        if standard_output:
            self.test_output += value
        else:
            self.test_error += value

    def _finish_test(self, _exit_code: int, _status) -> None:
        self._read_test_stream(standard_output=True)
        self._read_test_stream(standard_output=False)
        self.test_process = None
        self.test_button.setEnabled(True)
        self.save_button.setEnabled(True)
        try:
            line = next(item for item in reversed(self.test_output.splitlines())
                        if item.startswith(self.RESULT_PREFIX))
            result = json.loads(line[len(self.RESULT_PREFIX):])
        except (StopIteration, json.JSONDecodeError):
            self.status.setText("连接测试失败：" + (self.test_error.strip()[-300:] or "未返回测试结果"))
            return
        if result.get("ok"):
            sample = str(result.get("sample_title") or "已连接")
            credits = result.get("credits") or 0
            self.status.setText(
                f"连接成功｜{result.get('provider') or '搜索入口'}｜测试命中：{sample}｜本次 credits：{credits}")
        else:
            self.status.setText(
                f"连接失败｜{result.get('provider') or '搜索入口'}｜{result.get('error') or '未返回原因'}")

    def _test_process_error(self, _error) -> None:
        self.test_button.setEnabled(True)
        self.save_button.setEnabled(True)
        self.status.setText("连接测试进程无法启动；请检查当前 Python 环境与网络设置。")

    def save(self) -> None:
        values = self._settings()
        config = _load_json(LOCAL_CONFIG)
        config["SEARCH_PROVIDER"] = values["provider"]
        config["SEARCH_DEPTH"] = values["search_depth"]
        config["SEARCH_COUNTRY"] = values["country"]
        config["SEARCH_LANGUAGE"] = values["language"]
        config["SEARCH_BING_FALLBACK"] = values["bing_fallback"]
        if values.get("api_key"):
            config["TAVILY_API_KEY"] = values["api_key"]
        try:
            LOCAL_CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, "保存失败", str(error))
            return
        self.accept()


class IFindCredentialsDialog(QDialog):
    """管理两条彼此独立的 iFinD 凭证通道，且从不回显既有秘密。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("iFinD 数据与报价凭证（仅本机）")
        self.resize(570, 260)
        self.account = QLineEdit()
        self.account.setPlaceholderText("已配置；留空则不修改")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("已配置；留空则不修改")
        self.refresh_token = QLineEdit()
        self.refresh_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.refresh_token.setPlaceholderText("已配置；留空则不修改")
        hint = QLabel(
            "研究数据使用 iFinD 账号和密码；OptionHelper 正式报价使用独立的 Refresh Token。"
            "三项均为本机秘密：账号/密码仅写入被 Git 忽略的 config.local.json；"
            "Refresh Token 仅交由 OptionHelper 保存到被忽略的 .optionhelper/memory.md。"
            "若系统环境变量已设置 IFIND_ACCOUNT/IFIND_PASSWORD，它们优先于此处配置。"
        )
        hint.setWordWrap(True)
        save, cancel = QPushButton("安全保存"), QPushButton("取消")
        save.clicked.connect(self.save)
        cancel.clicked.connect(self.reject)
        form = QFormLayout(self)
        form.addRow("研究数据 iFinD 账号", self.account)
        form.addRow("研究数据 iFinD 密码", self.password)
        form.addRow("OptionHelper Refresh Token", self.refresh_token)
        form.addRow("", hint)
        form.addRow("", ResearchHelperWindow._row(save, cancel))

    def _save_refresh_token(self, token: str) -> tuple[bool, str]:
        """沿用 Skill 自己的原子保存入口，避免 GUI 了解或重写 memory.md 格式。"""
        from core import config

        skill_root = Path(config.OPTIONHELPER_SKILL_ROOT) if config.OPTIONHELPER_SKILL_ROOT else None
        script = skill_root / "scripts" / "environment_check.py" if skill_root else None
        if not skill_root or not script or not script.is_file():
            return False, "未找到最新版 OptionHelper Skill；请先配置 Skill 根目录。"
        try:
            # CLI 入口使用 getpass() 从 Windows 控制台读取，子进程 stdin 不可靠：它会继续等待
            # CONIN$，导致 GUI 虽已取得 Token 仍超时。直接载入同一 Skill 的保存函数，保留其
            # 格式校验、权限控制和原子替换，不把 Token 写入任何 GUI 自己管理的文件。
            spec = importlib.util.spec_from_file_location("optionhelper_environment_check", script)
            if spec is None or spec.loader is None:
                return False, "无法载入 OptionHelper 的 Refresh Token 保存组件。"
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.save_ifind_refresh_token(token, ROOT, skill_root=skill_root)
        except (OSError, ValueError, AttributeError, ImportError) as error:
            return False, f"OptionHelper 未接受该 Refresh Token：{error}"
        return True, ""

    def save(self) -> None:
        account, password, token = (self.account.text().strip(), self.password.text(), self.refresh_token.text().strip())
        if token:
            ok, message = self._save_refresh_token(token)
            if not ok:
                QMessageBox.warning(self, "Refresh Token 未保存", message)
                return
        config = _load_json(LOCAL_CONFIG)
        if account:
            config["IFIND_ACCOUNT"] = account
        if password:
            config["IFIND_PASSWORD"] = password
        try:
            LOCAL_CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, "保存失败", str(error))
            return
        self.accept()


class MarketConfirmationDialog(QDialog):
    """高风险市场/行业映射确认；结果由后端再次做数据源校验。"""

    def __init__(self, payload: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.payload = payload
        previous = payload.get("previous_confirmation") or {}
        previous = previous if isinstance(previous, dict) else {}
        self.setWindowTitle("确认研究市场与取数目标")
        self.resize(1080, 760)
        self.setMinimumSize(900, 620)

        self.topic = QLabel(str(payload.get("topic") or "—"))
        self.topic.setWordWrap(True)
        self.original = str(payload.get("original_market") or "A股")
        self.mode = QComboBox()
        self.mode.addItem("明确映射到 A 股研究口径并继续", "map_a")
        self.mode.addItem("仅研究当前市场（不生成产品报价）", "research_only")
        self.mode.addItem("保留原市场并选择主题 ETF/指数取数", "keep_market")
        # 即使解析器先识别到“全球/港股”等事件背景，也默认给出可执行的 A 股研究路径；
        # 选择该项时 value() 会强制写入 A股，不依赖禁用下拉框的显示值。
        self.mode.setCurrentIndex(0)

        self.market = QComboBox()
        self.market.addItems(["A股", "港股", "跨市场"])
        self.theme = QLineEdit(str(previous.get("research_theme")
                                   or payload.get("proposed_theme") or ""))
        self.theme.setPlaceholderText("例如：光模块 / 黄金；说明本次真正研究的细分主题")
        # 分析师选择的是“哪一组证券参与取数”，不是替系统猜行业名。标准行业、
        # 人工主题篮子和主题 ETF 是三条互斥路径，不能再用一个选项混合表达。
        self.scope = QComboBox()
        scope_options = [str(item).strip() for item in (payload.get("verified_scope_options") or [])
                         if str(item).strip()]
        if not scope_options and str(payload.get("proposed_scope") or "").strip():
            scope_options = [str(payload.get("proposed_scope") or "").strip()]
        theme_route = bool(payload.get("theme_etf_route"))
        theme_scope = str(payload.get("theme_etf_scope") or payload.get("proposed_theme") or "").strip()
        for scope in dict.fromkeys(scope_options):
            self.scope.addItem(
                f"{scope}（标准行业路径：按数据源行业成分研究）",
                {"scope": scope, "mode": "industry"},
            )
        basket = [item for item in (payload.get("theme_basket_candidates") or [])
                  if isinstance(item, dict) and item.get("code")]
        if basket and theme_scope:
            self.scope.addItem(
                f"{theme_scope}（人工主题篮子路径：按下方勾选公司研究）",
                {"scope": (scope_options[0] if scope_options else theme_scope),
                 "mode": "theme_basket"},
            )
        if theme_route and theme_scope:
            self.scope.addItem(
                f"{theme_scope}（主题 ETF 路径：按所选 ETF 真实成分研究）",
                {"scope": theme_scope, "mode": "theme_etf"},
            )
        if not scope_options and not (theme_route and theme_scope):
            self.scope.addItem("未形成可用研究路径，请改为仅研究或取消本次运行",
                               {"scope": "", "mode": "industry"})
        previous_scope = str(previous.get("research_scope") or "").strip()
        previous_research_mode = str(previous.get("research_mode") or "").strip()
        if previous_scope:
            for index in range(self.scope.count()):
                data = self.scope.itemData(index)
                data = data if isinstance(data, dict) else {"scope": data, "mode": "industry"}
                if (str(data.get("scope") or "").strip() == previous_scope
                        and (not previous_research_mode
                             or str(data.get("mode") or "") == previous_research_mode)):
                    self.scope.setCurrentIndex(index)
                    break
        else:
            recommended_mode = str(payload.get("recommended_research_mode") or "industry")
            for index in range(self.scope.count()):
                data = self.scope.itemData(index)
                if isinstance(data, dict) and str(data.get("mode") or "industry") == recommended_mode:
                    self.scope.setCurrentIndex(index)
                    break
        self.scope_hint = QLabel(
            "主题 ETF 路径不要求细分主题冒充标准行业：提交后会校验 ETF 官方名称、跟踪指数、主题暴露与流动性，再按真实成分取数。")
        self.scope_hint.setWordWrap(True)
        self.scope_hint.setStyleSheet("color:#666;")
        self.theme_basket_min = int(payload.get("theme_basket_min") or 5)
        self.theme_basket_codes: list[QListWidgetItem] = []
        basket_box = QWidget()
        basket_layout = QVBoxLayout(basket_box)
        basket_layout.setContentsMargins(0, 0, 0, 0)
        self.theme_basket = QListWidget()
        self.theme_basket.setMaximumHeight(165)
        self.theme_basket.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        if basket:
            for candidate in basket:
                name, code = str(candidate.get("name") or ""), str(candidate.get("code") or "")
                origin, note = str(candidate.get("origin") or "候选"), str(candidate.get("note") or "")
                item = QListWidgetItem(f"{name}（{code}）｜{origin}｜{note}")
                item.setData(Qt.ItemDataRole.UserRole, code)
                item.setToolTip(note)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                # 需求解析已经核验过的公司属于核心样本；iFinD 动态扩展项必须由分析师
                # 主动勾选，不能悄悄加入研究整体法。
                item.setCheckState(Qt.CheckState.Checked if candidate.get("core") else Qt.CheckState.Unchecked)
                self.theme_basket.addItem(item)
                self.theme_basket_codes.append(item)
            self.theme_basket.itemChanged.connect(self._sync_theme_basket_hint)
            basket_layout.addWidget(self.theme_basket)
            self.theme_basket_hint = QLabel()
            self.theme_basket_hint.setWordWrap(True)
            self.theme_basket_hint.setStyleSheet("color:#a66a00;")
            basket_layout.addWidget(self.theme_basket_hint)
        else:
            self.theme_basket = None
            self.theme_basket_hint = QLabel(
                "尚未形成可核验的细分主题公司篮子；若选择标准行业路径，"
                "仍会按已确认的标准行业取数，但不输出细分主题专属成分股结论。"
            )
            self.theme_basket_hint.setWordWrap(True)
            self.theme_basket_hint.setStyleSheet("color:#666;")
            basket_layout.addWidget(self.theme_basket_hint)
        self.theme_basket_selector = basket_box
        self.underlying = QComboBox()
        self.underlying.setEditable(True)
        self.underlying.addItem("", {"code": "", "name": ""})
        for item in payload.get("suggested_instruments") or []:
            origin = str(item.get("origin") or "")
            exposure = item.get("exposure") if isinstance(item.get("exposure"), dict) else {}
            exposure_label = str(exposure.get("label") or {
                "direct": "直接暴露", "partial": "部分暴露", "unrelated": "不相关",
            }.get(str(item.get("exposure_level") or ""), "待核验"))
            label = (f"{item.get('code', '')}｜{item.get('name', '')}｜主题暴露：{exposure_label}｜"
                     f"{origin}｜{item.get('note', '')}")
            self.underlying.addItem(label, item)
        previous_code = str(previous.get("underlying_code") or "").strip().upper()
        if previous_code:
            previous_code = (previous_code[:-3] + ".SH"
                             if previous_code.endswith(".SS") else previous_code)
            matched_index = next((index for index in range(self.underlying.count())
                                  if str((self.underlying.itemData(index) or {}).get("code") or "").upper()
                                  == previous_code), -1)
            if matched_index >= 0:
                self.underlying.setCurrentIndex(matched_index)
            else:
                self.underlying.setEditText(previous_code)
        self.underlying.lineEdit().setPlaceholderText("请选择研究 ETF；也可输入代码，例如 513050.SH")
        self.underlying_hint = QLabel(
            "仅在选择“主题 ETF 路径”时填写。该 ETF 的真实指数成分将成为研究取数篮子；"
            "它不会因此自动成为正式报价标的。"
        )
        self.underlying_hint.setWordWrap(True)
        self.underlying_hint.setStyleSheet("color:#666;")
        self.reason = QLineEdit(str(payload.get("reason") or ""))
        self.reason.setPlaceholderText("部分暴露时必填：说明为何该 ETF 仍可代表本次主题")
        self.partial_exposure_confirmed = QCheckBox(
            "若系统判定为“部分暴露”，我已核对跟踪指数和主要成分，确认按填写的映射理由继续"
        )
        self.partial_exposure_confirmed.setChecked(bool(previous.get("partial_exposure_confirmed", False)))
        self.partial_exposure_confirmed.setToolTip(
            "只用于部分暴露 ETF；证券真实性、跟踪指数真实性和流动性不足不能通过此项绕过。"
        )
        # 下拉框只承担路径选择，不随宽窗口横向铺满；长说明由下方可换行标签展示。
        self.mode.setMaximumWidth(560)
        self.market.setMaximumWidth(240)
        self.scope.setMaximumWidth(720)
        self.underlying.setMaximumWidth(720)

        errors = payload.get("errors") or []
        notice = str(payload.get("notice") or "")
        discovery_notice = str(payload.get("discovery_notice") or "")
        self.message = QLabel(
            ("上次校验未通过：\n• " + "\n• ".join(errors)) if errors
            else (notice + ("\n" + discovery_notice if discovery_notice else "")))
        self.message.setWordWrap(True)
        self.message.setStyleSheet("color:#a61b29;" if errors else "color:#666;")
        confirm, cancel = QPushButton("校验并继续"), QPushButton("取消本次运行")
        confirm.setProperty("role", "primary")
        cancel.setProperty("role", "danger")
        confirm.clicked.connect(self._submit)
        cancel.clicked.connect(self.reject)
        self.mode.currentIndexChanged.connect(self._sync_mode)
        self.scope.currentIndexChanged.connect(self._sync_scope_path)
        self.underlying.currentIndexChanged.connect(self._sync_underlying_hint)

        intro = QLabel(
            "这一步只确认“研究什么、数据从哪里取”。正式挂钩标的不会在这里自动确定；"
            "研究完成后，系统会另开窗口让分析师选择哪些证券进入 OptionHelper。"
        )
        intro.setWordWrap(True)
        intro.setProperty("kind", "callout")

        form = QFormLayout()
        form.setContentsMargins(22, 20, 22, 20)
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(14)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow(_purpose_label("解析主题", "系统对客户原始需求的摘要；只用于核对是否理解正确。"), self.topic)
        form.addRow(_purpose_label("处理方式", "决定映射到哪个市场研究，以及研究后是否允许进入产品报价流程。"), self.mode)
        form.addRow(_purpose_label("确认市场", "限定行情、行业和证券校验所使用的市场范围。"), self.market)
        form.addRow(_purpose_label("研究主题", "用于材料检索、论点生成和报告标题；不直接指定成分股。"), self.theme)
        scope_box = QWidget()
        scope_layout = QVBoxLayout(scope_box); scope_layout.setContentsMargins(0, 0, 0, 0)
        scope_layout.setSpacing(6)
        scope_layout.addWidget(self.scope); scope_layout.addWidget(self.scope_hint)
        form.addRow(_purpose_label("研究取数路径", "决定研究数据来自标准行业成分、人工勾选公司，还是 ETF 真实指数成分。"), scope_box)
        form.addRow(_purpose_label("主题研究篮子", "仅“人工主题篮子”路径参与取数；其他路径下不会使用这些勾选。"), self.theme_basket_selector)
        form.addRow(_purpose_label("主题 ETF / 指数", "仅“主题 ETF”路径必填；其真实指数成分用于研究，不等于正式报价标的。"), self.underlying)
        form.addRow(_purpose_label("取数目标说明", "展示系统为什么推荐当前 ETF，以及真实性、主题暴露和流动性校验规则。"), self.underlying_hint)
        form.addRow(_purpose_label("映射理由", "说明研究主题为何映射到当前行业、篮子或 ETF，并写入内部审计。"), self.reason)
        form.addRow(_purpose_label("部分暴露确认", "仅 ETF 为较宽行业或相邻产业链时使用；需同时填写映射理由。"), self.partial_exposure_confirmed)
        form.addRow("", self.message)
        form.addRow("", ResearchHelperWindow._row(confirm, cancel))

        content = QFrame()
        content.setObjectName("card")
        content.setLayout(form)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 18, 22, 20)
        outer.setSpacing(12)
        outer.addWidget(intro)
        outer.addWidget(scroll, 1)
        self._sync_mode()
        self._sync_scope_path()
        self._sync_underlying_hint()
        self._sync_theme_basket_hint()

    def _sync_mode(self) -> None:
        mode = self.mode.currentData()
        if mode == "map_a":
            self.market.setCurrentText("A股")
            self.market.setEnabled(False)
        elif mode == "research_only":
            self.market.setCurrentText(self.original)
            self.market.setEnabled(False)
        else:
            self.market.setCurrentText(self.original)
            self.market.setEnabled(True)
        self._sync_scope_path()

    def _sync_underlying_hint(self, *_args) -> None:
        data = self.underlying.currentData() if self.underlying.currentIndex() >= 0 else {}
        data = data if isinstance(data, dict) else {}
        note = str(data.get("note") or "").strip()
        origin = str(data.get("origin") or "").strip()
        exposure = data.get("exposure") if isinstance(data.get("exposure"), dict) else {}
        level = str(exposure.get("level") or data.get("exposure_level") or "")
        label = {"direct": "直接暴露", "partial": "部分暴露", "unrelated": "不相关"}.get(level, "待数据源核验")
        exposure_reason = str(exposure.get("reason") or "").strip()
        tracking = str(exposure.get("tracking_index") or data.get("tracking_index") or "").strip()
        components = [str(item) for item in (exposure.get("major_constituents") or []) if str(item).strip()]
        exposure_text = f"主题暴露：{label}" + (f"（{exposure_reason}）" if exposure_reason else "")
        if tracking:
            exposure_text += f"；跟踪指数：{tracking}"
        if components:
            exposure_text += "；主要成分：" + "、".join(components[:5])
        if level == "partial":
            exposure_text += "。必须勾选下方确认并填写映射理由后才能继续。"
        if note:
            self.underlying_hint.setText(
                f"{exposure_text}。{origin or '候选'}理由：{note}。该选择只确定研究取数篮子，"
                "研究完成后仍需单独确认是否用于报价。"
            )
        else:
            self.underlying_hint.setText(
                f"{exposure_text}。手工输入代码将在提交后校验证券真实性、主题暴露、真实跟踪指数、"
                "主要成分与近20日流动性；"
                "通过后仅作为本次研究取数目标。"
            )

    def _scope_value(self) -> tuple[str, str]:
        data = self.scope.currentData() if self.scope.currentIndex() >= 0 else {}
        if isinstance(data, dict):
            return str(data.get("scope") or "").strip(), str(data.get("mode") or "industry").strip()
        # 兼容旧 payload/测试代码。
        return str(data or "").strip(), "industry"

    def _sync_scope_path(self, *_args) -> None:
        scope, research_mode = self._scope_value()
        self.underlying.setEnabled(
            research_mode == "theme_etf")
        self.partial_exposure_confirmed.setEnabled(research_mode == "theme_etf")
        if research_mode == "theme_etf":
            self.scope_hint.setText(
                f"将研究“{scope or self.theme.text().strip()}”：所选 ETF 通过官方信息校验后，"
                "系统直接使用其真实成分作为研究篮子；下方主题公司勾选不参与本路径。")
            if self.theme_basket is not None:
                self.theme_basket.setEnabled(False)
            self.theme_basket_hint.setText("主题 ETF 路径将使用 ETF 真实成分，无需人工拼主题公司篮子。")
        elif research_mode == "theme_basket":
            self.scope_hint.setText(
                f"将研究“{self.theme.text().strip() or scope}”：只使用下方由分析师勾选、"
                "且已核验代码的主题公司取数；标准行业只用于校验 A 股映射。")
            if self.theme_basket is not None:
                self.theme_basket.setEnabled(True)
                self._sync_theme_basket_hint()
            else:
                self.theme_basket_hint.setText("当前没有可核验主题公司，不能使用人工主题篮子路径。")
        else:
            self.scope_hint.setText(
                f"将按数据源已验证标准行业“{scope or '—'}”的成分取数；"
                "下方主题公司候选不参与本路径，也不需要人工勾选。")
            if self.theme_basket is not None:
                self.theme_basket.setEnabled(False)
                self.theme_basket_hint.setText("标准行业路径自动使用行业成分，无需勾选主题公司。")
            else:
                scope_name = scope or "已确认的标准行业"
                self.theme_basket_hint.setText(
                    f"尚未形成可核验的细分主题公司篮子；本次仍按标准行业“{scope_name}”取数。"
                    "报告会明确这是标准行业口径，不把它写成细分主题专属成分股结论。"
                )

    def _selected_theme_basket_codes(self) -> list[str]:
        return [str(item.data(Qt.ItemDataRole.UserRole) or "").strip().upper()
                for item in self.theme_basket_codes
                if item.checkState() == Qt.CheckState.Checked and item.data(Qt.ItemDataRole.UserRole)]

    def _sync_theme_basket_hint(self, *_args) -> None:
        if not hasattr(self, "theme_basket_hint"):
            return
        if hasattr(self, "scope") and self._scope_value()[1] != "theme_basket":
            return
        count = len(self._selected_theme_basket_codes())
        if not self.theme_basket_codes:
            return
        if count < self.theme_basket_min:
            self.theme_basket_hint.setText(
                f"当前已选 {count} 只：仅作为核心样本，不生成“行业整体”PB、ROE、盈利等聚合结论；"
                f"建议至少勾选 {self.theme_basket_min} 只。")
        elif count < 8:
            self.theme_basket_hint.setText(
                f"当前已选 {count} 只：将按窄口径主题篮子聚合，报告会标注样本范围。")
        else:
            self.theme_basket_hint.setText(
                f"当前已选 {count} 只：将按主题行业篮子做整体法聚合（最多保留 20 只）。")

    def value(self) -> dict:
        mode = str(self.mode.currentData() or "")
        raw = self.underlying.currentText().strip()
        data = self.underlying.currentData() if self.underlying.currentIndex() >= 0 else {}
        data = data if isinstance(data, dict) else {}
        code = str(data.get("code") or raw.split("｜", 1)[0]).strip().upper()
        if code.endswith(".SS"):
            code = code[:-3] + ".SH"
        name = str(data.get("name") or "").strip()
        # “映射到 A 股”与“仅研究原市场”下市场下拉框是禁用控件。不要依赖禁用
        # 控件在不同 Qt/Windows 组合里是否保留 currentText，而是由处理方式确定值。
        market = "A股" if mode == "map_a" else (self.original if mode == "research_only"
                                                  else self.market.currentText().strip())
        scope, research_mode = self._scope_value()
        if research_mode == "theme_etf":
            scope = self.theme.text().strip() or scope
        return {
            "market": market,
            "research_theme": self.theme.text().strip(),
            "research_scope": scope,
            "research_mode": research_mode,
            # 确认页中该字段只是“主题 ETF 研究取数目标”。标准行业和
            # 人工篮子的报价标的必须等研究完成后再由分析师确认。
            "underlying_code": code if research_mode == "theme_etf" else "",
            "underlying_name": name if research_mode == "theme_etf" else "",
            "research_only": mode == "research_only",
            "reason": self.reason.text().strip(),
            "partial_exposure_confirmed": self.partial_exposure_confirmed.isChecked(),
            "theme_basket_codes": (self._selected_theme_basket_codes()
                                   if research_mode == "theme_basket" else []),
        }

    def _submit(self) -> None:
        """先在界面解释必填项，避免反复启动后端校验才知道漏选了 ETF。"""
        value = self.value()
        if not value["research_theme"]:
            QMessageBox.information(self, "请确认研究主题", "请填写本次真正要研究的细分主题，例如“光模块”或“黄金”。")
            return
        if not value["research_scope"]:
            QMessageBox.information(
                self, "未形成可用研究路径",
                "系统既没有形成可校验标准行业，也没有形成可用的主题 ETF 路径。\n\n"
                "请改选可用的标准行业路径，或选择主题 ETF 取数路径并填写 ETF 代码。",
            )
            return
        if value.get("research_mode") == "theme_etf" and not value["underlying_code"]:
            QMessageBox.information(
                self, "请确认主题 ETF 取数目标",
                "当前选择的是“主题 ETF 取数路径”，系统需要 ETF 的真实成分作为研究篮子。\n\n"
                "如只做标准行业研究，可改选标准行业取数路径并暂不填写 ETF；正式报价前再选择挂钩标的。",
            )
            return
        selected = self.underlying.currentData() if self.underlying.currentIndex() >= 0 else {}
        selected = selected if isinstance(selected, dict) else {}
        selected_level = str((selected.get("exposure") or {}).get("level")
                             or selected.get("exposure_level") or "")
        if value.get("research_mode") == "theme_etf" and selected_level == "partial":
            if not value["partial_exposure_confirmed"] or not value["reason"]:
                QMessageBox.information(
                    self, "请确认部分暴露 ETF",
                    "该候选属于较宽行业或相邻产业链，并非对研究主题的直接暴露。\n\n"
                    "请填写映射理由，并勾选“部分暴露确认”；也可以改选标注为“直接暴露”的 ETF。",
                )
                return
        if value.get("research_mode") == "theme_basket" and not value["theme_basket_codes"]:
            QMessageBox.information(
                self, "请选择主题公司",
                "当前选择的是“人工主题篮子路径”，至少需要勾选一只已核验主题公司。\n\n"
                "如希望自动按行业成分取数，请改选“标准行业路径”；如希望按基金真实成分取数，"
                "请改选“主题 ETF 路径”并选择 ETF。",
            )
            return
        if (value.get("research_mode") == "theme_basket" and self.theme_basket_codes
                and len(value["theme_basket_codes"]) < self.theme_basket_min):
            answer = QMessageBox.question(
                self, "主题篮子样本较少",
                f"当前只选择 {len(value['theme_basket_codes'])} 只主题公司。系统会保留研究，"
                "但不会输出行业整体估值、盈利或历史分位结论。\n\n仍要继续吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.accept()

class ResearchHelperWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Research Helper · 场外衍生品一页通")
        self.resize(1440, 900)
        self.setMinimumSize(1120, 720)
        self.process: QProcess | None = None
        self.option_process: QProcess | None = None
        self.profile_process: QProcess | None = None
        self._option_output = ""
        self._option_error_output = ""
        self._profile_output = ""
        self._profile_error_output = ""
        self.run_id = ""
        self.override_path = ""
        self.last_summary: dict = {}
        self._output_buffer = ""
        self.event_evidence_message = ""
        self.event_evidence: dict = {"事件事实": [], "产业机制": [], "A股暴露": [],
                                     "组合传导链": [], "传导关系": []}
        self._generated_override_path = ""
        self._selection_written_for_quote = ""
        self.quote_jobs: list[QuoteJob] = []
        self.active_quote_job: QuoteJob | None = None
        # 多标的报价先逐只运行 Recommender，再将全部结果汇总给分析师一次审核；
        # 正式报价仍串行消费一次性 selection，因而不会相互覆盖。
        self._recommender_batch: list[str] = []
        self._recommender_batch_comparison = False
        self._recommender_batch_results: list[dict] = []
        self._recommender_profiles: dict[str, dict] = {}
        self._quote_candidate_context: dict[str, dict] = {}
        # 同一批报价只自动提示一次；分析师可随时通过队列旁的按钮重新打开选择框。
        self._last_quote_inclusion_signature = ""
        self._quote_pdf_exporting = False
        self._run_cancelled = False
        self.quote_slow_timer = QTimer(self)
        self.quote_slow_timer.setSingleShot(True)
        self.quote_slow_timer.timeout.connect(self._quote_is_slow)
        self.quote_timeout_timer = QTimer(self)
        self.quote_timeout_timer.setSingleShot(True)
        self.quote_timeout_timer.timeout.connect(self._quote_timed_out)

        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText(
            "请完整粘贴客户原始需求，可包含研究主题、事件、期限，以及客户点名的 ETF/个股。\n"
            "例如：分析未来半年汽车电子智能化趋势，并为客户筛选可报价标的。"
        )
        self.prompt.setMinimumHeight(150)
        self.prompt.setMaximumHeight(230)
        self.prompt.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.prompt.setToolTip("支持多行输入和滚动；这里保留客户原文，不会因显示空间不足而截断。")
        self.horizon = QLineEdit("3个月")
        self.max_loss = QLineEdit("100%")
        self.principal = QComboBox()
        self.principal.addItem("接受本金波动", "yes")
        self.principal.addItem("不接受本金波动", "no")
        self.preference = QLineEdit()
        self.preference.setPlaceholderText("例如：更偏上涨参与（可选）")
        self.quote = QCheckBox("研究完成后准备正式报价审核")
        self.quote.setChecked(True)
        self.force_event = QCheckBox("按事件驱动型处理（启用事件证据硬门）")
        self.force_event.setToolTip(
            "勾选后不再依赖 LLM 判断报告类型；本次运行必须确认事件事实、产业机制、"
            "A股暴露及组合传导链，或提供可直接证明传导的原文。"
        )
        self.pdf = QCheckBox("导出 PDF 并校验一页（正式交付必需）")
        self.pdf.setChecked(True)
        self.source_label = QLabel("未添加补充材料（可选；支持 PDF 上传或粘贴文字）")
        self.source_label.setWordWrap(True)
        upload_sources = QPushButton("上传 PDF 材料")
        paste_sources = QPushButton("粘贴文字材料")
        open_sources = QPushButton("打开材料文件夹")
        self.override_label = QLabel("未导入人工数据补充（通常无需使用）")
        self.override_label.setWordWrap(True)
        choose_override = QPushButton("导入已填数据 JSON")
        edit_override = QPushButton("新建数据补充 JSON")
        self.evidence_label = QLabel()
        self.evidence_label.setWordWrap(True)
        edit_evidence = QPushButton("管理事件证据")
        clear_evidence = QPushButton("清空")
        llm_settings = QPushButton("LLM 设置")
        search_settings = QPushButton("搜索设置")
        ifind_settings = QPushButton("iFinD 凭证")
        choose_override.clicked.connect(self.choose_override)
        edit_override.clicked.connect(self.edit_override)
        upload_sources.clicked.connect(self.upload_sources)
        paste_sources.clicked.connect(self.paste_sources)
        open_sources.clicked.connect(self.open_sources_folder)
        edit_evidence.clicked.connect(self.edit_event_evidence)
        clear_evidence.clicked.connect(self.clear_event_evidence)
        self.force_event.toggled.connect(self._refresh_evidence_summary)
        llm_settings.clicked.connect(self.edit_llm_settings)
        search_settings.clicked.connect(self.edit_search_settings)
        ifind_settings.clicked.connect(self.edit_ifind_credentials)
        self.run_button = QPushButton("开始生成")
        self.run_button.setProperty("role", "primary")
        self.run_button.clicked.connect(self.start_run)
        self.stop_run_button = QPushButton("停止本次运行")
        self.stop_run_button.setProperty("role", "danger")
        self.stop_run_button.setEnabled(False)
        self.stop_run_button.clicked.connect(self.cancel_active_run)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.status = QLabel("待运行")
        self.status.setWordWrap(True)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.handoff = QPlainTextEdit()
        self.handoff.setReadOnly(True)
        self.handoff.setPlaceholderText("完成研究后，这里会汇总本次观点包、挂钩标的、正式报价与归档状态。")
        self.preview_tabs = QTabWidget()
        self.report_preview = QWebEngineView() if QWebEngineView is not None else QTextBrowser()
        self.quote_preview = QTextBrowser()
        self.quote_preview.setOpenExternalLinks(False)
        self.preview_tabs.addTab(self.report_preview, "一页通预览")
        self.preview_tabs.addTab(self.quote_preview, "报价表预览")
        self.delivery_status = QLabel("交付校验：等待运行。正式交付需导出 PDF 并实测为 1 页。")
        self.delivery_status.setWordWrap(True)
        self.artifacts = QListWidget()
        self.artifacts.setMinimumHeight(142)  # 常规 5-6 条交付物无需滚动即可辨认。
        self.artifacts.itemDoubleClicked.connect(self.open_artifact)
        self.history = QComboBox()
        self.history.currentIndexChanged.connect(self.show_selected_history)
        self.history_hint = QLabel("用于查看旧运行的内部底稿或把旧需求带回输入框；不会覆盖当前结果。")
        self.history_hint.setWordWrap(True)
        self.review = QPlainTextEdit(); self.review.setReadOnly(True)
        rerun = QPushButton("按本次需求重跑")
        rerun.clicked.connect(self.rerun_selected)
        refresh = QPushButton("刷新运行记录")
        refresh.clicked.connect(self.refresh_history)
        self.quote_review_button = QPushButton("审核产品并加入正式报价队列")
        self.quote_review_button.setProperty("role", "primary")
        self.quote_review_button.setEnabled(False)
        self.quote_review_button.clicked.connect(self.prepare_formal_quote)
        self.quote_review_hint = QLabel("请先完成研究报告；研究完成后才能确认待报价标的。")
        self.quote_review_hint.setWordWrap(True)
        self.quote_queue = QListWidget()
        self.quote_queue.setMaximumHeight(112)
        self.quote_queue.currentRowChanged.connect(self._sync_quote_queue_actions)
        self.cancel_quote_job_button = QPushButton("取消选中任务")
        self.cancel_quote_job_button.clicked.connect(self.cancel_selected_quote_job)
        self.retry_quote_job_button = QPushButton("重新审核并加入重试队列")
        self.retry_quote_job_button.clicked.connect(self.retry_selected_quote_job)
        self.include_quote_button = QPushButton("选择写入一页通的正式报价")
        self.include_quote_button.setProperty("role", "primary")
        self.include_quote_button.setEnabled(False)
        self.include_quote_button.clicked.connect(self.choose_comparison_quotes_for_report)
        self._sync_quote_queue_actions()

        # 按钮保持紧凑单行高度，窄屏时由左栏滚动承接，避免高按钮叠压相邻控件。
        for button in (upload_sources, paste_sources, open_sources, choose_override, edit_override,
                       edit_evidence, clear_evidence, llm_settings, search_settings, ifind_settings,
                       self.run_button, self.stop_run_button, self.quote_review_button, self.cancel_quote_job_button,
                       self.retry_quote_job_button, self.include_quote_button):
            button.setMinimumHeight(24)
        for button in (upload_sources, paste_sources, open_sources, choose_override, edit_override,
                       edit_evidence, llm_settings, search_settings, ifind_settings):
            button.setMinimumWidth(138)
        self.quote_review_button.setMinimumWidth(270)

        form = QFormLayout()
        form.setContentsMargins(22, 20, 22, 20)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow(_section_label("1. 需求与客户约束"))
        prompt_box = QWidget()
        prompt_layout = QVBoxLayout(prompt_box)
        prompt_layout.setContentsMargins(0, 0, 0, 0)
        prompt_layout.setSpacing(6)
        prompt_hint = QLabel("保留客户完整原文；输入框支持多行、自动换行和滚动查看。")
        prompt_hint.setProperty("kind", "caption")
        prompt_layout.addWidget(self.prompt)
        prompt_layout.addWidget(prompt_hint)
        form.addRow("客户需求", prompt_box)
        form.addRow("期限", self.horizon)
        form.addRow("最大损失", self.max_loss)
        form.addRow("本金波动", self.principal)
        form.addRow("收益偏好", self.preference)
        form.addRow("交付选项", self._row(self.quote, self.pdf))
        form.addRow(_section_label("2. 研究口径与补充材料"))
        research_target_hint = QLabel(
            "启动后按需求弹出确认卡，只确认标准行业、人工主题篮子或主题 ETF 取数路径；"
            "挂钩标的在研究完成后另行选择。")
        research_target_hint.setWordWrap(True)
        research_target_hint.setProperty("kind", "caption")
        form.addRow("研究取数目标", research_target_hint)

        material_box = QWidget()
        material_layout = QVBoxLayout(material_box)
        material_layout.setContentsMargins(0, 0, 0, 0)
        material_layout.setSpacing(7)
        material_layout.addWidget(self.source_label)
        material_layout.addWidget(self._row(upload_sources, paste_sources, open_sources))
        form.addRow("补充材料", material_box)

        evidence_box = QFrame()
        evidence_box.setObjectName("eventEvidenceBox")
        self.event_evidence_box = evidence_box
        evidence_layout = QVBoxLayout(evidence_box)
        evidence_layout.setContentsMargins(10, 9, 10, 9)
        evidence_layout.setSpacing(7)
        evidence_layout.addWidget(self.force_event)
        evidence_layout.addWidget(self.evidence_label)
        evidence_layout.addWidget(self._row(edit_evidence, clear_evidence))
        form.addRow("事件型需求", evidence_box)

        override_box = QWidget()
        override_layout = QVBoxLayout(override_box)
        override_layout.setContentsMargins(0, 0, 0, 0)
        override_layout.setSpacing(7)
        override_layout.addWidget(self.override_label)
        override_layout.addWidget(self._row(choose_override, edit_override))
        form.addRow("人工数据补充", override_box)
        form.addRow("分析模型", self._row(llm_settings))
        form.addRow("公开资料检索", self._row(search_settings))
        form.addRow("数据与报价凭证", self._row(ifind_settings))
        form.addRow(_section_label("3. 运行与交付"))
        form.addRow("", self._row(self.run_button, self.stop_run_button))

        input_card = QFrame()
        input_card.setObjectName("card")
        input_card.setLayout(form)
        quote_box = QFrame()
        quote_box.setObjectName("card")
        quote_layout = QVBoxLayout(quote_box)
        quote_layout.setContentsMargins(22, 20, 22, 20)
        quote_layout.setSpacing(10)
        quote_layout.addWidget(_section_label("正式报价（研究完成后才可用）"))
        quote_layout.addWidget(self.quote_review_hint)
        quote_layout.addWidget(self.quote_review_button)
        quote_layout.addWidget(QLabel("报价任务（仅当前会话）"))
        quote_layout.addWidget(self.quote_queue)
        quote_layout.addWidget(self._row(self.cancel_quote_job_button, self.retry_quote_job_button))
        quote_layout.addWidget(self.include_quote_button)

        run_box = QVBoxLayout()
        run_box.setContentsMargins(14, 14, 10, 14)
        run_box.setSpacing(14)
        run_box.addWidget(input_card)
        run_box.addWidget(quote_box)
        run_box.addStretch(1)
        run_widget = QWidget()
        run_widget.setLayout(run_box)
        run_widget.setMinimumWidth(600)
        run_scroll = QScrollArea()
        run_scroll.setWidgetResizable(True)
        run_scroll.setFrameShape(QFrame.Shape.NoFrame)
        run_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        run_scroll.setWidget(run_widget)

        delivery_box = QVBoxLayout()
        delivery_box.addWidget(QLabel("最终交付预览"))
        delivery_box.addWidget(self.delivery_status)
        delivery_box.addWidget(self.preview_tabs, 7)
        delivery_box.addWidget(QLabel("交付文件（双击打开）"))
        delivery_box.addWidget(self.artifacts, 2)
        delivery_box.addWidget(QLabel("运行进度"))
        delivery_box.addWidget(self.status)
        delivery_box.addWidget(self.progress)
        delivery_box.addWidget(QLabel("实时输出（本次运行的逐步日志）"))
        self.log.setMaximumHeight(128)
        delivery_box.addWidget(self.log, 1)
        delivery_tab = QWidget(); delivery_tab.setLayout(delivery_box)

        review_box = QVBoxLayout()
        review_box.addWidget(QLabel("运行摘要（内部排错用）"))
        review_box.addWidget(self.summary, 1)
        review_box.addWidget(QLabel("审核与交接（内部复核用）"))
        review_box.addWidget(self.handoff, 2)
        review_box.addWidget(QLabel("历史运行与内部底稿"))
        review_box.addWidget(self._row(self.history, refresh))
        review_box.addWidget(self.history_hint)
        review_box.addWidget(self.review, 3)
        review_box.addWidget(rerun)
        review_tab = QWidget(); review_tab.setLayout(review_box)

        result_tabs = QTabWidget()
        result_tabs.addTab(delivery_tab, "交付与报价")
        result_tabs.addTab(review_tab, "内部复核（可选）")
        result_box = QVBoxLayout()
        result_box.setContentsMargins(10, 14, 14, 14)
        result_box.addWidget(result_tabs, 1)
        result_widget = QWidget()
        result_widget.setLayout(result_box)
        splitter = QSplitter()
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(run_scroll)
        splitter.addWidget(result_widget)
        splitter.setSizes([690, 750])
        self.setCentralWidget(splitter)

        self.timer = QTimer(self)
        self.timer.setInterval(900)
        self.timer.timeout.connect(self.refresh_active_summary)
        self._refresh_evidence_summary()
        self._refresh_sources_summary()
        self.refresh_history()

    @staticmethod
    def _row(*widgets: QWidget) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        for widget in widgets:
            layout.addWidget(widget)
        layout.addStretch()
        return container

    def choose_override(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入人工数据补充 JSON", self.override_path or str(ROOT), "JSON (*.json)")
        if path:
            self.override_path = path
            self.override_label.setText(path)
            self._sync_evidence_from_override(path)

    def edit_override(self) -> None:
        dialog = JsonEditor(self, self.override_path)
        if dialog.exec() and dialog.path:
            self.override_path = str(dialog.path)
            self.override_label.setText(self.override_path)
            self._sync_evidence_from_override(self.override_path)

    def upload_sources(self) -> None:
        """将分析师选定的 PDF 复制到本项目 sources/，供下一次研究自动抽取。"""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "上传补充材料到 sources", str(ROOT), "PDF 研究材料 (*.pdf)")
        if not paths:
            return
        destination = ROOT / "sources"
        destination.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        failed: list[str] = []
        for raw in paths:
            source = Path(raw)
            target = destination / source.name
            if target.exists():
                target = destination / f"{source.stem}-{uuid.uuid4().hex[:6]}{source.suffix.lower()}"
            try:
                shutil.copy2(source, target)
                saved.append(target.name)
            except OSError as error:
                failed.append(f"{source.name}：{error}")
        if saved:
            self._refresh_sources_summary(last_uploaded=saved)
        if failed:
            QMessageBox.warning(self, "部分材料未上传", "\n".join(failed))

    def paste_sources(self) -> None:
        """保存分析师粘贴的文字材料，并交给与 PDF 相同的文档抽取链路。"""
        dialog = PastedMaterialDialog(self)
        if not dialog.exec():
            return
        destination = ROOT / "sources"
        destination.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = destination / f"补充文字-{stamp}-{uuid.uuid4().hex[:6]}.txt"
        text = (
            f"来源：{dialog.source.text().strip()}\n"
            f"录入时间：{datetime.now().astimezone().isoformat(timespec='seconds')}\n"
            "---\n"
            f"{dialog.content.toPlainText().strip()}\n"
        )
        try:
            target.write_text(text, encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, "保存补充材料失败", str(error))
            return
        self._refresh_sources_summary(last_uploaded=[f"文字材料：{dialog.source.text().strip()}"])

    def open_sources_folder(self) -> None:
        destination = ROOT / "sources"
        destination.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(destination.resolve())))

    def _refresh_sources_summary(self, *, last_uploaded: list[str] | None = None) -> None:
        root = ROOT / "sources"
        files = (sorted([*root.rglob("*.pdf"), *root.rglob("*.txt"), *root.rglob("*.md")])
                 if root.is_dir() else [])
        pdf_count = sum(path.suffix.lower() == ".pdf" for path in files)
        text_count = len(files) - pdf_count
        if last_uploaded:
            listed = "、".join(last_uploaded[:3]) + ("等" if len(last_uploaded) > 3 else "")
            text = f"刚添加 {len(last_uploaded)} 份：{listed}。下次研究会自动读取。"
        elif files:
            parts = []
            if pdf_count:
                parts.append(f"PDF {pdf_count} 份")
            if text_count:
                parts.append(f"文字材料 {text_count} 份")
            text = f"sources 文件夹已有 {'、'.join(parts)}；下次研究会自动读取。"
        else:
            text = "未添加补充材料（可选；支持 PDF 上传或粘贴文字）。"
        self.source_label.setText(text)
        self.source_label.setStyleSheet("color:#176b3a;" if files or last_uploaded else "color:#555;")

    def _sync_evidence_from_override(self, path: str) -> None:
        """兼容旧补数文件：其中已有事件证据时同步展示为表单状态。"""
        raw = _load_json(Path(path))
        if "强制事件驱动" in raw:
            self.force_event.setChecked(bool(raw.get("强制事件驱动")))
        evidence = raw.get("事件证据") if isinstance(raw, dict) else None
        if not isinstance(evidence, dict):
            return
        facts = [dict(item) for item in (evidence.get("事件事实") or []) if isinstance(item, dict)]
        mechanisms = [dict(item) for item in (evidence.get("产业机制") or []) if isinstance(item, dict)]
        exposures = [dict(item) for item in (evidence.get("A股暴露") or []) if isinstance(item, dict)]
        chains = [dict(item) for item in (evidence.get("组合传导链") or evidence.get("传导链") or []) if isinstance(item, dict)]
        links = [dict(item) for item in (evidence.get("传导关系") or []) if isinstance(item, dict)]
        self.event_evidence = {"事件事实": facts, "产业机制": mechanisms, "A股暴露": exposures,
                               "组合传导链": chains, "传导关系": links}
        self._refresh_evidence_summary()

    def _refresh_evidence_summary(self) -> None:
        facts = len(self.event_evidence.get("事件事实") or [])
        mechanisms = len(self.event_evidence.get("产业机制") or [])
        exposures = len(self.event_evidence.get("A股暴露") or [])
        chains = len(self.event_evidence.get("组合传导链") or [])
        links = len(self.event_evidence.get("传导关系") or [])
        forced = self.force_event.isChecked()
        if forced:
            self.event_evidence_box.setStyleSheet(
                "QFrame#eventEvidenceBox {background:#fff7ed; border:1px solid #d97706; "
                "border-radius:10px;}"
                "QCheckBox {color:#9a4f00; font-weight:700;}"
            )
        else:
            self.event_evidence_box.setStyleSheet(
                "QFrame#eventEvidenceBox {background:transparent; border:1px solid transparent;}"
            )
        if facts and (links or (mechanisms and exposures and chains)):
            detail = (f"直接传导 {links} 条" if links else
                      f"产业机制 {mechanisms} 条，A股暴露 {exposures} 条，组合链 {chains} 条")
            prefix = "已启用事件驱动硬门；" if forced else ""
            self.evidence_label.setText(f"{prefix}已录入：事件事实 {facts} 条，{detail}。")
            self.evidence_label.setStyleSheet("color:#176b3a;")
        else:
            missing = []
            if not facts:
                missing.append("事件事实")
            if not links and not mechanisms:
                missing.append("产业机制")
            if not links and not exposures:
                missing.append("A股暴露")
            if not links and mechanisms and exposures and not chains:
                missing.append("组合传导链")
            if forced:
                self.evidence_label.setText(
                    "已启用事件驱动型，本次运行将强制进入事件证据流程。当前待确认："
                    + "、".join(missing) + "。可以先管理证据，也可在运行中自动查找候选。"
                )
                self.evidence_label.setStyleSheet("color:#9a4f00; font-weight:600;")
            else:
                self.evidence_label.setText(
                    "仅事件型需求需要确认：" + "、".join(missing)
                    + "。可自动查找候选；普通板块/ETF研究无需处理。"
                )
                self.evidence_label.setStyleSheet("color:#8a5b14;")

    def edit_event_evidence(self) -> None:
        dialog = EventEvidenceDialog(self, self.event_evidence, topic=self.prompt.toPlainText().strip())
        if dialog.exec():
            self.event_evidence = dialog.payload()
            self._refresh_evidence_summary()

    def clear_event_evidence(self) -> None:
        self.event_evidence = {"事件事实": [], "产业机制": [], "A股暴露": [],
                               "组合传导链": [], "传导关系": []}
        self._refresh_evidence_summary()

    def _effective_override_data(self) -> dict | None:
        """合并高级补数与证据表单；报价任务在排队时快照这份受控输入。"""
        data: dict = {}
        if self.override_path:
            try:
                raw = json.loads(Path(self.override_path).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                QMessageBox.warning(self, "补数文件不可用", f"无法读取补数 JSON：{error}")
                return None
            if not isinstance(raw, dict):
                QMessageBox.warning(self, "补数文件不可用", "补数 JSON 顶层必须是对象。")
                return None
            data = raw
        facts = list(self.event_evidence.get("事件事实") or [])
        mechanisms = list(self.event_evidence.get("产业机制") or [])
        exposures = list(self.event_evidence.get("A股暴露") or [])
        chains = list(self.event_evidence.get("组合传导链") or [])
        links = list(self.event_evidence.get("传导关系") or [])
        if facts or mechanisms or exposures or chains or links:
            data["事件证据"] = {"事件事实": facts, "产业机制": mechanisms,
                                  "A股暴露": exposures, "组合传导链": chains,
                                  "传导关系": links}
        if self.force_event.isChecked():
            data["强制事件驱动"] = True
        else:
            data.pop("强制事件驱动", None)
        return data

    def _write_generated_override(self, data: dict) -> str:
        """把已快照的补数写成仅供本次子进程读取的临时文件。"""
        if not data:
            return ""
        runs_dir = RUNS
        runs_dir.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json",
                                             prefix="gui-overrides-", dir=runs_dir, delete=False)
        try:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        finally:
            handle.close()
        self._generated_override_path = handle.name
        return self._generated_override_path

    def _effective_override_path(self) -> str | None:
        data = self._effective_override_data()
        if data is None:
            return None
        return self._write_generated_override(data)

    @staticmethod
    def _confirmed_underlying(summary: dict) -> tuple[str, bool]:
        """读取主题 ETF 研究目标作为报价候选；它仍需在研究后再次确认。"""
        metadata = summary.get("metadata") or {}
        raw = metadata.get("分析师确认") or ""
        try:
            confirmation = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            confirmation = {}
        if bool(confirmation.get("research_only")):
            return "", True
        code = str(confirmation.get("underlying_code") or "").strip().upper()
        if code:
            return code, False
        return "", False

    @staticmethod
    def _quote_underlying_candidates(summary: dict, fallback: str) -> list[dict]:
        """构建默认待报价池；系统扩展候选默认隐藏，避免误当成可报价结论。"""
        return ResearchHelperWindow._quote_underlying_candidates_with_options(
            summary, fallback, include_system_discovered=False)

    @staticmethod
    def _quote_underlying_candidates_with_options(
        summary: dict, fallback: str, *, include_system_discovered: bool,
    ) -> list[dict]:
        """构建报价池，并按代码合并角色、正式名称及动态发现说明。"""
        from core.brief import _SECURITY_CODE_RE
        request = str(summary.get("request") or "")
        client_candidates: list[dict] = [
            {"code": match.group(0).upper(), "name": "", "origin": "客户点名",
             "note": "客户原始需求中明确写入的代码"}
            for match in _SECURITY_CODE_RE.finditer(request)
        ]
        metadata = summary.get("metadata") or {}
        raw = metadata.get("系统建议挂钩工具") or "[]"
        try:
            suggested = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            suggested = []
        system_candidates: list[dict] = []
        for item in suggested if isinstance(suggested, list) else []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip().upper()
            if not re.fullmatch(r"\d{6}\.(?:SH|SZ)", code):
                continue
            system_candidates.append({
                "code": code,
                "name": str(item.get("name") or "").strip(),
                "origin": "系统推荐",
                "note": str(item.get("note") or "").strip(),
            })
        system_by_code = {item["code"]: item for item in system_candidates}

        def enrich(item: dict) -> dict:
            """角色以客户/研究选择为准，名称与理由采用同代码的完整核验信息。"""
            result = dict(item)
            matched = system_by_code.get(str(result.get("code") or "").upper()) or {}
            if not str(result.get("name") or "").strip() and matched.get("name"):
                result["name"] = matched["name"]
            # 通用占位说明不应遮住动态发现阶段已经取得的具体主题/流动性依据。
            if matched.get("note"):
                result["note"] = matched["note"]
            return result

        # 客户明确点名时，只保留客户范围；同代码系统记录只用于补全名称与说明。
        if client_candidates:
            return ResearchHelperWindow._merge_quote_candidates(
                [enrich(item) for item in client_candidates])

        fallback = str(fallback or "").strip().upper()
        candidates: list[dict] = []
        if fallback:
            confirmation = metadata.get("分析师确认") or {}
            if isinstance(confirmation, str):
                try:
                    confirmation = json.loads(confirmation)
                except json.JSONDecodeError:
                    confirmation = {}
            confirmed_name = ""
            if isinstance(confirmation, dict) and str(
                    confirmation.get("underlying_code") or "").strip().upper() == fallback:
                confirmed_name = str(confirmation.get("underlying_name") or "").strip()
            candidates.append(enrich({
                "code": fallback, "name": confirmed_name, "origin": "研究取数目标",
                "note": "需求解析页用于主题 ETF 真实成分取数；报价前仍需再次确认",
            }))
            if include_system_discovered:
                candidates.extend(system_candidates)
        elif system_candidates:
            # 标准行业/人工篮子没有研究 ETF 时，默认给出排名第一的系统候选；其余
            # 只有在分析师主动选择“比较其他同主题工具”后才显示。
            candidates.extend(system_candidates if include_system_discovered else system_candidates[:1])
        return ResearchHelperWindow._merge_quote_candidates(candidates)

    @staticmethod
    def _merge_quote_candidates(candidates: list[dict]) -> list[dict]:
        """稳定去重并保留同代码记录中更完整的名称、说明和高优先级角色。"""
        merged: dict[str, dict] = {}
        order: list[str] = []
        origin_priority = {"客户点名": 3, "研究取数目标": 2, "系统推荐": 1}
        for raw in candidates:
            code = str(raw.get("code") or "").strip().upper()
            if not re.fullmatch(r"\d{6}\.(?:SH|SZ)", code):
                continue
            item = dict(raw)
            item["code"] = code
            if code not in merged:
                merged[code] = item
                order.append(code)
                continue
            current = merged[code]
            if not str(current.get("name") or "").strip() and str(item.get("name") or "").strip():
                current["name"] = str(item.get("name") or "").strip()
            current_origin = str(current.get("origin") or "")
            item_origin = str(item.get("origin") or "")
            if origin_priority.get(item_origin, 0) > origin_priority.get(current_origin, 0):
                current["origin"] = item_origin
            # 具体检索说明优先于通用占位说明，但同一句不会被重复拼接。
            current_note = str(current.get("note") or "").strip()
            item_note = str(item.get("note") or "").strip()
            if item_note and (not current_note or current_note.startswith("需求解析页用于")):
                current["note"] = item_note
            for key, value in item.items():
                if key not in current or current[key] in (None, "", [], {}):
                    current[key] = value
        return [merged[code] for code in order]

    def _start_recommender_batch(self, *, summary: dict, request: str,
                                 underlyings: list[str], candidates: list[dict] | None = None) -> None:
        self._recommender_batch = list(underlyings)
        self._recommender_batch_comparison = len(underlyings) > 1
        self._recommender_batch_results = []
        self._recommender_profiles = {}
        if candidates is not None:
            self._quote_candidate_context = {
                str(item.get("code") or "").strip().upper(): dict(item)
                for item in candidates if isinstance(item, dict) and item.get("code")
            }
        self._start_next_recommender(summary=summary, request=request)

    def _start_next_recommender(self, *, summary: dict, request: str) -> None:
        if self.process is not None or self.option_process is not None or self.profile_process is not None:
            return
        if not self._recommender_batch:
            if self._recommender_batch_comparison:
                self._review_recommender_batch(summary=summary, request=request)
            self._recommender_batch_comparison = False
            return
        underlying = self._recommender_batch.pop(0)
        self._start_product_profile(
            summary=summary, request=request, underlying=underlying,
            comparison_mode=self._recommender_batch_comparison,
        )

    @staticmethod
    def _has_research_artifact(summary: dict) -> bool:
        artifacts = summary.get("artifacts") or {}
        return any("研究报告" in str(name) and str(path) for name, path in artifacts.items())

    def _sync_quote_review(self, summary: dict) -> None:
        research_ready = self.process is None and self._has_research_artifact(summary)
        underlying, research_only = self._confirmed_underlying(summary)
        candidates = self._quote_underlying_candidates(summary, underlying)
        ready = research_ready and bool(candidates) and not research_only
        self.quote_review_button.setEnabled(ready)
        if research_only:
            self.quote_review_hint.setText("本次分析师选择“仅研究”，不能发起正式报价。")
        elif ready:
            codes = [str(item["code"]) for item in candidates]
            candidate_provided = any(
                item.get("origin") in {"系统推荐", "研究取数目标"}
                for item in candidates
            )
            if len(candidates) > 1:
                self.quote_review_hint.setText(
                    f"{'系统/研究路径提供' if candidate_provided else '客户点名'} {len(candidates)} 只待报价标的：{'、'.join(codes)}。"
                    "点击后勾选送入 OptionHelper 的候选，系统将逐只报价。")
            elif candidate_provided:
                self.quote_review_hint.setText(
                    f"待确认报价候选：{codes[0]}。它尚不是已确认挂钩标的；"
                    "点击后确认是否送入 OptionHelper 产品审核与正式报价。")
            else:
                self.quote_review_hint.setText(
                    f"客户点名待报价标的：{codes[0]}。点击后确认是否送入产品审核，"
                    "再用一次性 selection 发起正式报价。")
        elif research_ready:
            self.quote_review_hint.setText(
                "本次已完成行业研究，但系统未找到可供确认的 ETF 候选，因此未发起产品报价。"
                "可重新运行并检查 iFinD 凭证，或在客户需求中明确 ETF/个股代码。")
        else:
            self.quote_review_hint.setText("请先完成研究报告；研究完成后才能确认待报价标的。")

    def _selection_pending_path(self) -> Path:
        # 与桥接层使用同一配置，避免 GUI 写到默认位置而自定义项目路径从另一处读取。
        from core import config
        configured = Path(config.OPTIONHELPER_SELECTION_PATH)
        return configured if configured.is_absolute() else ROOT / configured

    def _refresh_quote_queue(self) -> None:
        selected = self._selected_quote_job_id()
        self.quote_queue.clear()
        for job in self.quote_jobs:
            display = (f"{job.job_id}｜{job.status}｜{job.underlying}"
                       + (f"｜{job.message}" if job.message else ""))
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, job.job_id)
            self.quote_queue.addItem(item)
            if job.job_id == selected:
                self.quote_queue.setCurrentItem(item)
        self._sync_quote_queue_actions()
        self._sync_quote_inclusion_action()

    def _completed_comparison_entries(self) -> list[dict]:
        entries = (self.last_summary.get("metadata") or {}).get("多标的报价比较") or []
        return [dict(item) for item in entries if isinstance(item, dict)
                and item.get("status") == "completed" and item.get("groups")]

    def _comparison_inclusion_signature(self, entries: list[dict] | None = None) -> str:
        entries = entries if entries is not None else self._completed_comparison_entries()
        return "|".join(sorted(str(item.get("job_id") or "") for item in entries))

    def _sync_quote_inclusion_action(self) -> None:
        if not hasattr(self, "include_quote_button"):
            return
        entries = self._completed_comparison_entries()
        self.include_quote_button.setEnabled(bool(entries) and self.active_quote_job is None)
        if entries:
            self.include_quote_button.setText(f"选择写入一页通的正式报价（{len(entries)} 份可选）")
        else:
            self.include_quote_button.setText("选择写入一页通的正式报价")

    def _maybe_offer_comparison_quote_inclusion(self) -> None:
        """队列排空时提示一次，不让多标的报价永远停留在内部比较页。"""
        if self.active_quote_job is not None or any(job.status in {"queued", "running"} for job in self.quote_jobs):
            return
        entries = self._completed_comparison_entries()
        signature = self._comparison_inclusion_signature(entries)
        if not signature:
            return
        metadata = self.last_summary.setdefault("metadata", {})
        if signature == self._last_quote_inclusion_signature or signature == str(
                metadata.get("多标的报价最后审核版本") or ""):
            return
        self._last_quote_inclusion_signature = signature
        QTimer.singleShot(0, self.choose_comparison_quotes_for_report)

    def choose_comparison_quotes_for_report(self) -> None:
        if self.active_quote_job is not None:
            QMessageBox.information(self, "报价仍在进行", "请等待所有正式报价完成后，再选择写入一页通的项目。")
            return
        entries = self._completed_comparison_entries()
        if not entries:
            QMessageBox.information(self, "暂无可写入报价", "本次尚无已完成且包含表格的多标的正式报价。")
            return
        dialog = BatchQuoteInclusionDialog(self, entries=entries)
        if not dialog.exec():
            return
        self._write_comparison_quotes_to_report(dialog.selected_entries())

    def _write_comparison_quotes_to_report(self, entries: list[dict]) -> None:
        """用分析师勾选的冻结报价替换一页通中的报价区，并重建交付物。"""
        from core import optionhelper_bridge as ohb
        from render import layout as report_layout

        report_file = Path(self._report_artifact(self.last_summary))
        if not report_file.is_file():
            QMessageBox.warning(self, "无法更新一页通", "找不到本次研究报告 HTML，无法写入所选正式报价。")
            return
        block = report_layout.multi_quote_block(entries)
        if not block:
            QMessageBox.warning(self, "报价表不完整", "所选正式报价没有可写入一页通的冻结表格。")
            return
        try:
            html = report_file.read_text(encoding="utf-8")
            html = re.sub(r'<section class="recommendation">.*?</section>\s*', "", html, flags=re.DOTALL)
            html = re.sub(r'<section class="quote">.*?</section>\s*', "", html, flags=re.DOTALL)
            footer_at = html.rfind('<div class="foot">')
            if footer_at >= 0:
                html = html[:footer_at] + block + html[footer_at:]
            else:
                html = html.replace("</div></body>", block + "</div></body>")
            report_file.write_text(html, encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, "无法更新一页通", str(error))
            return

        metadata = self.last_summary.setdefault("metadata", {})
        metadata["一页通纳入正式报价"] = [
            {key: entry.get(key) for key in ("job_id", "underlying", "product_id", "product_name", "quote_date")}
            for entry in entries
        ]
        metadata["多标的报价最后审核版本"] = self._comparison_inclusion_signature()
        metadata["正式报价状态"] = f"completed｜已由分析师选择 {len(entries)} 份正式报价写入一页通"
        self._persist_quote_delivery()

        first = entries[0]
        groups: list[ohb.QuoteGroup] = []
        for raw_group in first.get("groups") or []:
            if not isinstance(raw_group, dict):
                continue
            columns = [ohb.QuoteColumn(str(column.get("key") or ""), str(column.get("label") or ""))
                       for column in (raw_group.get("columns") or []) if isinstance(column, dict)]
            rows = [dict(row) for row in (raw_group.get("rows") or []) if isinstance(row, dict)]
            if columns and rows:
                groups.append(ohb.QuoteGroup(str(raw_group.get("title") or "参考报价"), columns, rows))
        result = ohb.OptionHelperResult(
            ok=True, product_id=str(first.get("product_id") or ""),
            product_name=str(first.get("product_name") or ""), reason=str(first.get("reason") or ""),
            main_risks=list(first.get("risks") or []), quote_groups=groups,
            quote_date=str(first.get("quote_date") or ""), quote_note=str(first.get("quote_note") or ""),
            report_path=str(first.get("report_path") or ""),
        )
        delivery_job = QuoteJob(
            job_id=f"quote-delivery-{uuid.uuid4().hex[:6]}", request="", underlying="多标的已选报价",
            market_prompt="", selection_payload={"constraints": {}, "selection": {}}, overrides={},
            export_pdf=self.pdf.isChecked(), comparison_mode=True,
            inclusion_entries=[dict(entry) for entry in entries],
            status="completed", message=f"已选 {len(entries)} 份正式报价，正在更新一页通与 PDF",
        )
        self._rebuild_quote_delivery(delivery_job, result, report_file)

    def _sync_quote_runtime_to_summary(self, job: QuoteJob) -> None:
        """把报价子任务状态写回研究摘要，避免队列、进度和预览各说各话。"""
        if not self.last_summary:
            return
        metadata = self.last_summary.setdefault("metadata", {})
        metadata["正式报价状态"] = f"{job.status}｜{job.job_id}｜{job.message}"
        stages = self.last_summary.setdefault("stages", [])
        stage = next((item for item in stages if item.get("key") == "optionhelper_quote_gui"), None)
        if stage is None:
            stage = {"key": "optionhelper_quote_gui", "label": "正式报价（GUI 后台任务）"}
            stages.append(stage)
        stage.update({
            "status": job.status,
            "detail": job.message,
            "error": job.message if job.status in {"failed", "blocked", "cancelled"} else "",
        })
        self._persist_quote_delivery()
        # 顶部摘要和报价预览必须与队列在同一次 UI 刷新中更新，不能等下一轮
        # 研究日志轮询才让用户看到“报价进行中”。
        self.show_summary(self.last_summary)

    def _selected_quote_job_id(self) -> str:
        item = self.quote_queue.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else ""

    def _selected_quote_job(self) -> QuoteJob | None:
        job_id = self._selected_quote_job_id()
        return next((job for job in self.quote_jobs if job.job_id == job_id), None)

    def _sync_quote_queue_actions(self, *_args) -> None:
        job = self._selected_quote_job()
        self.cancel_quote_job_button.setEnabled(bool(job and job.status in {"queued", "running"}))
        self.retry_quote_job_button.setEnabled(bool(job and job.status in {"failed", "blocked"}))

    def cancel_selected_quote_job(self) -> None:
        job = self._selected_quote_job()
        if job is None or job.status not in {"queued", "running"}:
            return
        if job.status == "running":
            job.forced_stop_reason = "分析师取消正在进行的正式报价；未形成正式报价。"
            self._stop_quote_timers()
            if self.option_process is not None:
                self.option_process.kill()
            else:
                self._finish_direct_quote(job)
            self.status.setText(f"{job.job_id}：正在取消正式报价…")
            return
        job.status, job.message = "cancelled", "分析师取消；未写入 selection"
        self._refresh_quote_queue()

    @staticmethod
    def _optionhelper_handoff(summary: dict) -> dict:
        path = next((Path(str(value)) for name, value in (summary.get("artifacts") or {}).items()
                     if "OptionHelper 观点包" in str(name)), None)
        return _load_json(path) if path else {}

    def _market_prompt_for_underlying(self, summary: dict, underlying: str,
                                      product_profile: dict | None = None) -> str:
        """把共同研究观点与本轮已确认挂钩标的组装，并清除旧运行的预选标的。"""
        handoff = self._optionhelper_handoff(summary)
        prompt = str(handoff.get("market_prompt") or "").strip()
        if not prompt:
            return ""
        # 兼容修复前的交接包：旧版会把自动 ETF 候选写成“拟挂钩标的”。
        prompt = "\n".join(
            line for line in prompt.splitlines()
            if not line.startswith("拟挂钩标的是") and not line.startswith("标的选择原因：")
        ).strip()
        code = str(underlying or "").strip().upper()
        context = self._quote_candidate_context.get(code) or {}
        name = str(context.get("name") or (product_profile or {}).get("name") or "").strip()
        note = str(context.get("note") or "").strip()
        label = f"{name}（{code}）" if name else code
        prompt += f"\n【本轮已确认挂钩标的】{label}。仅为该标的形成产品候选与报价。"
        if note:
            prompt += "\n【候选来源与关联依据】" + note
        from core.product_profile import render_for_prompt
        rendered = render_for_prompt(product_profile)
        if rendered:
            prompt += "\n" + rendered
        return prompt

    def _review_and_enqueue_quote(self, *, request: str, underlying: str, source_summary: dict,
                                  recommended_candidates: list[dict], source_run_id: str = "",
                                  comparison_mode: bool = False,
                                  confirmed_selection: dict | None = None,
                                  defer_pump: bool = False) -> bool:
        if not self.horizon.text().strip() or not self.max_loss.text().strip():
            QMessageBox.information(self, "缺少客户条件", "请填写期限和最大损失；或保留默认值。")
            return False
        constraint_summary = (
            f"期限 {self.horizon.text().strip() or '—'}；最大损失 {self.max_loss.text().strip() or '—'}；"
            f"{'接受' if self.principal.currentData() == 'yes' else '不接受'}本金波动"
            + (f"；收益偏好：{self.preference.text().strip()}" if self.preference.text().strip() else "")
        )
        if confirmed_selection is None:
            dialog = RecommendationReviewDialog(
                self, underlying=underlying, candidates=recommended_candidates,
                client_summary=constraint_summary,
                product_profile=self._recommender_profiles.get(underlying.upper()),
            )
            if not dialog.exec():
                return False
            confirmed_selection = dialog.selection()
        overrides = self._effective_override_data()
        if overrides is None:
            return False
        market_prompt = self._market_prompt_for_underlying(
            source_summary, underlying, self._recommender_profiles.get(underlying.upper()))
        if not market_prompt:
            QMessageBox.warning(self, "缺少本次观点包", "该研究运行未保存可复用的 OptionHelper 观点包；请重新生成研究报告后再报价。")
            return False
        # 即使只是排队，也不允许在未知 pending 上叠加；真正的 selection 只会在轮到任务时写入。
        if self._selection_pending_path().exists():
            QMessageBox.warning(
                self, "存在待报价选择",
                "发现尚未消费的 pending selection。为避免覆盖未知选择，本次任务未入队；"
                "请先完成或由原发起方清除该任务后重新审核。")
            return False
        payload = {
            "selection": confirmed_selection,
            "constraints": {
                "horizon": self.horizon.text().strip(),
                "max_loss": self.max_loss.text().strip(),
                "principal_fluctuation": self.principal.currentData() == "yes",
                **({"return_preference": self.preference.text().strip()}
                   if self.preference.text().strip() else {}),
            },
            "review": {
                "source_research_run": source_run_id or source_summary.get("run_id", ""),
                "source_research_artifacts": source_summary.get("artifacts", {}),
                "reviewed_in_gui": True,
                "queue_review": True,
                "product_profile": self._recommender_profiles.get(underlying.upper(), {}),
            },
        }
        job = QuoteJob(
            job_id="quote-" + uuid.uuid4().hex[:6], request=request, underlying=underlying,
            market_prompt=market_prompt,
            selection_payload=payload,
            overrides=json.loads(json.dumps(overrides, ensure_ascii=False)),
            export_pdf=self.pdf.isChecked(),
            underlying_name=str((self._quote_candidate_context.get(underlying.upper()) or {}).get("name") or ""),
            underlying_note=str((self._quote_candidate_context.get(underlying.upper()) or {}).get("note") or ""),
            source_run_id=source_run_id or str(source_summary.get("run_id") or ""),
            comparison_mode=comparison_mode,
        )
        self.quote_jobs.append(job)
        self._refresh_quote_queue()
        self.status.setText(f"正式报价任务 {job.job_id} 已入队；轮到时才会写入一次性 selection。")
        if not defer_pump:
            self._pump_quote_queue()
        return True

    def _review_recommender_batch(self, *, summary: dict, request: str) -> None:
        """在全部标的完成推荐后集中审核，避免连续弹出产品候选框。"""
        results = list(self._recommender_batch_results)
        self._recommender_batch_results = []
        if not results:
            return
        successful = [item for item in results if item.get("candidates")]
        failed = [item for item in results if not item.get("candidates")]
        if not successful:
            self.status.setText("所有待报价标的均未形成 OptionHelper 结构候选。")
        else:
            self.status.setText(
                f"多标的结构推荐完成：{len(successful)} 只形成候选，{len(failed)} 只无候选；等待统一审核。")
        constraint_summary = (
            f"期限 {self.horizon.text().strip() or '—'}；最大损失 {self.max_loss.text().strip() or '—'}；"
            f"{'接受' if self.principal.currentData() == 'yes' else '不接受'}本金波动"
            + (f"；收益偏好：{self.preference.text().strip()}" if self.preference.text().strip() else "")
        )
        dialog = BatchRecommendationReviewDialog(self, results=results, client_summary=constraint_summary)
        if not dialog.exec():
            return
        if self._selection_pending_path().exists():
            QMessageBox.warning(
                self, "存在待报价选择",
                "发现尚未消费的 pending selection。为避免覆盖未知选择，本轮多标的任务未入队；"
                "请先完成或由原发起方清除该任务后重新审核。")
            return
        enqueued = 0
        for underlying, selection in dialog.selections():
            if self._review_and_enqueue_quote(
                request=request, underlying=underlying, source_summary=summary,
                recommended_candidates=[], source_run_id=str(summary.get("run_id") or ""),
                comparison_mode=True, confirmed_selection=selection, defer_pump=True,
            ):
                enqueued += 1
        if enqueued:
            self.status.setText(f"已将 {enqueued} 只标的的正式报价任务加入队列。")
            self._pump_quote_queue()

    def prepare_formal_quote(self) -> None:
        if self.process is not None or self.option_process is not None or self.profile_process is not None:
            return
        summary = self.last_summary
        if not self._has_research_artifact(summary):
            QMessageBox.information(self, "无法报价", "请先完成研究报告。")
            return
        request = str(summary.get("request") or "").strip()
        underlying, research_only = self._confirmed_underlying(summary)
        candidates = self._quote_underlying_candidates(summary, underlying)
        expanded_candidates = self._quote_underlying_candidates_with_options(
            summary, underlying, include_system_discovered=True)
        primary_codes = {str(item.get("code") or "") for item in candidates}
        alternatives = [item for item in expanded_candidates
                        if str(item.get("code") or "") not in primary_codes]
        if not request or research_only or not candidates:
            QMessageBox.information(self, "无法报价", "本次运行没有客户点名或系统发现的可报价标的代码。")
            return
        # 不论候选来自客户、主题 ETF 研究目标还是系统发现，也不论只有一只还是
        # 多只，都必须在研究完成后统一展示并由分析师再次确认。研究取数选择绝不
        # 自动等同于正式挂钩标的选择。
        dialog = QuoteUnderlyingPoolDialog(
            self, candidates=candidates, alternatives=alternatives)
        if not dialog.exec():
            return
        selected_codes = dialog.selected_codes()
        metadata = summary.setdefault("metadata", {})
        metadata["分析师确认待报价池"] = "、".join(selected_codes)
        self._persist_quote_delivery()
        selected_set = set(selected_codes)
        self._start_recommender_batch(
            summary=summary, request=request, underlyings=selected_codes,
            candidates=[item for item in expanded_candidates
                        if str(item.get("code") or "") in selected_set],
        )

    def retry_selected_quote_job(self) -> None:
        job = self._selected_quote_job()
        if job is None or job.status not in {"failed", "blocked"}:
            return
        # 重试绝不复用旧 selection；重新运行 OptionHelper Recommender，再由分析师确认候选。
        self._start_product_profile(
            summary=self.last_summary, request=job.request, underlying=job.underlying,
            comparison_mode=job.comparison_mode,
        )

    def _start_product_profile(
        self, *, summary: dict, request: str, underlying: str, supplement: str = "",
        comparison_mode: bool = False,
    ) -> None:
        """先取得标的产品画像，再把事实交给 Recommender；始终不取代正式定价。"""
        if self.process is not None or self.option_process is not None or self.profile_process is not None:
            return
        self._profile_output = ""
        self._profile_error_output = ""
        process = QProcess(self)
        process.setWorkingDirectory(str(ROOT))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.readyReadStandardOutput.connect(self._read_profile_output)
        process.readyReadStandardError.connect(self._read_profile_error)
        process.finished.connect(
            lambda exit_code, _status: self._finish_product_profile(
                summary, request, underlying, exit_code, supplement=supplement,
                comparison_mode=comparison_mode)
        )
        process.errorOccurred.connect(lambda _error: self.status.setText("标的产品画像进程无法启动"))
        self.profile_process = process
        prefix = f"{underlying}：" if comparison_mode else ""
        self.status.setText(prefix + "Research Helper 正在生成标的产品画像（收益、波动、回撤、情景和流动性）…")
        process.start(sys.executable, [str(ROOT / "core" / "product_profile_worker.py")])
        process.write(json.dumps({
            "underlying": underlying,
            "name": self._quote_underlying_name(summary, underlying),
        }, ensure_ascii=False).encode("utf-8"))
        process.closeWriteChannel()

    def _quote_underlying_name(self, summary: dict, underlying: str) -> str:
        """尽量沿用本次已核验的候选名称，不因动态标的不在人工池中而显示为空。"""
        code = str(underlying or "").strip().upper()
        context = self._quote_candidate_context.get(code) or {}
        if context.get("name"):
            return str(context.get("name") or "").strip()
        for item in ResearchHelperWindow._quote_underlying_candidates_with_options(
                summary, "", include_system_discovered=True):
            if str(item.get("code") or "").strip().upper() == code:
                return str(item.get("name") or "").strip()
        confirmation = (summary.get("metadata") or {}).get("分析师确认") or {}
        if isinstance(confirmation, str):
            try:
                confirmation = json.loads(confirmation)
            except json.JSONDecodeError:
                confirmation = {}
        if isinstance(confirmation, dict) and str(confirmation.get("underlying_code") or "").upper() == code:
            return str(confirmation.get("underlying_name") or "").strip()
        return ""

    def _read_profile_output(self) -> None:
        if self.profile_process:
            self._profile_output += bytes(self.profile_process.readAllStandardOutput()).decode("utf-8", errors="replace")

    def _read_profile_error(self) -> None:
        if self.profile_process:
            self._profile_error_output += bytes(self.profile_process.readAllStandardError()).decode("utf-8", errors="replace")

    def _finish_product_profile(self, summary: dict, request: str, underlying: str, exit_code: int,
                                *, supplement: str = "", comparison_mode: bool = False) -> None:
        from core.product_profile_worker import extract_result

        self._read_profile_output()
        self._read_profile_error()
        self.profile_process = None
        payload = extract_result(self._profile_output)
        profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {
            "code": underlying.upper(), "source": "iFinD", "ok": False,
            "gaps": [str(payload.get("message") or "标的产品画像进程未返回可解析结果")],
        }
        self._recommender_profiles[underlying.upper()] = profile
        self._record_product_profile(summary, profile, exit_code=exit_code,
                                     stdout=self._profile_output, stderr=self._profile_error_output,
                                     underlying=underlying, comparison_mode=comparison_mode)
        self._start_optionhelper_recommender(
            summary=summary, request=request, underlying=underlying, supplement=supplement,
            comparison_mode=comparison_mode, product_profile=profile,
        )

    def _record_product_profile(self, summary: dict, profile: dict, *, exit_code: int,
                                stdout: str, stderr: str,
                                underlying: str, comparison_mode: bool) -> None:
        run_id = str(summary.get("run_id") or "").strip()
        if not run_id:
            return
        suffix = "-" + re.sub(r"[^A-Za-z0-9]+", "-", underlying).strip("-")
        path = RUNS / f"{run_id}.product-profile{suffix}.json"
        record = {"run_id": run_id, "worker_exit_code": exit_code, "product_profile": profile,
                  "stdout_tail": stdout[-2000:], "stderr": stderr[-2000:]}
        try:
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            summary.setdefault("artifacts", {})[f"标的产品画像（{underlying.upper()}）"] = str(path)
            summary.setdefault("metadata", {}).setdefault("标的产品画像", {})[underlying.upper()] = profile
            (RUNS / f"{run_id}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _start_optionhelper_recommender(
        self, *, summary: dict, request: str, underlying: str, supplement: str = "",
        comparison_mode: bool = False, product_profile: dict | None = None,
    ) -> None:
        if self.process is not None or self.option_process is not None or self.profile_process is not None:
            return
        handoff = self._optionhelper_handoff(summary)
        market_prompt = self._market_prompt_for_underlying(summary, underlying, product_profile)
        if not market_prompt:
            QMessageBox.warning(self, "缺少本次观点包", "该运行未保存可复用观点包；请重新生成研究报告后再报价。")
            return
        if supplement.strip():
            # 只追加分析师刚回答的客户约束，不改写已冻结的研究市场观点。
            market_prompt += "\n客户补充条件：" + supplement.strip()
        from core import config
        if not config.has_optionhelper():
            QMessageBox.warning(self, "OptionHelper 未就绪", "请先在本机完成 OptionHelper 解释器与 Skill 配置。")
            return
        constraints = {
            "underlying": underlying, "horizon": self.horizon.text().strip(),
            "market_view": str(handoff.get("market_view") or ""),
            "max_loss": self.max_loss.text().strip(),
            "principal_fluctuation": self.principal.currentData() == "yes",
            "output_type": "quote", "format": "html",
        }
        if self.preference.text().strip():
            constraints["return_preference"] = self.preference.text().strip()
        payload = {"skill_root": config.OPTIONHELPER_SKILL_ROOT, "project_root": str(ROOT),
                   "prompt": market_prompt, "constraints": constraints}
        self._option_output = ""
        self._option_error_output = ""
        process = QProcess(self)
        process.setWorkingDirectory(str(ROOT))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.readyReadStandardOutput.connect(self._read_option_output)
        process.readyReadStandardError.connect(self._read_option_error)
        process.finished.connect(
            lambda exit_code, _status: self._finish_recommender(
                summary, request, underlying, exit_code, comparison_mode=comparison_mode,
                product_profile=product_profile or self._recommender_profiles.get(underlying.upper()),
                market_prompt=market_prompt, constraints=dict(constraints))
        )
        process.errorOccurred.connect(lambda _error: self.status.setText("OptionHelper 推荐进程无法启动"))
        self.option_process = process
        prefix = f"{underlying}：" if comparison_mode else ""
        self.status.setText(prefix + "OptionHelper 正在基于本次观点包筛选结构候选…")
        process.start(config.OPTIONHELPER_PYTHON, [str(ROOT / "core" / "optionhelper_recommender_worker.py")])
        process.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        process.closeWriteChannel()

    def _read_option_output(self) -> None:
        if self.option_process:
            self._option_output += bytes(self.option_process.readAllStandardOutput()).decode("utf-8", errors="replace")

    def _read_option_error(self) -> None:
        if self.option_process:
            self._option_error_output += bytes(self.option_process.readAllStandardError()).decode("utf-8", errors="replace")

    def _record_recommender_result(
        self, summary: dict, payload: dict, stderr: str, *, exit_code: int, candidates: list,
        underlying: str = "", comparison_mode: bool = False, product_profile: dict | None = None,
        market_prompt: str = "", constraints: dict | None = None,
    ) -> None:
        """把审核前的 OptionHelper 调用写入本次 run，避免它成为不可追踪黑箱。"""
        run_id = str(summary.get("run_id") or "").strip()
        if not run_id:
            return
        result = payload.get("result") if payload.get("ok") is True else {}
        detail = result if isinstance(result, dict) else {}
        message = str(detail.get("message") or payload.get("message") or "")
        status = str(detail.get("status") or ("completed" if candidates else "failed"))
        context = self._quote_candidate_context.get(underlying.upper()) or {}
        record = {
            "run_id": run_id,
            "worker_exit_code": exit_code,
            "outer_ok": payload.get("ok") is True,
            "status": status,
            "message": message,
            "underlying": underlying.upper(),
            "underlying_name": str(
                context.get("name") or (product_profile or {}).get("name") or ""
            ).strip(),
            "market_prompt": market_prompt,
            "constraints": dict(constraints or {}),
            "candidate_count": len(candidates),
            "product_profile": product_profile or {},
            "candidates": [
                {
                    "product_id": str(item.get("product_id") or ""),
                    "product_name": str(item.get("product_name") or ""),
                    "reason": str(item.get("reason") or ""),
                }
                for item in candidates if isinstance(item, dict)
            ],
            "stderr": stderr[-2000:],
        }
        suffix = ("-" + re.sub(r"[^A-Za-z0-9]+", "-", underlying).strip("-")
                  if comparison_mode and underlying else "")
        path = RUNS / f"{run_id}.optionhelper-recommender{suffix}.json"
        try:
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            artifacts = summary.setdefault("artifacts", {})
            artifact_name = f"OptionHelper 推荐诊断（{underlying}）" if suffix else "OptionHelper 推荐诊断"
            artifacts[artifact_name] = str(path)
            stages = summary.setdefault("stages", [])
            stage = next((item for item in stages if item.get("key") == "optionhelper_recommender"), None)
            if stage is None:
                stage = {"key": "optionhelper_recommender", "label": "生成 OptionHelper 结构推荐"}
                stages.append(stage)
            stage.update({
                "status": "completed" if candidates else "failed",
                "error": "" if candidates else message,
                "detail": f"候选 {len(candidates)} 个；worker exit={exit_code}",
            })
            (RUNS / f"{run_id}.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            # 研究报告已先落盘，结构推荐随后在 GUI 异步进程完成；同步替换底稿中的
            # “尚未调用”，并说明正式报价为什么尚未发起。
            gap_path = self._gap_artifact(summary)
            if gap_path:
                from render import gaps
                gaps.refresh_optionhelper_recommender_result(gap_path, record)
        except OSError:
            # 日志辅助功能不应妨碍分析师在 UI 中获得本次推荐结果。
            pass

    def _finish_recommender(self, summary: dict, request: str, underlying: str, exit_code: int,
                            *, comparison_mode: bool = False, product_profile: dict | None = None,
                            market_prompt: str = "", constraints: dict | None = None) -> None:
        self._read_option_output()
        self._read_option_error()
        self.option_process = None
        try:
            # Worker 只应输出一行 JSON，但 Skill 的第三方步骤可能把进度写到
            # stdout；取最后一个 JSON 行，避免因此把真实错误吞成笼统提示。
            raw = self._option_output.strip()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = json.loads(next(
                    line for line in reversed(raw.splitlines())
                    if line.lstrip().startswith("{") and line.rstrip().endswith("}")
                ))
            result = payload.get("result") if payload.get("ok") is True else {}
            candidates = ((result.get("recommendation") or {}).get("candidates") or []) if isinstance(result, dict) else []
        except (json.JSONDecodeError, StopIteration):
            payload, result, candidates = {}, {}, []
        if not isinstance(payload, dict):
            payload, result, candidates = {}, {}, []
        self._record_recommender_result(
            summary, payload, self._option_error_output, exit_code=exit_code, candidates=candidates,
            underlying=underlying, comparison_mode=comparison_mode, product_profile=product_profile,
            market_prompt=market_prompt, constraints=constraints,
        )
        detail = result if isinstance(result, dict) else {}
        message = str(
            detail.get("message")
            or payload.get("message")
            or "OptionHelper 未返回可解析的推荐结果。"
        )
        if comparison_mode:
            # 多标的场景不在每一只返回后打断分析师；完整保留成功候选和失败原因，
            # 等待最后一只处理完毕后在同一个审核窗口集中展示。
            self._recommender_batch_results.append({
                "underlying": underlying,
                "product_profile": product_profile or {},
                "candidates": [dict(item) for item in candidates if isinstance(item, dict)],
                "message": message,
                "status": str(detail.get("status") or ("completed" if candidates else "failed")),
            })
            QTimer.singleShot(0, lambda: self._start_next_recommender(summary=summary, request=request))
            return
        if not candidates:
            # `run_recommendation_request` 会以正常 JSON 表示“还需补问”。此前 GUI
            # 只读外层 worker 的 message，丢掉内层 needs_input / unavailable 原因，
            # 于是所有这类状态都被误报为“未返回可解析的推荐结果”。
            status = str(detail.get("status") or "")
            if status == "needs_input":
                title = "OptionHelper 需要补充条件"
                prefix = "请补充后重新发起："
            elif status == "unavailable":
                title = "OptionHelper 当前无法形成候选"
                prefix = "未形成可验证候选："
            else:
                title = "未形成结构推荐"
                prefix = "OptionHelper 推荐失败："
            self.status.setText(f"{prefix}{message[:160]}")
            if status == "needs_input":
                answer, accepted = QInputDialog.getText(
                    self, "补充客户条件", message + "\n\n请直接填写客户的补充条件：",
                )
                if accepted and answer.strip():
                    QTimer.singleShot(
                        0,
                        lambda: self._start_optionhelper_recommender(
                            summary=summary, request=request, underlying=underlying, supplement=answer,
                            comparison_mode=comparison_mode, product_profile=product_profile,
                        ),
                    )
                else:
                    self.status.setText("OptionHelper 等待补充客户条件。")
                return
            QMessageBox.warning(self, title, message)
            if comparison_mode:
                # 单只标的无法形成结构候选不应阻塞其它客户点名标的的独立核验。
                QTimer.singleShot(0, lambda: self._start_next_recommender(summary=summary, request=request))
            return
        self.status.setText((f"{underlying}：" if comparison_mode else "")
                            + "OptionHelper 已形成结构候选，等待分析师确认。")
        enqueued = self._review_and_enqueue_quote(
            request=request, underlying=underlying, source_summary=summary,
            recommended_candidates=[dict(item) for item in candidates if isinstance(item, dict)],
            source_run_id=str(summary.get("run_id") or ""), comparison_mode=comparison_mode,
        )

    def _pump_quote_queue(self) -> None:
        """串行启动下一份报价；只消费本次内存候选，不重跑研究主流程。"""
        if (self.process is not None or self.option_process is not None or self.profile_process is not None
                or self.active_quote_job is not None):
            return
        job = next((item for item in self.quote_jobs if item.status == "queued"), None)
        if job is None:
            self._maybe_offer_comparison_quote_inclusion()
            return
        from core import config
        job.status, job.message, job.forced_stop_reason = "running", "正在使用本次已确认候选生成正式报价", ""
        self.active_quote_job = job
        self._sync_quote_runtime_to_summary(job)
        self._refresh_quote_queue()
        # 研究阶段的 100% 不能被误读成正式报价已完成；报价阶段改为不定进度。
        self.progress.setRange(0, 0)
        self.status.setText(f"{job.job_id}：正在调用 OptionHelper 正式报价，不会重跑研究…")
        payload = {
            "skill_root": config.OPTIONHELPER_SKILL_ROOT, "project_root": str(ROOT),
            # OptionHelper 的正式交付目录由请求哈希命名，且是不可覆盖归档。
            # 每一份人工确认后的报价都必须具备独立标识，否则相同研究观点的
            # 再报价会撞上既有 ReportRun，并被误认为“卡住”。该标识只用于
            # 隔离正式报价运行，不改变市场观点、客户约束或产品选择。
            "prompt": f"{job.market_prompt}\n\n[ResearchHelper 正式报价运行标识：{job.job_id}]",
            "constraints": job.selection_payload.get("constraints") or {},
            "selection": job.selection_payload.get("selection") or {},
        }
        self._option_output = ""
        self._option_error_output = ""
        process = QProcess(self)
        process.setWorkingDirectory(str(ROOT))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.readyReadStandardOutput.connect(self._read_option_output)
        process.readyReadStandardError.connect(self._read_option_error)
        process.finished.connect(lambda _code, _status: self._finish_direct_quote(job))
        process.errorOccurred.connect(lambda error: self._quote_process_error(job, error))
        self.option_process = process
        process.start(config.OPTIONHELPER_PYTHON, [str(ROOT / "core" / "optionhelper_quote_watchdog.py")])
        process.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        process.closeWriteChannel()
        # 正常正式报价在历史运行中约 5~6 秒完成。60 秒提示仍在等待，180 秒后
        # 主动停止，避免 iFinD/外部模块卡住时 UI 永远显示“报价中”。
        self.quote_slow_timer.start(60_000)
        self.quote_timeout_timer.start(180_000)

    def _stop_quote_timers(self) -> None:
        self.quote_slow_timer.stop()
        self.quote_timeout_timer.stop()

    def _quote_is_slow(self) -> None:
        job = self.active_quote_job
        if job is None or self.option_process is None:
            return
        job.message = "报价超过 60 秒，仍在等待 OptionHelper/iFinD 响应（180 秒后自动停止）"
        self._sync_quote_runtime_to_summary(job)
        self._refresh_quote_queue()
        self.status.setText(f"{job.job_id}：{job.message}")

    def _quote_timed_out(self) -> None:
        job = self.active_quote_job
        if job is None or self.option_process is None:
            return
        job.forced_stop_reason = "正式报价超过 180 秒，已自动停止；请检查 iFinD 凭证、网络或 OptionHelper 运行日志后重试。"
        self.status.setText(f"{job.job_id}：报价超时，正在停止进程…")
        self.option_process.kill()

    def _quote_process_error(self, job: QuoteJob, error) -> None:
        if job is not self.active_quote_job:
            return
        job.forced_stop_reason = f"OptionHelper 报价进程异常结束（{error}），未形成正式报价。"
        if self.option_process is not None and self.option_process.state() != QProcess.ProcessState.NotRunning:
            self.option_process.kill()
        else:
            QTimer.singleShot(0, lambda: self._finish_direct_quote(job))

    @staticmethod
    def _gap_artifact(data: dict) -> str:
        artifacts = data.get("artifacts") or {}
        exact = str(artifacts.get("内部底稿") or "")
        if exact:
            return exact
        return next((str(path) for name, path in artifacts.items() if "内部底稿" in str(name)), "")

    def _persist_quote_delivery(self) -> None:
        if not self.run_id:
            return
        try:
            (RUNS / f"{self.run_id}.json").write_text(
                json.dumps(self.last_summary, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _sync_failed_quote_to_gap(self, job: QuoteJob) -> None:
        """正式报价未形成时也写入底稿，不能只在队列里留一行失败文字。"""
        if job.comparison_mode:
            self._record_quote_comparison(job, status="failed", error=job.message)
            return
        gap_path = self._gap_artifact(self.last_summary)
        if not gap_path:
            return
        try:
            from core.optionhelper_bridge import OptionHelperResult
            from render import gaps
            result = OptionHelperResult(
                ok=False, stage="quote", error=job.message,
                client_constraints=dict(job.selection_payload.get("constraints") or {}),
                recovery_action="核对 OptionHelper/iFinD 诊断后重新发起本次报价；不要复用旧 selection。",
            )
            gaps.refresh_optionhelper_result(
                gap_path, result, input_record=self._quote_input_record(job, "正式报价失败"))
        except (OSError, TypeError):
            pass

    def _quote_input_record(self, job: QuoteJob, status: str) -> dict:
        """正式报价与结构推荐共用同一份可审计的逐标的输入记录。"""
        return {
            "underlying": job.underlying,
            "underlying_name": job.underlying_name,
            "status": status,
            "market_prompt": job.market_prompt,
            "constraints": dict(job.selection_payload.get("constraints") or {}),
            "product_profile": dict(self._recommender_profiles.get(job.underlying.upper()) or {}),
        }

    @staticmethod
    def _serialise_quote_groups(groups) -> list[dict]:
        return [
            {
                "title": str(getattr(group, "title", "") or "参考报价"),
                "columns": [
                    {"key": str(getattr(column, "key", "") or ""),
                     "label": str(getattr(column, "label", "") or "")}
                    for column in (getattr(group, "columns", []) or [])
                ],
                "rows": [dict(row) for row in (getattr(group, "rows", []) or [])],
            }
            for group in (groups or [])
        ]

    def _record_quote_comparison(self, job: QuoteJob, *, status: str, oh=None,
                                 error: str = "") -> None:
        """把多标的报价保存在运行记录，供报价预览横向复核。"""
        metadata = self.last_summary.setdefault("metadata", {})
        entries = metadata.setdefault("多标的报价比较", [])
        if not isinstance(entries, list):
            entries = []
            metadata["多标的报价比较"] = entries
        entry = {
            "job_id": job.job_id,
            "underlying": job.underlying,
            "status": status,
            "product_id": str(getattr(oh, "product_id", "") or ""),
            "product_name": str(getattr(oh, "product_name", "") or ""),
            "reason": str(getattr(oh, "reason", "") or ""),
            "risks": list(getattr(oh, "main_risks", []) or []),
            "quote_date": str(getattr(oh, "quote_date", "") or ""),
            "quote_note": str(getattr(oh, "quote_note", "") or ""),
            "groups": self._serialise_quote_groups(getattr(oh, "quote_groups", []) or []),
            "report_path": str(getattr(oh, "report_path", "") or ""),
            "error": error,
        }
        entries[:] = [item for item in entries if isinstance(item, dict)
                      and item.get("job_id") != job.job_id]
        entries.append(entry)
        if status == "completed" and entry["report_path"]:
            self.last_summary.setdefault("artifacts", {})[f"OptionHelper 正式报价（{job.underlying}）"] = entry["report_path"]

    def _complete_direct_quote(self, job: QuoteJob) -> None:
        self._quote_pdf_exporting = False
        self._sync_quote_runtime_to_summary(job)
        self.active_quote_job = None
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self._refresh_quote_queue()
        self._refresh_previews(self.last_summary)
        self._sync_delivery_status(self.last_summary)
        self.status.setText(f"{job.job_id}：{job.message}")
        QTimer.singleShot(0, self._pump_quote_queue)

    def _rebuild_quote_delivery(self, job: QuoteJob, oh, report_file: Path) -> None:
        """正式报价成功后同步最终 HTML、内部底稿与 PDF。

        研究与报价是异步两阶段，但最终交付必须是同一时点的三份文件。PDF 在 GUI
        的既有事件循环中导出，避免 ``html_to_pdf`` 另建 QApplication 导致卡死。
        """
        from render import gaps
        from render.pdf_out import page_count, print_page_async

        gap_file = Path(self._gap_artifact(self.last_summary))
        artifacts = self.last_summary.setdefault("artifacts", {})
        metadata = self.last_summary.setdefault("metadata", {})

        def finish(pdf: str = "", export_error: str = "") -> None:
            pages = None
            if pdf:
                try:
                    pages = page_count(pdf)
                except (OSError, ValueError):
                    export_error = "PDF 已导出但页数校验失败"
            if job.inclusion_entries:
                gaps.refresh_optionhelper_multi_result(
                    gap_file, job.inclusion_entries, html_path=str(report_file), pdf_path=pdf,
                    pdf_pages=pages, pdf_error=export_error,
                )
            else:
                gaps.refresh_optionhelper_result(
                    gap_file, oh, html_path=str(report_file), pdf_path=pdf,
                    pdf_pages=pages, pdf_error=export_error,
                    input_record=self._quote_input_record(job, "正式报价已完成"),
                )
            for name in [key for key in artifacts if "PDF" in str(key)]:
                artifacts.pop(name, None)
            if pages is not None:
                if pages == 1:
                    artifacts["PDF（正式交付）"] = pdf
                    metadata["一页通交付校验"] = "通过：PDF 实测 1 页"
                    job.message = "正式报价完成；HTML、内部底稿和 PDF 已同步更新并通过一页校验"
                else:
                    artifacts["PDF（超页，仅供内部复核）"] = pdf
                    metadata["一页通交付校验"] = f"不通过：PDF 实测 {pages} 页，禁止正式交付"
                    job.message = f"正式报价完成；HTML、内部底稿已更新，但 PDF 实测 {pages} 页"
            else:
                metadata["一页通交付校验"] = "未通过：正式报价后的 PDF 未成功导出，不能确认单页"
                job.message = "正式报价完成；HTML、内部底稿已更新，但最终 PDF 导出失败"
            job.status = "completed"
            self._persist_quote_delivery()
            self._complete_direct_quote(job)

        # QtWebEngine 缺失时仍交付 HTML/底稿，明确标出 PDF 未校验，不让旧 PDF 冒充最终版。
        if QWebEngineView is None or not isinstance(self.report_preview, QWebEngineView):
            finish(export_error="当前 PySide6 未安装 QtWebEngine，无法在界面内重导 PDF")
            return

        self._quote_pdf_exporting = True
        self.status.setText(f"{job.job_id}：正式报价完成，正在重建最终 PDF 并校验页数…")
        pdf_path = str(report_file.with_suffix(".pdf"))

        def after_load(ok: bool) -> None:
            try:
                self.report_preview.loadFinished.disconnect(after_load)
            except (RuntimeError, TypeError):
                pass
            if not ok:
                finish(export_error="最终 HTML 加载失败")
                return
            print_page_async(
                self.report_preview.page(), pdf_path,
                on_done=lambda path: finish(path or "", "printToPdf 返回空数据" if not path else ""),
            )

        self.report_preview.loadFinished.connect(after_load)
        self.report_preview.load(QUrl.fromLocalFile(str(report_file.resolve())))

    def _finish_direct_quote(self, job: QuoteJob) -> None:
        self._read_option_output()
        self._read_option_error()
        self._stop_quote_timers()
        self.option_process = None
        if job.forced_stop_reason:
            job.status, job.message = "failed", job.forced_stop_reason
            self._sync_failed_quote_to_gap(job)
            self._complete_direct_quote(job)
            return
        try:
            payload = json.loads(self._option_output)
            result = payload.get("result") if payload.get("ok") is True else {}
            # 旧桥接层返回 {result: {user_summary: ...}}，新版 OptionHelper
            # 直接返回项目结果（其中 report/recommendation 在顶层）。两种协议
            # 都应被读取，否则成功报价会被错误显示为“未返回正式报价”。
            summary = result.get("user_summary") if isinstance(result, dict) else {}
            if not isinstance(summary, dict) or not summary:
                summary = result if isinstance(result, dict) else {}
            report_path = str((summary.get("report") or {}).get("path") or "")
        except json.JSONDecodeError:
            payload, summary, report_path = {}, {}, ""
        if not report_path:
            job.status, job.message = "failed", str(payload.get("message") or "OptionHelper 未返回正式报价。")[:180]
        else:
            from core import optionhelper_bridge as ohb
            from render import layout as report_layout

            groups, quote_date, quote_note, error = ohb._quote_facts(report_path, ROOT)
            if not groups:
                job.status, job.message = "failed", error[:180]
            else:
                rec = summary.get("recommendation") or {}
                oh = ohb.OptionHelperResult(
                    ok=True, product_id=str(rec.get("product_id") or job.selection_payload["selection"].get("product_id") or ""),
                    product_name=str(rec.get("product_name") or ""), reason=str(rec.get("reason") or ""),
                    main_risks=list(rec.get("main_risks") or []), quote_groups=groups,
                    quote_date=quote_date, quote_note=quote_note, report_path=report_path,
                )
                if job.comparison_mode:
                    # 多标的报价是给分析师横向选择的中间交付，不能轮流覆写同一份
                    # 一页通；每份冻结报价单独保留在 artifacts，预览页集中展示。
                    job.status = "completed"
                    job.message = "正式报价完成；已加入多标的报价比较（未改写主题研究报告）"
                    self._record_quote_comparison(job, status="completed", oh=oh)
                    self._complete_direct_quote(job)
                    return
                report_file = Path(self._report_artifact(self.last_summary))
                try:
                    html = report_file.read_text(encoding="utf-8")
                    # 研究报告先交付时没有 OptionHelper 结构；报价完成后必须把
                    # “推荐结构 + 报价表”整体插入页脚之前。旧实现仅把表格追加到
                    # 文末，因而页脚跑到表格上方且结构建议缺失。
                    html = re.sub(r'<section class="recommendation">.*?</section>\s*', "", html, flags=re.DOTALL)
                    html = re.sub(r'<section class="quote">.*?</section>\s*', "", html, flags=re.DOTALL)
                    # 兼容修复前已生成的“系统候选被写成挂钩标的”卡片。
                    html = re.sub(
                        r'<div class="under">.*?(?=<section class="recommendation"|'
                        r'<section class="quote"|<div class="foot">)',
                        "", html, flags=re.DOTALL,
                    )
                    profile = self._recommender_profiles.get(job.underlying.upper()) or {}
                    append = (
                        report_layout.confirmed_underlying_block(
                            job.underlying, name=job.underlying_name,
                            reason=job.underlying_note, product_profile=profile,
                        )
                        + report_layout._recommendation_block(oh)
                        + report_layout._quote_block(oh)
                    )
                    footer_at = html.rfind('<div class="foot">')
                    if footer_at >= 0:
                        html = html[:footer_at] + append + html[footer_at:]
                    else:
                        html = html.replace("</div></body>", append + "</div></body>")
                    report_file.write_text(html, encoding="utf-8")
                    # 报价改变版面，必须重建内部底稿与 PDF；不能只用正则改 HTML 后
                    # 留下“本次未调用”的底稿和报价前 PDF。
                    self._rebuild_quote_delivery(job, oh, report_file)
                    return
                except OSError as error:
                    job.status, job.message = "failed", f"报价完成但无法更新研究报告：{error}"[:180]
        if job.status == "failed":
            self._sync_failed_quote_to_gap(job)
            self._persist_quote_delivery()
        self._complete_direct_quote(job)

    def edit_llm_settings(self) -> None:
        dialog = LlmSettingsDialog(self)
        if dialog.exec():
            QMessageBox.information(self, "LLM 设置已保存", "新设置会在下一次分析任务启动时生效。")

    def edit_search_settings(self) -> None:
        dialog = SearchSettingsDialog(self)
        if dialog.exec():
            QMessageBox.information(
                self, "搜索设置已保存",
                "Tavily/Bing 搜索设置会在下一次自动查找事件证据时生效。",
            )

    def edit_ifind_credentials(self) -> None:
        dialog = IFindCredentialsDialog(self)
        if dialog.exec():
            QMessageBox.information(
                self, "iFinD 凭证已保存",
                "研究数据账号/密码和 Refresh Token 会在下一次分析或正式报价任务中分别生效。",
            )

    def cancel_active_run(self) -> None:
        """允许分析师中断研究主进程；不把半份结果伪装成完成。"""
        if self.process is None or self.process.state() == QProcess.ProcessState.NotRunning:
            return
        self._run_cancelled = True
        self.stop_run_button.setEnabled(False)
        self.status.setText("正在停止本次研究；已生成的文件仅供内部查看，不能作为正式交付…")
        if self.run_id:
            path = RUNS / f"{self.run_id}.json"
            data = _load_json(path)
            if data:
                data["status"] = "cancelled"
                data["error"] = "分析师主动停止运行"
                try:
                    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                except OSError:
                    pass
        self.process.kill()

    def start_run(self, _checked: bool = False, *, quote_job: QuoteJob | None = None) -> None:
        if self.process is not None:
            return
        if quote_job is None and (self.option_process is not None or self.active_quote_job is not None):
            QMessageBox.information(
                self, "正式报价进行中",
                "当前正式报价尚未结束。请等待其完成、失败或取消后，再发起新的研究运行；"
                "这样不会覆盖报价任务关联的研究摘要和预览。",
            )
            return
        if quote_job is None and any(job.status == "queued" for job in self.quote_jobs):
            QMessageBox.information(
                self, "报价任务正在排队",
                "请等待队列完成，或先取消排队任务；不能在排队报价之间插入普通运行。")
            return
        request = quote_job.request if quote_job else self.prompt.toPlainText().strip()
        if not request:
            QMessageBox.information(self, "缺少需求", "请先输入客户需求。")
            return
        constraints = quote_job.selection_payload.get("constraints", {}) if quote_job else {}
        horizon = str(constraints.get("horizon") or self.horizon.text().strip())
        max_loss = str(constraints.get("max_loss") or self.max_loss.text().strip())
        principal = ("yes" if constraints.get("principal_fluctuation") is True else "no"
                     if constraints.get("principal_fluctuation") is False else str(self.principal.currentData()))
        preference = str(constraints.get("return_preference") or self.preference.text().strip())
        if not horizon or not max_loss:
            QMessageBox.information(self, "缺少客户条件", "请填写期限和最大损失；或保留默认值。")
            return
        args = [str(ROOT / "main.py"), "-b", request,
                "--confirm-market",
                "--horizon", horizon,
                "--max-loss", max_loss,
                "--principal-fluctuation", principal]
        if preference:
            args += ["--return-preference", preference]
        # 正式报价只能由已审核的后台任务启动；普通研究即使勾选“准备审核”也不能绕过
        # 产品选择、理由、适用情形和风险的人工确认。
        if quote_job is not None:
            args += ["--optionhelper", "quote"]
        else:
            # 研究阶段总是给分析师展示候选逻辑；上传到 sources/ 的材料也只会在此模式
            # 被抽取成候选，经过勾选后才可能进入正文。
            args.append("--pick")
        if (quote_job.export_pdf if quote_job is not None else self.pdf.isChecked()):
            args.append("--pdf")
        effective_override = (self._write_generated_override(quote_job.overrides)
                              if quote_job is not None else self._effective_override_path())
        if effective_override is None:
            return
        if effective_override:
            args += ["--overrides", effective_override]
        self.run_id = ""
        self._run_cancelled = False
        self.last_summary = {}
        self._output_buffer = ""
        self.event_evidence_message = ""
        self.log.clear()
        self.summary.clear()
        self.handoff.clear()
        self._clear_previews()
        self.artifacts.clear()
        self.status.setText(f"正在启动 {quote_job.job_id}…" if quote_job else "正在启动…")
        self.progress.setRange(0, 0)
        self.run_button.setEnabled(False)
        self.stop_run_button.setEnabled(True)
        process = QProcess(self)
        process.setWorkingDirectory(str(ROOT))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self.read_output)
        process.finished.connect(self.finished)
        process.errorOccurred.connect(self.process_error)
        self.process = process
        process.start(sys.executable, args)
        self.timer.start()

    def read_output(self) -> None:
        if not self.process:
            return
        text = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self.log.moveCursor(QTextCursor.MoveOperation.End)
        self.log.insertPlainText(text)
        self.log.ensureCursorVisible()
        self._output_buffer += text
        lines = self._output_buffer.split("\n")
        self._output_buffer = lines.pop()
        for line in lines:
            if "运行编号：" in line:
                self.run_id = line.split("运行编号：", 1)[1].split("（", 1)[0].strip()
            if line.startswith("MARKET_CONFIRMATION_REQUIRED="):
                try:
                    payload = json.loads(line.split("=", 1)[1])
                except json.JSONDecodeError:
                    continue
                dialog = MarketConfirmationDialog(payload, self)
                if dialog.exec():
                    # QProcess 的 stdin 在 Windows 上可能仍由子进程按本地代码页解码。
                    # 市场、行业等确认项含中文，直接写 UTF-8 会被读成乱码（如 A股 变成
                    # 非法市场值）。JSON 的 ASCII 转义在所有 Windows 代码页下语义相同。
                    confirmation = json.dumps(dialog.value(), ensure_ascii=True) + "\n"
                    self.process.write(confirmation.encode("ascii"))
                    self.status.setText("正在校验分析师确认…")
                else:
                    self.process.write(b'{"cancelled":true}\n')
                    self.status.setText("已取消市场/行业确认")
            elif line.startswith("MARKET_CONFIRMATION_REJECTED="):
                self.status.setText("确认未通过；请根据校验原因修改后重试")
            elif line.startswith("EVENT_EVIDENCE_CONTEXT_REQUIRED="):
                try:
                    payload = json.loads(line.split("=", 1)[1])
                except json.JSONDecodeError:
                    payload = {}
                evidence = payload.get("existing_evidence")
                evidence = evidence if isinstance(evidence, dict) else self.event_evidence
                context = payload.get("research_context")
                context = context if isinstance(context, dict) else {}
                dialog = EventEvidenceDialog(
                    self, evidence, topic=self.prompt.toPlainText().strip(),
                    discovery_mode="complete", research_context=context,
                )
                # 研究取数对象已经确认，自动进入第二阶段检索。
                QTimer.singleShot(0, dialog.start_discovery)
                if dialog.exec():
                    self.event_evidence = dialog.payload()
                    self._refresh_evidence_summary()
                    response = json.dumps(
                        {"event_evidence": self.event_evidence}, ensure_ascii=True) + "\n"
                    self.process.write(response.encode("ascii"))
                    self.status.setText("事件证据已确认，正在校验证据链并继续研究…")
                else:
                    self.process.write(b'{"cancelled":true}\n')
                    self.status.setText("已取消事件证据确认")
            elif line.startswith("EVENT_EVIDENCE_REQUIRED="):
                try:
                    payload = json.loads(line.split("=", 1)[1])
                except json.JSONDecodeError:
                    payload = {}
                self.event_evidence_message = str(payload.get("message") or "事件证据不足，未生成报告。")
                missing = payload.get("missing") or []
                text = (
                    self.event_evidence_message
                    + "\n\n未生成报告。请打开“管理事件证据”，先自动查找候选并确认；"
                      "也可以从已上传材料导入或手工补录，保存后重跑。"
                )
                if missing:
                    text += "\n\n缺少：\n• " + "\n• ".join(str(item) for item in missing)
                self.status.setText("未生成报告：事件证据不足")
                self.review.setPlainText(text)
                answer = QMessageBox.question(
                    self, "缺少事件传导证据，未生成报告", text + "\n\n现在打开“管理事件证据”吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if answer == QMessageBox.StandardButton.Yes:
                    self.edit_event_evidence()
            elif line.startswith("LOGIC_PICK_REQUIRED="):
                try:
                    payload = json.loads(line.split("=", 1)[1])
                except json.JSONDecodeError:
                    payload = {}
                dialog = LogicPickDialog(self, payload)
                if dialog.exec():
                    selected = dialog.selected_indices()
                    self.process.write((" ".join(str(item) for item in selected) + "\n").encode("utf-8"))
                    self.status.setText("已确认报告逻辑，正在生成研究报告…" if selected else "已交由系统自动挑选逻辑…")
                else:
                    # 终端约定：空行代表自动选择。取消也要写回，防止子进程一直等待输入。
                    self.process.write(b"\n")
                    self.status.setText("已取消人工勾选，改由系统自动挑选逻辑…")
        self.refresh_active_summary()

    def process_error(self, _error) -> None:
        if self.process:
            self.status.setText("启动失败：" + self.process.errorString())
        self.stop_run_button.setEnabled(False)

    def refresh_active_summary(self) -> None:
        if not self.run_id:
            return
        summary = _load_json(RUNS / f"{self.run_id}.json")
        if not summary:
            return
        self.show_summary(summary)
        current = next((item for item in summary.get("stages", []) if item.get("status") == "running"), None)
        self.status.setText((f"正在执行：{current.get('label')}" if current else f"状态：{summary.get('status', 'running')}"))

    def finished(self, exit_code: int, _exit_status) -> None:
        self.timer.stop()
        self.refresh_active_summary()
        completed_quote_job = self.active_quote_job
        self.run_button.setEnabled(True)
        self.stop_run_button.setEnabled(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(1 if exit_code == 0 else 0)
        if self._run_cancelled:
            self.status.setText("已停止本次运行；若有已生成文件，仅供内部复核。")
        elif self.event_evidence_message:
            self.status.setText("未生成报告：" + self.event_evidence_message)
        elif self.last_summary:
            delivery = str((self.last_summary.get("metadata") or {}).get("一页通交付校验") or "")
            if self.last_summary.get("status") == "failed":
                reason = str(self.last_summary.get("error") or
                             (self.last_summary.get("metadata") or {}).get("终止原因") or
                             "请查看实时输出。")
                self.status.setText(f"未完成：{reason}")
            elif delivery and not delivery.startswith("通过"):
                self.status.setText("研究已完成，但未通过正式交付校验：" + delivery)
            else:
                self.status.setText(f"完成：{self.last_summary.get('status')}（退出码 {exit_code}）")
        else:
            self.status.setText(f"进程结束（退出码 {exit_code}）；未找到运行摘要。")
        self.process = None
        if completed_quote_job is not None:
            completed_quote_job.run_id = str(self.last_summary.get("run_id") or self.run_id or "")
            quote_stage = self._stage(self.last_summary, "optionhelper_quote")
            if quote_stage.get("status") == "completed":
                completed_quote_job.status = "completed"
                completed_quote_job.message = "正式报价完成；selection 已归档失效"
            else:
                error = str(quote_stage.get("error") or self.event_evidence_message
                            or self.last_summary.get("error") or "未进入或未完成正式报价阶段")
                completed_quote_job.status = "failed"
                completed_quote_job.message = error[:180]
            self.active_quote_job = None
        # 报价链路尚未开始（例如研究阶段失败）时，pending 不应留给下一次运行误用。
        if self._selection_written_for_quote:
            pending = Path(self._selection_written_for_quote)
            if pending.exists():
                try:
                    pending.unlink()
                    self.status.setText(self.status.text() + "；报价未启动，待报价选择已失效。")
                except OSError:
                    pass
            self._selection_written_for_quote = ""
        if self._generated_override_path:
            try:
                Path(self._generated_override_path).unlink(missing_ok=True)
            except OSError:
                pass
            self._generated_override_path = ""
        self.refresh_history()
        if self.last_summary:
            self._sync_quote_review(self.last_summary)
        self._refresh_quote_queue()
        if (completed_quote_job is None and not self._run_cancelled and not self.event_evidence_message
                and self.quote.isChecked() and self._has_research_artifact(self.last_summary)):
            # 研究取数、正文和交付校验结束后，才进入独立的挂钩标的确认。
            # 使用下一个事件循环节拍，避免与研究 QProcess 的 finished 信号交叉。
            _underlying, research_only = self._confirmed_underlying(self.last_summary)
            if self.quote_review_button.isEnabled():
                QTimer.singleShot(0, self.prepare_formal_quote)
            elif not research_only:
                QTimer.singleShot(0, lambda: QMessageBox.information(
                    self, "没有待报价候选",
                    "研究报告已经完成，但客户未点名可报价证券，系统也没有找到通过基础筛选的 ETF/指数候选。\n\n"
                    "本次只保留研究报告，不会自动指定挂钩标的。可补充明确代码后重新运行。",
                ))
        # 用事件循环下一拍启动，避免 QProcess 刚结束时与下一份任务的信号/临时文件清理交叉。
        QTimer.singleShot(0, self._pump_quote_queue)

    def show_summary(self, data: dict) -> None:
        self.last_summary = data
        compact = {
            "run_id": data.get("run_id"), "status": data.get("status"),
            "duration_seconds": data.get("duration_seconds"), "metadata": data.get("metadata", {}),
            "stages": [{key: item.get(key) for key in ("label", "status", "duration_seconds", "error")}
                       for item in data.get("stages", [])],
            "recovery_actions": data.get("recovery_actions", []),
        }
        if self.active_quote_job is not None:
            compact["正式报价后台任务"] = {
                "job_id": self.active_quote_job.job_id,
                "status": self.active_quote_job.status,
                "message": self.active_quote_job.message,
            }
        if self.event_evidence_message:
            compact["report_blocked"] = self.event_evidence_message
        self.summary.setPlainText(json.dumps(compact, ensure_ascii=False, indent=2))
        self.handoff.setPlainText(self._handoff_text(data))
        self._sync_delivery_status(data)
        self._refresh_previews(data)
        self.artifacts.clear()
        for name, path in (data.get("artifacts") or {}).items():
            self.artifacts.addItem(f"{name}｜{path}")
        self._sync_quote_review(data)
        self._sync_quote_inclusion_action()

    def _clear_previews(self) -> None:
        if QWebEngineView is not None and isinstance(self.report_preview, QWebEngineView):
            self.report_preview.setHtml("")
        else:
            self.report_preview.setHtml("")
        self.quote_preview.setHtml("<p>等待正式报价结果…</p>")
        self.delivery_status.setText("交付校验：等待运行。正式交付需导出 PDF 并实测为 1 页。")
        self.delivery_status.setStyleSheet("color:#555;")

    def _sync_delivery_status(self, data: dict) -> None:
        value = str((data.get("metadata") or {}).get("一页通交付校验") or "").strip()
        if not value:
            self.delivery_status.setText("交付校验：尚未取得 PDF 实测结果；仅可作为内部草稿。")
            self.delivery_status.setStyleSheet("color:#8a5b14;")
        elif value.startswith("通过"):
            self.delivery_status.setText("交付校验：" + value + "。")
            self.delivery_status.setStyleSheet("color:#176b3a;font-weight:600;")
        else:
            self.delivery_status.setText("交付校验：" + value + "。请勿作为正式一页通发送。")
            self.delivery_status.setStyleSheet("color:#a11d2c;font-weight:600;")

    @staticmethod
    def _report_artifact(data: dict) -> str:
        artifacts = data.get("artifacts") or {}
        exact = str(artifacts.get("研究报告") or "")
        if exact:
            return exact
        return next((str(path) for name, path in artifacts.items() if "研究报告" in str(name)), "")

    def _comparison_quote_preview(self, data: dict) -> str:
        """呈现多标的冻结报价事实，仅用于人工横向复核。"""
        entries = (data.get("metadata") or {}).get("多标的报价比较") or []
        if not isinstance(entries, list) or not entries:
            return ""
        blocks = [
            "<h3>多标的正式报价比较</h3>",
            "<p>每一项均由 OptionHelper 独立生成；请比较结构、条款与风险后再决定向客户推荐哪一项。"
            "此处不计算或宣称跨标的胜率排序。</p>",
        ]
        selected = (data.get("metadata") or {}).get("一页通纳入正式报价") or []
        if isinstance(selected, list) and selected:
            labels = [
                f"{item.get('underlying') or '—'}｜{item.get('product_name') or item.get('product_id') or '—'}"
                for item in selected if isinstance(item, dict)
            ]
            if labels:
                blocks.append("<p><b>已写入一页通：</b>" + _html_escape("；".join(labels)) + "</p>")
        active = self.active_quote_job
        if active is not None and active.comparison_mode and active.status == "running":
            blocks.append(f"<p><b>{_html_escape(active.underlying)}</b> 正在生成正式报价…</p>")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            underlying = _html_escape(str(entry.get("underlying") or "—"))
            status = _html_escape(str(entry.get("status") or "—"))
            if entry.get("status") != "completed":
                blocks.append(
                    f"<section><h4>{underlying}｜{status}</h4><p>"
                    f"{_html_escape(str(entry.get('error') or '未形成正式报价。'))}</p></section>"
                )
                continue
            product = _html_escape(str(entry.get("product_name") or entry.get("product_id") or "—"))
            reason = _html_escape(str(entry.get("reason") or "—"))
            quote_date = _html_escape(str(entry.get("quote_date") or "—"))
            blocks.append(
                f"<section class='quote'><div class='q-heading'>{underlying}｜{product}</div>"
                f"<p>推荐理由：{reason}<br>报价日期：{quote_date}</p>"
            )
            for group in entry.get("groups") or []:
                if not isinstance(group, dict):
                    continue
                columns = [item for item in (group.get("columns") or []) if isinstance(item, dict)
                           and item.get("key") and item.get("label")]
                rows = [item for item in (group.get("rows") or []) if isinstance(item, dict)]
                if not columns or not rows:
                    continue
                head = "".join(f"<th>{_html_escape(str(column['label']))}</th>" for column in columns)
                body = "".join(
                    "<tr>" + "".join(
                        f"<td>{_html_escape(str(row.get(column['key']) or '—'))}</td>" for column in columns
                    ) + "</tr>" for row in rows
                )
                blocks.append(
                    f"<div class='q-group'><div class='q-group-title'>"
                    f"{_html_escape(str(group.get('title') or '参考报价'))}</div>"
                    f"<table class='q-table'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
                )
            risks = "；".join(str(item) for item in (entry.get("risks") or []) if str(item).strip())
            note = str(entry.get("quote_note") or "")
            if risks:
                blocks.append(f"<p>主要风险：{_html_escape(risks)}</p>")
            if note:
                blocks.append(f"<p class='q-note'>{_html_escape(note)}</p>")
            blocks.append("</section>")
        return "".join(blocks)

    def _refresh_previews(self, data: dict) -> None:
        """只展示已经生成的本地交付物；不解析展示结果回灌任何业务或报价判断。"""
        report_path = Path(self._report_artifact(data))
        if not report_path.is_file():
            self._clear_previews()
            return
        if QWebEngineView is not None and isinstance(self.report_preview, QWebEngineView):
            self.report_preview.load(QUrl.fromLocalFile(str(report_path.resolve())))
        else:
            try:
                self.report_preview.setHtml(report_path.read_text(encoding="utf-8"))
            except OSError:
                self.report_preview.setHtml("<p>研究报告无法读取。</p>")
        try:
            html = report_path.read_text(encoding="utf-8")
        except OSError:
            self.quote_preview.setHtml("<p>无法读取最终报告中的报价表。</p>")
            return
        # 这仅是 UI 从最终 HTML 中截取冻结报价表的展示片段；正式报价事实仍由
        # OptionHelper designer-input 生成，GUI 不从这里提取字段、更不做二次计算。
        active = self.active_quote_job
        if active is not None and active.status == "running":
            self.quote_preview.setHtml(
                f"<p><b>正式报价生成中</b>（{active.job_id}）。研究报告已完成；"
                "报价表将在 OptionHelper 返回并写入最终报告后显示。</p>"
            )
            return
        comparison = self._comparison_quote_preview(data)
        if comparison:
            self.quote_preview.setHtml(comparison)
            return
        matched = re.search(r'(<section class="quote">.*?</section>)', html, flags=re.DOTALL)
        if matched:
            self.quote_preview.setHtml(matched.group(1))
        else:
            self.quote_preview.setHtml(
                "<p>本次未生成正式参考报价表。完成研究后，可在“正式报价审核”中发起报价。</p>"
            )

    @staticmethod
    def _stage(data: dict, key: str) -> dict:
        return next((item for item in (data.get("stages") or []) if item.get("key") == key), {})

    @staticmethod
    def _markdown_section(text: str, heading: str, limit: int = 2600) -> str:
        marker = f"## {heading}"
        start = text.find(marker)
        if start < 0:
            return ""
        start = text.find("\n", start) + 1
        end = text.find("\n## ", start)
        section = text[start:end if end >= 0 else len(text)].strip()
        return section[:limit] + ("\n…（详见内部底稿）" if len(section) > limit else "")

    def _handoff_text(self, data: dict) -> str:
        """把散落在运行日志、底稿和 selection 归档中的审核重点聚到一张可读卡。"""
        lines = []
        metadata = data.get("metadata") or {}
        delivery = str(metadata.get("一页通交付校验") or "").strip()
        if delivery:
            lines += ["【一页通交付校验】", delivery, ""]
        confirmation_raw = metadata.get("分析师确认") or ""
        try:
            confirmation = json.loads(confirmation_raw)
        except (TypeError, json.JSONDecodeError):
            confirmation = {}
        if confirmation:
            lines += ["【分析师确认】",
                      f"市场：{confirmation.get('market') or '—'}",
                      f"研究口径：{confirmation.get('research_scope') or '—'}",
                      f"挂钩标的：{confirmation.get('underlying_code') or '—'}",
                      f"处理方式：{'仅研究' if confirmation.get('research_only') else '研究并可报价'}"]
            if confirmation.get("reason"):
                lines.append("映射理由：" + str(confirmation["reason"]))
            lines.append("")

        research = self._stage(data, "research_report") or self._stage(data, "report")
        if research:
            lines += ["【研究交付】", f"状态：{research.get('status') or '—'}"]
            if research.get("error"):
                lines.append("说明：" + str(research["error"]))
            lines.append("")

        quote = self._stage(data, "optionhelper_quote")
        if quote:
            lines += ["【正式报价】", f"状态：{quote.get('status') or '—'}"]
            if quote.get("error"):
                lines.append("原因：" + str(quote["error"]))
            lines.append("")
        elif self._has_research_artifact(data):
            lines += ["【正式报价】", "尚未发起。请审核本次研究后点击“审核产品选择并发起正式报价”。", ""]

        artifacts = data.get("artifacts") or {}
        archive = next((str(path) for name, path in artifacts.items() if "selection归档" in str(name)), "")
        if archive:
            payload = _load_json(Path(archive))
            selection = payload.get("selection") if isinstance(payload, dict) else {}
            lifecycle = payload.get("lifecycle") if isinstance(payload, dict) else {}
            lines += ["【一次性 selection 审计】",
                      f"产品编号：{(selection or {}).get('product_id') or '—'}",
                      f"标的：{', '.join((selection or {}).get('underlyings') or []) or '—'}",
                      f"生命周期：{(lifecycle or {}).get('state') or '—'}",
                      f"报价结果：{(lifecycle or {}).get('quote_status') or '—'}",
                      f"归档：{archive}", ""]

        draft = next((str(path) for name, path in artifacts.items() if "内部底稿" in str(name)), "")
        if draft:
            try:
                content = Path(draft).read_text(encoding="utf-8")
            except OSError:
                content = ""
            viewpoint = self._markdown_section(content, "二、给 OptionHelper 的观点包")
            if viewpoint:
                lines += ["【观点包（节选）】", viewpoint]
        if not lines:
            return "等待运行结果…"
        return "\n".join(lines)

    def open_artifact(self, item) -> None:
        path = item.text().split("｜", 1)[-1]
        if Path(path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def refresh_history(self) -> None:
        RUNS.mkdir(parents=True, exist_ok=True)
        current = self.history.currentData()
        current_run_path = str(RUNS / f"{self.run_id}.json") if self.run_id else ""
        self.history.blockSignals(True)
        self.history.clear()
        self.history.addItem("选择一条历史运行以查看内部底稿…", "")
        for path in sorted(RUNS.glob("run-*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            data = _load_json(path)
            self.history.addItem(f"{data.get('run_id', path.stem)} · {data.get('status', '—')}", str(path))
        # 运行完成后保持当前 run 的选中态，但绝不自动把历史内容盖到当前结果页；
        # 首次启动则停留在占位项，须由分析师显式选择一条旧运行。
        desired = current_run_path or str(current or "")
        index = self.history.findData(desired) if desired else 0
        if index >= 0:
            self.history.setCurrentIndex(index)
        self.history.blockSignals(False)

    def show_selected_history(self) -> None:
        path = self.history.currentData()
        if path and path != str(RUNS / f"{self.run_id}.json"):
            data = _load_json(Path(path))
            draft = next((value for name, value in (data.get("artifacts") or {}).items() if "内部底稿" in name), "")
            try:
                detail = Path(draft).read_text(encoding="utf-8") if draft else "该运行未生成内部底稿。"
                self.review.setPlainText(
                    f"【历史运行复核】{data.get('run_id', '—')}｜{data.get('status', '—')}\n"
                    "下方仅展示该次内部底稿；当前报告预览、交付文件和正式报价审核保持为本次结果。\n\n"
                    + detail)
            except OSError:
                self.review.setPlainText("内部底稿文件不可读取或已移动。")

    def rerun_selected(self) -> None:
        path = self.history.currentData()
        data = _load_json(Path(path)) if path else {}
        request = str(data.get("request") or "").strip()
        if not request:
            QMessageBox.information(self, "无法重跑", "所选运行没有可用的原始需求。")
            return
        self.prompt.setPlainText(request)
        self.start_run()


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationDisplayName("Research Helper")
    app.setStyleSheet(APPLE_STYLE)
    window = ResearchHelperWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
