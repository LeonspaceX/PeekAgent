"""Chat session CRUD – manages data/context/*.json files."""

import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from src.config import CONTEXT_DIR

ATTACHMENTS_DIR = CONTEXT_DIR / "attachments"


def normalize_session_title(title: str | None) -> str:
    value = (title or "").replace("\r", "").replace("\n", "").strip()
    return value or "新对话"


class ChatManager:
    def __init__(self):
        CONTEXT_DIR.mkdir(parents=True, exist_ok=True)

    def list_sessions(self) -> list[dict]:
        """Return list of {id, title, created_at} sorted by created_at desc."""
        sessions = []
        for f in CONTEXT_DIR.glob("*.json"):
            try:
                with open(f, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                sessions.append({
                    "id": data["id"],
                    "title": normalize_session_title(data.get("title", "新对话")),
                    "created_at": data.get("created_at", ""),
                })
            except (json.JSONDecodeError, KeyError):
                continue
        sessions.sort(key=lambda s: s["created_at"], reverse=True)
        return sessions

    def create_session(self, title: str = "新对话") -> dict:
        sid = str(uuid.uuid4())
        data = {
            "id": sid,
            "title": normalize_session_title(title),
            "created_at": datetime.now().isoformat(),
            "messages": [],
        }
        self._save(sid, data)
        return data

    def load_session(self, sid: str) -> dict | None:
        path = CONTEXT_DIR / f"{sid}.json"
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data["title"] = normalize_session_title(data.get("title", "新对话"))
        return data

    def save_session(self, data: dict):
        self._save(data["id"], data)

    def delete_session(self, sid: str):
        path = CONTEXT_DIR / f"{sid}.json"
        if path.exists():
            path.unlink()
        # Remove session attachments directory
        attach_dir = ATTACHMENTS_DIR / sid
        if attach_dir.exists():
            shutil.rmtree(attach_dir, ignore_errors=True)

    def rename_session(self, sid: str, new_title: str):
        data = self.load_session(sid)
        if data:
            data["title"] = normalize_session_title(new_title)
            self._save(sid, data)

    def append_message(self, sid: str, role: str, content: str):
        data = self.load_session(sid)
        if data:
            data["messages"].append({"role": role, "content": content})
            self._save(sid, data)

    def _save(self, sid: str, data: dict):
        path = CONTEXT_DIR / f"{sid}.json"
        data = dict(data)
        data["title"] = normalize_session_title(data.get("title", "新对话"))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
