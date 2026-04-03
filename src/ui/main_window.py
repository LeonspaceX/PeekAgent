"""Main floating window for PeekAgent."""

import os
import base64
import shutil
import mimetypes
import uuid
from PySide6.QtCore import Qt, QPoint, QSize, QRect, QTimer, QEvent
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QWidget, QSystemTrayIcon, QMenu,
    QApplication,
)
from PySide6.QtGui import QIcon, QAction, QCursor, QPainter, QColor, QPainterPath, QBrush, QRegion
from qfluentwidgets import (
    ToolButton, FluentIcon, BodyLabel, InfoBar, InfoBarPosition,
    MessageBox, MSFluentWindow, isDarkTheme,
)
from src.ui.chat_view import ChatView
from src.ui.input_area import InputArea
from src.ui.sidebar import Sidebar
from src.chat_manager import ChatManager, ATTACHMENTS_DIR, normalize_session_title
from src.api_client import ApiClient, TitleWorker
from src.config import ICON_PATH, Settings
from src.tool_runtime import ToolCall, ToolExecutionWorker, ToolParser, ToolRuntime


class _GripWidget(QWidget):
    """Invisible widget that still receives mouse events (no painting)."""
    def paintEvent(self, event):
        pass  # Draw nothing — truly invisible, but accepts mouse input


