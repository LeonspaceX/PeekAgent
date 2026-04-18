"""Phase 2 tool parsing and execution runtime."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import subprocess
import threading
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

import mss
import requests
from markdownify import markdownify as html_to_markdown
from PySide6.QtCore import QMimeData, QThread, QUrl, Signal
from PySide6.QtGui import QGuiApplication
from readability import Document

from src.background_task_manager import BackgroundTaskManager, BackgroundTaskResult
from src.chat_manager import ATTACHMENTS_DIR
from src.config import BASE_DIR, Settings
from src.ssh_manager import (
    client_command as ssh_client_command,
    client_connect as ssh_client_connect,
    client_disconnect as ssh_client_disconnect,
    client_list as ssh_client_list,
    disconnect_all_clients,
)


_TOOL_CALLS_PATTERN = re.compile(r"(?is)<tool_calls>([\s\S]*?)</tool_calls>")
_NONE_PATTERN = re.compile(r"(?is)<none>(.*?)</none>")
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_TEXT_EXTS = {
    ".md",
    ".py",
    ".json",
    ".yaml",
    ".yml",
    ".txt",
    ".toml",
    ".ini",
    ".cfg",
    ".html",
    ".js",
    ".ts",
    ".css",
}
_MAX_TEXT_RESULT = 50000
_COMMAND_TIMEOUT_SECONDS = 30
_SEARCH_MAX_FILES = 2000
_SEARCH_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache"}
_PS_PROCESS_ENCODING = "utf-8"
_PS_SETUP = "$ProgressPreference='SilentlyContinue'\n$ErrorActionPreference='Continue'\n"
_PS_RESULT_PREFIX = "__PEEKAGENT_B64_"
_WEB_FETCH_TIMEOUT = (10, 60)
_WEB_SEARCH_TIMEOUT = (10, 45)
_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_WEB_SEARCH_DEPTH_DEFAULT = "basic"
_WEB_SEARCH_DEPTH_LABELS = {
    "basic": "基础",
    "advanced": "高级",
    "fast": "快速",
}
_WEB_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/134.0.0.0 Safari/537.36"
    ),
    "Accept": "text/markdown, text/html;q=0.9, text/plain;q=0.7, */*;q=0.5",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@dataclass
class ToolCall:
    tool_name: str
    raw_body: str
    payload: Any = None
    parse_error: str | None = None

    @property
    def display_name(self) -> str:
        return {
            "read": "读取文件",
            "search": "搜索文本",
            "write": "写入文件",
            "add": "追加内容",
            "replace": "替换内容",
            "command": "执行命令",
            "background": "后台任务",
            "capture": "截图",
            "web-fetch": "抓取网页",
            "web-search": "联网搜索",
            "clipboard": "写入剪贴板",
            "client_list": "SSH 客户端列表",
            "client_connect": "连接 SSH 客户端",
            "client_command": "SSH 远程执行命令",
            "client_disconnect": "断开 SSH 客户端",
        }.get(self.tool_name, self.tool_name)


@dataclass
class ToolResult:
    tool_name: str
    status: str
    detail: str
    content: str
    attachments: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


class ToolParser:
    _TOOL_NAMES = {
        "read",
        "search",
        "write",
        "add",
        "replace",
        "command",
        "background",
        "capture",
        "web-fetch",
        "web-search",
        "clipboard",
        "client_list",
        "client_connect",
        "client_command",
        "client_disconnect",
    }

    @staticmethod
    def parse_response(text: str) -> tuple[str, list[list[ToolCall]]]:
        masked_text, placeholders = ToolParser._mask_none_blocks(text)
        groups: list[list[ToolCall]] = []
        display = _TOOL_CALLS_PATTERN.sub('', masked_text)

        for match in _TOOL_CALLS_PATTERN.finditer(masked_text):
            tool_block = match.group(1)
            calls: list[ToolCall] = []
            try:
                root = ET.fromstring(f"<tool_calls>{tool_block}</tool_calls>")
                for child in root:
                    tool_name = child.tag.lower()
                    if tool_name not in ToolParser._TOOL_NAMES:
                        raise ValueError(f"unsupported tool `{tool_name}`")
                    raw_body = ET.tostring(child, encoding="unicode")
                    payload = None
                    parse_error = None
                    try:
                        payload = ToolParser._parse_tool_payload(child)
                    except Exception as exc:
                        parse_error = str(exc)
                    calls.append(
                        ToolCall(
                            tool_name=tool_name,
                            raw_body=raw_body,
                            payload=payload,
                            parse_error=parse_error,
                        )
                    )
            except Exception as exc:
                calls.append(
                    ToolCall(
                        tool_name="tool_calls",
                        raw_body=tool_block,
                        parse_error=str(exc),
                    )
                )
            if calls:
                groups.append(calls)

        for key, value in placeholders.items():
            display = display.replace(key, value)
        return display.strip(), groups

    @staticmethod
    def _mask_none_blocks(text: str) -> tuple[str, dict[str, str]]:
        placeholders: dict[str, str] = {}

        def repl(match: re.Match[str]) -> str:
            key = f"__PEEK_NONE_{len(placeholders)}__"
            placeholders[key] = match.group(1)
            return key

        return _NONE_PATTERN.sub(repl, text), placeholders


    @staticmethod
    def _parse_tool_payload(node: ET.Element):
        tool_name = node.tag.lower()

        if tool_name == "capture":
            return {}

        if tool_name == "read":
            path = (node.get("path") or (node.text or "")).strip()
            if not path:
                raise ValueError("`read` requires `path`")
            start_line = ToolParser._parse_optional_positive_int(node.get("start_line"), "read.start_line")
            end_line = ToolParser._parse_optional_positive_int(node.get("end_line"), "read.end_line")
            if start_line is not None and end_line is not None and start_line > end_line:
                raise ValueError("`read.start_line` must be <= `read.end_line`")
            return {"path": path, "start_line": start_line, "end_line": end_line}

        if tool_name == "web-fetch":
            url = (node.get("url") or (node.text or "")).strip()
            if not url:
                raise ValueError("`web-fetch` requires `url`")
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("`web-fetch` only supports http/https URL")
            return {"url": url}

        if tool_name == "web-search":
            query = (node.get("query") or (node.text or "")).strip()
            if not query:
                raise ValueError("`web-search` requires `query`")
            topic = (node.get("topic") or "general").strip().lower() or "general"
            if topic not in {"general", "news"}:
                raise ValueError("`web-search.topic` must be `general` or `news`")
            max_results = ToolParser._parse_optional_positive_int(node.get("max_results"), "web-search.max_results") or 5
            search_depth = (node.get("search_depth") or _WEB_SEARCH_DEPTH_DEFAULT).strip().lower() or _WEB_SEARCH_DEPTH_DEFAULT
            if search_depth not in _WEB_SEARCH_DEPTH_LABELS:
                raise ValueError("`web-search.search_depth` must be `basic`, `advanced`, or `fast`")
            days = ToolParser._parse_optional_positive_int(node.get("days"), "web-search.days")
            if topic != "news":
                days = None
            return {
                "query": query,
                "topic": topic,
                "max_results": max_results,
                "search_depth": search_depth,
                "days": days,
                "include_domains": ToolParser._parse_csv_list(node.get("include_domains")),
                "exclude_domains": ToolParser._parse_csv_list(node.get("exclude_domains")),
            }


        if tool_name == "clipboard":
            raw_paths = []
            if node.get("path"):
                raw_paths.append(node.get("path", ""))
            raw_paths.extend(ToolParser._parse_csv_list(node.get("paths")))
            paths = [item.strip() for item in raw_paths if item and item.strip()]
            text_content = (node.get("text") or ToolParser._node_text(node)).strip()
            if paths:
                return {"kind": "files", "paths": paths}
            if text_content:
                return {"kind": "text", "text": text_content}
            raise ValueError("`clipboard` requires text, `path`, or `paths`")

        if tool_name in {"write", "add"}:
            path = (node.get("path") or "").strip()
            if not path:
                raise ValueError(f"`{tool_name}` requires `path`")
            content_node = node.find("content")
            if content_node is None:
                raise ValueError(f"`{tool_name}` requires `<content>`")
            return {"path": path, "content": ToolParser._node_text(content_node)}

        if tool_name == "replace":
            path = (node.get("path") or "").strip()
            if not path:
                raise ValueError("`replace` requires `path`")
            replacements = []

            replacement_nodes = node.findall("replacement")
            if not replacement_nodes:
                raise ValueError("`replace` requires one or more `<replacement>` blocks")
            for index, replacement_node in enumerate(replacement_nodes, 1):
                old_node = replacement_node.find("old")
                new_node = replacement_node.find("new")
                if old_node is None or new_node is None:
                    raise ValueError(f"`replace.replacement[{index}]` requires `<old>` and `<new>`")
                replacements.append({
                    "old": ToolParser._node_text(old_node),
                    "new": ToolParser._node_text(new_node),
                })

            if not replacements:
                raise ValueError("`replace` requires at least one replacement pair")

            return {"path": path, "replacements": replacements}

        if tool_name == "search":
            path = (node.get("path") or "").strip()
            pattern = (node.get("pattern") or "").strip()
            if not path:
                raise ValueError("`search` requires `path`")
            if not pattern:
                raise ValueError("`search` requires `pattern`")
            glob = (node.get("glob") or "*").strip() or "*"
            max_results = ToolParser._parse_optional_positive_int(node.get("max_results"), "search.max_results") or 20
            before = ToolParser._parse_optional_non_negative_int(node.get("before"), "search.before") or 2
            after = ToolParser._parse_optional_non_negative_int(node.get("after"), "search.after") or 2
            case_sensitive = (node.get("case_sensitive") or "false").strip().lower() == "true"
            return {
                "path": path,
                "pattern": pattern,
                "glob": glob,
                "max_results": max_results,
                "before": before,
                "after": after,
                "case_sensitive": case_sensitive,
            }

        if tool_name == "command":
            content = (node.get("content") or ToolParser._node_text(node)).strip()
            if not content:
                raise ValueError("`command` requires content")
            timeout = node.get("timeout_seconds")
            if timeout is not None:
                try:
                    timeout = int(timeout)
                except (TypeError, ValueError) as exc:
                    raise ValueError("`command.timeout_seconds` must be a positive integer") from exc
                if timeout <= 0:
                    raise ValueError("`command.timeout_seconds` must be greater than 0")
            else:
                timeout = 30
            context = node.get("context")
            if context is not None:
                context = context.strip() or None
            return {"content": content, "context": context, "timeout_seconds": timeout}

        if tool_name == "background":
            title = (node.get("title") or "").strip()
            content = (node.get("content") or ToolParser._node_text(node)).strip()
            if not title:
                raise ValueError("`background` requires `title`")
            if not content:
                raise ValueError("`background` requires content")
            timeout = ToolParser._parse_optional_positive_int(node.get("timeout_seconds"), "background.timeout_seconds")
            if timeout is None:
                raise ValueError("`background` requires `timeout_seconds`")
            context = node.get("context")
            if context is not None:
                context = context.strip() or None
            return {"title": title, "content": content, "timeout_seconds": timeout, "context": context}

        if tool_name == "client_list":
            return {}

        if tool_name == "client_connect":
            name = (node.get("name") or (node.text or "")).strip()
            if not name:
                raise ValueError("`client_connect` requires `name`")
            return {"name": name}

        if tool_name == "client_command":
            name = (node.get("name") or "").strip()
            command = (node.get("command") or ToolParser._node_text(node)).strip()
            if not name:
                raise ValueError("`client_command` requires `name`")
            if not command:
                raise ValueError("`client_command` requires `command`")
            timeout = ToolParser._parse_optional_positive_int(node.get("timeout"), "client_command.timeout") or 30
            return {"name": name, "command": command, "timeout": timeout}

        if tool_name == "client_disconnect":
            name = (node.get("name") or (node.text or "")).strip()
            if not name:
                raise ValueError("`client_disconnect` requires `name`")
            return {"name": name}

        raise ValueError(f"unsupported tool `{tool_name}`")

    @staticmethod
    def _node_text(node: ET.Element) -> str:
        return ''.join(node.itertext())

    @staticmethod
    def _parse_optional_positive_int(value: str | None, field_name: str) -> int | None:
        if value is None or not value.strip():
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"`{field_name}` must be a positive integer") from exc
        if parsed <= 0:
            raise ValueError(f"`{field_name}` must be greater than 0")
        return parsed

    @staticmethod
    def _parse_optional_non_negative_int(value: str | None, field_name: str) -> int | None:
        if value is None or not value.strip():
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"`{field_name}` must be a non-negative integer") from exc
        if parsed < 0:
            raise ValueError(f"`{field_name}` must be >= 0")
        return parsed

    @staticmethod
    def _parse_csv_list(value: str | None) -> list[str]:
        if value is None or not value.strip():
            return []
        return [item.strip() for item in value.split(",") if item.strip()]


class PowerShellContextManager:
    def __init__(self):
        self._contexts: dict[str, subprocess.Popen] = {}
        self._context_io_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def close_all(self):
        with self._lock:
            contexts = list(self._contexts.values())
            self._contexts.clear()
            self._context_io_locks.clear()
        for process in contexts:
            self._close_process(process)

    def run_once_detailed(self, command: str, timeout_seconds: int | None = None) -> tuple[str, bool, int | None]:
        effective_timeout = timeout_seconds if timeout_seconds is not None else _COMMAND_TIMEOUT_SECONDS
        marker = self._create_marker()
        process = self._create_process()
        try:
            assert process.stdin is not None
            process.stdin.write(self._build_capture_script(command, marker) + "\n")
            process.stdin.flush()
            decoded, timed_out, exit_code = self._read_until_marker(process, marker, effective_timeout)
        finally:
            self._close_process(process)

        if timed_out:
            timeout_text = (
                f"命令执行超过 {effective_timeout} 秒，当前一次性 PowerShell 进程已被关闭。"
                " 如果这个命令本来就需要交互输入，请改用更明确、不会阻塞的命令。"
            )
            return timeout_text, True, None

        return decoded, False, exit_code

    def run_once(self, command: str, timeout_seconds: int | None = None) -> tuple[str, bool]:
        output, timed_out, _ = self.run_once_detailed(command, timeout_seconds)
        return output, timed_out

    def run_detailed(
        self,
        command: str,
        context_id: str | None,
        timeout_seconds: int | None = None,
    ) -> tuple[str, str, bool, bool, int | None]:
        with self._lock:
            created = False
            if not context_id:
                context_id = self._create_context_locked()
                created = True
            elif context_id not in self._contexts:
                context_id = self._create_context_locked(context_id)
                created = True
            process = self._contexts[context_id]
            context_lock = self._context_io_locks[context_id]

        with context_lock:
            marker = self._create_marker()
            script = self._build_capture_script(command, marker)
            try:
                assert process.stdin is not None
                process.stdin.write(script + "\n")
                process.stdin.flush()
            except Exception:
                with self._lock:
                    self._contexts.pop(context_id, None)
                self._close_process(process)
                process = self._create_process()
                with self._lock:
                    self._contexts[context_id] = process
                assert process.stdin is not None
                process.stdin.write(script + "\n")
                process.stdin.flush()

            effective_timeout = timeout_seconds if timeout_seconds is not None else _COMMAND_TIMEOUT_SECONDS
            output, timed_out, exit_code = self._read_until_marker(
                process,
                marker,
                effective_timeout,
                timeout_closer=lambda: self._close_context_process(context_id, process),
            )
        if timed_out:
            timeout_text = (
                f"命令执行超过 {effective_timeout} 秒，当前 PowerShell 上下文已被关闭。"
                " 如果这个命令本来就需要交互输入，请改用更明确、不会阻塞的命令。"
            )
            return context_id, timeout_text, created, True, None

        return context_id, output, created, False, exit_code

    def run(self, command: str, context_id: str | None, timeout_seconds: int | None = None) -> tuple[str, str, bool, bool]:
        context_id, output, created, timed_out, _ = self.run_detailed(command, context_id, timeout_seconds)
        return context_id, output, created, timed_out

    def _create_context_locked(self, context_id: str | None = None) -> str:
        context_id = context_id or uuid.uuid4().hex[:8]
        self._contexts[context_id] = self._create_process()
        self._context_io_locks[context_id] = threading.Lock()
        return context_id

    def _close_context_process(self, context_id: str, process: subprocess.Popen):
        with self._lock:
            current = self._contexts.get(context_id)
            if current is process:
                self._contexts.pop(context_id, None)
                self._context_io_locks.pop(context_id, None)
        self._close_process(process)

    @staticmethod
    def _create_process() -> subprocess.Popen:
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            kwargs["startupinfo"] = startupinfo
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        return subprocess.Popen(
            ["powershell", "-NoLogo", "-NoProfile", "-Command", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding=_PS_PROCESS_ENCODING,
            errors="replace",
            bufsize=1,
            cwd=str(BASE_DIR),
            **kwargs,
        )

    @staticmethod
    def _close_process(process: subprocess.Popen):
        try:
            if process.stdin:
                process.stdin.close()
        except Exception:
            pass
        try:
            process.terminate()
        except Exception:
            pass
        try:
            process.wait(timeout=1)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    @staticmethod
    def _create_marker() -> str:
        return f"{_PS_RESULT_PREFIX}{uuid.uuid4().hex}__"

    @staticmethod
    def _build_capture_script(command: str, marker: str) -> str:
        command_literal = base64.b64encode(command.encode("utf-8")).decode("ascii")
        marker_literal = json.dumps(marker)
        return (
            f"{_PS_SETUP}"
            "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
            "$OutputEncoding = [Console]::OutputEncoding\n"
            f"$__peekMarker = {marker_literal}\n"
            f"$__peekCommand = {json.dumps(command_literal)}\n"
            "$__peekSource = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($__peekCommand)); "
            "$__peekExitCode = 0; "
            "try { "
            "$__peekOutput = (& ([ScriptBlock]::Create($__peekSource)) *>&1 | Out-String -Width 4096); "
            "if ($LASTEXITCODE -is [int]) { $__peekExitCode = $LASTEXITCODE } "
            "} catch { "
            "$__peekOutput = ($_ | Out-String -Width 4096); "
            "$__peekExitCode = if ($LASTEXITCODE -is [int]) { $LASTEXITCODE } else { 1 } "
            "}; "
            "$__peekResult = @{ output = $__peekOutput; exit_code = $__peekExitCode } | ConvertTo-Json -Compress -Depth 4; "
            "$__peekBytes = [System.Text.Encoding]::UTF8.GetBytes($__peekResult); "
            "$__peekPayload = [Convert]::ToBase64String($__peekBytes); "
            'Write-Output ($__peekMarker + $__peekPayload)' "\n"
        )

    @staticmethod
    def _decode_payload(payload: str | None) -> tuple[str, int | None]:
        if not payload:
            return "", None
        try:
            decoded = base64.b64decode(payload).decode("utf-8")
            data = json.loads(decoded)
            if isinstance(data, dict):
                return str(data.get("output", "")).strip(), data.get("exit_code")
            return decoded.strip(), None
        except Exception:
            return payload.strip(), None

    @staticmethod
    def _read_until_marker(
        process: subprocess.Popen,
        marker: str,
        timeout_seconds: int,
        timeout_closer=None,
    ) -> tuple[str, bool, int | None]:
        timed_out = threading.Event()

        def on_timeout():
            timed_out.set()
            if timeout_closer:
                timeout_closer()
            else:
                PowerShellContextManager._close_process(process)

        timer = threading.Timer(timeout_seconds, on_timeout)
        timer.daemon = True
        timer.start()
        raw_lines: list[str] = []
        payload: str | None = None
        try:
            assert process.stdout is not None
            while True:
                line = process.stdout.readline()
                if not line:
                    break
                line = line.rstrip("\r\n")
                if line.startswith(marker):
                    payload = line[len(marker) :]
                    break
                raw_lines.append(line)
        finally:
            timer.cancel()

        if timed_out.is_set():
            return "", True, None

        output, exit_code = PowerShellContextManager._decode_payload(payload)
        if not output and raw_lines:
            output = "\n".join(raw_lines).strip()
        return output, False, exit_code


class ToolRuntime:
    def __init__(self):
        self.settings = Settings()
        self.command_contexts = PowerShellContextManager()
        self.background_tasks = BackgroundTaskManager(self._run_background_command)

    def close(self):
        self.background_tasks.close()
        self.command_contexts.close_all()
        disconnect_all_clients()

    def get_command_output_limit(self) -> int:
        value = self.settings.get("tools", "command_output_limit", 12000)
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = 12000
        return max(100, value)

    def get_mode(self, tool_name: str) -> str:
        if tool_name == "read":
            return "auto" if self.settings.get("tools", "read_enabled", True) else "off"
        if tool_name == "search":
            return "auto" if self.settings.get("tools", "search_enabled", True) else "off"
        if tool_name == "capture":
            return self.settings.get("tools", "capture_mode", "manual")
        if tool_name == "background":
            return self.settings.get("tools", "command_mode", "manual")
        if tool_name == "web-fetch":
            return "auto" if self.settings.get("tools", "web_fetch_enabled", True) else "off"
        if tool_name == "web-search":
            return "auto" if self.settings.get("tools", "web_search_enabled", True) else "off"
        if tool_name == "clipboard":
            return "auto" if self.settings.get("tools", "clipboard_enabled", True) else "off"
        if tool_name in {"client_list", "client_connect", "client_command", "client_disconnect"}:
            return self.settings.get("tools", "ssh_remote_command_mode", "manual")
        key = tool_name.replace("-", "_")
        return self.settings.get("tools", f"{key}_mode", "manual")

    def _get_tavily_api_key(self) -> str:
        return (self.settings.get("integrations", "tavily_api_key", "") or "").strip()

    def _run_background_command(
        self,
        command: str,
        context_id: str | None,
        timeout_seconds: int | None,
    ) -> tuple[str, bool, int | None, str | None]:
        if context_id:
            context_id, output, _, timed_out, exit_code = self.command_contexts.run_detailed(
                command,
                context_id,
                timeout_seconds,
            )
            return output, timed_out, exit_code, context_id
        output, timed_out, exit_code = self.command_contexts.run_once_detailed(command, timeout_seconds)
        return output, timed_out, exit_code, None

    def execute(self, call: ToolCall, session_id: str | None = None) -> ToolResult:
        if call.parse_error:
            message = self._error_content(f"{call.display_name}调用格式无效：{call.parse_error}")
            return ToolResult(call.tool_name, "error", message, message)

        try:
            if call.tool_name == "read":
                return self._read(call.payload)
            if call.tool_name == "search":
                return self._search(call.payload)
            if call.tool_name == "write":
                return self._write(call.payload)
            if call.tool_name == "add":
                return self._add(call.payload)
            if call.tool_name == "replace":
                return self._replace(call.payload)
            if call.tool_name == "command":
                return self._command(call.payload)
            if call.tool_name == "background":
                return self._background(call.payload, session_id)
            if call.tool_name == "capture":
                return self._capture(session_id)
            if call.tool_name == "web-fetch":
                return self._web_fetch(call.payload)
            if call.tool_name == "web-search":
                return self._web_search(call.payload)
            if call.tool_name == "clipboard":
                return self._clipboard(call.payload)
            if call.tool_name == "client_list":
                return self._client_list()
            if call.tool_name == "client_connect":
                return self._client_connect(call.payload)
            if call.tool_name == "client_command":
                return self._client_command(call.payload)
            if call.tool_name == "client_disconnect":
                return self._client_disconnect(call.payload)
        except Exception as exc:
            message = self._error_content(f"{call.display_name}失败：{exc}")
            return ToolResult(call.tool_name, "error", message, message)

        message = self._error_content(f"不支持的工具 `{call.tool_name}`。")
        return ToolResult(call.tool_name, "error", message, message)

    def _read(self, payload: dict[str, Any]) -> ToolResult:
        path = self._resolve_path(payload["path"])
        if not path.exists():
            raise FileNotFoundError(f"文件不存在：{path}")

        if path.is_dir():
            names = sorted(item.name for item in path.iterdir())
            preview = "\n".join(names[:200])
            detail = preview or "目录为空。"
            content = self._success_content(f"已读取目录：{path}\n{detail}")
            return ToolResult("read", "success", detail, content)

        if path.suffix.lower() in _IMAGE_EXTS:
            detail = str(path)
            content = self._success_content(f"已读取图片：{path}")
            return ToolResult("read", "success", detail, content, attachments=[str(path)])

        mime = mimetypes.guess_type(path.name)[0] or ""
        if mime.startswith("text/") or path.suffix.lower() in _TEXT_EXTS:
            full_text = path.read_text(encoding="utf-8")
            start_line = payload.get("start_line")
            end_line = payload.get("end_line")
            if start_line is not None or end_line is not None:
                lines = full_text.splitlines()
                start_line = start_line or 1
                end_line = end_line or max(1, len(lines))
                if lines:
                    start_index = max(0, start_line - 1)
                    end_index = min(len(lines), end_line)
                    if start_index >= len(lines):
                        raise ValueError("读取起始行超出文件范围")
                    if end_index <= start_index:
                        raise ValueError("未找到匹配内容")
                    selected = lines[start_index:end_index]
                    text = "\n".join(f"{start_index + i + 1}: {line}" for i, line in enumerate(selected))
                else:
                    text = ""
                detail = f"{path}\n第 {start_line} 行 - 第 {end_line} 行"
            else:
                text = full_text
                detail = str(path)

            if len(text) > _MAX_TEXT_RESULT:
                text = text[:_MAX_TEXT_RESULT] + "\n...[内容已截断]"
            content = self._success_content(f"已读取文件：{path}\n\n{text}")
            return ToolResult("read", "success", detail, content)

        size = path.stat().st_size
        detail = f"{path}\n大小：{size} 字节"
        content = self._error_content(f"`{path}` 是二进制文件，当前 `read` 只适合文本和图片。")
        return ToolResult("read", "error", detail, content)

    def _search(self, payload: dict[str, Any]) -> ToolResult:
        base_path = self._resolve_path(payload["path"])
        if not base_path.exists():
            raise FileNotFoundError(f"路径不存在：{base_path}")

        if base_path.is_file():
            files = [base_path]
            truncated = False
        else:
            files = []
            truncated = False
            for file_path in base_path.rglob(payload["glob"]):
                if not file_path.is_file():
                    continue
                if any(part in _SEARCH_SKIP_DIRS for part in file_path.parts):
                    continue
                files.append(file_path)
                if len(files) >= _SEARCH_MAX_FILES:
                    truncated = True
                    break

        flags = 0 if payload.get("case_sensitive") else re.IGNORECASE
        pattern = re.compile(re.escape(payload["pattern"]), flags)
        before = payload.get("before", 2)
        after = payload.get("after", 2)
        max_results = payload.get("max_results", 20)
        results: list[str] = []

        for file_path in files:
            try:
                file_text = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            lines = file_text.splitlines()
            for index, line in enumerate(lines):
                if not pattern.search(line):
                    continue
                start = max(0, index - before)
                end = min(len(lines), index + after + 1)
                snippet = "\n".join(f"{i + 1}: {lines[i]}" for i in range(start, end))
                try:
                    display_path = str(file_path.relative_to(BASE_DIR))
                except ValueError:
                    display_path = str(file_path)
                results.append(f"[{len(results) + 1}] {display_path}:{index + 1}\n{snippet}")
                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break

        if not results:
            raise ValueError("未找到匹配内容")

        body = "\n\n".join(results)
        if len(body) > _MAX_TEXT_RESULT:
            body = body[:_MAX_TEXT_RESULT] + "\n...[内容已截断]"
        if truncated:
            body += f"\n\n...[已提前停止扫描，最多检查 {_SEARCH_MAX_FILES} 个文件]"
        detail = str(base_path)
        content = self._success_content(f"已搜索路径：{base_path}\n\n{body}")
        return ToolResult("search", "success", detail, content)

    def _write(self, payload: dict[str, str]) -> ToolResult:
        path = self._resolve_path(payload["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload["content"], encoding="utf-8")
        detail = str(path)
        content = self._success_content(f"已写入文件：{path}")
        return ToolResult("write", "success", detail, content)

    def _add(self, payload: dict[str, str]) -> ToolResult:
        path = self._resolve_path(payload["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(payload["content"])
        detail = str(path)
        content = self._success_content(f"已在文件末尾追加内容：{path}")
        return ToolResult("add", "success", detail, content)

    def _replace(self, payload: dict[str, Any]) -> ToolResult:
        path = self._resolve_path(payload["path"])
        if not path.exists():
            raise FileNotFoundError(f"文件不存在：{path}")

        original = path.read_text(encoding="utf-8")
        replacements = payload.get("replacements") or []
        if not replacements:
            raise ValueError("`replace` requires at least one replacement pair")

        new_text = original
        for index, item in enumerate(replacements, 1):
            old = item.get("old", "")
            new = item.get("new", "")
            if not old:
                raise ValueError(f"`replace.replacements[{index}].old` 不能为空")

            match_count = new_text.count(old)
            if match_count == 0:
                raise ValueError(f"第 {index} 组未找到匹配的旧文本")
            if match_count > 1:
                raise ValueError(f"第 {index} 组旧文本命中了 {match_count} 处，`replace` exact 模式只允许唯一命中")

            new_text = new_text.replace(old, new, 1)
        path.write_text(new_text, encoding="utf-8")
        detail = str(path)
        content = self._success_content(f"已精确替换文件内容：{path}\n共执行 {len(replacements)} 组替换")
        return ToolResult("replace", "success", detail, content)

    def _command(self, payload: dict[str, Any]) -> ToolResult:
        output_limit = self.get_command_output_limit()
        context_id = payload.get("context")
        if context_id:
            context_id, output, created, timed_out = self.command_contexts.run(
                payload["content"],
                context_id,
                payload.get("timeout_seconds"),
            )
            detail = payload["content"]
            if timed_out:
                content = self._error_content(output)
                return ToolResult("command", "error", detail, content, meta={"context": context_id})
            output = self._truncate_command_output(output, output_limit)
            if output:
                info = (
                    f"PowerShell 上下文：`{context_id}`。"
                    f"{' 已新建该上下文。' if created else ''}\n\n返回信息：\n{output}"
                )
            else:
                info = (
                    f"PowerShell 上下文：`{context_id}`。"
                    f"{' 已新建该上下文。' if created else ''}\n未产生可见输出。"
                )
            content = self._success_content(info)
            return ToolResult("command", "success", detail, content, meta={"context": context_id})

        output, timed_out = self.command_contexts.run_once(
            payload["content"],
            payload.get("timeout_seconds"),
        )
        detail = payload["content"]
        if timed_out:
            content = self._error_content(output)
            return ToolResult("command", "error", detail, content)
        output = self._truncate_command_output(output, output_limit)
        if output:
            info = f"本次未保留终端上下文。\n\n返回信息：\n{output}"
        else:
            info = "本次未保留终端上下文。\n未产生可见输出。"
        content = self._success_content(info)
        return ToolResult("command", "success", detail, content)

    def _background(self, payload: dict[str, Any], session_id: str | None) -> ToolResult:
        context_line = f"PowerShell 上下文：{payload.get('context')}\n" if payload.get("context") else ""
        task_id = self.background_tasks.start_task(
            title=payload["title"],
            command=payload["content"],
            context_id=payload.get("context"),
            timeout_seconds=payload["timeout_seconds"],
            session_id=session_id,
        )
        detail = (
            f"任务ID：{task_id}\n"
            f"任务标题：{payload['title']}\n"
            f"超时时间：{payload['timeout_seconds']} 秒\n"
            f"{context_line}"
            f"命令：\n{payload['content']}"
        )
        content = self._success_content(f"任务已启动，ID: {task_id}")
        return ToolResult(
            "background",
            "success",
            detail,
            content,
            meta={
                "task_id": task_id,
                "title": payload["title"],
                "timeout_seconds": payload["timeout_seconds"],
            },
        )

    def _capture(self, session_id: str | None) -> ToolResult:
        attach_dir = ATTACHMENTS_DIR / session_id if session_id else ATTACHMENTS_DIR / "tool-temp"
        attach_dir.mkdir(parents=True, exist_ok=True)
        capture_path = attach_dir / f"capture_{uuid.uuid4().hex[:12]}.png"
        with mss.mss() as sct:
            sct.shot(output=str(capture_path))
        detail = "截图成功！"
        content = self._success_content(f"截图已保存：{capture_path}")
        return ToolResult("capture", "success", detail, content, attachments=[str(capture_path)])

    def _web_fetch(self, payload: dict[str, str]) -> ToolResult:
        url = payload["url"]
        response = requests.get(url, headers=_WEB_FETCH_HEADERS, timeout=_WEB_FETCH_TIMEOUT)
        response.raise_for_status()
        response.encoding = response.encoding or response.apparent_encoding or "utf-8"
        final_url = response.url or url

        content_type = (response.headers.get("Content-Type") or "").lower()
        body = response.text or ""
        markdown = ""

        if "text/markdown" in content_type or "text/x-markdown" in content_type:
            markdown = body
        elif "text/plain" in content_type and final_url.lower().endswith((".md", ".markdown")):
            markdown = body
        else:
            article = Document(body)
            title = article.short_title() or final_url
            article_html = article.summary(html_partial=True)
            markdown_body = html_to_markdown(article_html, heading_style="ATX").strip()
            if title and not markdown_body.startswith("# "):
                markdown = f"# {title}\n\n{markdown_body}".strip()
            else:
                markdown = markdown_body

        markdown = markdown.strip()
        if not markdown:
            raise ValueError("网页正文为空，无法提取可用内容。")
        if len(markdown) > _MAX_TEXT_RESULT:
            markdown = markdown[:_MAX_TEXT_RESULT] + "\n...[内容已截断]"

        detail = final_url if final_url == url else f"请求地址: {url}\n最终地址: {final_url}"
        content = self._success_content(f"已抓取网页：{final_url}\n\n{markdown}")
        return ToolResult("web-fetch", "success", detail, content)

    def _web_search(self, payload: dict[str, Any]) -> ToolResult:
        api_key = self._get_tavily_api_key()
        if not api_key:
            raise ValueError("未配置 Tavily API Key，请先在设置中填写。")

        body: dict[str, Any] = {
            "api_key": api_key,
            "query": payload["query"],
            "topic": payload.get("topic", "general"),
            "search_depth": payload.get("search_depth", _WEB_SEARCH_DEPTH_DEFAULT),
            "max_results": payload.get("max_results", 5),
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        if payload.get("include_domains"):
            body["include_domains"] = payload["include_domains"]
        if payload.get("exclude_domains"):
            body["exclude_domains"] = payload["exclude_domains"]
        if payload.get("topic") == "news" and payload.get("days") is not None:
            body["days"] = payload["days"]

        response = requests.post(_TAVILY_SEARCH_URL, json=body, timeout=_WEB_SEARCH_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        results = data.get("results") or []
        if not results:
            raise ValueError("未找到可用搜索结果。")

        lines: list[str] = []
        for index, item in enumerate(results, 1):
            title = (item.get("title") or item.get("url") or "未命名结果").strip()
            url = (item.get("url") or "").strip()
            snippet = (item.get("content") or item.get("snippet") or "").strip()
            domain = (urlparse(url).netloc or "").strip()
            published = (item.get("published_date") or item.get("published_at") or "").strip()

            lines.append(f"[{index}] {title}")
            meta_parts = [part for part in (domain, published, url) if part]
            if meta_parts:
                lines.append(" | ".join(meta_parts))
            if snippet:
                lines.append(snippet)
            lines.append("")

        body_text = "\n".join(lines).strip()
        if len(body_text) > _MAX_TEXT_RESULT:
            body_text = body_text[:_MAX_TEXT_RESULT] + "\n...[内容已截断]"

        topic = payload.get("topic", "general")
        topic_label = "新闻" if topic == "news" else "通用"
        depth = payload.get("search_depth", _WEB_SEARCH_DEPTH_DEFAULT)
        depth_label = _WEB_SEARCH_DEPTH_LABELS.get(depth, depth)
        detail_lines = [
            f"查询词: {payload['query']}",
            f"主题: {topic_label}",
        ]
        if payload.get("days") is not None:
            detail_lines.append(f"时间范围天数: {payload['days']}")
        if payload.get("include_domains"):
            detail_lines.append("包含站点: " + ", ".join(payload["include_domains"]))
        if payload.get("exclude_domains"):
            detail_lines.append("排除站点: " + ", ".join(payload["exclude_domains"]))
        detail_lines.append(f"搜索深度: {depth_label}")
        detail = "\n".join(detail_lines)
        content = self._success_content(f"已完成网页搜索：{payload['query']}\n\n{body_text}")
        return ToolResult("web-search", "success", detail, content)

    def _clipboard(self, payload: dict[str, Any]) -> ToolResult:
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            raise ValueError("当前环境不可用系统剪贴板。")

        if payload.get("kind") == "files":
            paths = [self._resolve_path(item) for item in payload.get("paths", [])]
            missing = [str(item) for item in paths if not item.exists()]
            if missing:
                raise FileNotFoundError("以下路径不存在：" + ", ".join(missing))

            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(item)) for item in paths])
            clipboard.setMimeData(mime)
            detail = "\n".join(str(item) for item in paths)
            content = self._success_content("已将文件列表写入系统剪贴板。")
            return ToolResult("clipboard", "success", detail, content)

        text_value = payload.get("text", "")
        clipboard.setText(text_value)
        detail = text_value if len(text_value) <= 500 else text_value[:500] + "..."
        content = self._success_content("已将文本写入系统剪贴板。")
        return ToolResult("clipboard", "success", detail, content)

    def _client_list(self) -> ToolResult:
        clients = ssh_client_list()
        if not clients:
            detail = "读取已配置 SSH 客户端及连接状态"
            content = self._success_content("当前没有已配置的 SSH 客户端。")
            return ToolResult("client_list", "success", detail, content)

        lines = [f"- {item['name']} | {'已连接' if item.get('connected') else '未连接'}" for item in clients]
        body = "\n".join(lines)
        detail = "读取已配置 SSH 客户端及连接状态"
        content = self._success_content(f"SSH 客户端列表：\n{body}")
        return ToolResult("client_list", "success", detail, content)

    def _client_connect(self, payload: dict[str, Any]) -> ToolResult:
        _, created = ssh_client_connect(payload["name"])
        detail = f"连接至{payload['name']}"
        content = self._success_content(
            f"{'已建立' if created else '已复用'} SSH 会话：{payload['name']}"
        )
        return ToolResult("client_connect", "success", detail, content)

    def _client_command(self, payload: dict[str, Any]) -> ToolResult:
        result = ssh_client_command(payload["name"], payload["command"], payload.get("timeout", 30))
        detail = f"执行节点：{payload['name']}\n执行命令：{payload['command']}"
        if not result.get("ok"):
            message = result.get("error", "远程命令执行失败。")
            content = self._error_content(message)
            return ToolResult("client_command", "error", detail, content)

        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        exit_code = result.get("exit_code", -1)
        output_parts = [f"exit_code: {exit_code}"]
        if stdout:
            output_parts.append(f"stdout:\n{self._truncate_command_output(stdout, self.get_command_output_limit())}")
        if stderr:
            output_parts.append(f"stderr:\n{self._truncate_command_output(stderr, self.get_command_output_limit())}")
        if not stdout and not stderr:
            output_parts.append("命令执行完成，无可见输出。")
        content = self._success_content("\n\n".join(output_parts))
        return ToolResult("client_command", "success", detail, content)

    def _client_disconnect(self, payload: dict[str, Any]) -> ToolResult:
        disconnected = ssh_client_disconnect(payload["name"])
        detail = payload["name"]
        if disconnected:
            content = self._success_content(f"已断开 SSH 会话：{payload['name']}")
        else:
            content = self._success_content(f"SSH 会话 `{payload['name']}` 当前未连接。")
        return ToolResult("client_disconnect", "success", detail, content)

    @staticmethod
    def _success_content(message: str) -> str:
        message = message.strip()
        return "[调用成功！]" if not message else f"[调用成功！] {message}"

    @staticmethod
    def _error_content(message: str) -> str:
        message = message.strip()
        return "[调用失败]" if not message else f"[调用失败] {message}"

    @staticmethod
    def _truncate_command_output(output: str, limit: int) -> str:
        if len(output) <= limit:
            return output
        return output[:limit] + "\n...[输出已截断]"

    @staticmethod
    def _resolve_path(path_str: str) -> Path:
        path = Path(os.path.expandvars(os.path.expanduser(path_str.strip())))
        if not path.is_absolute():
            path = BASE_DIR / path
        return path.resolve()


class ToolExecutionWorker(QThread):
    finished_with_result = Signal(object)

    def __init__(self, runtime: ToolRuntime, call: ToolCall, session_id: str | None):
        super().__init__()
        self.runtime = runtime
        self.call = call
        self.session_id = session_id

    def run(self):
        result = self.runtime.execute(self.call, self.session_id)
        self.finished_with_result.emit(result)
