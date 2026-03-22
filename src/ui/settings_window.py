"""Settings window – Phase 1: General + Appearance + Model + About."""

import json
import math
import requests
import shutil
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QStackedWidget,
    QFormLayout, QFrame, QButtonGroup, QFileDialog, QScrollArea,
)
from qfluentwidgets import (
    LineEdit, ComboBox, PushButton, ToolButton, FluentIcon,
    SwitchButton, SubtitleLabel, StrongBodyLabel, BodyLabel, InfoBar, InfoBarPosition,
    ListWidget, ColorPickerButton, PrimaryPushButton, RadioButton, ProgressBar,
)
from src.config import Settings, PROMPT_DIR, HIGHLIGHT_THEME_PATH, RESOURCE_DIR
from src.llm_client import LLMClient


class _AsyncWorker(QThread):
    """Generic worker that runs a callable in a background thread."""
    finished = Signal(object)  # result
    errored = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            result = self._fn()
            self.finished.emit(result)
        except Exception as e:
            self.errored.emit(str(e))


class SettingsWindow(QWidget):
    settings_saved = Signal()
    reset_window_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = Settings()
        self.setWindowTitle("PeekAgent 设置")
        self.setWindowFlags(Qt.WindowType.Window)
        self.resize(600, 450)
        self._init_ui()
        self._load_values()

    def _init_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Left nav
        self.nav_list = ListWidget(self)
        self.nav_list.setFixedWidth(140)
        for name in ["通用", "外观", "模型", "Tavily", "工具", "提示词", "关于"]:
            self.nav_list.addItem(name)
        self.nav_list.currentRowChanged.connect(self._switch_page)
        layout.addWidget(self.nav_list)

        # Right content
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self._build_general_page())
        self.stack.addWidget(self._build_appearance_page())
        self.stack.addWidget(self._build_model_page())
        self.stack.addWidget(self._build_tavily_page())
        self.stack.addWidget(self._wrap_scroll(self._build_tools_page()))
        self.stack.addWidget(self._build_prompt_page())
        self.stack.addWidget(self._build_about_page())
        layout.addWidget(self.stack, 1)

        self.nav_list.setCurrentRow(0)

    def _switch_page(self, index: int):
        self.stack.setCurrentIndex(index)

    def _wrap_scroll(self, page: QWidget) -> QScrollArea:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    def _build_general_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(20, 20, 20, 20)
        form.setSpacing(12)

        form.addRow(SubtitleLabel("通用设置"))

        self.hotkey_edit = LineEdit(self)
        self.hotkey_edit.setPlaceholderText("例如: alt+z")
        form.addRow("全局快捷键:", self.hotkey_edit)

        self.auto_start_switch = SwitchButton(self)
        form.addRow("开机自启:", self.auto_start_switch)

        self.always_top_switch = SwitchButton(self)
        form.addRow("窗口置顶:", self.always_top_switch)

        self.reset_window_btn = PushButton("重置窗口位置与大小", self)
        self.reset_window_btn.clicked.connect(self._reset_window_geometry)
        form.addRow("窗口布局:", self.reset_window_btn)

        self.external_prompt_editor_switch = SwitchButton(self)
        form.addRow("外部prompt编辑:", self.external_prompt_editor_switch)

        return page

    def _build_appearance_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(20, 20, 20, 20)
        form.setSpacing(12)

        form.addRow(SubtitleLabel("外观设置"))

        self.primary_theme_color_edit = LineEdit(self)
        self.primary_theme_color_edit.setPlaceholderText("#0ea5a4")
        form.addRow("主要主题色:", self.primary_theme_color_edit)

        self.theme_color1_edit = LineEdit(self)
        self.theme_color1_edit.setPlaceholderText("#1a73e8")
        form.addRow("用户气泡色:", self.theme_color1_edit)

        self.theme_color2_edit = LineEdit(self)
        self.theme_color2_edit.setPlaceholderText("#7c3aed")
        form.addRow("AI 气泡色:", self.theme_color2_edit)

        highlight_row = QHBoxLayout()
        highlight_row.setSpacing(8)
        self.import_highlight_btn = PushButton("导入 JSON", self)
        self.import_highlight_btn.clicked.connect(self._import_highlight_theme)
        highlight_row.addWidget(self.import_highlight_btn)

        self.restore_highlight_btn = PushButton("恢复默认", self)
        self.restore_highlight_btn.clicked.connect(self._restore_default_highlight_theme)
        highlight_row.addWidget(self.restore_highlight_btn)
        form.addRow("代码高亮主题:", highlight_row)

        return page

    def _build_model_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(20, 20, 20, 20)
        form.setSpacing(12)

        form.addRow(SubtitleLabel("模型设置"))

        self.endpoint_url_edit = LineEdit(self)
        self.endpoint_url_edit.setPlaceholderText("https://api.openai.com/v1")
        form.addRow("端点 URL:", self.endpoint_url_edit)

        self.api_key_edit = LineEdit(self)
        self.api_key_edit.setPlaceholderText("sk-...")
        self.api_key_edit.setEchoMode(LineEdit.EchoMode.Password)
        form.addRow("API Key:", self.api_key_edit)

        endpoint_row = QHBoxLayout()
        endpoint_row.setSpacing(12)
        self.endpoint_type_group = QButtonGroup(self)
        self.openai_radio = RadioButton("OpenAI", self)
        self.anthropic_radio = RadioButton("Anthropic", self)
        self.endpoint_type_group.addButton(self.openai_radio)
        self.endpoint_type_group.addButton(self.anthropic_radio)
        endpoint_row.addWidget(self.openai_radio)
        endpoint_row.addWidget(self.anthropic_radio)
        endpoint_row.addStretch()
        form.addRow("端点格式:", endpoint_row)

        # Model selection row
        model_row = QHBoxLayout()
        self.model_input_container = QWidget(self)
        self.model_input_container.setFixedHeight(36)
        self.model_input_layout = QHBoxLayout(self.model_input_container)
        self.model_input_layout.setContentsMargins(0, 0, 0, 0)
        self.model_input_layout.setSpacing(0)
        self.model_combo = ComboBox(self)
        self.model_combo.setFixedHeight(36)
        self.model_combo.setMinimumWidth(200)
        self.model_combo.setMaximumWidth(420)
        self.model_edit = LineEdit(self)
        self.model_edit.setFixedHeight(36)
        self.model_edit.setMaximumWidth(420)
        self.model_edit.setPlaceholderText("手动输入模型名")
        self.model_input_layout.addWidget(self.model_combo)
        self.model_input_layout.addWidget(self.model_edit)
        model_row.addWidget(self.model_input_container, 1)

        self.fetch_models_btn = ToolButton(FluentIcon.SYNC, self)
        self.fetch_models_btn.setFixedSize(32, 32)
        self.fetch_models_btn.clicked.connect(self._fetch_models)
        model_row.addWidget(self.fetch_models_btn)

        self.test_btn = ToolButton(FluentIcon.DEVELOPER_TOOLS, self)
        self.test_btn.setFixedSize(32, 32)
        self.test_btn.clicked.connect(self._test_connection)
        model_row.addWidget(self.test_btn)

        form.addRow("模型:", model_row)

        self.stream_switch = SwitchButton(self)
        form.addRow("流式输出:", self.stream_switch)

        return page

    def _build_tavily_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        layout.addWidget(SubtitleLabel("Tavily 配置"))

        key_row = QHBoxLayout()
        key_row.setSpacing(8)
        self.tavily_api_key_edit = LineEdit(self)
        self.tavily_api_key_edit.setPlaceholderText("tvly-...")
        self.tavily_api_key_edit.setEchoMode(LineEdit.EchoMode.Password)
        key_row.addWidget(self.tavily_api_key_edit, 1)

        self.tavily_refresh_btn = PushButton("刷新", self)
        self.tavily_refresh_btn.setIcon(FluentIcon.SYNC)
        self.tavily_refresh_btn.clicked.connect(self._refresh_tavily_usage)
        key_row.addWidget(self.tavily_refresh_btn)
        layout.addLayout(key_row)

        layout.addWidget(StrongBodyLabel("额度用量"))

        self.tavily_usage_bar = ProgressBar(self)
        self.tavily_usage_bar.setRange(0, 1000)
        self.tavily_usage_bar.setValue(0)
        layout.addWidget(self.tavily_usage_bar)

        self.tavily_usage_text = BodyLabel("点击刷新以获取用量", self)
        layout.addWidget(self.tavily_usage_text)

        self.tavily_plan_text = BodyLabel("套餐类型：未获取", self)
        layout.addWidget(self.tavily_plan_text)

        layout.addStretch()
        return page

    def _build_tools_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setContentsMargins(20, 20, 20, 20)
        form.setSpacing(16)

        form.addRow(SubtitleLabel("工具设置"))

        self.read_switch = SwitchButton(self)
        form.addRow("读取文件:", self.read_switch)

        self.search_switch = SwitchButton(self)
        form.addRow("搜索文本:", self.search_switch)

        self.find_switch = SwitchButton(self)
        form.addRow("查找界面元素:", self.find_switch)

        self.capture_switch = SwitchButton(self)
        form.addRow("截图:", self.capture_switch)

        self.web_search_switch = SwitchButton(self)
        form.addRow("联网搜索:", self.web_search_switch)

        self.clipboard_switch = SwitchButton(self)
        form.addRow("写入剪贴板:", self.clipboard_switch)

        self.write_mode_group, write_row = self._create_mode_row()
        form.addRow("写入文件:", write_row)

        self.add_mode_group, add_row = self._create_mode_row()
        form.addRow("追加内容:", add_row)

        self.replace_mode_group, replace_row = self._create_mode_row()
        form.addRow("替换内容:", replace_row)

        self.command_mode_group, command_row = self._create_mode_row()
        form.addRow("执行命令:", command_row)

        self.click_mode_group, click_row = self._create_mode_row()
        form.addRow("点击:", click_row)

        self.scroll_mode_group, scroll_row = self._create_mode_row()
        form.addRow("滚动:", scroll_row)

        self.input_mode_group, input_row = self._create_mode_row()
        form.addRow("输入文本:", input_row)

        self.press_mode_group, press_row = self._create_mode_row()
        form.addRow("按键:", press_row)

        self.select_mode_group, select_row = self._create_mode_row()
        form.addRow("拖拽选择:", select_row)

        self.web_fetch_mode_group, web_fetch_row = self._create_mode_row()
        form.addRow("抓取网页:", web_fetch_row)

        self.command_output_limit_edit = LineEdit(self)
        self.command_output_limit_edit.setPlaceholderText("12000")
        self.command_output_limit_edit.setValidator(QIntValidator(100, 1000000, self))
        form.addRow("命令输出截断长度:", self.command_output_limit_edit)
        return page

    def _create_mode_row(self):
        row = QHBoxLayout()
        row.setSpacing(12)
        group = QButtonGroup(self)

        off = RadioButton("关闭", self)
        auto = RadioButton("自动", self)
        manual = RadioButton("手动审批", self)

        off.setProperty("modeValue", "off")
        auto.setProperty("modeValue", "auto")
        manual.setProperty("modeValue", "manual")

        for button in (off, auto, manual):
            group.addButton(button)
            row.addWidget(button)

        row.addStretch()
        return group, row

    def _build_prompt_page(self) -> QWidget:
        from qfluentwidgets import PlainTextEdit
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        layout.addWidget(SubtitleLabel("系统提示词"))
        layout.addWidget(BodyLabel("编辑 SYSTEM.md 的内容，作为每次对话的系统提示词。"))

        self.system_prompt_edit = PlainTextEdit(self)
        self.system_prompt_edit.setPlaceholderText("输入系统提示词...")
        system_path = PROMPT_DIR / "SYSTEM.md"
        if system_path.exists():
            self.system_prompt_edit.setPlainText(system_path.read_text(encoding="utf-8"))
        layout.addWidget(self.system_prompt_edit, 1)

        layout.addWidget(SubtitleLabel("记忆提示"))
        layout.addWidget(BodyLabel("编辑 MEMORY.md 的内容，它会以“你的记忆：{MEMORY.md}”的形式插入系统提示词。"))

        self.memory_prompt_edit = PlainTextEdit(self)
        self.memory_prompt_edit.setPlaceholderText("输入记忆内容...")
        memory_path = PROMPT_DIR / "MEMORY.md"
        if memory_path.exists():
            self.memory_prompt_edit.setPlainText(memory_path.read_text(encoding="utf-8"))
        layout.addWidget(self.memory_prompt_edit, 1)

        save_btn = PrimaryPushButton("保存", self)
        save_btn.clicked.connect(self._save_prompt)
        layout.addWidget(save_btn)

        return page

    def _save_prompt(self):
        PROMPT_DIR.mkdir(parents=True, exist_ok=True)
        system_path = PROMPT_DIR / "SYSTEM.md"
        memory_path = PROMPT_DIR / "MEMORY.md"
        system_path.write_text(self.system_prompt_edit.toPlainText(), encoding="utf-8")
        memory_path.write_text(self.memory_prompt_edit.toPlainText(), encoding="utf-8")
        InfoBar.success("已保存", "SYSTEM.md 和 MEMORY.md 已更新", parent=self,
                        position=InfoBarPosition.TOP, duration=3000)

    def _reset_window_geometry(self):
        self.settings.set("window", "x", 0)
        self.settings.set("window", "y", 0)
        self.settings.set("window", "width", 600)
        self.settings.set("window", "height", 800)
        self.reset_window_requested.emit()
        InfoBar.success("已重置", "窗口位置已恢复到 (0, 0)，大小已恢复到 600 x 800", parent=self,
                        position=InfoBarPosition.TOP, duration=3000)

    def _build_about_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        layout.addWidget(SubtitleLabel("关于 PeekAgent"))
        layout.addWidget(BodyLabel("版本: 0.1.0"))
        layout.addWidget(BodyLabel("快速唤起的悬浮 AI 助手"))
        layout.addStretch()

        return page

    # --- Load / Save ---

    def _load_values(self):
        s = self.settings
        self.hotkey_edit.setText(s.get("general", "hotkey", "alt+z"))
        self.auto_start_switch.setChecked(s.get("general", "auto_start", False))
        self.always_top_switch.setChecked(s.get("general", "always_on_top", True))
        self.external_prompt_editor_switch.setChecked(
            s.get("general", "external_prompt_editor_enabled", False)
        )

        self.primary_theme_color_edit.setText(s.get("appearance", "primary_theme_color", "#0ea5a4"))
        self.theme_color1_edit.setText(s.get("appearance", "theme_color_1", "#1a73e8"))
        self.theme_color2_edit.setText(s.get("appearance", "theme_color_2", "#7c3aed"))

        self.endpoint_url_edit.setText(s.get("model", "endpoint_url", ""))
        self.api_key_edit.setText(s.get("model", "api_key", ""))
        endpoint_type = s.get("model", "endpoint_type", "openai")
        self.openai_radio.setChecked(endpoint_type != "anthropic")
        self.anthropic_radio.setChecked(endpoint_type == "anthropic")
        model = s.get("model", "model_name", "")
        if model:
            self.model_combo.addItem(model)
            self.model_combo.setCurrentText(model)
            self.model_edit.setText(model)
        self._set_manual_model_input(False)
        self.stream_switch.setChecked(s.get("model", "stream", True))
        self.read_switch.setChecked(s.get("tools", "read_enabled", True))
        self.search_switch.setChecked(s.get("tools", "search_enabled", True))
        self.find_switch.setChecked(s.get("tools", "find_enabled", True))
        self.capture_switch.setChecked(s.get("tools", "capture_enabled", True))
        self.web_search_switch.setChecked(s.get("tools", "web_search_enabled", True))
        self.clipboard_switch.setChecked(s.get("tools", "clipboard_enabled", True))
        self.tavily_api_key_edit.setText(s.get("integrations", "tavily_api_key", ""))
        self._set_mode_group(self.write_mode_group, s.get("tools", "write_mode", "manual"))
        self._set_mode_group(self.add_mode_group, s.get("tools", "add_mode", "manual"))
        self._set_mode_group(self.replace_mode_group, s.get("tools", "replace_mode", "manual"))
        self._set_mode_group(self.command_mode_group, s.get("tools", "command_mode", "manual"))
        self._set_mode_group(self.click_mode_group, s.get("tools", "click_mode", "manual"))
        self._set_mode_group(self.scroll_mode_group, s.get("tools", "scroll_mode", "manual"))
        self._set_mode_group(self.input_mode_group, s.get("tools", "input_mode", "manual"))
        self._set_mode_group(self.press_mode_group, s.get("tools", "press_mode", "manual"))
        self._set_mode_group(self.select_mode_group, s.get("tools", "select_mode", "manual"))
        self._set_mode_group(self.web_fetch_mode_group, s.get("tools", "web_fetch_mode", "manual"))
        self.command_output_limit_edit.setText(str(s.get("tools", "command_output_limit", 12000)))

    def _save_values(self):
        s = self.settings
        s.set("general", "hotkey", self.hotkey_edit.text())
        s.set("general", "auto_start", self.auto_start_switch.isChecked())
        s.set("general", "always_on_top", self.always_top_switch.isChecked())
        s.set("general", "external_prompt_editor_enabled", self.external_prompt_editor_switch.isChecked())

        s.set("appearance", "primary_theme_color", self.primary_theme_color_edit.text())
        s.set("appearance", "theme_color_1", self.theme_color1_edit.text())
        s.set("appearance", "theme_color_2", self.theme_color2_edit.text())

        s.set("model", "endpoint_url", self.endpoint_url_edit.text())
        s.set("model", "api_key", self.api_key_edit.text())
        s.set("model", "endpoint_type", self._current_endpoint_type())
        s.set("model", "model_name", self._current_model_text())
        s.set("model", "stream", self.stream_switch.isChecked())
        s.set("tools", "read_enabled", self.read_switch.isChecked())
        s.set("tools", "search_enabled", self.search_switch.isChecked())
        s.set("tools", "find_enabled", self.find_switch.isChecked())
        s.set("tools", "capture_enabled", self.capture_switch.isChecked())
        s.set("tools", "web_search_enabled", self.web_search_switch.isChecked())
        s.set("tools", "clipboard_enabled", self.clipboard_switch.isChecked())
        s.set("integrations", "tavily_api_key", self.tavily_api_key_edit.text())
        s.set("tools", "write_mode", self._mode_group_value(self.write_mode_group))
        s.set("tools", "add_mode", self._mode_group_value(self.add_mode_group))
        s.set("tools", "replace_mode", self._mode_group_value(self.replace_mode_group))
        s.set("tools", "command_mode", self._mode_group_value(self.command_mode_group))
        s.set("tools", "click_mode", self._mode_group_value(self.click_mode_group))
        s.set("tools", "scroll_mode", self._mode_group_value(self.scroll_mode_group))
        s.set("tools", "input_mode", self._mode_group_value(self.input_mode_group))
        s.set("tools", "press_mode", self._mode_group_value(self.press_mode_group))
        s.set("tools", "select_mode", self._mode_group_value(self.select_mode_group))
        s.set("tools", "web_fetch_mode", self._mode_group_value(self.web_fetch_mode_group))
        s.set("tools", "command_output_limit", self._command_output_limit_value())

    def _import_highlight_theme(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "导入高亮主题",
            str(HIGHLIGHT_THEME_PATH.parent),
            "JSON Files (*.json)",
        )
        if not file_path:
            return
        try:
            data = json.loads(Path(file_path).read_text(encoding="utf-8"))
        except Exception as exc:
            InfoBar.error("导入失败", str(exc), parent=self,
                          position=InfoBarPosition.TOP, duration=5000)
            return
        if not isinstance(data, dict):
            InfoBar.error("导入失败", "高亮主题必须是一个对象型 JSON", parent=self,
                          position=InfoBarPosition.TOP, duration=5000)
            return

        HIGHLIGHT_THEME_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, HIGHLIGHT_THEME_PATH)
        InfoBar.success("导入成功", "已覆盖 data/highlight.json", parent=self,
                        position=InfoBarPosition.TOP, duration=3000)
        self.settings_saved.emit()

    def _restore_default_highlight_theme(self):
        default_theme_path = RESOURCE_DIR / "highlight.json"
        if not default_theme_path.exists():
            InfoBar.error("恢复失败", "默认高亮主题不存在", parent=self,
                          position=InfoBarPosition.TOP, duration=5000)
            return
        HIGHLIGHT_THEME_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(default_theme_path, HIGHLIGHT_THEME_PATH)
        InfoBar.success("恢复成功", "已恢复默认高亮主题", parent=self,
                        position=InfoBarPosition.TOP, duration=3000)
        self.settings_saved.emit()

    def _refresh_tavily_usage(self):
        api_key = self.tavily_api_key_edit.text().strip()
        if not api_key:
            InfoBar.warning("提示", "请先填写 Tavily API Key", parent=self,
                            position=InfoBarPosition.TOP, duration=3000)
            return

        self.tavily_refresh_btn.setEnabled(False)
        self.tavily_usage_text.setText("正在刷新用量...")
        self.tavily_plan_text.setText("套餐类型：正在获取...")

        def do_fetch():
            response = requests.get(
                "https://api.tavily.com/usage",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=(10, 30),
            )
            response.raise_for_status()
            return response.json()

        self._tavily_usage_worker = _AsyncWorker(do_fetch, self)
        self._tavily_usage_worker.finished.connect(self._on_tavily_usage_fetched)
        self._tavily_usage_worker.errored.connect(self._on_tavily_usage_error)
        self._tavily_usage_worker.start()

    def _on_tavily_usage_fetched(self, data):
        self.tavily_refresh_btn.setEnabled(True)

        account = data.get("account") or {}
        used = account.get("plan_usage")
        limit = account.get("plan_limit")
        current_plan = account.get("current_plan") or "未返回"

        used_text = self._format_tavily_usage_value(used)
        limit_text = self._format_tavily_usage_value(limit)

        progress_value = 0
        if isinstance(used, (int, float)) and isinstance(limit, (int, float)) and limit > 0:
            progress_value = max(0, min(1000, math.floor((used / limit) * 1000)))

        self.tavily_usage_bar.setValue(progress_value)
        self.tavily_usage_text.setText(f"已用用量：{used_text} / {limit_text}")
        self.tavily_plan_text.setText(f"套餐类型：{current_plan}")

    def _on_tavily_usage_error(self, err):
        self.tavily_refresh_btn.setEnabled(True)
        self.tavily_usage_text.setText("点击刷新以获取用量")
        self.tavily_plan_text.setText("套餐类型：未获取")
        InfoBar.error("刷新失败", err, parent=self,
                      position=InfoBarPosition.TOP, duration=5000)

    def closeEvent(self, event):
        self._save_values()
        self.settings_saved.emit()
        super().closeEvent(event)

    def _fetch_models(self):
        url = self.endpoint_url_edit.text().strip().rstrip("/")
        key = self.api_key_edit.text().strip()
        if not url or not key:
            InfoBar.warning("提示", "请先填写端点 URL 和 API Key", parent=self,
                            position=InfoBarPosition.TOP, duration=3000)
            return
        self.fetch_models_btn.setEnabled(False)

        def do_fetch():
            client = LLMClient(url, key, self._current_endpoint_type())
            return client.fetch_models()

        self._fetch_worker = _AsyncWorker(do_fetch, self)
        self._fetch_worker.finished.connect(self._on_models_fetched)
        self._fetch_worker.errored.connect(self._on_models_error)
        self._fetch_worker.start()

    def _on_models_fetched(self, models):
        self.fetch_models_btn.setEnabled(True)
        if not models:
            self._set_manual_model_input(True)
            InfoBar.warning("未获取到模型", "端点返回了空模型列表，已切换为手动填写模型名。", parent=self,
                            position=InfoBarPosition.TOP, duration=5000)
            return
        current_model = self._current_model_text()
        self.model_combo.clear()
        self.model_combo.addItems(models)
        self._set_manual_model_input(False)
        if current_model and current_model in models:
            self.model_combo.setCurrentText(current_model)
            self.model_edit.setText(current_model)
        elif models:
            self.model_combo.setCurrentText(models[0])
            self.model_edit.setText(models[0])
        InfoBar.success("成功", f"获取到 {len(models)} 个模型", parent=self,
                        position=InfoBarPosition.TOP, duration=3000)

    def _on_models_error(self, err):
        self.fetch_models_btn.setEnabled(True)
        self._set_manual_model_input(True)
        InfoBar.warning("获取失败", f"{err}\n已切换为手动填写模型名。", parent=self,
                        position=InfoBarPosition.TOP, duration=5000)

    def _test_connection(self):
        url = self.endpoint_url_edit.text().strip().rstrip("/")
        key = self.api_key_edit.text().strip()
        model = self._current_model_text()
        if not url or not key or not model:
            InfoBar.warning("提示", "请先完整填写端点信息", parent=self,
                            position=InfoBarPosition.TOP, duration=3000)
            return
        self.test_btn.setEnabled(False)

        def do_test():
            client = LLMClient(url, key, self._current_endpoint_type())
            return client.test_connection(model)

        self._test_worker = _AsyncWorker(do_test, self)
        self._test_worker.finished.connect(self._on_test_ok)
        self._test_worker.errored.connect(self._on_test_error)
        self._test_worker.start()

    def _on_test_ok(self, ms):
        self.test_btn.setEnabled(True)
        InfoBar.success("测试成功", f"延迟: {ms} ms", parent=self,
                        position=InfoBarPosition.TOP, duration=3000)

    def _on_test_error(self, err):
        self.test_btn.setEnabled(True)
        InfoBar.error("测试失败", err, parent=self,
                      position=InfoBarPosition.TOP, duration=5000)

    def _current_endpoint_type(self) -> str:
        return "anthropic" if self.anthropic_radio.isChecked() else "openai"

    def _current_model_text(self) -> str:
        if self.model_edit.isVisible():
            return self.model_edit.text().strip()
        return self.model_combo.currentText().strip()

    def _set_manual_model_input(self, manual: bool):
        current = self._current_model_text()
        if manual:
            self.model_edit.setText(current)
            self.model_combo.hide()
            self.model_edit.show()
        else:
            if current and self.model_combo.findText(current) < 0:
                self.model_combo.addItem(current)
            if current:
                self.model_combo.setCurrentText(current)
            self.model_edit.hide()
            self.model_combo.show()

    @staticmethod
    def _mode_group_value(group: QButtonGroup) -> str:
        checked = group.checkedButton()
        if checked is None:
            return "manual"
        return checked.property("modeValue") or "manual"

    @staticmethod
    def _set_mode_group(group: QButtonGroup, value: str):
        for button in group.buttons():
            if button.property("modeValue") == value:
                button.setChecked(True)
                return
        buttons = group.buttons()
        if buttons:
            buttons[-1].setChecked(True)

    def _command_output_limit_value(self) -> int:
        text = self.command_output_limit_edit.text().strip()
        if not text:
            return 12000
        try:
            value = int(text)
        except ValueError:
            return 12000
        return max(100, value)

    @staticmethod
    def _format_tavily_usage_value(value) -> str:
        if value is None:
            return "--"
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if value.is_integer():
                return str(int(value))
            return f"{value:.2f}".rstrip("0").rstrip(".")
        return str(value)
