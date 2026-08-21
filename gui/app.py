"""最小可用桌面界面：客户输入、实时阶段、结果与人工补数入口。

界面不重写研究流程：它启动同一份 ``main.py``，并消费 ``output/runs`` 的结构化运行记录。
这样命令行、未来打包版和 GUI 对一次运行的状态解释完全一致。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMainWindow,
    QMessageBox, QPushButton, QPlainTextEdit, QProgressBar, QSplitter, QVBoxLayout, QWidget,
)

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "output" / "runs"
LOCAL_CONFIG = ROOT / "config.local.json"


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class JsonEditor(QDialog):
    def __init__(self, parent: QWidget | None = None, initial_path: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("人工补数 JSON")
        self.resize(740, 520)
        self.path = Path(initial_path) if initial_path else None
        self.editor = QPlainTextEdit()
        template = {"字段覆盖": {}, "外部事实": {}}
        if self.path and self.path.is_file():
            try:
                self.editor.setPlainText(self.path.read_text(encoding="utf-8"))
            except OSError:
                self.editor.setPlainText(json.dumps(template, ensure_ascii=False, indent=2))
        else:
            self.editor.setPlainText(json.dumps(template, ensure_ascii=False, indent=2))
        hint = QLabel("字段覆盖必须含“值”和“来源”；判定字段不可人工覆盖。保存后可作为本次运行的补数文件。")
        hint.setWordWrap(True)
        save = QPushButton("保存…")
        add_field = QPushButton("添加字段覆盖…")
        add_fact = QPushButton("添加外部事实…")
        close = QPushButton("关闭")
        save.clicked.connect(self.save)
        add_field.clicked.connect(self.add_field)
        add_fact.clicked.connect(self.add_fact)
        close.clicked.connect(self.reject)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(add_field)
        actions.addWidget(add_fact)
        actions.addWidget(save)
        actions.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(self.editor)
        layout.addLayout(actions)

    def _data(self) -> dict:
        try:
            value = json.loads(self.editor.toPlainText())
            return value if isinstance(value, dict) else {"字段覆盖": {}, "外部事实": {}}
        except json.JSONDecodeError:
            QMessageBox.warning(self, "JSON 无效", "请先修正 JSON 后再使用表单添加。")
            return {}

    def _write_data(self, value: dict) -> None:
        value.setdefault("字段覆盖", {})
        value.setdefault("外部事实", {})
        self.editor.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))

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
        if self.original == "A股":
            self.mode.setCurrentIndex(0)

        self.market = QComboBox()
        self.market.addItems(["A股", "港股", "跨市场"])
        self.scope = QLineEdit(str(payload.get("proposed_scope") or ""))
        self.scope.setPlaceholderText("多个 A 股一级行业用“、”分隔；不能只填数字")
        self.underlying = QComboBox()
        self.underlying.setEditable(True)
        self.underlying.addItem("", {"code": "", "name": ""})
        for item in payload.get("suggested_instruments") or []:
            label = f"{item.get('code', '')}｜{item.get('name', '')}｜{item.get('note', '')}"
            self.underlying.addItem(label, item)
        self.underlying.lineEdit().setPlaceholderText("可输入白名单外代码，例如 513050.SH")
        self.reason = QLineEdit(str(payload.get("reason") or ""))
        self.reason.setPlaceholderText("选填：说明为什么采用这个研究口径/标的")

        errors = payload.get("errors") or []
        self.message = QLabel(
            ("上次校验未通过：\n• " + "\n• ".join(errors)) if errors
            else str(payload.get("notice") or ""))
        self.message.setWordWrap(True)
        self.message.setStyleSheet("color:#a61b29;" if errors else "color:#666;")
        confirm, cancel = QPushButton("校验并继续"), QPushButton("取消本次运行")
        confirm.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        self.mode.currentIndexChanged.connect(self._sync_mode)

        form = QFormLayout(self)
        form.addRow("解析主题", self.topic)
        form.addRow("处理方式", self.mode)
        form.addRow("确认市场", self.market)
        form.addRow("研究口径", self.scope)
        form.addRow("ETF / 指数", self.underlying)
        form.addRow("映射理由", self.reason)
        form.addRow("", self.message)
        form.addRow("", ResearchHelperWindow._row(confirm, cancel))
        self._sync_mode()

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

    def value(self) -> dict:
        raw = self.underlying.currentText().strip()
        data = self.underlying.currentData() if self.underlying.currentIndex() >= 0 else {}
        data = data if isinstance(data, dict) else {}
        code = str(data.get("code") or raw.split("｜", 1)[0]).strip().upper()
        name = str(data.get("name") or "").strip()
        return {
            "market": self.market.currentText(),
            "research_scope": self.scope.text().strip(),
            "underlying_code": "" if self.mode.currentData() == "research_only" else code,
            "underlying_name": "" if self.mode.currentData() == "research_only" else name,
            "research_only": self.mode.currentData() == "research_only",
            "reason": self.reason.text().strip(),
        }

class ResearchHelperWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Research Helper · 场外衍生品一页通")
        self.resize(1280, 820)
        self.process: QProcess | None = None
        self.run_id = ""
        self.override_path = ""
        self.last_summary: dict = {}
        self._output_buffer = ""

        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("输入客户需求，例如：根据目前酒ETF 512690.SH 的市场情况推荐产品")
        self.prompt.setMinimumHeight(110)
        self.horizon = QLineEdit("3个月")
        self.max_loss = QLineEdit("100%")
        self.principal = QComboBox()
        self.principal.addItem("接受本金波动", "yes")
        self.principal.addItem("不接受本金波动", "no")
        self.preference = QLineEdit()
        self.preference.setPlaceholderText("例如：更偏上涨参与（可选）")
        self.quote = QCheckBox("生成 OptionHelper 正式报价")
        self.pdf = QCheckBox("额外导出 PDF")
        self.override_label = QLabel("未选择人工补数文件")
        self.override_label.setWordWrap(True)
        choose_override = QPushButton("选择补数 JSON…")
        edit_override = QPushButton("新建/编辑补数 JSON…")
        llm_settings = QPushButton("LLM 设置…")
        choose_override.clicked.connect(self.choose_override)
        edit_override.clicked.connect(self.edit_override)
        llm_settings.clicked.connect(self.edit_llm_settings)
        self.run_button = QPushButton("开始生成")
        self.run_button.clicked.connect(self.start_run)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.status = QLabel("待运行")
        self.status.setWordWrap(True)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.artifacts = QListWidget()
        self.artifacts.itemDoubleClicked.connect(self.open_artifact)
        self.history = QComboBox()
        self.history.currentIndexChanged.connect(self.show_selected_history)
        self.review = QPlainTextEdit(); self.review.setReadOnly(True)
        rerun = QPushButton("按本次需求重跑")
        rerun.clicked.connect(self.rerun_selected)
        refresh = QPushButton("刷新运行记录")
        refresh.clicked.connect(self.refresh_history)

        form = QFormLayout()
        form.addRow("客户需求", self.prompt)
        form.addRow("期限", self.horizon)
        form.addRow("最大损失", self.max_loss)
        form.addRow("本金波动", self.principal)
        form.addRow("收益偏好", self.preference)
        form.addRow("交付选项", self._row(self.quote, self.pdf))
        form.addRow("人工补数", self._row(self.override_label, choose_override, edit_override))
        form.addRow("分析模型", self._row(llm_settings))
        form.addRow("", self.run_button)

        input_card = QFrame()
        input_card.setFrameShape(QFrame.Shape.StyledPanel)
        input_card.setLayout(form)
        run_box = QVBoxLayout()
        run_box.addWidget(input_card)
        run_box.addWidget(self.status)
        run_box.addWidget(self.progress)
        run_box.addWidget(QLabel("实时输出"))
        run_box.addWidget(self.log, 1)
        run_widget = QWidget()
        run_widget.setLayout(run_box)

        result_box = QVBoxLayout()
        result_box.addWidget(QLabel("本次运行摘要"))
        result_box.addWidget(self.summary, 2)
        result_box.addWidget(QLabel("交付文件（双击打开）"))
        result_box.addWidget(self.artifacts, 1)
        result_box.addWidget(QLabel("历史运行"))
        result_box.addWidget(self._row(self.history, refresh))
        result_box.addWidget(QLabel("内部底稿复核"))
        result_box.addWidget(self.review, 3)
        result_box.addWidget(rerun)
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
        path, _ = QFileDialog.getOpenFileName(self, "选择人工补数 JSON", self.override_path or str(ROOT), "JSON (*.json)")
        if path:
            self.override_path = path
            self.override_label.setText(path)

    def edit_override(self) -> None:
        dialog = JsonEditor(self, self.override_path)
        if dialog.exec() and dialog.path:
            self.override_path = str(dialog.path)
            self.override_label.setText(self.override_path)

    def edit_llm_settings(self) -> None:
        dialog = LlmSettingsDialog(self)
        if dialog.exec():
            QMessageBox.information(self, "LLM 设置已保存", "新设置会在下一次分析任务启动时生效。")

    def start_run(self) -> None:
        if self.process is not None:
            return
        request = self.prompt.toPlainText().strip()
        if not request:
            QMessageBox.information(self, "缺少需求", "请先输入客户需求。")
            return
        if not self.horizon.text().strip() or not self.max_loss.text().strip():
            QMessageBox.information(self, "缺少客户条件", "请填写期限和最大损失；或保留默认值。")
            return
        args = [str(ROOT / "main.py"), "-b", request,
                "--confirm-market",
                "--horizon", self.horizon.text().strip(),
                "--max-loss", self.max_loss.text().strip(),
                "--principal-fluctuation", str(self.principal.currentData())]
        if self.preference.text().strip():
            args += ["--return-preference", self.preference.text().strip()]
        if self.quote.isChecked():
            args += ["--optionhelper", "quote"]
        if self.pdf.isChecked():
            args.append("--pdf")
        if self.override_path:
            args += ["--overrides", self.override_path]
        self.run_id = ""
        self.last_summary = {}
        self._output_buffer = ""
        self.log.clear()
        self.summary.clear()
        self.artifacts.clear()
        self.status.setText("正在启动…")
        self.progress.setRange(0, 0)
        self.run_button.setEnabled(False)
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
                    self.process.write((json.dumps(dialog.value(), ensure_ascii=False) + "\n").encode("utf-8"))
                    self.status.setText("正在校验分析师确认…")
                else:
                    self.process.write(b'{"cancelled":true}\n')
                    self.status.setText("已取消市场/行业确认")
            elif line.startswith("MARKET_CONFIRMATION_REJECTED="):
                self.status.setText("确认未通过；请根据校验原因修改后重试")
        self.refresh_active_summary()

    def process_error(self, _error) -> None:
        if self.process:
            self.status.setText("启动失败：" + self.process.errorString())

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
        self.run_button.setEnabled(True)
        self.progress.setRange(0, 1)
        self.progress.setValue(1 if exit_code == 0 else 0)
        if self.last_summary:
            self.status.setText(f"完成：{self.last_summary.get('status')}（退出码 {exit_code}）")
        else:
            self.status.setText(f"进程结束（退出码 {exit_code}）；未找到运行摘要。")
        self.process = None
        self.refresh_history()

    def show_summary(self, data: dict) -> None:
        self.last_summary = data
        compact = {
            "run_id": data.get("run_id"), "status": data.get("status"),
            "duration_seconds": data.get("duration_seconds"), "metadata": data.get("metadata", {}),
            "stages": [{key: item.get(key) for key in ("label", "status", "duration_seconds", "error")}
                       for item in data.get("stages", [])],
            "recovery_actions": data.get("recovery_actions", []),
        }
        self.summary.setPlainText(json.dumps(compact, ensure_ascii=False, indent=2))
        self.artifacts.clear()
        for name, path in (data.get("artifacts") or {}).items():
            self.artifacts.addItem(f"{name}｜{path}")

    def open_artifact(self, item) -> None:
        path = item.text().split("｜", 1)[-1]
        if Path(path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def refresh_history(self) -> None:
        RUNS.mkdir(parents=True, exist_ok=True)
        current = self.history.currentData()
        self.history.blockSignals(True)
        self.history.clear()
        for path in sorted(RUNS.glob("run-*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            data = _load_json(path)
            self.history.addItem(f"{data.get('run_id', path.stem)} · {data.get('status', '—')}", str(path))
        index = self.history.findData(current) if current else 0
        if index >= 0:
            self.history.setCurrentIndex(index)
        self.history.blockSignals(False)
        self.show_selected_history()

    def show_selected_history(self) -> None:
        path = self.history.currentData()
        if path and (not self.process or path != str(RUNS / f"{self.run_id}.json")):
            data = _load_json(Path(path))
            self.show_summary(data)
            draft = next((value for name, value in (data.get("artifacts") or {}).items() if "内部底稿" in name), "")
            try:
                self.review.setPlainText(Path(draft).read_text(encoding="utf-8") if draft else "该运行未生成内部底稿。")
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
