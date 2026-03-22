"""High-level application API client backed by the shared LLM adapter."""

from PySide6.QtCore import QThread, Signal

from src.config import PROMPT_DIR, RESOURCE_DIR, Settings
from src.llm_client import LLMClient, StreamWorker


class ApiClient:
    def __init__(self):
        self._client: LLMClient | None = None
        self._current_worker: StreamWorker | None = None
        self._last_url: str = ""
        self._last_key: str = ""
        self._last_endpoint_type: str = ""

    def _ensure_client(self):
        settings = Settings()
        url = settings.get("model", "endpoint_url", "")
        key = settings.get("model", "api_key", "")
        endpoint_type = settings.get("model", "endpoint_type", "openai")
        if not url or not key:
            raise ValueError("请先在设置中配置端点 URL 和 API Key")

        url = url.rstrip("/")
        if (
            self._client is None
            or url != self._last_url
            or key != self._last_key
            or endpoint_type != self._last_endpoint_type
        ):
            self._client = LLMClient(url, key, endpoint_type)
            self._last_url = url
            self._last_key = key
            self._last_endpoint_type = endpoint_type

    def _build_system_prompt(self) -> str:
        parts = []
        system_path = PROMPT_DIR / "SYSTEM.md"
        memory_path = PROMPT_DIR / "MEMORY.md"
        tools_path = RESOURCE_DIR / "TOOLS.md"
        if system_path.exists():
            parts.append(system_path.read_text(encoding="utf-8"))
        if memory_path.exists():
            memory = memory_path.read_text(encoding="utf-8")
            parts.append(f"你的记忆：\n{memory}".rstrip())
        if tools_path.exists():
            tools = tools_path.read_text(encoding="utf-8").strip()
            if tools:
                parts.append(tools)
        return "\n\n".join(parts)

    def send_stream(self, messages: list) -> StreamWorker:
        self._ensure_client()
        settings = Settings()
        model = settings.get("model", "model_name", "")
        if not model:
            raise ValueError("请先在设置中选择模型")

        worker = StreamWorker(
            self._client,
            model=model,
            messages=messages,
            system_prompt=self._build_system_prompt(),
        )
        self._current_worker = worker
        return worker

    def create_completion(
        self,
        model: str,
        messages: list,
        system_prompt: str | None = None,
        **extra_payload,
    ) -> dict:
        self._ensure_client()
        merged_system_prompt = self._build_system_prompt()
        if system_prompt:
            merged_system_prompt = (
                f"{merged_system_prompt}\n\n{system_prompt}" if merged_system_prompt else system_prompt
            )
        return self._client.create_completion(
            model=model,
            messages=messages,
            system_prompt=merged_system_prompt,
            **extra_payload,
        )

    def extract_text(self, response_json: dict) -> str:
        self._ensure_client()
        return self._client.extract_text(response_json)

    def cancel(self):
        if self._current_worker:
            self._current_worker.cancel()


class TitleWorker(QThread):
    """Generate a short title for a conversation in background."""

    title_ready = Signal(str)

    def __init__(self, api_client: ApiClient, model: str, user_msg: str, ai_msg: str):
        super().__init__()
        self.api_client = api_client
        self.model = model
        self.user_msg = user_msg
        self.ai_msg = ai_msg

    def run(self):
        try:
            response = self.api_client.create_completion(
                model=self.model,
                messages=[
                    {"role": "user", "content": self.user_msg},
                    {"role": "assistant", "content": self.ai_msg[:200]},
                    {"role": "user", "content": "请为这段对话生成标题。"},
                ],
                system_prompt="根据以下对话生成一个简短的标题（10字以内），只输出标题文字，不要引号和标点。",
                max_tokens=30,
            )
            title = self.api_client.extract_text(response).strip().strip('"\'')
            if title:
                self.title_ready.emit(title)
        except Exception:
            pass
