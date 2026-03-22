"""Input area widget: multi-line text input + send button + attachment."""

import ctypes
import os
import subprocess
import sys
import uuid
import tempfile
from pathlib import Path
from ctypes import wintypes

from PySide6.QtCore import Signal, Qt, QSize, QMimeData, QTimer, QThread
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QSizePolicy, QFileDialog,
    QLabel, QScrollArea,
)
from PySide6.QtGui import QKeyEvent, QImage, QPixmap, QDragEnterEvent, QDropEvent
from qfluentwidgets import PlainTextEdit, PushButton, ToolButton, FluentIcon, InfoBar, InfoBarPosition
from src.config import Settings


def _open_default_editor_and_wait(path: str):
    if os.name == "nt":
        SEE_MASK_NOCLOSEPROCESS = 0x00000040
        SW_SHOWNORMAL = 1
        INFINITE = 0xFFFFFFFF

        class SHELLEXECUTEINFOW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("fMask", wintypes.ULONG),
                ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR),
                ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR),
                ("nShow", ctypes.c_int),
                ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", wintypes.LPVOID),
                ("lpClass", wintypes.LPCWSTR),
                ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD),
                ("hIcon", wintypes.HANDLE),
                ("hProcess", wintypes.HANDLE),
            ]

        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        execute_info = SHELLEXECUTEINFOW()
        execute_info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
        execute_info.fMask = SEE_MASK_NOCLOSEPROCESS
        execute_info.lpVerb = "open"
        execute_info.lpFile = path
        execute_info.nShow = SW_SHOWNORMAL

        if not shell32.ShellExecuteExW(ctypes.byref(execute_info)):
            raise ctypes.WinError()
        if not execute_info.hProcess:
            raise RuntimeError("系统没有返回可等待的编辑器进程。")

        kernel32.WaitForSingleObject(execute_info.hProcess, INFINITE)
        kernel32.CloseHandle(execute_info.hProcess)
        return

    if sys.platform == "darwin":
        subprocess.run(["open", path], check=True)
        return

    subprocess.run(["xdg-open", path], check=True)


class _ExternalEditWorker(QThread):
    completed = Signal(str)
    errored = Signal(str)

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self._path = path

    def run(self):
        try:
            _open_default_editor_and_wait(self._path)
            self.completed.emit(self._path)
        except Exception as exc:
            self.errored.emit(str(exc))


class ChatInput(PlainTextEdit):
    """Multi-line input that sends on Enter, newline on Shift+Enter."""
    submit = Signal()
    files_dropped = Signal(list)  # list[str] paths

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("输入消息...")
        self.setFixedHeight(36)
        self._min_h = 36
        self._max_h = 120
        self.textChanged.connect(self._adjust_height)
        self.setAcceptDrops(True)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.setCursorWidth(2)
        self.cursorPositionChanged.connect(self._refresh_cursor)
        self.selectionChanged.connect(self._refresh_cursor)
        self._cursor_refresh_timer = QTimer(self)
        self._cursor_refresh_timer.setSingleShot(True)
        self._cursor_refresh_timer.timeout.connect(self.viewport().update)

    def keyPressEvent(self, e: QKeyEvent):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(e)
                self._refresh_cursor()
            else:
                e.accept()
                self.submit.emit()
            return
        super().keyPressEvent(e)
        self._refresh_cursor()

    def _refresh_cursor(self):
        self.viewport().update()
        self._cursor_refresh_timer.start(0)

    def _adjust_height(self):
        doc_h = int(self.document().size().height()) + 12
        new_h = max(self._min_h, min(doc_h, self._max_h))
        if new_h != self.height():
            self.setFixedHeight(new_h)
            self.ensureCursorVisible()

    # --- Drag & drop files ---

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls() or e.mimeData().hasImage():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dropEvent(self, e: QDropEvent):
        paths = []
        if e.mimeData().hasUrls():
            for url in e.mimeData().urls():
                if url.isLocalFile():
                    paths.append(url.toLocalFile())
        if paths:
            e.acceptProposedAction()
            self.files_dropped.emit(paths)
        else:
            super().dropEvent(e)

    # --- Paste image / file ---

    def canInsertFromMimeData(self, source: QMimeData) -> bool:
        if source.hasImage() or source.hasUrls():
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source: QMimeData):
        if source.hasImage():
            img = source.imageData()
            if isinstance(img, QImage) and not img.isNull():
                tmp_dir = Path(tempfile.gettempdir()) / "peekagent_paste"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                name = f"paste_{uuid.uuid4().hex[:8]}.png"
                path = str(tmp_dir / name)
                img.save(path)
                self.files_dropped.emit([path])
                return
        if source.hasUrls():
            paths = [u.toLocalFile() for u in source.urls() if u.isLocalFile()]
            if paths:
                self.files_dropped.emit(paths)
                return
        super().insertFromMimeData(source)