class MainWindow(QWidget):
    EDGE_SIZE = 8  # pixels for resize grip
    WORKER_SHUTDOWN_TIMEOUT_MS = 2000

    def __init__(self):
        super().__init__()
        self.settings = Settings()
        self.chat_mgr = ChatManager()
        self.api_client = ApiClient()
        self.tool_runtime = ToolRuntime()
        self._current_session = None
        self._stream_worker = None
        self._tool_worker = None
        self._title_worker = None
        self._tool_queue: list[ToolCall] = []
        self._pending_tool_call: ToolCall | None = None
        self._pending_tool_id: str | None = None
        self._consecutive_auto_tool_rounds = 0
        self._tool_flow_stopped = False
        self._drag_pos = None
        self._resize_edge = None
        self._title_typing_timer = QTimer(self)
        self._title_typing_timer.setInterval(60)
        self._title_typing_timer.timeout.connect(self._advance_title_typing)
        self._pending_title_text = ""
        self._title_typing_index = 0
        self._title_typing_session_id = None
        self._shutdown_done = False

        self._save_geo_timer = QTimer(self)
        self._save_geo_timer.setSingleShot(True)
        self._save_geo_timer.setInterval(500)
        self._save_geo_timer.timeout.connect(self._save_geometry)

        self._init_window()
        self._init_ui()
        self._apply_theme()
        self._init_resize_grips()
        self._load_or_create_session()

        # Pre-warm API client in background thread to avoid first-send delay
        QTimer.singleShot(200, self._prewarm_client)

    def _prewarm_client(self):
        import threading
        def _warm():
            try:
                self.api_client._ensure_client()
            except Exception:
                pass
        threading.Thread(target=_warm, daemon=True).start()

    def _init_window(self):
        self.setWindowTitle("PeekAgent")
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
        )
        self.setWindowFlag(
            Qt.WindowType.WindowStaysOnTopHint,
            bool(self.settings.get("general", "always_on_top", True)),
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # Restore window geometry
        w = self.settings.get("window", "width", 420)
        h = self.settings.get("window", "height", 620)
        self.resize(w, h)

        x = self.settings.get("window", "x", -1)
        y = self.settings.get("window", "y", -1)
        if x >= 0 and y >= 0:
            self.move(x, y)
        else:
            # Default: bottom-right corner
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(screen.width() - w - 20, screen.height() - h - 20)

    BORDER_RADIUS = 12

    def _init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Title bar
        title_bar = QWidget(self)
        title_bar.setFixedHeight(40)
        tb_layout = QHBoxLayout(title_bar)
        tb_layout.setContentsMargins(8, 0, 8, 0)
        tb_layout.setSpacing(6)

        self.menu_btn = ToolButton(FluentIcon.MENU, self)
        self.menu_btn.setFixedSize(32, 32)
        self.menu_btn.clicked.connect(self._toggle_sidebar)
        tb_layout.addWidget(self.menu_btn)

        self.title_label = BodyLabel("新对话", self)
        self.title_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        tb_layout.addWidget(self.title_label, 1)

        self.close_btn = ToolButton(FluentIcon.CLOSE, self)
        self.close_btn.setFixedSize(32, 32)
        self.close_btn.clicked.connect(self.hide)
        tb_layout.addWidget(self.close_btn)

        main_layout.addWidget(title_bar)

        # Chat view
        self.chat_view = ChatView(self)
        self.chat_view.copy_requested.connect(self._copy_message)
        self.chat_view.edit_requested.connect(self._edit_message)
        self.chat_view.regenerate_requested.connect(self._regenerate_message)
        self.chat_view.tool_approval_requested.connect(self._handle_tool_approval)
        main_layout.addWidget(self.chat_view, 1)

        # Input area
        self.input_area = InputArea(self)
        self.input_area.message_sent.connect(self._on_send)
        self.input_area.stop_requested.connect(self._on_stop)
        main_layout.addWidget(self.input_area)

        # Overlay mask (click to close sidebar)
        self._overlay = QWidget(self)
        self._overlay.setStyleSheet("background: rgba(0, 0, 0, 80);")
        self._overlay.hide()
        self._overlay.mousePressEvent = lambda e: self._close_sidebar()

        # Sidebar (overlay, not in layout)
        self.sidebar = Sidebar(self)
        self.sidebar.session_selected.connect(self._switch_session)
        self.sidebar.session_new.connect(self._new_session)
        self.sidebar.session_deleted.connect(self._delete_session)
        self.sidebar.session_renamed.connect(self._rename_session)
        self.sidebar.raise_()

    def _apply_theme(self, dark_mode: bool | None = None):
        dark_mode = isDarkTheme() if dark_mode is None else dark_mode
        primary_color = self.settings.get("appearance", "primary_theme_color", "#0ea5a4")
        user_color = self.settings.get("appearance", "theme_color_1", "#1a73e8")
        ai_color = self.settings.get("appearance", "theme_color_2", "#7c3aed")
        self.chat_view.set_theme(primary_color, user_color, ai_color, dark_mode)
        self.chat_view.apply_highlight_theme()
        self.sidebar.apply_theme()
        self.input_area.apply_settings()
        self.update()

    def apply_theme(self, dark_mode: bool | None = None):
        self._apply_theme(dark_mode)

    def set_always_on_top(self, enabled: bool):
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, bool(enabled))
        if was_visible:
            self.show()
            self.raise_()
            self.activateWindow()

    def focus_input(self):
        self.input_area.focus_text_input()

    def _toggle_sidebar(self):
        if self.sidebar._expanded:
            self._close_sidebar()
        else:
            self._open_sidebar()

    def _open_sidebar(self):
        self._overlay.setGeometry(self.rect())
        self._overlay.show()
        self._overlay.raise_()
        self.sidebar.raise_()
        self.sidebar.expand()

    def _close_sidebar(self):
        self.sidebar.collapse()
        self._overlay.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(),
                            self.BORDER_RADIUS, self.BORDER_RADIUS)
        painter.setClipPath(path)
        painter.fillPath(path, QBrush(QColor("#202020") if isDarkTheme() else QColor(255, 255, 255)))
        painter.end()

    # --- Session management ---

    def _load_or_create_session(self):
        sessions = self.chat_mgr.list_sessions()
        if sessions:
            self._switch_session(sessions[0]["id"])
        else:
            self._new_session()
        self._refresh_sidebar()

    def _new_session(self):
        self._stop_title_typing()
        self._reset_tool_flow()
        data = self.chat_mgr.create_session()
        self._current_session = data
        self.title_label.setText(data["title"])
        self.chat_view.clear_chat()
        self._refresh_sidebar()

    def _switch_session(self, sid: str):
        self._stop_title_typing()
        self._reset_tool_flow()
        data = self.chat_mgr.load_session(sid)
        if not data:
            return
        self._current_session = data
        self.title_label.setText(data["title"])
        self._render_current_session()
        self.sidebar.collapse()

    def _delete_session(self, sid: str):
        w = MessageBox("删除会话", "确定要删除这个会话吗？此操作不可撤销。", self)
        if w.exec():
            if self._current_session and self._current_session["id"] == sid:
                self._stop_title_typing()
            self.chat_mgr.delete_session(sid)
            if self._current_session and self._current_session["id"] == sid:
                self._current_session = None
                self.chat_view.clear_chat()
                self.title_label.setText("PeekAgent")
                self._load_or_create_session()
            self._refresh_sidebar()

    def _rename_session(self, sid: str, new_title: str):
        if not new_title:
            return
        if self._current_session and self._current_session["id"] == sid:
            self._stop_title_typing()
        self.chat_mgr.rename_session(sid, new_title)
        if self._current_session and self._current_session["id"] == sid:
            self._current_session["title"] = new_title
            self.title_label.setText(new_title)

    def _refresh_sidebar(self):
        sessions = self.chat_mgr.list_sessions()
        current_id = self._current_session["id"] if self._current_session else ""
        self.sidebar.load_sessions(sessions, current_id)

    # --- Chat ---

    def _reset_tool_flow(self):
        self._tool_queue.clear()
        self._pending_tool_call = None
        self._pending_tool_id = None
        self._consecutive_auto_tool_rounds = 0
        self._tool_flow_stopped = False

    def _on_send(self, text: str, attachments: list = None):
        if not self._current_session:
            self._new_session()
        self._reset_tool_flow()
        sid = self._current_session["id"]

        # Copy attachments to session directory, collect stored paths
        stored_paths = []
        if attachments:
            attach_dir = ATTACHMENTS_DIR / sid
            attach_dir.mkdir(parents=True, exist_ok=True)
            for p in attachments:
                dest = attach_dir / os.path.basename(p)
                # Avoid name collision
                if dest.exists():
                    stem, ext = os.path.splitext(dest.name)
                    import uuid as _uuid
                    dest = attach_dir / f"{stem}_{_uuid.uuid4().hex[:6]}{ext}"
                shutil.copy2(p, dest)
                stored_paths.append(str(dest))

        # Save message with attachment refs (paths only, no base64)
        msg = {"role": "user", "content": text}
        if stored_paths:
            msg["attachments"] = stored_paths
        message_index = len(self._current_session["messages"])
        self._current_session["messages"].append(msg)
        self.chat_mgr.save_session(self._current_session)
        self.chat_view.add_message("user", self._message_display_text(msg), message_index)
        self.input_area.set_streaming(True)

        # Start streaming
        try:
            self._request_assistant_reply()
        except Exception as e:
            self._show_error(str(e))
            self.input_area.set_streaming(False)

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

    def _build_api_messages(self) -> list:
        """Convert session messages to API format, resolving attachment paths."""
        result = []
        for m in self._current_session["messages"]:
            role = m.get("role")
            if role == "tool" and m.get("status") not in {"success", "error", "denied"}:
                continue
            api_role = "user" if role == "tool" else role
            attachments = m.get("attachments", [])
            if not attachments:
                result.append({"role": api_role, "content": m["content"]})
                continue
            parts = []
            if m["content"]:
                parts.append({"type": "text", "text": m["content"]})
            for p in attachments:
                ext = os.path.splitext(p)[1].lower()
                if ext in self._IMAGE_EXTS:
                    try:
                        with open(p, "rb") as f:
                            b64 = base64.b64encode(f.read()).decode("ascii")
                        mime = mimetypes.guess_type(p)[0] or "image/png"
                        parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"}
                        })
                    except OSError:
                        parts.append({"type": "text", "text": f"[附件: {os.path.basename(p)} (读取失败)]"})
                else:
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            file_text = f.read(50000)
                        parts.append({"type": "text", "text": f"--- {os.path.basename(p)} ---\n{file_text}"})
                    except (UnicodeDecodeError, OSError):
                        parts.append({"type": "text", "text": f"[附件: {os.path.basename(p)} (二进制文件)]"})
            result.append({"role": api_role, "content": parts})
        return result

    def _message_display_text(self, message: dict) -> str:
        content = message.get("display_content", message.get("content", ""))
        attachments = message.get("attachments", [])
        if not attachments:
            return content
        names = [os.path.basename(path) for path in attachments]
        attachment_text = " ".join(f"[{name}]" for name in names)
        if content:
            return f"{content}\n\n{attachment_text}"
        return attachment_text

    def _render_current_session(self):
        self.chat_view.clear_chat()
        if not self._current_session:
            return
        for index, message in enumerate(self._current_session["messages"]):
            if message.get("role") == "tool":
                self.chat_view.add_tool_message(self._tool_payload(message))
                continue
            display_text = self._message_display_text(message)
            if message.get("role") == "assistant" and not display_text.strip():
                continue
            self.chat_view.add_message(
                message["role"],
                display_text,
                index,
            )

    @staticmethod
    def _tool_payload(message: dict) -> dict:
        return {
            "id": message.get("tool_id", ""),
            "toolName": message.get("tool_name", ""),
            "title": message.get("title", ""),
            "detail": message.get("detail", ""),
            "status": message.get("status", "pending"),
            "requiresApproval": message.get("requires_approval", False),
            "expanded": message.get("expanded", message.get("status") == "pending"),
        }

    def _request_assistant_reply(self):
        messages = self._build_api_messages()
        worker = self.api_client.send_stream(messages)
        self._stream_worker = worker
        assistant_index = len(self._current_session["messages"])
        self.chat_view.start_stream(assistant_index)
        worker.token_received.connect(self.chat_view.append_token)
        worker.stream_finished.connect(self._on_stream_done)
        worker.error_occurred.connect(self._on_stream_error)
        worker.start()

    def _on_stream_done(self, full_text: str):
        self.chat_view.finish_stream()
        self._stream_worker = None
        display_text, tool_calls = ToolParser.parse_response(full_text)
        assistant_message = {"role": "assistant", "content": full_text, "display_content": display_text}
        self._current_session["messages"].append(assistant_message)
        self.chat_mgr.save_session(self._current_session)
        if tool_calls:
            if self._tool_round_has_manual_gate(tool_calls):
                self._consecutive_auto_tool_rounds = 0
            else:
                self._consecutive_auto_tool_rounds += 1
                auto_tool_round_limit = self._auto_tool_round_limit()
                if self._consecutive_auto_tool_rounds > auto_tool_round_limit:
                    self._append_tool_message(
                        tool_id=uuid.uuid4().hex[:12],
                        tool_name="command",
                        title="工具调用",
                        detail=f"为了避免无限循环，本轮工具自动调用已在第 {auto_tool_round_limit} 轮后终止。",
                        status="error",
                        content="工具调用已被中止，因为连续调用次数过多，疑似进入循环。",
                    )
                    self.input_area.set_streaming(False)
                    return
            self._start_tool_sequence(tool_calls)
            return

        self.input_area.set_streaming(False)
        self._maybe_generate_title()

    def _auto_tool_round_limit(self) -> int:
        value = self.settings.get("tools", "auto_tool_round_limit", 8)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 8
        return max(1, parsed)

    def _tool_round_has_manual_gate(self, tool_calls: list[ToolCall]) -> bool:
        return any(self.tool_runtime.get_mode(call.tool_name) == "manual" for call in tool_calls)

    def _start_tool_sequence(self, tool_calls: list[ToolCall]):
        self._tool_queue = list(tool_calls)
        self._process_next_tool_call()

    def _process_next_tool_call(self):
        if self._pending_tool_call is not None or self._tool_worker is not None:
            return
        if not self._tool_queue:
            if self._tool_flow_stopped:
                self._tool_flow_stopped = False
                return
            try:
                self._request_assistant_reply()
            except Exception as e:
                self._show_error(str(e))
                self.input_area.set_streaming(False)
            return

        call = self._tool_queue.pop(0)
        mode = self.tool_runtime.get_mode(call.tool_name)
        if mode == "off":
            self._append_tool_message(
                tool_id=uuid.uuid4().hex[:12],
                tool_name=call.tool_name,
                title=call.display_name,
                detail=self._tool_request_detail(call),
                status="error",
                content="[调用失败！]工具在设置中被关闭",
            )
            self._process_next_tool_call()
            return

        tool_id = uuid.uuid4().hex[:12]
        if mode == "manual":
            self._append_tool_message(
                tool_id=tool_id,
                tool_name=call.tool_name,
                title=call.display_name,
                detail=self._tool_request_detail(call),
                status="pending",
                content="",
                requires_approval=True,
                expanded=True,
            )
            self._pending_tool_call = call
            self._pending_tool_id = tool_id
            return

        self._run_tool_call(call, tool_id)

    def _run_tool_call(self, call: ToolCall, tool_id: str):
        existing = self._find_message_by_tool_id(tool_id)
        if existing is None:
            self._append_tool_message(
                tool_id=tool_id,
                tool_name=call.tool_name,
                title=call.display_name,
                detail=self._tool_request_detail(call),
                status="running",
                content="",
                expanded=False,
            )
        else:
            self._update_tool_message(
                existing,
                status="running",
                requires_approval=False,
                expanded=False,
            )
        self._pending_tool_call = None
        self._pending_tool_id = None
        if call.tool_name == "clipboard":
            result = self.tool_runtime.execute(call, self._current_session["id"] if self._current_session else None)
            self._on_tool_finished(tool_id, result)
            self._process_next_tool_call()
            return
        worker = ToolExecutionWorker(
            self.tool_runtime,
            call,
            self._current_session["id"] if self._current_session else None,
        )
        worker.finished_with_result.connect(lambda result, tid=tool_id: self._on_tool_finished(tid, result))
        worker.finished.connect(lambda: self._on_tool_worker_finished(worker))
        self._tool_worker = worker
        worker.start()

    def _on_tool_finished(self, tool_id: str, result):
        message = self._find_message_by_tool_id(tool_id)
        if message is None:
            self._append_tool_message(
                tool_id=tool_id,
                tool_name=result.tool_name,
                title=ToolCall(tool_name=result.tool_name, raw_body="").display_name,
                detail=result.detail,
                status=result.status,
                content=result.content,
                attachments=result.attachments,
                expanded=False,
            )
            return
        self._update_tool_message(
            message,
            detail=result.detail,
            status=result.status,
            content=result.content,
            attachments=result.attachments,
            requires_approval=False,
            expanded=False,
        )

    def _on_tool_worker_finished(self, worker):
        if self._tool_worker is worker:
            self._tool_worker = None
        worker.deleteLater()
        self._process_next_tool_call()

    def _handle_tool_approval(self, tool_id: str, approved: bool):
        if tool_id != self._pending_tool_id or self._pending_tool_call is None:
            return
        message = self._find_message_by_tool_id(tool_id)
        if message is None:
            return
        call = self._pending_tool_call
        if not approved:
            self._update_tool_message(
                message,
                status="denied",
                content=f"[调用失败] {call.display_name}未执行，因为用户拒绝了这次审批。",
                requires_approval=False,
                expanded=False,
            )
            self._pending_tool_call = None
            self._pending_tool_id = None
            self._process_next_tool_call()
            return
        self._run_tool_call(call, tool_id)

    def _append_tool_message(
        self,
        *,
        tool_id: str,
        tool_name: str,
        title: str,
        detail: str,
        status: str,
        content: str,
        attachments: list[str] | None = None,
        requires_approval: bool = False,
        expanded: bool = False,
    ) -> dict:
        message = {
            "role": "tool",
            "tool_id": tool_id,
            "tool_name": tool_name,
            "title": title,
            "detail": detail,
            "status": status,
            "content": content,
            "requires_approval": requires_approval,
            "attachments": list(attachments or []),
            "expanded": expanded,
        }
        self._current_session["messages"].append(message)
        self.chat_mgr.save_session(self._current_session)
        self.chat_view.add_tool_message(self._tool_payload(message))
        return message

    def _update_tool_message(self, message: dict, **updates):
        message.update(updates)
        self.chat_mgr.save_session(self._current_session)
        self.chat_view.update_tool_message(self._tool_payload(message))

    def _find_message_by_tool_id(self, tool_id: str) -> dict | None:
        if not self._current_session:
            return None
        for message in self._current_session["messages"]:
            if message.get("role") == "tool" and message.get("tool_id") == tool_id:
                return message
        return None

    def _tool_request_detail(self, call: ToolCall) -> str:
        if call.parse_error:
            return call.parse_error
        if call.tool_name == "read":
            detail = str(call.payload.get("path", ""))
            if call.payload.get("start_line") is not None or call.payload.get("end_line") is not None:
                detail += f"\n第 {call.payload.get('start_line') or 1} 行 - 第 {call.payload.get('end_line') or '末尾'} 行"
            return detail
        if call.tool_name == "search":
            return f"{call.payload.get('path', '')}\n搜索内容: {call.payload.get('pattern', '')}"
        if call.tool_name in {"write", "add"}:
            return str(call.payload.get("path", ""))
        if call.tool_name == "replace":
            old = call.payload.get("old", "")
            new = call.payload.get("new", "")
            old_preview = old if len(old) <= 160 else old[:160] + "..."
            new_preview = new if len(new) <= 160 else new[:160] + "..."
            return (
                f"{call.payload.get('path', '')}\n"
                f"旧文本:\n{old_preview}\n\n"
                f"新文本:\n{new_preview}"
            )
        if call.tool_name == "command":
            return str(call.payload.get("content", ""))
        if call.tool_name == "client_list":
            return "读取已配置 SSH 客户端及连接状态"
        if call.tool_name == "client_connect":
            return f"连接至{call.payload.get('name', '')}"
        if call.tool_name == "client_command":
            return (
                f"执行节点：{call.payload.get('name', '')}\n"
                f"执行命令：{call.payload.get('command', '')}"
            )
        if call.tool_name == "client_disconnect":
            return str(call.payload.get("name", ""))
        if call.tool_name == "web-fetch":
            return str(call.payload.get("url", ""))
        if call.tool_name == "web-search":
            topic = call.payload.get("topic", "general")
            topic_label = "新闻" if topic == "news" else "通用"
            depth = call.payload.get("search_depth", "basic")
            depth_label = {
                "basic": "基础",
                "advanced": "高级",
                "fast": "快速",
            }.get(depth, depth)
            parts = [
                f"查询词: {call.payload.get('query', '')}",
                f"主题: {topic_label}",
                f"搜索深度: {depth_label}",
            ]
            if call.payload.get("days") is not None:
                parts.append(f"时间范围天数: {call.payload.get('days')}")
            if call.payload.get("include_domains"):
                parts.append("包含站点: " + ", ".join(call.payload.get("include_domains", [])))
            if call.payload.get("exclude_domains"):
                parts.append("排除站点: " + ", ".join(call.payload.get("exclude_domains", [])))
            return "\n".join(parts)
        if call.tool_name == "clipboard":
            if call.payload.get("kind") == "files":
                return "\n".join(call.payload.get("paths", []))
            return str(call.payload.get("text", ""))
        if call.tool_name == "capture":
            return "截取当前整个屏幕。"
        return f"{call.display_name}\n该工具尚未定义详情格式。"

    def _maybe_generate_title(self):
        if not self._current_session or self._current_session.get("title") != "新对话":
            return
        user_msg = next((m.get("content", "") for m in self._current_session["messages"] if m.get("role") == "user"), "")
        ai_msg = next(
            (
                m.get("display_content", m.get("content", ""))
                for m in self._current_session["messages"]
                if m.get("role") == "assistant" and m.get("display_content", m.get("content", "")).strip()
            ),
            "",
        )
        if user_msg and ai_msg:
            self._generate_title(user_msg, ai_msg)

    def _generate_title(self, user_msg: str, ai_msg: str):
        try:
            self.api_client._ensure_client()
            model = Settings().get("model", "model_name", "")
            if not model:
                return
            worker = TitleWorker(self.api_client, model, user_msg, ai_msg)
            worker.title_ready.connect(self._on_title_ready)
            self._title_worker = worker
            worker.start()
        except Exception:
            pass

    def _on_title_ready(self, title: str):
        if self._current_session:
            self._start_title_typing(normalize_session_title(title))
        self._title_worker = None

    def _start_title_typing(self, title: str):
        self._stop_title_typing()
        self._pending_title_text = normalize_session_title(title)
        self._title_typing_index = 0
        self._title_typing_session_id = self._current_session["id"] if self._current_session else None
        self.title_label.setText("")
        self._title_typing_timer.start()

    def _advance_title_typing(self):
        if not self._current_session or self._current_session["id"] != self._title_typing_session_id:
            self._stop_title_typing()
            return
        if self._title_typing_index >= len(self._pending_title_text):
            final_title = self._pending_title_text
            self._stop_title_typing()
            self._current_session["title"] = final_title
            self.chat_mgr.save_session(self._current_session)
            self.title_label.setText(final_title)
            self._refresh_sidebar()
            return

        self._title_typing_index += 1
        self.title_label.setText(self._pending_title_text[:self._title_typing_index])

    def _stop_title_typing(self):
        self._title_typing_timer.stop()
        self._pending_title_text = ""
        self._title_typing_index = 0
        self._title_typing_session_id = None

    def _on_stream_error(self, error: str):
        self.chat_view.finish_stream()
        self._show_error(error)
        self.input_area.set_streaming(False)
        self._stream_worker = None

    def _on_stop(self):
        """User clicked stop — cancel the stream immediately."""
        if self._stream_worker:
            self._stream_worker.cancel()
            # Don't wait for thread to finish — just finalize UI now
            self.chat_view.finish_stream()
            self.input_area.set_streaming(False)
            self._stream_worker = None
            self._reset_tool_flow()
            return
        if self._pending_tool_call and self._pending_tool_id:
            message = self._find_message_by_tool_id(self._pending_tool_id)
            if message is not None:
                self._update_tool_message(
                    message,
                    status="denied",
                    content="[调用失败] 工具调用已取消，因为用户手动停止了当前流程。",
                    requires_approval=False,
                )
        if self._tool_worker is not None:
            self._tool_flow_stopped = True
        self._tool_queue.clear()
        self._pending_tool_call = None
        self._pending_tool_id = None
        self.input_area.set_streaming(False)

    def _show_error(self, msg: str):
        InfoBar.error(
            title="错误",
            content=msg,
            parent=self,
            position=InfoBarPosition.TOP,
            duration=5000,
        )

    def _copy_message(self, index: int):
        if not self._current_session:
            return
        messages = self._current_session.get("messages", [])
        if not (0 <= index < len(messages)):
            return
        QApplication.clipboard().setText(self._message_display_text(messages[index]))

    def _edit_message(self, index: int, new_text: str):
        if self._stream_worker or self._pending_tool_call or self._tool_queue:
            self._show_error("请等待当前回复完成后再修改消息")
            return
        if not self._current_session:
            return
        self._stop_title_typing()
        self._reset_tool_flow()
        messages = self._current_session.get("messages", [])
        if not (0 <= index < len(messages)):
            return
        message = messages[index]
        if message.get("role") != "user":
            return
        if not new_text.strip() and not message.get("attachments"):
            self._show_error("消息内容不能为空")
            return

        message["content"] = new_text
        self._current_session["messages"] = messages[:index + 1]
        self.chat_mgr.save_session(self._current_session)
        self._render_current_session()
        self.input_area.set_streaming(True)
        try:
            self._request_assistant_reply()
        except Exception as e:
            self._show_error(str(e))
            self.input_area.set_streaming(False)

    def _regenerate_message(self, index: int):
        if self._stream_worker or self._pending_tool_call or self._tool_queue:
            self._show_error("请等待当前回复完成后再重新生成")
            return
        if not self._current_session:
            return
        self._stop_title_typing()
        self._reset_tool_flow()
        messages = self._current_session.get("messages", [])
        if not (0 <= index < len(messages)):
            return
        if messages[index].get("role") != "assistant":
            return
        user_index = None
        for i in range(index - 1, -1, -1):
            if messages[i].get("role") == "user":
                user_index = i
                break
        if user_index is None:
            self._show_error("未找到可重新生成的上一条用户消息")
            return

        self._current_session["messages"] = messages[:index]
        self.chat_mgr.save_session(self._current_session)
        self._render_current_session()
        self.input_area.set_streaming(True)
        try:
            self._request_assistant_reply()
        except Exception as e:
            self._show_error(str(e))
            self.input_area.set_streaming(False)

    # --- Resize grips (transparent edge widgets above WebEngineView) ---

    def _init_resize_grips(self):
        """Create edge widgets for resize, since QWebEngineView eats mouse events."""
        for name in ("top", "bottom", "left", "right", "tl", "tr", "bl", "br"):
            grip = _GripWidget(self)
            grip.setMouseTracking(True)
            grip.setCursor(self._cursor_for(name))
            grip.installEventFilter(self)
            grip.setProperty("_edge", name)
            grip.raise_()
            setattr(self, f"_grip_{name}", grip)
        self._layout_grips()

    @staticmethod
    def _cursor_for(name):
        m = {
            "left": Qt.CursorShape.SizeHorCursor,
            "right": Qt.CursorShape.SizeHorCursor,
            "top": Qt.CursorShape.SizeVerCursor,
            "bottom": Qt.CursorShape.SizeVerCursor,
            "tl": Qt.CursorShape.SizeFDiagCursor,
            "br": Qt.CursorShape.SizeFDiagCursor,
            "tr": Qt.CursorShape.SizeBDiagCursor,
            "bl": Qt.CursorShape.SizeBDiagCursor,
        }
        return m.get(name, Qt.CursorShape.ArrowCursor)

    def _layout_grips(self):
        E = self.EDGE_SIZE
        w, h = self.width(), self.height()
        self._grip_top.setGeometry(E, 0, w - 2 * E, E)
        self._grip_bottom.setGeometry(E, h - E, w - 2 * E, E)
        self._grip_left.setGeometry(0, E, E, h - 2 * E)
        self._grip_right.setGeometry(w - E, E, E, h - 2 * E)
        self._grip_tl.setGeometry(0, 0, E, E)
        self._grip_tr.setGeometry(w - E, 0, E, E)
        self._grip_bl.setGeometry(0, h - E, E, E)
        self._grip_br.setGeometry(w - E, h - E, E, E)

    _EDGE_MAP = {
        "left": ("left",), "right": ("right",), "top": ("top",), "bottom": ("bottom",),
        "tl": ("top", "left"), "tr": ("top", "right"),
        "bl": ("bottom", "left"), "br": ("bottom", "right"),
    }

    def eventFilter(self, obj, event):
        edge_name = obj.property("_edge") if obj else None
        if not edge_name:
            return super().eventFilter(obj, event)

        if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self._resize_edge = self._EDGE_MAP.get(edge_name)
            self._drag_pos = event.globalPosition().toPoint()
            self._resize_geo = self.geometry()
            return True
        if event.type() == QEvent.Type.MouseMove and self._resize_edge and self._drag_pos:
            delta = event.globalPosition().toPoint() - self._drag_pos
            geo = QRect(self._resize_geo)
            if "right" in self._resize_edge:
                geo.setRight(geo.right() + delta.x())
            if "bottom" in self._resize_edge:
                geo.setBottom(geo.bottom() + delta.y())
            if "left" in self._resize_edge:
                geo.setLeft(geo.left() + delta.x())
            if "top" in self._resize_edge:
                geo.setTop(geo.top() + delta.y())
            if geo.width() >= 300 and geo.height() >= 400:
                self.setGeometry(geo)
            return True
        if event.type() == QEvent.Type.MouseButtonRelease:
            self._drag_pos = None
            self._resize_edge = None
            return True
        return super().eventFilter(obj, event)

    # --- Window dragging (title bar) ---

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()
        if pos.y() < 40:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._resize_edge = None
        event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos and not self._resize_edge:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        self._resize_edge = None

    # --- Save geometry on resize/move (debounced) ---

    def _save_geometry(self):
        self.settings.set("window", "width", self.width())
        self.settings.set("window", "height", self.height())
        pos = self.pos()
        self.settings.set("window", "x", pos.x())
        self.settings.set("window", "y", pos.y())

    def reset_geometry_to_default(self):
        self.resize(600, 800)
        self.move(0, 0)
        self._save_geometry()

    def shutdown(self):
        if self._shutdown_done:
            return
        self._shutdown_done = True

        self._title_typing_timer.stop()
        self._save_geo_timer.stop()
        self._tool_queue.clear()
        self._pending_tool_call = None
        self._pending_tool_id = None
        self._tool_flow_stopped = True

        try:
            self.api_client.cancel()
        except Exception:
            pass

        stream_worker = self._stream_worker
        self._stream_worker = None
        if stream_worker is not None:
            try:
                stream_worker.cancel()
            except Exception:
                pass
            try:
                stream_worker.wait(self.WORKER_SHUTDOWN_TIMEOUT_MS)
            except Exception:
                pass

        tool_worker = self._tool_worker
        self._tool_worker = None
        if tool_worker is not None:
            try:
                tool_worker.wait(self.WORKER_SHUTDOWN_TIMEOUT_MS)
            except Exception:
                pass

        title_worker = self._title_worker
        self._title_worker = None
        if title_worker is not None:
            try:
                title_worker.wait(self.WORKER_SHUTDOWN_TIMEOUT_MS)
            except Exception:
                pass

        try:
            self.tool_runtime.close()
        except Exception:
            pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._save_geo_timer.start()
        self._overlay.setGeometry(self.rect())
        self.sidebar.setFixedHeight(self.height())
        self._layout_grips()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._save_geo_timer.start()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
