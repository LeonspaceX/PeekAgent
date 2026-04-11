"""Global configuration manager. Reads/writes data/settings.json."""

import json
import os
import sys
import threading
from copy import deepcopy
from pathlib import Path

def _resolve_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _resolve_packaged_path(*parts: str) -> Path:
    direct_path = BASE_DIR.joinpath(*parts)
    if direct_path.exists():
        return direct_path

    internal_path = BASE_DIR / "_internal"
    if internal_path.exists():
        candidate = internal_path.joinpath(*parts)
        if candidate.exists():
            return candidate

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass).joinpath(*parts)
        if candidate.exists():
            return candidate

    return direct_path


BASE_DIR = _resolve_base_dir()
RESOURCE_DIR = _resolve_packaged_path("src", "resources")
ICON_PATH = _resolve_packaged_path("icon.png")
DATA_DIR = BASE_DIR / "data"
CONTEXT_DIR = DATA_DIR / "context"
PROMPT_DIR = DATA_DIR / "prompt"
HIGHLIGHT_THEME_PATH = DATA_DIR / "highlight.json"
SETTINGS_PATH = DATA_DIR / "settings.json"
VERSION_PATH = _resolve_packaged_path("version.txt")
HIGHLIGHT_RESOURCE_BUNDLE_PATH = RESOURCE_DIR / "highlight.json"

DEFAULT_SETTINGS = {
    "general": {
        "hotkey": "alt+z",
        "auto_start": False,
        "always_on_top": True,
        "external_prompt_editor_enabled": False,
        "task_complete_notification_enabled": False,
        "task_complete_notification_threshold_seconds": 30,
        "tool_result_context_limit": 5,
        "github_mirror": "https://v6.gh-proxy.org/",
    },
    "appearance": {
        "theme_mode": "light",
        "background_effect": "none",
        "primary_theme_color": "#0ea5a4",
        "theme_color_1": "#1a73e8",
        "theme_color_2": "#7c3aed",
    },
    "tools": {
        "read_enabled": True,
        "search_enabled": True,
        "write_mode": "manual",
        "add_mode": "manual",
        "replace_mode": "manual",
        "command_mode": "manual",
        "ssh_remote_command_mode": "manual",
        "capture_mode": "manual",
        "web_fetch_enabled": True,
        "web_search_enabled": True,
        "clipboard_enabled": True,
        "command_output_limit": 12000,
        "auto_tool_round_limit": 8,
    },
    "model": {
        "channels": [
            {
                "name": "默认渠道",
                "endpoint_url": "",
                "api_key": "",
                "endpoint_type": "openai",
            }
        ],
        "active_channel_index": 0,
        "model_name": "",
        "stream": True,
    },
    "integrations": {
        "tavily_api_key": "",
    },
    "window": {
        "width": 420,
        "height": 620,
        "x": -1,
        "y": -1,
    },
}


def detect_system_dark_mode() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return int(value) == 0
    except Exception:
        return False


def build_initial_settings() -> dict:
    data = deepcopy(DEFAULT_SETTINGS)
    if detect_system_dark_mode():
        data["appearance"]["theme_mode"] = "dark"
        data["appearance"]["primary_theme_color"] = "#23b5b5"
        data["appearance"]["theme_color_1"] = "#3b82f6"
        data["appearance"]["theme_color_2"] = "#8b5cf6"
    else:
        data["appearance"]["theme_mode"] = "light"
    return data


def get_app_version() -> str:
    if not getattr(sys, "frozen", False):
        return "development"
    try:
        value = VERSION_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return "unknown"
    return value or "unknown"


def build_default_highlight_theme_bundle() -> dict:
    bundle = load_highlight_theme_bundle(HIGHLIGHT_RESOURCE_BUNDLE_PATH)
    return bundle or {"light": {}, "dark": {}}


def _normalize_model_channel(channel: dict, fallback_name: str = "默认渠道") -> dict:
    endpoint_type = channel.get("endpoint_type", channel.get("endpoint_format", "openai"))
    if endpoint_type not in {"openai", "anthropic", "gemini"}:
        endpoint_type = "openai"
    name = str(channel.get("name") or fallback_name).strip() or fallback_name
    return {
        "name": name,
        "endpoint_url": str(channel.get("endpoint_url", channel.get("endpoint", ""))),
        "api_key": str(channel.get("api_key", "")),
        "endpoint_type": endpoint_type,
    }