class AttachmentChip(QWidget):
    """Small chip showing filename + remove button."""
    removed = Signal(str)  # file path

    def __init__(self, filepath: str, parent=None):
        super().__init__(parent)
        self.filepath = filepath
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 2, 2)
        layout.setSpacing(4)

        name = os.path.basename(filepath)
        if len(name) > 20:
            name = name[:17] + "..."
        label = QLabel(name, self)
        label.setStyleSheet("font-size: 12px; color: #555;")
        layout.addWidget(label)

        close_btn = ToolButton(FluentIcon.CLOSE, self)
        close_btn.setFixedSize(18, 18)
        close_btn.clicked.connect(lambda: self.removed.emit(self.filepath))
        layout.addWidget(close_btn)

        self.setStyleSheet("""
            AttachmentChip {
                background: #f0f0f0; border-radius: 8px;
            }
        """)
        self.setFixedHeight(28)


class InputArea(QWidget):
    message_sent = Signal(str, list)  # text, list of file paths
    stop_requested = Signal()  # request to stop streaming

    def __init__(self, parent=None):
        super().__init__(parent)
        self.settings = Settings()
        self._attachments: list[str] = []
        self._streaming = False
        self._controls_enabled = True
        self._external_editing = False
        self._external_edit_worker = None
        self._external_edit_original_text = ""
        self._external_edit_path = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 4, 12, 12)
        outer.setSpacing(4)

        # Attachment preview row
        self._attach_row = QWidget(self)
        self._attach_layout = QHBoxLayout(self._attach_row)
        self._attach_layout.setContentsMargins(0, 0, 0, 0)
        self._attach_layout.setSpacing(6)
        self._attach_layout.addStretch()
        self._attach_row.hide()
        outer.addWidget(self._attach_row)

        # Input row
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        self.attach_btn = ToolButton(FluentIcon.FOLDER_ADD, self)
        self.attach_btn.setFixedSize(34, 34)
        self.attach_btn.clicked.connect(self._pick_files)
        row.addWidget(self.attach_btn, 0, Qt.AlignmentFlag.AlignBottom)

        self.external_edit_btn = ToolButton(FluentIcon.DOCUMENT, self)
        self.external_edit_btn.setFixedSize(34, 34)
        self.external_edit_btn.setToolTip("外部编辑当前输入")
        self.external_edit_btn.clicked.connect(self._open_external_editor)
        row.addWidget(self.external_edit_btn, 0, Qt.AlignmentFlag.AlignBottom)

        self.text_input = ChatInput(self)
        self.text_input.submit.connect(self._on_send)
        self.text_input.files_dropped.connect(self._add_files)
        row.addWidget(self.text_input, 1)

        self.send_btn = ToolButton(FluentIcon.SEND, self)
        self.send_btn.setFixedSize(34, 34)
        self.send_btn.clicked.connect(self._on_btn_click)
        row.addWidget(self.send_btn, 0, Qt.AlignmentFlag.AlignBottom)

        outer.addLayout(row)
        self._refresh_control_state()
        self._update_external_edit_button_visibility()

    def _pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择文件", "", "所有文件 (*)")
        if paths:
            self._add_files(paths)

    def _add_files(self, paths: list[str]):
        for p in paths:
            if p not in self._attachments:
                self._attachments.append(p)
                chip = AttachmentChip(p, self._attach_row)
                chip.removed.connect(self._remove_file)
                # Insert before the stretch
                self._attach_layout.insertWidget(
                    self._attach_layout.count() - 1, chip)
        self._attach_row.setVisible(bool(self._attachments))

    def _remove_file(self, path: str):
        if path in self._attachments:
            self._attachments.remove(path)
        # Remove the chip widget
        for i in range(self._attach_layout.count()):
            item = self._attach_layout.itemAt(i)
            w = item.widget() if item else None
            if isinstance(w, AttachmentChip) and w.filepath == path:
                w.deleteLater()
                break
        self._attach_row.setVisible(bool(self._attachments))

    def _on_send(self):
        text = self.text_input.toPlainText().strip()
        if text or self._attachments:
            self.message_sent.emit(text, list(self._attachments))
            self.text_input.clear()
            self._clear_attachments()

    def _clear_attachments(self):
        self._attachments.clear()
        while self._attach_layout.count() > 1:
            item = self._attach_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._attach_row.hide()

    def _on_btn_click(self):
        if self._streaming:
            self.stop_requested.emit()
        else:
            self._on_send()

    def _open_external_editor(self):
        if self._external_editing:
            return

        text = self.text_input.toPlainText()
        tmp_dir = Path(tempfile.gettempdir()) / "peekagent_prompt"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        edit_path = tmp_dir / f"prompt_{uuid.uuid4().hex[:8]}.txt"
        edit_path.write_text(text, encoding="utf-8")

        self._external_edit_original_text = text
        self._external_edit_path = str(edit_path)
        self._external_editing = True
        self._refresh_control_state()

        worker = _ExternalEditWorker(str(edit_path), self)
        worker.completed.connect(self._on_external_edit_done)
        worker.errored.connect(self._on_external_edit_error)
        worker.finished.connect(lambda: self._on_external_edit_worker_finished(worker))
        self._external_edit_worker = worker
        worker.start()

    def _on_external_edit_done(self, path: str):
        self._external_editing = False
        self._refresh_control_state()
        try:
            new_text = Path(path).read_text(encoding="utf-8-sig")
        except Exception as exc:
            self.text_input.setPlainText(self._external_edit_original_text)
            InfoBar.error(
                "读取失败",
                f"外部编辑后的内容读取失败：{exc}",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
        else:
            self.text_input.setPlainText(new_text)
        finally:
            self.text_input.setFocus()
            self._cleanup_external_edit_file(path)

    def _on_external_edit_error(self, err: str):
        self._external_editing = False
        self._refresh_control_state()
        self.text_input.setPlainText(self._external_edit_original_text)
        self.text_input.setFocus()
        self._cleanup_external_edit_file(self._external_edit_path)
        InfoBar.error(
            "打开失败",
            f"无法打开系统默认编辑器：{err}",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )

    def _on_external_edit_worker_finished(self, worker):
        if self._external_edit_worker is worker:
            self._external_edit_worker = None

    @staticmethod
    def _cleanup_external_edit_file(path: str):
        if not path:
            return
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

    def _refresh_control_state(self):
        text_enabled = self._controls_enabled and not self._streaming and not self._external_editing
        self.text_input.setEnabled(text_enabled)
        self.attach_btn.setEnabled(text_enabled)
        self.external_edit_btn.setEnabled(self._controls_enabled and not self._streaming and not self._external_editing)
        self.send_btn.setEnabled(self._controls_enabled and not self._external_editing)

    def _update_external_edit_button_visibility(self):
        visible = bool(self.settings.get("general", "external_prompt_editor_enabled", False))
        self.external_edit_btn.setVisible(visible)

    def apply_settings(self):
        self._update_external_edit_button_visibility()
        self._refresh_control_state()

    def set_streaming(self, streaming: bool):
        """Switch between send/stop mode."""
        self._streaming = streaming
        if streaming:
            self.send_btn.setIcon(FluentIcon.PAUSE)
        else:
            self.send_btn.setIcon(FluentIcon.SEND)
        self._refresh_control_state()

    def set_enabled(self, enabled: bool):
        self._controls_enabled = enabled
        self._refresh_control_state()
