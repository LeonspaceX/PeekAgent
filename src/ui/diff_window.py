"""Standalone diff viewer backed by Diff2Html."""

from __future__ import annotations

import difflib
import json
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QUrl
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMenu, QVBoxLayout, QWidget

from src.config import RESOURCE_DIR


class DiffWindow(QWidget):
    _WM_ENTERSIZEMOVE = 0x0231
    _WM_EXITSIZEMOVE = 0x0232

    def __init__(self, path: str, before: str, after: str, target_screen=None):
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle("查看更改")
        self.setMinimumSize(520, 360)
        self.resize(820, 560)
        self._centered = False
        self._target_screen = target_screen

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._view = QWebEngineView(self)
        self._view.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self._view)

        self._diff = self._build_diff(path, before, after)
        self._view.loadFinished.connect(self._on_loaded)
        self._view.setUrl(QUrl.fromLocalFile(str(RESOURCE_DIR / "diff.html")))

    def showEvent(self, event):
        if not self._centered:
            self._centered = True
            screen = self._target_screen or QApplication.primaryScreen()
            if screen is not None:
                area = screen.availableGeometry()
                geometry = self.frameGeometry()
                geometry.moveCenter(area.center())
                self.move(geometry.topLeft())
        super().showEvent(event)

    @staticmethod
    def _build_diff(path: str, before: str, after: str) -> str:
        display_path = Path(path).as_posix()
        lines = list(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile=f"a/{display_path}",
                tofile=f"b/{display_path}",
                lineterm="",
            )
        )
        return "\n".join(lines) + ("\n" if lines else "")

    def _on_loaded(self, ok: bool):
        if ok:
            self._view.page().runJavaScript(
                f"renderDiff({json.dumps(self._diff, ensure_ascii=False)});"
            )

    def _show_context_menu(self, pos: QPoint):
        menu = QMenu(self._view)
        copy_action = menu.addAction("复制")
        copy_action.setEnabled(bool(self._view.page().selectedText()))
        chosen = menu.exec(self._view.mapToGlobal(pos))
        if chosen is copy_action:
            self._view.page().triggerAction(self._view.page().WebAction.Copy)

    def nativeEvent(self, event_type, message):
        if event_type in {
            b"windows_generic_MSG",
            b"windows_dispatcher_MSG",
            "windows_generic_MSG",
            "windows_dispatcher_MSG",
        }:
            try:
                msg = wintypes.MSG.from_address(int(message)).message
            except (TypeError, ValueError, OSError):
                msg = 0
            if msg == self._WM_ENTERSIZEMOVE:
                self._view.setUpdatesEnabled(False)
            elif msg == self._WM_EXITSIZEMOVE:
                self._view.setUpdatesEnabled(True)
                self._view.update()
        return super().nativeEvent(event_type, message)