def migrate_model_settings(data: dict) -> bool:
    """Migrate model settings to channels format in-place.

    Returns True when the input data changed and should be persisted.
    """
    if not isinstance(data, dict):
        return False
    model_settings = data.get("model")
    if not isinstance(model_settings, dict):
        return False

    changed = False
    has_legacy_endpoint = "endpoint_url" in model_settings or "endpoint" in model_settings
    if "channels" not in model_settings and has_legacy_endpoint:
        model_settings["channels"] = [
            _normalize_model_channel(
                {
                    "name": "默认渠道",
                    "endpoint_url": model_settings.get("endpoint_url", model_settings.get("endpoint", "")),
                    "api_key": model_settings.get("api_key", ""),
                    "endpoint_type": model_settings.get(
                        "endpoint_type",
                        model_settings.get("endpoint_format", "openai"),
                    ),
                }
            )
        ]
        model_settings["active_channel_index"] = 0
        changed = True

    if "channels" in model_settings:
        channels = model_settings.get("channels")
        if not isinstance(channels, list):
            channels = []
            changed = True
        normalized_channels = []
        for index, channel in enumerate(channels):
            if not isinstance(channel, dict):
                changed = True
                continue
            normalized = _normalize_model_channel(channel, f"渠道{index + 1}")
            if normalized != channel:
                changed = True
            normalized_channels.append(normalized)
        if not normalized_channels:
            normalized_channels = deepcopy(DEFAULT_SETTINGS["model"]["channels"])
            changed = True
        model_settings["channels"] = normalized_channels

        active_index = model_settings.get("active_channel_index", 0)
        if not isinstance(active_index, int):
            active_index = 0
            changed = True
        clamped_index = max(0, min(active_index, len(normalized_channels) - 1))
        if clamped_index != active_index:
            changed = True
        model_settings["active_channel_index"] = clamped_index

    for legacy_key in ("endpoint_url", "endpoint", "api_key", "endpoint_type", "endpoint_format"):
        if legacy_key in model_settings:
            model_settings.pop(legacy_key, None)
            changed = True

    return changed


def load_highlight_theme_bundle(path: Path = HIGHLIGHT_THEME_PATH) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    light = data.get("light")
    dark = data.get("dark")
    if not isinstance(light, dict) or not isinstance(dark, dict):
        return None
    return {"light": light, "dark": dark}


def get_highlight_theme_for_mode(dark_mode: bool, path: Path = HIGHLIGHT_THEME_PATH) -> dict:
    bundle = load_highlight_theme_bundle(path)
    if bundle is None:
        try:
            bundle = build_default_highlight_theme_bundle()
        except Exception:
            return {}
    return bundle["dark" if dark_mode else "light"]


class Settings:
    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._data = {}
                    cls._instance._data_lock = threading.RLock()
                    cls._instance._load()
        return cls._instance

    def _load(self):
        with self._data_lock:
            if SETTINGS_PATH.exists():
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            else:
                self._data = build_initial_settings()
            needs_save = migrate_model_settings(self._data)
            # Merge defaults for any missing keys
            for section, defaults in DEFAULT_SETTINGS.items():
                if section not in self._data:
                    self._data[section] = deepcopy(defaults)
                    needs_save = True
                else:
                    for key, val in defaults.items():
                        if key not in self._data[section]:
                            self._data[section][key] = deepcopy(val)
                            needs_save = True
            if migrate_model_settings(self._data):
                needs_save = True
            if needs_save:
                self.save()

    def save(self):
        with self._data_lock:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)

    def get(self, section: str, key: str, default=None):
        with self._data_lock:
            return self._data.get(section, {}).get(key, default)

    def set(self, section: str, key: str, value, save: bool = True):
        with self._data_lock:
            if section not in self._data:
                self._data[section] = {}
            self._data[section][key] = value
            if save:
                self.save()

    def save_model_active_channel_index(self, index: int):
        with self._data_lock:
            model_settings = self._data.setdefault("model", {})
            model_settings["active_channel_index"] = index

            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            if SETTINGS_PATH.exists():
                try:
                    file_data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                except Exception:
                    file_data = deepcopy(self._data)
            else:
                file_data = deepcopy(self._data)
            if not isinstance(file_data, dict):
                file_data = deepcopy(self._data)
            migrate_model_settings(file_data)
            file_model = file_data.setdefault("model", {})
            file_model["active_channel_index"] = index
            SETTINGS_PATH.write_text(
                json.dumps(file_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    @property
    def data(self) -> dict:
        with self._data_lock:
            return json.loads(json.dumps(self._data))
