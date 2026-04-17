"""Lite Toolcall WebSocket server integration."""

from __future__ import annotations

import base64
import json
import mimetypes
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, QTimer, QUrl, Signal
from PySide6.QtNetwork import QHostAddress
from PySide6.QtWebSockets import QWebSocket, QWebSocketProtocol, QWebSocketServer

from src.config import RESOURCE_DIR, Settings, get_app_version
from src.tool_runtime import ToolCall, ToolParser, ToolResult, ToolRuntime


@dataclass(frozen=True)
class LiteToolcallConfig:
    enabled: bool
    connection_mode: str
    host: str
    port: int
    token: str

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "LiteToolcallConfig":
        settings = settings or Settings()
        mode = settings.get("lite_toolcall", "connection_mode", "forward")
        if mode not in {"forward", "reverse"}:
            mode = "forward"
        try:
            port = int(settings.get("lite_toolcall", "port", 8765))
        except (TypeError, ValueError):
            port = 8765
        return cls(
            enabled=bool(settings.get("lite_toolcall", "enabled", False)),
            connection_mode=mode,
            host=(settings.get("lite_toolcall", "host", "127.0.0.1") or "127.0.0.1").strip(),
            port=max(1, min(65535, port)),
            token=str(settings.get("lite_toolcall", "token", "") or ""),
        )


class _RunWorker(QThread):
    finished_with_payload = Signal(object)

    def __init__(self, runtime: ToolRuntime, raw: str, parent=None):
        super().__init__(parent)
        self._runtime = runtime
        self._raw = raw
        self._session_id = f"lite-toolcall-{uuid.uuid4().hex[:12]}"

    def run(self):
        payload = self._execute()
        self.finished_with_payload.emit(payload)

    def _execute(self) -> dict[str, Any]:
        raw = self._raw.strip()
        if not raw:
            return {"result": "[调用失败] raw 不能为空。", "status": 0}
        if not raw.lower().startswith("<tool_calls"):
            return {"result": "[调用失败] raw 必须是完整的 <tool_calls>...</tool_calls> XML。", "status": 0}

        _, groups = ToolParser.parse_response(raw)
        calls = [call for group in groups for call in group]
        if not calls:
            return {"result": "[调用失败] 未解析到可执行工具调用。", "status": 0}

        results: list[ToolResult] = []
        for call in calls:
            result = self._execute_call(call)
            results.append(result)

        status = 1 if all(result.status == "success" for result in results) else 0
        response: dict[str, Any] = {
            "result": self._format_results(results),
            "status": status,
        }
        image = self._first_image(results)
        if image is not None:
            response.update(image)
        return response

    def _execute_call(self, call: ToolCall) -> ToolResult:
        if call.parse_error:
            message = self._runtime._error_content(f"{call.display_name}调用格式无效：{call.parse_error}")
            return ToolResult(call.tool_name, "error", message, message)

        mode = self._runtime.get_mode(call.tool_name)
        if mode not in {"auto", "on"}:
            message = self._runtime._error_content(f"{call.display_name}工具在设置中关闭。")
            return ToolResult(call.tool_name, "error", message, message)

        return self._runtime.execute(call, self._session_id)

    @staticmethod
    def _format_results(results: list[ToolResult]) -> str:
        parts: list[str] = []
        for index, result in enumerate(results, 1):
            title = ToolCall(tool_name=result.tool_name, raw_body="").display_name
            if len(results) == 1:
                parts.append(result.content)
            else:
                parts.append(f"[{index}] {title}\n{result.content}")
        return "\n\n".join(part.strip() for part in parts if part.strip())

    @staticmethod
    def _first_image(results: list[ToolResult]) -> dict[str, Any] | None:
        for result in results:
            for attachment in result.attachments:
                path = Path(attachment)
                if not path.exists() or not path.is_file():
                    continue
                mime = mimetypes.guess_type(path.name)[0] or ""
                if not mime.startswith("image/"):
                    continue
                try:
                    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
                except OSError:
                    continue
                return {"img_base64": encoded, "img_mime": mime}
        return None


