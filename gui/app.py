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
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QProcess, QTimer, QUrl, Qt
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow,
    QMessageBox, QPushButton, QPlainTextEdit, QProgressBar, QSplitter, QTabWidget,
    QTextBrowser, QVBoxLayout, QWidget,
)

try:  # 预览是增强功能；少数精简 PySide6 安装不带 WebEngine 时仍可启动应用。
    from PySide6.QtWebEngineWidgets import QWebEngineView
except ImportError:  # pragma: no cover - 取决于终端用户安装的 Qt 组件
    QWebEngineView = None

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "output" / "runs"
LOCAL_CONFIG = ROOT / "config.local.json"

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
    source_run_id: str = ""
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
                    "事件证据": {"事件事实": [], "传导关系": []}}
        if self.path and self.path.is_file():
            try:
                self.editor.setPlainText(self.path.read_text(encoding="utf-8"))
            except OSError:
                self.editor.setPlainText(json.dumps(template, ensure_ascii=False, indent=2))
        else:
            self.editor.setPlainText(json.dumps(template, ensure_ascii=False, indent=2))
        hint = QLabel("字段覆盖必须含“值”和“来源”；判定字段不可人工覆盖。事件驱动报告还须填写事件事实和传导关系，且每条都要有来源。")
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
                "事件证据": {"事件事实": [], "传导关系": []},
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


class MaterialCandidateDialog(QDialog):
    """展示本地/直链材料的原文片段；分类及导入始终由分析师点击完成。"""

    def __init__(self, parent: QWidget | None = None, *, topic: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("从材料导入事件证据")
        self.resize(900, 620)
        self.topic = topic
        self.candidates = []
        self.imported_facts: list[dict] = []
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
        import_link = QPushButton("将选中原文作为传导证据")
        close = QPushButton("取消")
        choose.clicked.connect(self.choose_material)
        direct.clicked.connect(self.download_material)
        import_fact.clicked.connect(lambda: self.import_selected(transmission=False))
        import_link.clicked.connect(lambda: self.import_selected(transmission=True))
        close.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "选择公司 IR、业绩公告或产业链材料后，系统只展示逐字原文候选。请自行核对原件，"
            "再把选中内容明确归入事件事实或传导证据。直链仅支持 HTTPS 的原始文件，不抓取网页。"))
        layout.itemAt(layout.count() - 1).widget().setWordWrap(True)
        layout.addWidget(self.material_label)
        layout.addWidget(ResearchHelperWindow._row(choose, direct))
        layout.addWidget(QLabel("候选原文（可多选）"))
        layout.addWidget(self.list)
        layout.addWidget(QLabel("作为传导证据时的关系类型"))
        layout.addWidget(self.relation)
        layout.addWidget(ResearchHelperWindow._row(import_fact, import_link, close))

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

    def import_selected(self, *, transmission: bool) -> None:
        selected = self.list.selectedItems()
        if not selected:
            QMessageBox.information(self, "尚未选择", "请选择至少一条原文候选。")
            return
        payload: list[dict] = []
        for widget in selected:
            candidate = self.candidates[int(widget.data(Qt.ItemDataRole.UserRole))]
            item = {"内容": candidate.content, "来源": candidate.source,
                    "链接": candidate.reference, "材料页码": candidate.page}
            if transmission:
                item["关系"] = self.relation.currentText()
            payload.append(item)
        if transmission:
            self.imported_links = payload
        else:
            self.imported_facts = payload
        self.accept()


