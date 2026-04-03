"""PeekAgent - Lightweight floating AI assistant."""

import json
import sys
import os
import shutil
import threading
import subprocess


def _append_chromium_flag(flag: str):
    current = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
    flags = [item for item in current.split() if item]
    if flag not in flags:
        flags.append(flag)
    if flags:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags)


_append_chromium_flag("--disable-direct-composition")
_append_chromium_flag("--disable-features=DirectComposition")

# Ensure data directories exist and seed default prompt files
from src.config import BASE_DIR, ICON_PATH, SETTINGS_PATH, build_initial_settings, build_default_highlight_theme_bundle

for d in ["data/context", "data/prompt"]:
    (BASE_DIR / d).mkdir(parents=True, exist_ok=True)

# Copy editable prompt files if they don't exist yet
_prompt_dir = BASE_DIR / "data" / "prompt"
_data_dir = BASE_DIR / "data"
_defaults = {
    "SYSTEM.md": None,  # created inline below
    "MEMORY.md": None,
}
for name, src in _defaults.items():
    dest = _prompt_dir / name
    if not dest.exists():
        if src and src.exists():
            shutil.copy2(src, dest)
        elif name == "SYSTEM.md":
            dest.write_text("You are PeekAgent, a helpful AI assistant. Be concise and helpful.\n", encoding="utf-8")
        else:
            dest.write_text("", encoding="utf-8")

