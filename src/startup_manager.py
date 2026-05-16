"""Windows auto-start management."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from src.config import BASE_DIR
from src.utils.shell import shell_execute_and_wait


_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_REG_VALUE_NAME = "PeekAgent"


def is_windows() -> bool:
    return os.name == "nt"


def build_startup_command() -> str:
    if getattr(sys, "frozen", False):
        return subprocess.list2cmdline([sys.executable, "--no-open-window"])

    python_path = Path(sys.executable).resolve()
    pythonw_path = python_path.with_name("pythonw.exe")
    launcher = pythonw_path if pythonw_path.exists() else python_path
    main_path = (BASE_DIR / "main.py").resolve()
    return subprocess.list2cmdline([str(launcher), str(main_path), "--no-open-window"])


def configure_auto_start(enabled: bool):
    if not is_windows():
        raise RuntimeError("当前平台不支持开机自启配置。")

    import winreg

    access = winreg.KEY_SET_VALUE
    if hasattr(winreg, "KEY_WOW64_64KEY"):
        access |= winreg.KEY_WOW64_64KEY

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _REG_PATH, 0, access) as key:
        if enabled:
            winreg.SetValueEx(key, _REG_VALUE_NAME, 0, winreg.REG_SZ, build_startup_command())
            return
        try:
            winreg.DeleteValue(key, _REG_VALUE_NAME)
        except FileNotFoundError:
            pass


def _helper_invocation(enabled: bool, error_path: str) -> tuple[str, str]:
    flag = "on" if enabled else "off"
    if getattr(sys, "frozen", False):
        return sys.executable, subprocess.list2cmdline(
            [f"--configure-auto-start={flag}", f"--startup-error-file={error_path}"]
        )

    python_path = Path(sys.executable).resolve()
    pythonw_path = python_path.with_name("pythonw.exe")
    launcher = pythonw_path if pythonw_path.exists() else python_path
    main_path = (BASE_DIR / "main.py").resolve()
    params = [str(main_path), f"--configure-auto-start={flag}", f"--startup-error-file={error_path}"]
    return str(launcher), subprocess.list2cmdline(params)


def request_auto_start_update(enabled: bool):
    if not is_windows():
        raise RuntimeError("当前平台不支持开机自启配置。")

    error_file = tempfile.NamedTemporaryFile(prefix="peekagent_startup_", suffix=".txt", delete=False)
    error_path = error_file.name
    error_file.close()

    executable, parameters = _helper_invocation(enabled, error_path)

    try:
        try:
            exit_code = shell_execute_and_wait("runas", executable, parameters)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1223:
                raise RuntimeError("已取消管理员权限请求。") from exc
            raise
        if exit_code != 0:
            message = ""
            try:
                message = Path(error_path).read_text(encoding="utf-8").strip()
            except Exception:
                message = ""
            raise RuntimeError(message or "开机自启系统配置失败。")
    finally:
        try:
            Path(error_path).unlink(missing_ok=True)
        except Exception:
            pass


def maybe_handle_startup_helper(argv: list[str]) -> int | None:
    flag = None
    error_path = ""
    for item in argv:
        if item.startswith("--configure-auto-start="):
            flag = item.split("=", 1)[1].strip().lower()
        elif item.startswith("--startup-error-file="):
            error_path = item.split("=", 1)[1]

    if flag not in {"on", "off"}:
        return None

    try:
        configure_auto_start(flag == "on")
        return 0
    except Exception as exc:
        if error_path:
            try:
                Path(error_path).write_text(str(exc), encoding="utf-8")
            except Exception:
                pass
        return 1