class EventEvidenceDialog(QDialog):
    """事件型研究的表单式证据包；运行时由主窗口自动写成临时 overrides。"""

    def __init__(self, parent: QWidget | None = None, evidence: dict | None = None,
                 *, topic: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("事件证据包")
        self.resize(860, 560)
        evidence = evidence or {}
        self.facts = [dict(item) for item in (evidence.get("事件事实") or []) if isinstance(item, dict)]
        self.links = [dict(item) for item in (evidence.get("传导关系") or []) if isinstance(item, dict)]
        self.fact_list, self.link_list = QListWidget(), QListWidget()
        self.fact_list.setMinimumHeight(175); self.link_list.setMinimumHeight(175)
        self.hint = QLabel(
            "此处平时可留空。只有事件型需求在运行时，才需要同时具备：①已披露的事件事实；"
            "②该事件到本次行业或 ETF 的传导依据。可从已上传材料导入原文，不必重复手填；"
            "导入后仍需由分析师确认分类与来源。"
        )
        self.hint.setWordWrap(True)

        add_fact, remove_fact = QPushButton("添加事件事实…"), QPushButton("删除选中")
        add_link, remove_link = QPushButton("添加传导证据…"), QPushButton("删除选中")
        import_material = QPushButton("从已上传材料导入原文")
        add_fact.clicked.connect(self.add_fact); remove_fact.clicked.connect(self.remove_fact)
        add_link.clicked.connect(self.add_link); remove_link.clicked.connect(self.remove_link)
        import_material.clicked.connect(lambda: self.import_material(topic))
        save, cancel = QPushButton("保存证据"), QPushButton("取消")
        save.clicked.connect(self.accept); cancel.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.hint)
        layout.addWidget(import_material)
        layout.addWidget(QLabel("1. 事件事实（业绩实际、指引、公告等）"))
        layout.addWidget(self.fact_list)
        layout.addWidget(ResearchHelperWindow._row(add_fact, remove_fact))
        layout.addWidget(QLabel("2. 传导证据（为何影响本次行业或 ETF）"))
        layout.addWidget(self.link_list)
        layout.addWidget(ResearchHelperWindow._row(add_link, remove_link))
        layout.addWidget(ResearchHelperWindow._row(save, cancel))
        self.refresh()

    def import_material(self, topic: str) -> None:
        dialog = MaterialCandidateDialog(self, topic=topic)
        # 默认从项目 sources/ 选择材料：材料上传和证据导入因而是一条连续路径。
        # 对于不在 sources/ 的官方披露，弹窗内仍可选择本地文件或 HTTPS 直链。
        source_files = sorted((ROOT / "sources").rglob("*.pdf")) if (ROOT / "sources").is_dir() else []
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

    def _edit_item(self, *, transmission: bool) -> dict | None:
        dialog = QDialog(self)
        dialog.setWindowTitle("添加传导证据" if transmission else "添加事件事实")
        content, source, link = QPlainTextEdit(), QLineEdit(), QLineEdit()
        source.setPlaceholderText("来源（必填），例如 SK hynix 业绩公告 p4")
        link.setPlaceholderText("链接或本地材料路径（可选）")
        relation = QComboBox()
        relation.addItems(["直接竞争", "供应链", "客户需求", "技术替代", "估值情绪映射", "其他"])
        confirm, cancel = QPushButton("添加"), QPushButton("取消")
        confirm.clicked.connect(dialog.accept); cancel.clicked.connect(dialog.reject)
        form = QFormLayout(dialog)
        if transmission:
            form.addRow("关系类型", relation)
            form.addRow("传导说明", content)
        else:
            form.addRow("已披露事实", content)
        form.addRow("来源 / 链接", self._source_row(source, link, dialog))
        form.addRow("", ResearchHelperWindow._row(confirm, cancel))
        if not dialog.exec():
            return None
        if not content.toPlainText().strip() or not source.text().strip():
            QMessageBox.information(self, "缺少信息", "内容和来源均为必填项。")
            return None
        item = {"内容": content.toPlainText().strip(), "来源": source.text().strip(),
                "链接": link.text().strip()}
        if transmission:
            item["关系"] = relation.currentText()
        return item

    def add_fact(self) -> None:
        if item := self._edit_item(transmission=False):
            self.facts.append(item); self.refresh()

    def add_link(self) -> None:
        if item := self._edit_item(transmission=True):
            self.links.append(item); self.refresh()

    def remove_fact(self) -> None:
        row = self.fact_list.currentRow()
        if row >= 0:
            self.facts.pop(row); self.refresh()

    def remove_link(self) -> None:
        row = self.link_list.currentRow()
        if row >= 0:
            self.links.pop(row); self.refresh()

    def refresh(self) -> None:
        self.fact_list.clear(); self.link_list.clear()
        for item in self.facts:
            self.fact_list.addItem(f"事实｜{item.get('内容', '')}\n来源：{item.get('来源', '')}")
        for item in self.links:
            self.link_list.addItem(f"{item.get('关系', '传导')}｜{item.get('内容', '')}\n来源：{item.get('来源', '')}")

    def payload(self) -> dict:
        return {"事件事实": list(self.facts), "传导关系": list(self.links)}