_highlight_dest = _data_dir / "highlight.json"
if not _highlight_dest.exists():
    _highlight_dest.write_text(
        json.dumps(build_default_highlight_theme_bundle(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

if not SETTINGS_PATH.exists():
    SETTINGS_PATH.write_text(
        json.dumps(build_initial_settings(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PySide6.QtGui import QIcon, QAction
from PySide6.QtCore import QObject, Qt, Signal, QLocale, QTimer
import keyboard
from qfluentwidgets import FluentTranslator, Theme, isDarkTheme, setTheme, setThemeColor

from src.config import Settings
from src.startup_manager import maybe_handle_startup_helper
from src.ui.main_window import MainWindow
from src.ui.settings_window import SettingsWindow
from src.ui.update_window import UpdateCompleteDialog


def _extract_update_finish_arg(argv: list[str]) -> tuple[str | None, list[str]]:
    update_finish_version = None
    filtered = [argv[0]]
    for item in argv[1:]:
        if item.startswith("--update-finish="):
            update_finish_version = item.split("=", 1)[1].strip()
            continue
        filtered.append(item)
    return update_finish_version, filtered


class _HotkeyBridge(QObject):
    activated = Signal()


class PeekAgentApp:
    def __init__(self, argv: list[str], update_finish_version: str | None = None):
        self.app = QApplication(argv)
        self.app.setQuitOnLastWindowClosed(False)
        self._fluent_translator = FluentTranslator(QLocale("zh_CN"), self.app)
        self.app.installTranslator(self._fluent_translator)

        if ICON_PATH.exists():
            self.app.setWindowIcon(QIcon(str(ICON_PATH)))

        self.settings = Settings()
        self.main_window = MainWindow()
        self.settings_window = None
        self._hotkey_handle = None
        self._registered_hotkey = None
        self._shutdown_lock = threading.Lock()
        self._shutting_down = False
        self._update_finish_version = update_finish_version
        self.hotkey_bridge = _HotkeyBridge()
        self.hotkey_bridge.activated.connect(self._toggle_window_from_hotkey)

        self._setup_tray()
        self._setup_hotkey()
        self._apply_theme()

        self.main_window.show()
        if self._update_finish_version:
            QTimer.singleShot(300, self._show_update_complete_dialog)

    def _setup_tray(self):
        icon = QIcon(str(ICON_PATH)) if ICON_PATH.exists() else QIcon()
        self.tray = QSystemTrayIcon(icon)
        self.tray.setToolTip("PeekAgent")

        self.tray_menu = QMenu()
        show_action = QAction("显示/隐藏", self.tray_menu, triggered=self._toggle_window)
        settings_action = QAction("设置", self.tray_menu, triggered=self._open_settings)
        quit_action = QAction("退出", self.tray_menu, triggered=self._quit)

        self.tray_menu.addAction(show_action)
        self.tray_menu.addAction(settings_action)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(quit_action)

        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _setup_hotkey(self):
        hotkey = (self.settings.get("general", "hotkey", "alt+z") or "").strip()
        if not hotkey:
            hotkey = "alt+z"
        normalized = hotkey.lower()
        if self._hotkey_handle is not None and normalized == self._registered_hotkey:
            return
        try:
            new_handle = keyboard.add_hotkey(
                hotkey,
                self.hotkey_bridge.activated.emit,
                suppress=(sys.platform == "win32"),
            )
        except Exception as exc:
            if hasattr(self, "tray"):
                self.tray.showMessage(
                    "快捷键注册失败",
                    f"`{hotkey}` 无法注册：{exc}",
                    QSystemTrayIcon.MessageIcon.Warning,
                    5000,
                )
            return

        if self._hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self._hotkey_handle)
            except Exception:
                pass
        self._hotkey_handle = new_handle
        self._registered_hotkey = normalized

    def _toggle_window(self):
        if self.main_window.isVisible():
            self.main_window.hide()
        else:
            self.main_window.show()
            self.main_window.activateWindow()
            self.main_window.raise_()

    def _toggle_window_from_hotkey(self):
        if self._shutting_down:
            return
        if self.main_window.isVisible():
            self.main_window.hide()
            return
        self.main_window.show()
        self.main_window.activateWindow()
        self.main_window.raise_()
        QTimer.singleShot(0, self.main_window.focus_input)

    def _on_tray_activated(self, reason):
        if self._shutting_down:
            return
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_window()

    def _open_settings(self):
        if self._shutting_down:
            return
        if self.settings_window is None or not self.settings_window.isVisible():
            self.settings_window = SettingsWindow()
            self.settings_window.settings_saved.connect(self._setup_hotkey)
            self.settings_window.settings_saved.connect(self._apply_theme)
            self.settings_window.always_on_top_changed.connect(self.main_window.set_always_on_top)
            self.settings_window.update_apply_requested.connect(self._launch_update_and_quit)
            self.settings_window.reset_window_requested.connect(
                self.main_window.reset_geometry_to_default
            )
            self.settings_window.apply_theme(isDarkTheme())
        self.settings_window.show()
        self.settings_window.activateWindow()

    def _apply_theme(self):
        theme_mode = (self.settings.get("appearance", "theme_mode", "light") or "light").strip().lower()
        theme = {
            "light": Theme.LIGHT,
            "dark": Theme.DARK,
            "auto": Theme.AUTO,
        }.get(theme_mode, Theme.LIGHT)
        setTheme(theme)
        primary_color = self.settings.get("appearance", "primary_theme_color", "#0ea5a4")
        setThemeColor(primary_color)
        self.main_window.apply_theme(isDarkTheme())
        if self.settings_window is not None:
            self.settings_window.apply_theme(isDarkTheme())

    def _release_hotkey(self):
        if self._hotkey_handle is not None:
            try:
                keyboard.remove_hotkey(self._hotkey_handle)
            except Exception:
                pass
            self._hotkey_handle = None
        self._registered_hotkey = None

    def _graceful_quit(self):
        with self._shutdown_lock:
            if self._shutting_down:
                return
            self._shutting_down = True

        try:
            self._release_hotkey()

            if self.settings_window is not None:
                try:
                    self.settings_window.close()
                except Exception:
                    pass

            if self.main_window is not None:
                try:
                    self.main_window.shutdown()
                except Exception:
                    pass
                try:
                    self.main_window.close()
                except Exception:
                    pass

            if hasattr(self, "tray"):
                try:
                    self.tray.hide()
                except Exception:
                    pass
        finally:
            self.app.quit()

    def _launch_update_and_quit(self, script_path: str):
        try:
            subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    script_path,
                ],
                creationflags=0,
                close_fds=False,
            )
        except Exception as exc:
            if hasattr(self, "tray"):
                self.tray.showMessage(
                    "升级失败",
                    f"启动升级脚本失败：{exc}",
                    QSystemTrayIcon.MessageIcon.Warning,
                    5000,
                )
            return
        self._graceful_quit()

    def _show_update_complete_dialog(self):
        dialog = UpdateCompleteDialog(self._update_finish_version or "", self.main_window)
        dialog.exec()

    def _quit(self):
        self._graceful_quit()

    def run(self) -> int:
        return self.app.exec()


if __name__ == "__main__":
    update_finish_version, qt_argv = _extract_update_finish_arg(sys.argv)
    helper_exit_code = maybe_handle_startup_helper(qt_argv[1:])
    if helper_exit_code is not None:
        sys.exit(helper_exit_code)
    app = PeekAgentApp(qt_argv, update_finish_version=update_finish_version)
    exit_code = 0
    try:
        exit_code = app.run()
    except KeyboardInterrupt:
        app._graceful_quit()
        exit_code = 0
    finally:
        app._graceful_quit()
    sys.exit(exit_code)
