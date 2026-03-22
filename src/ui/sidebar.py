"""Sidebar widget for conversation list."""

from PySide6.QtCore import Signal, Qt, QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QAbstractItemView, QFrame,
)
from qfluentwidgets import (
    PushButton, ToolButton, FluentIcon, LineEdit,
    MessageBox, BodyLabel,
)


class SessionItem(QWidget):
    """Single session row in the sidebar list. Supports inline rename."""
    rename_confirmed = Signal(str, str)  # sid, new_title
    delete_requested = Signal(str)
    clicked = Signal(str)

    def __init__(self, sid: str, title: str, parent=None):
        super().__init__(parent)
        self.sid = sid
        self._title = title
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 4, 4)
        layout.setSpacing(4)

        self.label = BodyLabel(title, self)
        self.label.setFixedHeight(28)
        self.label.setStyleSheet("font-size: 13px; font-weight: bold;")
        layout.addWidget(self.label, 1)

        self.name_edit = LineEdit(self)
        self.name_edit.setFixedHeight(28)
        self.name_edit.hide()
        self.name_edit.returnPressed.connect(self._finish_rename)
        self.name_edit.editingFinished.connect(self._finish_rename)
        layout.addWidget(self.name_edit, 1)

        self.edit_btn = ToolButton(FluentIcon.EDIT, self)
        self.edit_btn.setFixedSize(28, 28)
        self.edit_btn.clicked.connect(self._start_rename)
        layout.addWidget(self.edit_btn)

        self.del_btn = ToolButton(FluentIcon.DELETE, self)
        self.del_btn.setFixedSize(28, 28)
        self.del_btn.clicked.connect(lambda: self.delete_requested.emit(self.sid))
        layout.addWidget(self.del_btn)

    def _start_rename(self):
        self.name_edit.setText(self._title)
        self.label.hide()
        self.name_edit.show()
        self.name_edit.setFocus()
        self.name_edit.selectAll()

    def _finish_rename(self):
        if not self.name_edit.isVisible():
            return
        new_title = self.name_edit.text().strip()
        self.name_edit.hide()
        self.label.show()
        if new_title and new_title != self._title:
            self._title = new_title
            self.label.setText(new_title)
            self.rename_confirmed.emit(self.sid, new_title)


class Sidebar(QFrame):
    """Slide-out overlay sidebar with session list."""
    session_selected = Signal(str)
    session_new = Signal()
    session_renamed = Signal(str, str)  # sid, new_title
    session_deleted = Signal(str)

    SIDEBAR_WIDTH = 240

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(self.SIDEBAR_WIDTH)
        self.move(-self.SIDEBAR_WIDTH, 0)
        self.setObjectName("sidebar")
        self.setStyleSheet("""
            #sidebar {
                background: rgba(245, 245, 245, 0.97);
                border-right: 1px solid #e0e0e0;
            }
        """)
        self._expanded = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # New chat button
        self.new_btn = PushButton(FluentIcon.ADD, "新对话", self)
        self.new_btn.clicked.connect(self.session_new.emit)
        layout.addWidget(self.new_btn)

        # Session list
        self.list_widget = QListWidget(self)
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_widget.setStyleSheet("""
            QListWidget { border: none; background: transparent; }
            QListWidget::item { border-radius: 6px; padding: 2px; }
            QListWidget::item:selected { background: rgba(0,0,0,0.06); }
            QListWidget::item:hover { background: rgba(0,0,0,0.04); }
        """)
        layout.addWidget(self.list_widget, 1)

        self._anim = QPropertyAnimation(self, b"pos")
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def expand(self):
        self._anim.stop()
        from PySide6.QtCore import QPoint
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(QPoint(0, 0))
        self._anim.start()
        self._expanded = True

    def collapse(self):
        self._anim.stop()
        from PySide6.QtCore import QPoint
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(QPoint(-self.SIDEBAR_WIDTH, 0))
        self._anim.start()
        self._expanded = False

    def load_sessions(self, sessions: list[dict], current_id: str = ""):
        self.list_widget.clear()
        for s in sessions:
            item = QListWidgetItem(self.list_widget)
            widget = SessionItem(s["id"], s["title"])
            widget.rename_confirmed.connect(lambda sid, title: self.session_renamed.emit(sid, title))
            widget.delete_requested.connect(self._on_delete)
            item.setSizeHint(widget.sizeHint())
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, widget)
            if s["id"] == current_id:
                self.list_widget.setCurrentItem(item)
        self.list_widget.itemClicked.connect(self._on_item_clicked)

    def _on_item_clicked(self, item: QListWidgetItem):
        widget = self.list_widget.itemWidget(item)
        if widget:
            self.session_selected.emit(widget.sid)

    def _on_delete(self, sid: str):
        self.session_deleted.emit(sid)
