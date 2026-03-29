"""Native code preview widget for appearance settings."""

from __future__ import annotations

import re

from PySide6.QtCore import QFileSystemWatcher, QTimer, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QPalette,
    QSyntaxHighlighter,
    QTextCharFormat,
)
from PySide6.QtWidgets import QFrame, QPlainTextEdit
from qfluentwidgets import isDarkTheme

from src.config import HIGHLIGHT_THEME_PATH, get_highlight_theme_for_mode


_PREVIEW_CODE = """#include <iostream>

int main() {
    std::cout << "Hello World!" << std::endl;
    return 0;
}"""


class _CppPreviewHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self._theme: dict = {}

    def set_theme(self, theme: dict):
        self._theme = theme or {}
        self.rehighlight()

    def highlightBlock(self, text: str):
        tokens = (self._theme.get("tokens") or {}) if isinstance(self._theme, dict) else {}

        self._apply_pattern(text, r"//.*$", self._format(tokens.get("comment")))
        self._apply_pattern(text, r'"(?:\\.|[^"\\])*"', self._format(tokens.get("string")))
        self._apply_pattern(text, r"\b\d+\b", self._format(tokens.get("number")))
        self._apply_pattern(text, r"#\s*include\b", self._format(tokens.get("meta-keyword")))
        self._apply_pattern(text, r"<[^>\n]+>", self._format(tokens.get("string")))
        self._apply_pattern(text, r"\b(?:int|return)\b", self._format(tokens.get("keyword")))
        self._apply_pattern(text, r"\b(?:main)\b(?=\s*\()", self._format(tokens.get("function")))
        self._apply_pattern(text, r"\b(?:std|cout|endl)\b", self._format(tokens.get("built_in")))
        self._apply_pattern(text, r"\b(?:iostream)\b", self._format(tokens.get("type")))
        self._apply_pattern(text, r"[{}();<>]", self._format(tokens.get("punctuation")))
        self._apply_pattern(text, r"<<", self._format(tokens.get("operator")))

    def _apply_pattern(self, text: str, pattern: str, fmt: QTextCharFormat):
        if not fmt.isValid():
            return
        for match in re.finditer(pattern, text):
            self.setFormat(match.start(), match.end() - match.start(), fmt)

    @staticmethod
    def _format(color_value: str | None) -> QTextCharFormat:
        fmt = QTextCharFormat()
        if not color_value:
            return fmt
        color = QColor(color_value)
        if color.isValid():
            fmt.setForeground(color)
        return fmt


class HighlightPreview(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._watcher = QFileSystemWatcher(self)
        self._theme_refresh_timer = QTimer(self)
        self._theme_refresh_timer.setSingleShot(True)
        self._theme_refresh_timer.setInterval(80)
        self._theme_refresh_timer.timeout.connect(self.apply_highlight_theme)
        self._watcher.fileChanged.connect(self._schedule_highlight_theme_refresh)
        self._watcher.directoryChanged.connect(self._schedule_highlight_theme_refresh)
        self._highlighter = _CppPreviewHighlighter(self.document())

        self.setReadOnly(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setPlainText(_PREVIEW_CODE)
        self.setMinimumHeight(132)

        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(10)
        self.setFont(font)

        self._refresh_watched_paths()
        self.apply_theme()

    def _refresh_watched_paths(self):
        directory = str(HIGHLIGHT_THEME_PATH.parent)
        if directory and directory not in self._watcher.directories():
            self._watcher.addPath(directory)

        file_path = str(HIGHLIGHT_THEME_PATH)
        watched_files = self._watcher.files()
        if HIGHLIGHT_THEME_PATH.exists():
            if file_path not in watched_files:
                self._watcher.addPath(file_path)
        elif file_path in watched_files:
            self._watcher.removePath(file_path)

    def _schedule_highlight_theme_refresh(self):
        self._refresh_watched_paths()
        self._theme_refresh_timer.start()

    def apply_theme(self, dark_mode: bool | None = None):
        dark_mode = isDarkTheme() if dark_mode is None else dark_mode
        self._refresh_watched_paths()
        theme = get_highlight_theme_for_mode(dark_mode)
        base = theme.get("base") if isinstance(theme, dict) else {}

        fallback_text = "#d6deeb" if dark_mode else "#1f2937"
        fallback_background = "#0f1720" if dark_mode else "#f7f8fa"
        border_color = "rgba(255, 255, 255, 0.10)" if dark_mode else "rgba(0, 0, 0, 0.08)"
        text_color = QColor((base or {}).get("color", fallback_text))
        background_color = QColor((base or {}).get("background", fallback_background))
        selection_color = QColor((base or {}).get("selection", "rgba(14, 165, 164, 0.18)"))

        if not text_color.isValid():
            text_color = QColor(fallback_text)
        if not background_color.isValid():
            background_color = QColor(fallback_background)
        if not selection_color.isValid():
            selection_color = QColor(14, 165, 164, 46)

        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Base, background_color)
        palette.setColor(QPalette.ColorRole.Text, text_color)
        palette.setColor(QPalette.ColorRole.Highlight, selection_color)
        self.setPalette(palette)
        self.viewport().setAutoFillBackground(True)

        self.setStyleSheet(
            "QPlainTextEdit {"
            f"background: {background_color.name()};"
            f"color: {text_color.name()};"
            f"border: 1px solid {border_color};"
            "border-radius: 8px;"
            "padding: 10px 12px;"
            f"selection-background-color: {selection_color.name(QColor.NameFormat.HexArgb)};"
            "}"
        )
        self._highlighter.set_theme(theme)

    def apply_highlight_theme(self):
        self.apply_theme()