class LogicPickDialog(QDialog):
    """让分析师在 GUI 中选择数据/材料候选逻辑，而非让 LLM 静默决定。"""

    def __init__(self, parent: QWidget | None, payload: dict) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择本次报告逻辑")
        self.resize(900, 650)
        self._auto = False
        suggested = {int(item) for item in (payload.get("suggested") or []) if str(item).isdigit()}
        self.suggested = suggested
        self.checks: list[tuple[int, QCheckBox]] = []
        hint = QLabel(
            "请选择 2–3 条作为报告正文主轴。数据触发项来自已核验行情；材料提炼项仅在您核对原文后才应勾选。"
            "“采用系统建议”只按证据质量和结构组合，不替代专业判断。")
        hint.setWordWrap(True)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        for raw in payload.get("candidates") or []:
            try:
                index = int(raw.get("index"))
            except (TypeError, ValueError):
                continue
            source_kind = "数据触发" if raw.get("kind") == "thesis" else "材料原文（请核对）"
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
        if not self.selected_indices():
            QMessageBox.information(self, "请选择逻辑", "请至少勾选一条逻辑，或选择“交由系统自动挑选”。")
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
                 client_summary: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("确认 OptionHelper 推荐后发起正式报价")
        self.resize(760, 440)
        self.underlying = underlying
        self.candidates = candidates
        self.choice = QComboBox()
        for item in candidates:
            label = f"主候选" if int(item.get("rank") or 99) == 1 else f"备选 {item.get('rank', '')}"
            self.choice.addItem(f"{label}｜{item.get('product_name') or item.get('product_id')}", item)
        self.detail = QPlainTextEdit(); self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(220)
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
        self.setWindowTitle("确认研究市场、行业口径与挂钩工具")
        self.resize(720, 430)

        self.topic = QLabel(str(payload.get("topic") or "—"))
        self.topic.setWordWrap(True)
        self.original = str(payload.get("original_market") or "A股")
        self.mode = QComboBox()
        self.mode.addItem("明确映射到 A 股研究口径并继续", "map_a")
        self.mode.addItem("仅研究原市场，不生成产品报价", "research_only")
        self.mode.addItem("保留原市场并指定 ETF/指数", "keep_market")
        # 即使解析器先识别到“全球/港股”等事件背景，也默认给出可执行的 A 股研究路径；
        # 选择该项时 value() 会强制写入 A股，不依赖禁用下拉框的显示值。
        self.mode.setCurrentIndex(0)

        self.market = QComboBox()
        self.market.addItems(["A股", "港股", "跨市场"])
        self.theme = QLineEdit(str(payload.get("proposed_theme") or ""))
        self.theme.setPlaceholderText("例如：光模块 / 黄金；说明本次真正研究的细分主题")
        self.scope = QLineEdit(str(payload.get("proposed_scope") or ""))
        self.scope.setPlaceholderText("多个 A 股一级行业用“、”分隔；不能只填数字")
        self.underlying = QComboBox()
        self.underlying.setEditable(True)
        self.underlying.addItem("", {"code": "", "name": ""})
        for item in payload.get("suggested_instruments") or []:
            origin = str(item.get("origin") or "")
            label = f"{item.get('code', '')}｜{item.get('name', '')}｜{origin}｜{item.get('note', '')}"
            self.underlying.addItem(label, item)
        self.underlying.lineEdit().setPlaceholderText("请选择建议标的；也可输入代码，例如 513050.SH")
        self.underlying_hint = QLabel("请选择候选后查看其主题匹配与流动性说明；手工输入代码会在提交后重新核验。")
        self.underlying_hint.setWordWrap(True)
        self.underlying_hint.setStyleSheet("color:#666;")
        self.reason = QLineEdit(str(payload.get("reason") or ""))
        self.reason.setPlaceholderText("选填：说明为什么采用这个研究口径/标的")

        errors = payload.get("errors") or []
        notice = str(payload.get("notice") or "")
        discovery_notice = str(payload.get("discovery_notice") or "")
        self.message = QLabel(
            ("上次校验未通过：\n• " + "\n• ".join(errors)) if errors
            else (notice + ("\n" + discovery_notice if discovery_notice else "")))
        self.message.setWordWrap(True)
        self.message.setStyleSheet("color:#a61b29;" if errors else "color:#666;")
        confirm, cancel = QPushButton("校验并继续"), QPushButton("取消本次运行")
        confirm.clicked.connect(self._submit)
        cancel.clicked.connect(self.reject)
        self.mode.currentIndexChanged.connect(self._sync_mode)
        self.underlying.currentIndexChanged.connect(self._sync_underlying_hint)

        form = QFormLayout(self)
        form.addRow("解析主题", self.topic)
        form.addRow("处理方式", self.mode)
        form.addRow("确认市场", self.market)
        form.addRow("研究主题（研究什么）", self.theme)
        form.addRow("研究口径", self.scope)
        form.addRow("ETF / 指数（产品路径必填）", self.underlying)
        form.addRow("候选理由", self.underlying_hint)
        form.addRow("映射理由", self.reason)
        form.addRow("", self.message)
        form.addRow("", ResearchHelperWindow._row(confirm, cancel))
        self._sync_mode()
        self._sync_underlying_hint()

    def _sync_mode(self) -> None:
        mode = self.mode.currentData()
        if mode == "map_a":
            self.market.setCurrentText("A股")
            self.market.setEnabled(False)
            self.underlying.setEnabled(True)
        elif mode == "research_only":
            self.market.setCurrentText(self.original)
            self.market.setEnabled(False)
            self.underlying.setEnabled(False)
        else:
            self.market.setCurrentText(self.original)
            self.market.setEnabled(True)
            self.underlying.setEnabled(True)

    def _sync_underlying_hint(self, *_args) -> None:
        data = self.underlying.currentData() if self.underlying.currentIndex() >= 0 else {}
        data = data if isinstance(data, dict) else {}
        note = str(data.get("note") or "").strip()
        origin = str(data.get("origin") or "").strip()
        if note:
            self.underlying_hint.setText(f"{origin or '候选'}理由：{note}")
        else:
            self.underlying_hint.setText("手工输入代码将在提交后校验证券真实性、主题暴露与近20日流动性。")

    def value(self) -> dict:
        mode = str(self.mode.currentData() or "")
        raw = self.underlying.currentText().strip()
        data = self.underlying.currentData() if self.underlying.currentIndex() >= 0 else {}
        data = data if isinstance(data, dict) else {}
        code = str(data.get("code") or raw.split("｜", 1)[0]).strip().upper()
        name = str(data.get("name") or "").strip()
        # “映射到 A 股”与“仅研究原市场”下市场下拉框是禁用控件。不要依赖禁用
        # 控件在不同 Qt/Windows 组合里是否保留 currentText，而是由处理方式确定值。
        market = "A股" if mode == "map_a" else (self.original if mode == "research_only"
                                                  else self.market.currentText().strip())
        return {
            "market": market,
            "research_theme": self.theme.text().strip(),
            "research_scope": self.scope.text().strip(),
            "underlying_code": "" if mode == "research_only" else code,
            "underlying_name": "" if mode == "research_only" else name,
            "research_only": mode == "research_only",
            "reason": self.reason.text().strip(),
        }

    def _submit(self) -> None:
        """先在界面解释必填项，避免反复启动后端校验才知道漏选了 ETF。"""
        value = self.value()
        if not value["research_theme"]:
            QMessageBox.information(self, "请确认研究主题", "请填写本次真正要研究的细分主题，例如“光模块”或“黄金”。")
            return
        if not value["research_scope"]:
            QMessageBox.information(self, "请确认研究口径", "请填写研究篮子/行业口径。")
            return
        if not value["research_only"] and not value["underlying_code"]:
            QMessageBox.information(
                self, "请确认挂钩工具",
                "“映射到 A 股并继续”或“保留原市场”会进入产品路径，必须从下拉建议中选择或输入 ETF/指数代码。\n\n"
                "如果这次只需要行业研究、不做产品报价，请把“处理方式”改为“仅研究原市场，不生成产品报价”。",
            )
            return
        self.accept()

class ResearchHelperWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Research Helper · 场外衍生品一页通")
        self.resize(1280, 820)
        self.process: QProcess | None = None
        self.option_process: QProcess | None = None
        self._option_output = ""
        self._option_error_output = ""
        self.run_id = ""
        self.override_path = ""
        self.last_summary: dict = {}
        self._output_buffer = ""
        self.event_evidence_message = ""
        self.event_evidence: dict = {"事件事实": [], "传导关系": []}
        self._generated_override_path = ""
        self._selection_written_for_quote = ""
        self.quote_jobs: list[QuoteJob] = []
        self.active_quote_job: QuoteJob | None = None
        self._quote_pdf_exporting = False
        self._run_cancelled = False
        self.quote_slow_timer = QTimer(self)
        self.quote_slow_timer.setSingleShot(True)
        self.quote_slow_timer.timeout.connect(self._quote_is_slow)
        self.quote_timeout_timer = QTimer(self)
        self.quote_timeout_timer.setSingleShot(True)
        self.quote_timeout_timer.timeout.connect(self._quote_timed_out)

        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("输入客户需求，例如：根据目前酒ETF 512690.SH 的市场情况推荐产品")
        self.prompt.setFixedHeight(72)
        self.horizon = QLineEdit("3个月")
        self.max_loss = QLineEdit("100%")
        self.principal = QComboBox()
        self.principal.addItem("接受本金波动", "yes")
        self.principal.addItem("不接受本金波动", "no")
        self.preference = QLineEdit()
        self.preference.setPlaceholderText("例如：更偏上涨参与（可选）")
        self.quote = QCheckBox("研究完成后准备正式报价审核")
        self.quote.setChecked(True)
        self.pdf = QCheckBox("导出 PDF 并校验一页（正式交付必需）")
        self.pdf.setChecked(True)
        self.source_label = QLabel("未上传补充材料（可选；支持 PDF）")
        self.source_label.setWordWrap(True)
        upload_sources = QPushButton("上传补充材料")
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
        ifind_settings = QPushButton("iFinD 凭证")
        choose_override.clicked.connect(self.choose_override)
        edit_override.clicked.connect(self.edit_override)
        upload_sources.clicked.connect(self.upload_sources)
        open_sources.clicked.connect(self.open_sources_folder)
        edit_evidence.clicked.connect(self.edit_event_evidence)
        clear_evidence.clicked.connect(self.clear_event_evidence)
        llm_settings.clicked.connect(self.edit_llm_settings)
        ifind_settings.clicked.connect(self.edit_ifind_credentials)
        self.run_button = QPushButton("开始生成")
        self.run_button.clicked.connect(self.start_run)
        self.stop_run_button = QPushButton("停止本次运行")
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
        self.quote_review_button.setEnabled(False)
        self.quote_review_button.clicked.connect(self.prepare_formal_quote)
        self.quote_review_hint = QLabel("请先完成一份含已确认挂钩标的的研究报告。")
        self.quote_review_hint.setWordWrap(True)
        self.quote_queue = QListWidget()
        self.quote_queue.setMaximumHeight(112)
        self.quote_queue.currentRowChanged.connect(self._sync_quote_queue_actions)
        self.cancel_quote_job_button = QPushButton("取消选中任务")
        self.cancel_quote_job_button.clicked.connect(self.cancel_selected_quote_job)
        self.retry_quote_job_button = QPushButton("重新审核并加入重试队列")
        self.retry_quote_job_button.clicked.connect(self.retry_selected_quote_job)
        self._sync_quote_queue_actions()

        # 页面主操作统一采用可点击尺寸；文字不以省略号替代，避免分析师无法理解功能。
        for button in (upload_sources, open_sources, choose_override, edit_override,
                       edit_evidence, clear_evidence, llm_settings, ifind_settings,
                       self.run_button, self.stop_run_button, self.quote_review_button, self.cancel_quote_job_button,
                       self.retry_quote_job_button):
            button.setMinimumHeight(32)
        for button in (upload_sources, open_sources, choose_override, edit_override,
                       edit_evidence, llm_settings, ifind_settings):
            button.setMinimumWidth(138)
        self.quote_review_button.setMinimumWidth(270)

        form = QFormLayout()
        form.addRow(QLabel("<b>1. 需求与客户约束</b>"))
        form.addRow("客户需求", self.prompt)
        form.addRow("期限", self.horizon)
        form.addRow("最大损失", self.max_loss)
        form.addRow("本金波动", self.principal)
        form.addRow("收益偏好", self.preference)
        form.addRow("交付选项", self._row(self.quote, self.pdf))
        form.addRow(QLabel("<b>2. 研究口径与补充材料</b>"))
        form.addRow("市场/行业/ETF", QLabel("启动后按需求自动弹出确认卡；高风险映射必须人工确认。"))
        form.addRow("补充材料", self._row(self.source_label, upload_sources, open_sources))
        form.addRow("事件型需求", self._row(self.evidence_label, edit_evidence, clear_evidence))
        form.addRow("人工数据补充", self._row(self.override_label, choose_override, edit_override))
        form.addRow("分析模型", self._row(llm_settings))
        form.addRow("数据与报价凭证", self._row(ifind_settings))
        form.addRow(QLabel("<b>3. 运行与交付</b>"))
        form.addRow("", self._row(self.run_button, self.stop_run_button))

        input_card = QFrame()
        input_card.setFrameShape(QFrame.Shape.StyledPanel)
        input_card.setLayout(form)
        quote_box = QFrame()
        quote_box.setFrameShape(QFrame.Shape.StyledPanel)
        quote_layout = QVBoxLayout(quote_box)
        quote_layout.addWidget(QLabel("正式报价（研究完成后才可用）"))
        quote_layout.addWidget(self.quote_review_hint)
        quote_layout.addWidget(self.quote_review_button)
        quote_layout.addWidget(QLabel("报价任务（仅当前会话）"))
        quote_layout.addWidget(self.quote_queue)
        quote_layout.addWidget(self._row(self.cancel_quote_job_button, self.retry_quote_job_button))

        run_box = QVBoxLayout()
        run_box.addWidget(input_card)
        run_box.addWidget(quote_box)
        run_box.addStretch(1)
        run_widget = QWidget()
        run_widget.setLayout(run_box)

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
        result_box.addWidget(result_tabs, 1)
        result_widget = QWidget()
        result_widget.setLayout(result_box)
        splitter = QSplitter()
        splitter.addWidget(run_widget)
        splitter.addWidget(result_widget)
        splitter.setSizes([720, 560])
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

    def open_sources_folder(self) -> None:
        destination = ROOT / "sources"
        destination.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(destination.resolve())))

    def _refresh_sources_summary(self, *, last_uploaded: list[str] | None = None) -> None:
        files = sorted((ROOT / "sources").rglob("*.pdf")) if (ROOT / "sources").is_dir() else []
        if last_uploaded:
            listed = "、".join(last_uploaded[:3]) + ("等" if len(last_uploaded) > 3 else "")
            text = f"刚上传 {len(last_uploaded)} 份：{listed}。下次研究会自动读取。"
        elif files:
            text = f"sources 文件夹已有 {len(files)} 份 PDF；下次研究会自动读取。"
        else:
            text = "未上传补充材料（可选；支持 PDF）。"
        self.source_label.setText(text)
        self.source_label.setStyleSheet("color:#176b3a;" if files or last_uploaded else "color:#555;")

    def _sync_evidence_from_override(self, path: str) -> None:
        """兼容旧补数文件：其中已有事件证据时同步展示为表单状态。"""
        raw = _load_json(Path(path))
        evidence = raw.get("事件证据") if isinstance(raw, dict) else None
        if not isinstance(evidence, dict):
            return
        facts = [dict(item) for item in (evidence.get("事件事实") or []) if isinstance(item, dict)]
        links = [dict(item) for item in (evidence.get("传导关系") or []) if isinstance(item, dict)]
        self.event_evidence = {"事件事实": facts, "传导关系": links}
        self._refresh_evidence_summary()

    def _refresh_evidence_summary(self) -> None:
        facts = len(self.event_evidence.get("事件事实") or [])
        links = len(self.event_evidence.get("传导关系") or [])
        if facts and links:
            self.evidence_label.setText(f"已录入：事件事实 {facts} 条，传导证据 {links} 条。")
            self.evidence_label.setStyleSheet("color:#176b3a;")
        else:
            missing = []
            if not facts:
                missing.append("事件事实")
            if not links:
                missing.append("传导证据")
            self.evidence_label.setText("仅事件型需求需要填写：" + "、".join(missing) + "。普通板块/ETF研究无需填写。")
            self.evidence_label.setStyleSheet("color:#8a5b14;")

    def edit_event_evidence(self) -> None:
        dialog = EventEvidenceDialog(self, self.event_evidence, topic=self.prompt.toPlainText().strip())
        if dialog.exec():
            self.event_evidence = dialog.payload()
            self._refresh_evidence_summary()

    def clear_event_evidence(self) -> None:
        self.event_evidence = {"事件事实": [], "传导关系": []}
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
        links = list(self.event_evidence.get("传导关系") or [])
        if facts or links:
            data["事件证据"] = {"事件事实": facts, "传导关系": links}
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
        """仅接受本次市场确认写进运行日志的标的，避免从报告标题猜代码。"""
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
        # 普通 A 股板块若无需确认，仍可能已有研究标的；只接受看起来像代码的记录。
        candidate = str(metadata.get("分析标的") or "").strip().upper()
        return candidate, False

    @staticmethod
    def _has_research_artifact(summary: dict) -> bool:
        artifacts = summary.get("artifacts") or {}
        return any("研究报告" in str(name) and str(path) for name, path in artifacts.items())

    def _sync_quote_review(self, summary: dict) -> None:
        ready = self.process is None and self._has_research_artifact(summary)
        underlying, research_only = self._confirmed_underlying(summary)
        ready = ready and bool(underlying) and not research_only
        self.quote_review_button.setEnabled(ready)
        if research_only:
            self.quote_review_hint.setText("本次分析师选择“仅研究”，不能发起正式报价。")
        elif ready:
            self.quote_review_hint.setText(
                f"已确认挂钩标的：{underlying}。先审核产品选择，再用一次性 selection 发起报价。")
        else:
            self.quote_review_hint.setText("请先完成一份含已确认挂钩标的的研究报告。")

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

    def _review_and_enqueue_quote(self, *, request: str, underlying: str, source_summary: dict,
                                  recommended_candidates: list[dict], source_run_id: str = "") -> None:
        if not self.horizon.text().strip() or not self.max_loss.text().strip():
            QMessageBox.information(self, "缺少客户条件", "请填写期限和最大损失；或保留默认值。")
            return
        constraint_summary = (
            f"期限 {self.horizon.text().strip() or '—'}；最大损失 {self.max_loss.text().strip() or '—'}；"
            f"{'接受' if self.principal.currentData() == 'yes' else '不接受'}本金波动"
            + (f"；收益偏好：{self.preference.text().strip()}" if self.preference.text().strip() else "")
        )
        dialog = RecommendationReviewDialog(
            self, underlying=underlying, candidates=recommended_candidates,
            client_summary=constraint_summary,
        )
        if not dialog.exec():
            return
        overrides = self._effective_override_data()
        if overrides is None:
            return
        handoff = self._optionhelper_handoff(source_summary)
        market_prompt = str(handoff.get("market_prompt") or "").strip()
        if not market_prompt:
            QMessageBox.warning(self, "缺少本次观点包", "该研究运行未保存可复用的 OptionHelper 观点包；请重新生成研究报告后再报价。")
            return
        # 即使只是排队，也不允许在未知 pending 上叠加；真正的 selection 只会在轮到任务时写入。
        if self._selection_pending_path().exists():
            QMessageBox.warning(
                self, "存在待报价选择",
                "发现尚未消费的 pending selection。为避免覆盖未知选择，本次任务未入队；"
                "请先完成或由原发起方清除该任务后重新审核。")
            return
        payload = {
            "selection": dialog.selection(),
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
            },
        }
        job = QuoteJob(
            job_id="quote-" + uuid.uuid4().hex[:6], request=request, underlying=underlying,
            market_prompt=market_prompt,
            selection_payload=payload,
            overrides=json.loads(json.dumps(overrides, ensure_ascii=False)),
            export_pdf=self.pdf.isChecked(),
            source_run_id=source_run_id or str(source_summary.get("run_id") or ""),
        )
        self.quote_jobs.append(job)
        self._refresh_quote_queue()
        self.status.setText(f"正式报价任务 {job.job_id} 已入队；轮到时才会写入一次性 selection。")
        self._pump_quote_queue()

    def prepare_formal_quote(self) -> None:
        if self.process is not None or self.option_process is not None:
            return
        summary = self.last_summary
        if not self._has_research_artifact(summary):
            QMessageBox.information(self, "无法报价", "请先完成研究报告。")
            return
        request = str(summary.get("request") or "").strip()
        underlying, research_only = self._confirmed_underlying(summary)
        if not request or research_only or not underlying:
            QMessageBox.information(self, "无法报价", "本次运行没有可用于正式报价的已确认挂钩标的。")
            return
        self._start_optionhelper_recommender(summary=summary, request=request, underlying=underlying)

    def retry_selected_quote_job(self) -> None:
        job = self._selected_quote_job()
        if job is None or job.status not in {"failed", "blocked"}:
            return
        # 重试绝不复用旧 selection；重新运行 OptionHelper Recommender，再由分析师确认候选。
        self._start_optionhelper_recommender(summary=self.last_summary, request=job.request, underlying=job.underlying)

    def _start_optionhelper_recommender(
        self, *, summary: dict, request: str, underlying: str, supplement: str = "",
    ) -> None:
        if self.process is not None or self.option_process is not None:
            return
        handoff = self._optionhelper_handoff(summary)
        market_prompt = str(handoff.get("market_prompt") or "").strip()
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
            lambda exit_code, _status: self._finish_recommender(summary, request, underlying, exit_code)
        )
        process.errorOccurred.connect(lambda _error: self.status.setText("OptionHelper 推荐进程无法启动"))
        self.option_process = process
        self.status.setText("OptionHelper 正在基于本次观点包筛选结构候选…")
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
    ) -> None:
        """把审核前的 OptionHelper 调用写入本次 run，避免它成为不可追踪黑箱。"""
        run_id = str(summary.get("run_id") or "").strip()
        if not run_id:
            return
        result = payload.get("result") if payload.get("ok") is True else {}
        detail = result if isinstance(result, dict) else {}
        message = str(detail.get("message") or payload.get("message") or "")
        status = str(detail.get("status") or ("completed" if candidates else "failed"))
        record = {
            "run_id": run_id,
            "worker_exit_code": exit_code,
            "outer_ok": payload.get("ok") is True,
            "status": status,
            "message": message,
            "candidate_count": len(candidates),
            "stderr": stderr[-2000:],
        }
        path = RUNS / f"{run_id}.optionhelper-recommender.json"
        try:
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            artifacts = summary.setdefault("artifacts", {})
            artifacts["OptionHelper 推荐诊断"] = str(path)
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
        except OSError:
            # 日志辅助功能不应妨碍分析师在 UI 中获得本次推荐结果。
            pass

    def _finish_recommender(self, summary: dict, request: str, underlying: str, exit_code: int) -> None:
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
        )
        if not candidates:
            # `run_recommendation_request` 会以正常 JSON 表示“还需补问”。此前 GUI
            # 只读外层 worker 的 message，丢掉内层 needs_input / unavailable 原因，
            # 于是所有这类状态都被误报为“未返回可解析的推荐结果”。
            detail = result if isinstance(result, dict) else {}
            message = str(
                detail.get("message")
                or payload.get("message")
                or "OptionHelper 未返回可解析的推荐结果。"
            )
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
                        ),
                    )
                else:
                    self.status.setText("OptionHelper 等待补充客户条件。")
                return
            QMessageBox.warning(self, title, message)
            return
        self.status.setText("OptionHelper 已形成结构候选，等待分析师确认。")
        self._review_and_enqueue_quote(request=request, underlying=underlying, source_summary=summary,
                                       recommended_candidates=[dict(item) for item in candidates if isinstance(item, dict)],
                                       source_run_id=str(summary.get("run_id") or ""))

    def _pump_quote_queue(self) -> None:
        """串行启动下一份报价；只消费本次内存候选，不重跑研究主流程。"""
        if self.process is not None or self.option_process is not None or self.active_quote_job is not None:
            return
        job = next((item for item in self.quote_jobs if item.status == "queued"), None)
        if job is None:
            return
        from core import config
        job.status, job.message, job.forced_stop_reason = "running", "正在使用本次已确认候选生成正式报价", ""
        self.active_quote_job = job
        self._refresh_quote_queue()
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

    def _complete_direct_quote(self, job: QuoteJob) -> None:
        self._quote_pdf_exporting = False
        self.active_quote_job = None
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
            gaps.refresh_optionhelper_result(
                gap_file, oh, html_path=str(report_file), pdf_path=pdf,
                pdf_pages=pages, pdf_error=export_error,
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
            self.active_quote_job = None
            self._refresh_quote_queue()
            self.status.setText(f"{job.job_id}：{job.message}")
            QTimer.singleShot(0, self._pump_quote_queue)
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
                report_file = Path(self._report_artifact(self.last_summary))
                try:
                    html = report_file.read_text(encoding="utf-8")
                    # 研究报告先交付时没有 OptionHelper 结构；报价完成后必须把
                    # “推荐结构 + 报价表”整体插入页脚之前。旧实现仅把表格追加到
                    # 文末，因而页脚跑到表格上方且结构建议缺失。
                    html = re.sub(r'<section class="recommendation">.*?</section>\s*', "", html, flags=re.DOTALL)
                    html = re.sub(r'<section class="quote">.*?</section>\s*', "", html, flags=re.DOTALL)
                    append = report_layout._recommendation_block(oh) + report_layout._quote_block(oh)
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
        self._complete_direct_quote(job)

    def edit_llm_settings(self) -> None:
        dialog = LlmSettingsDialog(self)
        if dialog.exec():
            QMessageBox.information(self, "LLM 设置已保存", "新设置会在下一次分析任务启动时生效。")

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
            elif line.startswith("EVENT_EVIDENCE_REQUIRED="):
                try:
                    payload = json.loads(line.split("=", 1)[1])
                except json.JSONDecodeError:
                    payload = {}
                self.event_evidence_message = str(payload.get("message") or "事件证据不足，未生成报告。")
                missing = payload.get("missing") or []
                text = self.event_evidence_message + "\n\n未生成报告。请通过“管理事件证据”从已上传材料导入原文，或手动补录后重跑。"
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
            if delivery and not delivery.startswith("通过"):
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
    window = ResearchHelperWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
