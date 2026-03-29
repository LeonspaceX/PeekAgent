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
        "capture_enabled": True,
        "web_fetch_mode": "manual",
        "web_search_enabled": True,
        "clipboard_enabled": True,
        "command_output_limit": 12000,
        "auto_tool_round_limit": 8,
    },
    "model": {
        "endpoint_url": "",
        "api_key": "",
        "model_name": "",
        "endpoint_type": "openai",
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
            # Merge defaults for any missing keys
            for section, defaults in DEFAULT_SETTINGS.items():
                if section not in self._data:
                    self._data[section] = dict(defaults)
                else:
                    for key, val in defaults.items():
                        if key not in self._data[section]:
                            self._data[section][key] = val

    def save(self):
        with self._data_lock:
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)

    def get(self, section: str, key: str, default=None):
        with self._data_lock:
            return self._data.get(section, {}).get(key, default)

    def set(self, section: str, key: str, value):
        with self._data_lock:
            if section not in self._data:
                self._data[section] = {}
            self._data[section][key] = value
            self.save()

    @property
    def data(self) -> dict:
        with self._data_lock:
            return json.loads(json.dumps(self._data))
