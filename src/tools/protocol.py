"""Lightweight tool protocol types and XML parser."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

_TOOL_CALLS_PATTERN = re.compile(r"(?is)<tool_calls>([\s\S]*?)</tool_calls>")
_NONE_PATTERN = re.compile(r"(?is)<none>(.*?)</none>")
_WEB_SEARCH_DEPTH_DEFAULT = "basic"
_WEB_SEARCH_DEPTH_LABELS = {"basic": "基础", "advanced": "高级", "fast": "快速"}


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
            "weather": "天气查询",
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
        "weather",
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

        if tool_name == "weather":
            city = (node.get("city") or (node.text or "")).strip()
            if not city:
                raise ValueError("`weather` requires `city`")
            return {"city": city}

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