class LiteToolcallServer(QObject):
    statusChanged = Signal(str, str)
    HEARTBEAT_TIMEOUT_SECONDS = 15
    MAX_RECONNECT_ATTEMPTS = 5

    def __init__(self, parent=None):
        super().__init__(parent)
        self._config: LiteToolcallConfig | None = None
        self._runtime: ToolRuntime | None = None
        self._server: QWebSocketServer | None = None
        self._reverse_socket: QWebSocket | None = None
        self._clients: set[QWebSocket] = set()
        self._authed: set[QWebSocket] = set()
        self._last_activity: dict[QWebSocket, float] = {}
        self._workers: set[_RunWorker] = set()
        self._permanent_disconnect = False
        self._reverse_reconnect_attempts = 0

        self._timeout_timer = QTimer(self)
        self._timeout_timer.setInterval(1000)
        self._timeout_timer.timeout.connect(self._on_timeout)

        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.setInterval(5000)
        self._reconnect_timer.timeout.connect(self._connect_reverse)

    def start(self, config: LiteToolcallConfig) -> bool:
        self.stop(emit_status=False)
        self._config = config
        self._permanent_disconnect = False
        self._reverse_reconnect_attempts = 0

        if not config.enabled:
            return False
        if not config.token:
            self.statusChanged.emit("Lite Toolcall 启动失败", "Token 不能为空。")
            return False

        self._runtime = ToolRuntime()
        if config.connection_mode == "reverse":
            self._connect_reverse()
            return True
        return self._listen_forward()

    def stop(self, emit_status: bool = True):
        self._reconnect_timer.stop()
        self._timeout_timer.stop()

        for worker in list(self._workers):
            worker.finished_with_payload.disconnect()
            worker.finished.disconnect()
            worker.quit()
            worker.wait(1000)
            worker.deleteLater()
        self._workers.clear()

        for socket in list(self._clients):
            self._disconnect_socket_signals(socket)
            self._close_socket(socket)
        self._clients.clear()
        self._authed.clear()
        self._last_activity.clear()

        if self._reverse_socket is not None:
            self._disconnect_socket_signals(self._reverse_socket)
            self._reverse_socket.close()
            self._reverse_socket.deleteLater()
            self._reverse_socket = None

        if self._server is not None:
            self._server.close()
            self._server.deleteLater()
            self._server = None

        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None

        if emit_status:
            self.statusChanged.emit("Lite Toolcall 已停止", "服务已关闭。")

    def restart_if_changed(self, config: LiteToolcallConfig):
        if self._config == config:
            return
        if config.enabled:
            self.start(config)
        else:
            self.stop()
            self._config = config

    def _listen_forward(self) -> bool:
        assert self._config is not None
        self._server = QWebSocketServer(
            "PeekAgent Lite Toolcall",
            QWebSocketServer.SslMode.NonSecureMode,
            self,
        )
        self._server.newConnection.connect(self._on_new_connection)
        ok = self._server.listen(QHostAddress(self._config.host), self._config.port)
        if not ok:
            error_text = self._server.errorString()
            self.statusChanged.emit("Lite Toolcall 启动失败", error_text or "监听失败。")
            self.stop(emit_status=False)
            return False
        self._timeout_timer.start()
        self.statusChanged.emit(
            "Lite Toolcall 已启动",
            f"正在监听 ws://{self._config.host}:{self._config.port}",
        )
        return True

    def _connect_reverse(self):
        if self._config is None or self._permanent_disconnect:
            return
        if self._reverse_reconnect_attempts >= self.MAX_RECONNECT_ATTEMPTS:
            self.statusChanged.emit(
                "Lite Toolcall 重连停止",
                f"已达到最大重试次数 {self.MAX_RECONNECT_ATTEMPTS} 次。",
            )
            return
        if self._reverse_socket is not None:
            self._disconnect_socket_signals(self._reverse_socket)
            self._reverse_socket.deleteLater()
        self._reverse_socket = QWebSocket("", QWebSocketProtocol.Version.VersionLatest, self)
        self._connect_socket_signals(self._reverse_socket)
        self._reverse_socket.connected.connect(self._on_reverse_connected)
        url = QUrl(f"ws://{self._config.host}:{self._config.port}")
        self._reverse_socket.open(url)
        if not self._timeout_timer.isActive():
            self._timeout_timer.start()
        self.statusChanged.emit("Lite Toolcall 正在连接", f"正在连接 {url.toString()}")

    def _on_reverse_connected(self):
        if self._reverse_socket is None:
            return
        self._clients.add(self._reverse_socket)
        self._last_activity[self._reverse_socket] = time.monotonic()
        self._reverse_reconnect_attempts = 0
        self.statusChanged.emit("Lite Toolcall 已连接", "反向连接已建立，等待客户端认证。")

    def _on_new_connection(self):
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            self._clients.add(socket)
            self._last_activity[socket] = time.monotonic()
            self._connect_socket_signals(socket)
            self.statusChanged.emit("Lite Toolcall 客户端已连接", "等待认证。")

    def _connect_socket_signals(self, socket: QWebSocket):
        socket.textMessageReceived.connect(lambda message, ws=socket: self._on_message(ws, message))
        socket.disconnected.connect(lambda ws=socket: self._on_disconnected(ws))
        socket.errorOccurred.connect(lambda _error, ws=socket: self._on_socket_error(ws))

    @staticmethod
    def _disconnect_socket_signals(socket: QWebSocket):
        for signal in (socket.textMessageReceived, socket.disconnected, socket.errorOccurred):
            try:
                signal.disconnect()
            except (RuntimeError, TypeError):
                pass

    def _on_socket_error(self, socket: QWebSocket):
        error_text = socket.errorString()
        if error_text:
            self.statusChanged.emit("Lite Toolcall 连接错误", error_text)

    def _on_disconnected(self, socket: QWebSocket):
        self._clients.discard(socket)
        self._authed.discard(socket)
        self._last_activity.pop(socket, None)
        if socket is self._reverse_socket:
            self._reverse_socket = None
            self.statusChanged.emit("Lite Toolcall 连接断开", "反向连接已断开。")
            self._schedule_reverse_reconnect()
        else:
            self.statusChanged.emit("Lite Toolcall 客户端断开", "连接已关闭。")
        socket.deleteLater()

    def _schedule_reverse_reconnect(self):
        if (
            self._permanent_disconnect
            or self._config is None
            or not self._config.enabled
            or self._config.connection_mode != "reverse"
        ):
            return
        if self._reverse_reconnect_attempts >= self.MAX_RECONNECT_ATTEMPTS:
            self.statusChanged.emit(
                "Lite Toolcall 重连停止",
                f"已达到最大重试次数 {self.MAX_RECONNECT_ATTEMPTS} 次。",
            )
            return
        if self._reconnect_timer.isActive():
            return
        self._reverse_reconnect_attempts += 1
        self.statusChanged.emit(
            "Lite Toolcall 准备重连",
            f"将在 5 秒后重试（{self._reverse_reconnect_attempts}/{self.MAX_RECONNECT_ATTEMPTS}）。",
        )
        self._reconnect_timer.start()

    def _on_timeout(self):
        if not self._clients:
            return
        now = time.monotonic()
        for socket in list(self._clients):
            last_activity = self._last_activity.get(socket, now)
            if now - last_activity < self.HEARTBEAT_TIMEOUT_SECONDS:
                continue
            self._close_socket(socket)
            self.statusChanged.emit("Lite Toolcall 连接超时", "15 秒未收到心跳或返回，连接已关闭。")

    def _on_message(self, socket: QWebSocket, message: str):
        self._last_activity[socket] = time.monotonic()
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            self._send(socket, {"result": "[调用失败] 消息不是有效 JSON。", "status": 0})
            return
        if not isinstance(data, dict):
            self._send(socket, {"result": "[调用失败] 消息必须是 JSON 对象。", "status": 0})
            return

        action = data.get("action")
        if action == "auth":
            self._handle_auth(socket, data)
            return

        if socket not in self._authed:
            self._send(socket, {"result": "[调用失败] 请先完成 auth 认证。", "status": 0})
            socket.close()
            return

        if action == "hello":
            return
        if action == "get_prompt":
            self._send(socket, {"prompt": self._build_prompt()})
            return
        if action == "ping":
            self._send(socket, {"action": "pong"})
            return
        if action == "disconnect":
            self._permanent_disconnect = True
            self._close_socket(socket)
            return
        if action == "run":
            self._handle_run(socket, data)
            return

        self._send(socket, {"result": f"[调用失败] 不支持的 action：{action}", "status": 0})

    def _handle_auth(self, socket: QWebSocket, data: dict[str, Any]):
        assert self._config is not None
        if str(data.get("token", "")) != self._config.token:
            self._send(socket, {"result": "[调用失败] Token 认证失败。", "status": 0})
            self.statusChanged.emit("Lite Toolcall 认证失败", "收到错误 token，连接已关闭。")
            socket.close()
            return
        self._authed.add(socket)
        self._send(socket, {"action": "hello", "name": "PeekAgent", "ver": get_app_version()})
        self.statusChanged.emit("Lite Toolcall 认证成功", "客户端已通过认证。")

    def _handle_run(self, socket: QWebSocket, data: dict[str, Any]):
        if self._runtime is None:
            self._send(socket, {"result": "[调用失败] Lite Toolcall 服务未就绪。", "status": 0})
            return
        raw = str(data.get("raw", "") or "")
        worker = _RunWorker(self._runtime, raw, self)
        self._workers.add(worker)
        worker.finished_with_payload.connect(lambda payload, ws=socket: self._send(ws, payload))
        worker.finished.connect(lambda w=worker: self._on_worker_finished(w))
        worker.start()

    def _on_worker_finished(self, worker: _RunWorker):
        self._workers.discard(worker)
        worker.deleteLater()

    @staticmethod
    def _build_prompt() -> str:
        prompt_path = RESOURCE_DIR / "TOOLS.md"
        try:
            return prompt_path.read_text(encoding="utf-8")
        except OSError:
            return "PeekAgent 工具说明不可用。"

    @staticmethod
    def _send(socket: QWebSocket, payload: dict[str, Any]):
        try:
            socket.sendTextMessage(json.dumps(payload, ensure_ascii=False))
        except RuntimeError:
            pass

    @staticmethod
    def _close_socket(socket: QWebSocket):
        try:
            socket.close()
        except RuntimeError:
            pass
