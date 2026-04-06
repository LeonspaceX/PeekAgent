"""Shared LLM endpoint adapter for OpenAI-compatible, Anthropic-compatible, and Gemini APIs."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import requests
from PySide6.QtCore import QThread, Signal

APP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/134.0.0.0 Safari/537.36"
)
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096


def build_request_headers(api_key: str, endpoint_type: str, stream: bool = False) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": APP_USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    if endpoint_type == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = ANTHROPIC_VERSION
    elif endpoint_type == "gemini":
        headers["x-goog-api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _decode_sse_line(raw_line) -> str:
    if isinstance(raw_line, bytes):
        return raw_line.decode("utf-8", errors="replace")
    return raw_line


def _extract_openai_text(message_content) -> str:
    if isinstance(message_content, str):
        return message_content
    if isinstance(message_content, list):
        texts = []
        for item in message_content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                if text:
                    texts.append(text)
        return "".join(texts)
    return ""


def _extract_anthropic_text(message_content) -> str:
    if isinstance(message_content, str):
        return message_content
    if isinstance(message_content, list):
        texts = []
        for item in message_content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                if text:
                    texts.append(text)
        return "".join(texts)
    return ""


def _extract_gemini_text(parts) -> str:
    if isinstance(parts, str):
        return parts
    if isinstance(parts, list):
        texts = []
        for item in parts:
            if not isinstance(item, dict):
                continue
            text = item.get("text", "")
            if text:
                texts.append(text)
        return "".join(texts)
    return ""


def _data_url_to_anthropic_source(url: str) -> dict | None:
    if not url.startswith("data:") or ";base64," not in url:
        return None
    header, data = url.split(",", 1)
    media_type = header[5:].split(";", 1)[0] or "image/png"
    return {
        "type": "base64",
        "media_type": media_type,
        "data": data,
    }


@dataclass
class RequestConfig:
    model: str
    messages: list
    system_prompt: str = ""
    stream: bool = False
    max_tokens: int | None = None
    extra_payload: dict | None = None


class LLMClient:
    def __init__(self, endpoint_url: str, api_key: str, endpoint_type: str = "openai"):
        self.base_url = endpoint_url.strip().rstrip("/")
        self.api_key = api_key.strip()
        self.endpoint_type = endpoint_type or "openai"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": APP_USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })

    def _messages_path(self) -> str:
        if self.endpoint_type == "anthropic":
            return "/messages"
        if self.endpoint_type == "gemini":
            return ""
        return "/chat/completions"

    @staticmethod
    def _normalize_gemini_model_name(model: str) -> str:
        model = (model or "").strip()
        return model if model.startswith("models/") else f"models/{model}"

    def _build_openai_payload(self, config: RequestConfig) -> dict:
        messages = list(config.messages)
        if config.system_prompt:
            messages = [{"role": "system", "content": config.system_prompt}] + messages
        payload = {
            "model": config.model,
            "messages": messages,
        }
        if config.stream:
            payload["stream"] = True
        if config.max_tokens is not None:
            payload["max_tokens"] = config.max_tokens
        if config.extra_payload:
            payload.update(config.extra_payload)
        return payload

    def _convert_anthropic_content(self, content) -> list[dict]:
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        if not isinstance(content, list):
            return [{"type": "text", "text": str(content)}]

        blocks = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                text = item.get("text", "")
                if text:
                    blocks.append({"type": "text", "text": text})
            elif item_type == "image_url":
                image_url = item.get("image_url", {}).get("url", "")
                source = _data_url_to_anthropic_source(image_url)
                if source:
                    blocks.append({"type": "image", "source": source})
        return blocks or [{"type": "text", "text": ""}]

    def _build_anthropic_payload(self, config: RequestConfig) -> dict:
        messages = []
        for message in config.messages:
            if message.get("role") == "system":
                continue
            messages.append({
                "role": message["role"],
                "content": self._convert_anthropic_content(message.get("content", "")),
            })
        payload = {
            "model": config.model,
            "messages": messages,
            "max_tokens": config.max_tokens or DEFAULT_MAX_TOKENS,
        }
        if config.system_prompt:
            payload["system"] = config.system_prompt
        if config.stream:
            payload["stream"] = True
        if config.extra_payload:
            payload.update(config.extra_payload)
        return payload

    def _convert_gemini_part(self, item: dict) -> dict | None:
        item_type = item.get("type")
        if item_type == "text":
            text = item.get("text", "")
            if text:
                return {"text": text}
        elif item_type == "image_url":
            image_url = item.get("image_url", {}).get("url", "")
            source = _data_url_to_anthropic_source(image_url)
            if source:
                return {
                    "inline_data": {
                        "mime_type": source["media_type"],
                        "data": source["data"],
                    }
                }
        return None

    def _convert_gemini_content(self, content) -> list[dict]:
        if isinstance(content, str):
            return [{"text": content}]
        if not isinstance(content, list):
            return [{"text": str(content)}]

        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            part = self._convert_gemini_part(item)
            if part:
                parts.append(part)
        return parts or [{"text": ""}]

    def _build_gemini_payload(self, config: RequestConfig) -> dict:
        contents = []
        for message in config.messages:
            role = message.get("role")
            if role == "system":
                continue
            contents.append({
                "role": "model" if role == "assistant" else "user",
                "parts": self._convert_gemini_content(message.get("content", "")),
            })

        payload = {"contents": contents}
        if config.system_prompt:
            payload["system_instruction"] = {
                "parts": [{"text": config.system_prompt}],
            }
        generation_config = {}
        if config.max_tokens is not None:
            generation_config["maxOutputTokens"] = config.max_tokens
        if generation_config:
            payload["generationConfig"] = generation_config
        if config.extra_payload:
            payload.update(config.extra_payload)
        return payload

    def build_payload(self, config: RequestConfig) -> dict:
        if self.endpoint_type == "anthropic":
            return self._build_anthropic_payload(config)
        if self.endpoint_type == "gemini":
            return self._build_gemini_payload(config)
        return self._build_openai_payload(config)

    def _build_request_url(self, model: str, stream: bool = False) -> str:
        if self.endpoint_type != "gemini":
            return f"{self.base_url}{self._messages_path()}"
        action = "streamGenerateContent" if stream else "generateContent"
        url = f"{self.base_url}/{self._normalize_gemini_model_name(model)}:{action}"
        if stream:
            url = f"{url}?alt=sse"
        return url

    def create_completion(self, model: str, messages: list, system_prompt: str = "", **extra_payload) -> dict:
        max_tokens = extra_payload.pop("max_tokens", None)
        payload = self.build_payload(RequestConfig(
            model=model,
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            extra_payload=extra_payload or None,
        ))
        response = self.session.post(
            self._build_request_url(model, stream=False),
            json=payload,
            headers=build_request_headers(self.api_key, self.endpoint_type),
            timeout=(10, 60),
        )
        response.raise_for_status()
        return response.json()

    def create_stream(self, model: str, messages: list, system_prompt: str = "", **extra_payload):
        max_tokens = extra_payload.pop("max_tokens", None)
        payload = self.build_payload(RequestConfig(
            model=model,
            messages=messages,
            system_prompt=system_prompt,
            stream=True,
            max_tokens=max_tokens,
            extra_payload=extra_payload or None,
        ))
        response = self.session.post(
            self._build_request_url(model, stream=True),
            json=payload,
            headers=build_request_headers(self.api_key, self.endpoint_type, stream=True),
            timeout=(10, 300),
            stream=True,
        )
        response.encoding = "utf-8"
        response.raise_for_status()
        return response

    def fetch_models(self) -> list[str]:
        if self.endpoint_type == "gemini":
            models = []
            page_token = ""
            while True:
                params = {}
                params["pageSize"] = 1000
                if page_token:
                    params["pageToken"] = page_token
                response = self.session.get(
                    f"{self.base_url}/models",
                    params=params,
                    headers=build_request_headers(self.api_key, self.endpoint_type),
                    timeout=(10, 30),
                )
                response.raise_for_status()
                data = response.json()
                for model in data.get("models", []):
                    if not isinstance(model, dict):
                        continue
                    methods = model.get("supportedGenerationMethods") or model.get("supported_generation_methods") or []
                    if methods and "generateContent" not in methods:
                        continue
                    model_id = model.get("name", "") or model.get("baseModelId") or model.get("base_model_id", "")
                    if model_id:
                        models.append(model_id)
                page_token = data.get("nextPageToken", "")
                if not page_token:
                    break
            return sorted(set(models))
        response = self.session.get(
            f"{self.base_url}/models",
            headers=build_request_headers(self.api_key, self.endpoint_type),
            timeout=(10, 30),
        )
        response.raise_for_status()
        data = response.json()
        return sorted(model["id"] for model in data.get("data", []) if model.get("id"))

    def test_connection(self, model: str) -> int:
        start = time.time()
        self.create_completion(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
        )
        return int((time.time() - start) * 1000)

    def extract_text(self, response_json: dict) -> str:
        if self.endpoint_type == "anthropic":
            return _extract_anthropic_text(response_json.get("content", []))
        if self.endpoint_type == "gemini":
            candidates = response_json.get("candidates") or []
            if not candidates:
                return ""
            content = candidates[0].get("content", {})
            return _extract_gemini_text(content.get("parts", []))
        choices = response_json.get("choices") or []
        if not choices:
            return ""
        return _extract_openai_text(choices[0].get("message", {}).get("content"))


class StreamWorker(QThread):
    token_received = Signal(str)
    stream_finished = Signal(str)
    error_occurred = Signal(str)

    def __init__(self, client: LLMClient, model: str, messages: list, system_prompt: str = ""):
        super().__init__()
        self.client = client
        self.model = model
        self.messages = messages
        self.system_prompt = system_prompt
        self._cancelled = False
        self._response = None

    def run(self):
        full_text = ""
        try:
            with self.client.create_stream(
                model=self.model,
                messages=self.messages,
                system_prompt=self.system_prompt,
            ) as response:
                self._response = response
                if self.client.endpoint_type == "anthropic":
                    full_text = self._run_anthropic_stream(response)
                elif self.client.endpoint_type == "gemini":
                    full_text = self._run_gemini_stream(response)
                else:
                    full_text = self._run_openai_stream(response)
            if not self._cancelled:
                self.stream_finished.emit(full_text)
        except Exception as e:
            if not self._cancelled:
                self.error_occurred.emit(str(e))
        finally:
            self._response = None

    def _run_openai_stream(self, response) -> str:
        full_text = ""
        for raw_line in response.iter_lines(decode_unicode=False):
            if self._cancelled:
                return full_text
            if not raw_line:
                continue
            line = _decode_sse_line(raw_line).strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            event = json.loads(data)
            choices = event.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            chunk_text = _extract_openai_text(delta.get("content"))
            if chunk_text:
                full_text += chunk_text
                self.token_received.emit(chunk_text)
        return full_text

    def _run_anthropic_stream(self, response) -> str:
        full_text = ""
        for raw_line in response.iter_lines(decode_unicode=False):
            if self._cancelled:
                return full_text
            if not raw_line:
                continue
            line = _decode_sse_line(raw_line).strip()
            if line.startswith("event:"):
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            event = json.loads(data)
            event_type = event.get("type")
            if event_type == "content_block_start":
                block = event.get("content_block", {})
                chunk_text = block.get("text", "")
            elif event_type == "content_block_delta":
                delta = event.get("delta", {})
                chunk_text = delta.get("text", "")
            else:
                chunk_text = ""
            if chunk_text:
                full_text += chunk_text
                self.token_received.emit(chunk_text)
            if event_type == "message_stop":
                break
        return full_text

    def _run_gemini_stream(self, response) -> str:
        full_text = ""
        for raw_line in response.iter_lines(decode_unicode=False):
            if self._cancelled:
                return full_text
            if not raw_line:
                continue
            line = _decode_sse_line(raw_line).strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            event = json.loads(data)
            candidates = event.get("candidates") or []
            if not candidates:
                continue
            content = candidates[0].get("content", {})
            chunk_text = _extract_gemini_text(content.get("parts", []))
            if chunk_text:
                full_text += chunk_text
                self.token_received.emit(chunk_text)
        return full_text

    def cancel(self):
        self._cancelled = True
        if self._response is not None:
            self._response.close()
