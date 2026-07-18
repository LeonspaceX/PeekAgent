"""Collect the system prompt's environment profile without blocking the UI."""

from __future__ import annotations

import getpass
import os
import platform
import threading


SYSTEM_PROFILE_ENV_VAR = "PEEKAGENT_SYSTEM_PROFILE"
_profile_ready = threading.Event()
_warmup_lock = threading.Lock()
_warmup_started = False


def _detect_system_profile() -> str:
    username = getpass.getuser() or "unknown"
    os_name = platform.system() or "Unknown OS"
    os_version = platform.release() or platform.version() or "unknown"
    powershell_version = "unknown"

    if os_name == "Windows":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
            ) as key:
                product_name = str(winreg.QueryValueEx(key, "ProductName")[0] or "Windows").strip()
                try:
                    display_version = str(winreg.QueryValueEx(key, "DisplayVersion")[0] or "").strip()
                except FileNotFoundError:
                    display_version = str(winreg.QueryValueEx(key, "ReleaseId")[0] or "").strip()
                build_number = str(winreg.QueryValueEx(key, "CurrentBuildNumber")[0] or "").strip()
                try:
                    ubr = winreg.QueryValueEx(key, "UBR")[0]
                except FileNotFoundError:
                    ubr = None

            os_name = product_name
            if display_version:
                os_version = display_version
            if build_number:
                build_text = build_number
                if ubr not in (None, ""):
                    build_text = f"{build_text}.{ubr}"
                os_version = f"{os_version} (build {build_text})" if display_version else f"build {build_text}"
        except Exception:
            pass

        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\PowerShell\3\PowerShellEngine",
            ) as key:
                powershell_version = str(winreg.QueryValueEx(key, "PowerShellVersion")[0] or "unknown").strip()
        except Exception:
            pass

    return (
        "当前运行环境：\n"
        f"- 操作系统：{os_name} {os_version}\n"
        f"- 当前用户名：{username}\n"
        f"- PowerShell 版本：{powershell_version}"
    )


def start_system_profile_warmup() -> None:
    """Start the one-time system-profile collection as early as possible."""
    global _warmup_started
    with _warmup_lock:
        if _warmup_started:
            return
        _warmup_started = True

    def _warm() -> None:
        try:
            os.environ[SYSTEM_PROFILE_ENV_VAR] = _detect_system_profile()
        finally:
            _profile_ready.set()

    threading.Thread(target=_warm, name="system-profile-warmup", daemon=True).start()


def is_system_profile_ready() -> bool:
    return _profile_ready.is_set()


def get_system_profile() -> str:
    return os.environ.get(SYSTEM_PROFILE_ENV_VAR, "")
