"""Chat view widget using QWebEngineView for Markdown/LaTeX/code rendering."""

import json
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QPoint, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWidgets import QMenu
from qfluentwidgets import FluentIcon
from src.config import HIGHLIGHT_THEME_PATH, RESOURCE_DIR


class _ChatBridge(QObject):
    copyRequested = Signal(int)
    editRequested = Signal(int, str)
    regenerateRequested = Signal(int)
    toolApprovalRequested = Signal(str, bool)

    @Slot(int)
    def copyMessage(self, index: int):
        self.copyRequested.emit(index)

    @Slot(int, str)
    def editMessage(self, index: int, text: str):
        self.editRequested.emit(index, text)

    @Slot(int)
    def regenerateMessage(self, index: int):
        self.regenerateRequested.emit(index)

    @Slot(str)
    def approveTool(self, tool_id: str):
        self.toolApprovalRequested.emit(tool_id, True)

    @Slot(str)
    def denyTool(self, tool_id: str):
        self.toolApprovalRequested.emit(tool_id, False)


class _ChatPage(QWebEnginePage):
    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked and url.isValid():
            QDesktopServices.openUrl(url)
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)


class ChatView(QWebEngineView):
    copy_requested = Signal(int)
    edit_requested = Signal(int, str)
    regenerate_requested = Signal(int)
    tool_approval_requested = Signal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loaded = False
        self._pending_js: list[str] = []
        self.setPage(_ChatPage(self))
        self._channel = QWebChannel(self.page())
        self._bridge = _ChatBridge(self)
        self._channel.registerObject("chatBridge", self._bridge)
        self.page().setWebChannel(self._channel)
        self._bridge.copyRequested.connect(self.copy_requested)
        self._bridge.editRequested.connect(self.edit_requested)
        self._bridge.regenerateRequested.connect(self.regenerate_requested)
        self._bridge.toolApprovalRequested.connect(self.tool_approval_requested)
        # Prevent flicker when parent has WA_TranslucentBackground
        self.page().setBackgroundColor(QColor(255, 255, 255))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        html_path = RESOURCE_DIR / "chat.html"
        self.setUrl(QUrl.fromLocalFile(str(html_path)))
        self.loadFinished.connect(self._on_load_finished)

    def _on_load_finished(self, ok: bool):
        self._loaded = ok
        if ok:
            self.page().runJavaScript(self._build_icons_script())
            self.page().runJavaScript(self._build_highlight_theme_script())
            while self._pending_js:
                self.page().runJavaScript(self._pending_js.pop(0))

    def _build_icons_script(self) -> str:
        icons = {
            "edit": self._icon_data_uri(FluentIcon.EDIT),
            "copy": self._icon_data_uri(FluentIcon.COPY),
            "refresh": self._icon_data_uri(FluentIcon.SYNC),
            "check": self._icon_data_uri(FluentIcon.ACCEPT_MEDIUM),
            "tool_read": self._icon_data_uri(FluentIcon.DOCUMENT),
            "tool_search": self._icon_data_uri(FluentIcon.SEARCH),
            "tool_write": self._icon_data_uri(FluentIcon.EDIT),
            "tool_add": self._icon_data_uri(FluentIcon.ADD),
            "tool_replace": self._icon_data_uri(FluentIcon.EDIT),
            "tool_command": self._icon_data_uri(FluentIcon.DEVELOPER_TOOLS),
            "tool_capture": self._icon_data_uri(FluentIcon.CAMERA),
            "tool_web-fetch": self._icon_data_uri(FluentIcon.GLOBE),
            "tool_web-search": self._icon_data_uri(FluentIcon.SEARCH),
            "tool_clipboard": self._icon_data_uri(FluentIcon.COPY),
            "tool_client_list": self._icon_data_uri(FluentIcon.FOLDER),
            "tool_client_connect": self._icon_data_uri(FluentIcon.LINK),
            "tool_client_command": self._icon_data_uri(FluentIcon.DEVELOPER_TOOLS),
            "tool_client_disconnect": self._icon_data_uri(FluentIcon.CANCEL_MEDIUM),
        }
        return f"setActionIcons({json.dumps(icons, ensure_ascii=False)});"

    def _build_highlight_theme_script(self) -> str:
        theme = self._load_highlight_theme()
        return f"setHighlightTheme({json.dumps(theme, ensure_ascii=False)});"

    @staticmethod
    def _load_highlight_theme() -> dict:
        if not HIGHLIGHT_THEME_PATH.exists():
            return {}
        try:
            data = json.loads(HIGHLIGHT_THEME_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _icon_data_uri(icon) -> str:
        pixmap = icon.qicon().pixmap(16, 16)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        pixmap.save(buffer, "PNG")
        return f"data:image/png;base64,{bytes(data.toBase64()).decode('ascii')}"

    @staticmethod
    def _to_js_arg(value) -> str:
        return json.dumps(value, ensure_ascii=False)

    def _run_js(self, js: str):
        if self._loaded:
            self.page().runJavaScript(js)
        else:
            self._pending_js.append(js)

    def add_message(self, role: str, content: str, index: int):
        self._run_js(
            f"renderMessage({self._to_js_arg(role)}, {self._to_js_arg(content)}, {index});"
        )

    def add_tool_message(self, payload: dict):
        self._run_js(f"renderToolMessage({self._to_js_arg(payload)});")

    def update_tool_message(self, payload: dict):
        self._run_js(f"updateToolMessage({self._to_js_arg(payload)});")

    def start_stream(self, index: int):
        self._run_js(f"startStream({index});")

    def append_token(self, token: str):
        self._run_js(f"appendToken({self._to_js_arg(token)});")

    def finish_stream(self):
        self._run_js("finishStream();")

    def clear_chat(self):
        self._run_js("clearChat();")

    def set_theme(self, primary_color: str, user_color: str, ai_color: str, dark_mode: bool = False):
        self.page().setBackgroundColor(QColor("#181818") if dark_mode else QColor(255, 255, 255))
        self._run_js(
            f"setTheme({self._to_js_arg(primary_color)}, {self._to_js_arg(user_color)}, {self._to_js_arg(ai_color)}, {json.dumps(bool(dark_mode))});"
        )

    def apply_highlight_theme(self):
        self._run_js(self._build_highlight_theme_script())

    def _show_context_menu(self, pos: QPoint):
        menu = QMenu(self)
        copy_action = menu.addAction("复制")
        copy_action.setEnabled(bool(self.page().selectedText()))
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is copy_action:
            self.page().triggerAction(self.page().WebAction.Copy)
