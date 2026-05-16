"""Windows shell execution helpers."""

from __future__ import annotations

import ctypes
from ctypes import wintypes


def shell_execute_and_wait(verb: str, file: str, parameters: str = "", directory: str | None = None) -> int:
    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_SHOWNORMAL = 1
    INFINITE = 0xFFFFFFFF

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", wintypes.LPVOID),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    execute_info = SHELLEXECUTEINFOW()
    execute_info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    execute_info.fMask = SEE_MASK_NOCLOSEPROCESS
    execute_info.lpVerb = verb
    execute_info.lpFile = file
    execute_info.lpParameters = parameters
    execute_info.lpDirectory = directory
    execute_info.nShow = SW_SHOWNORMAL

    try:
        if not shell32.ShellExecuteExW(ctypes.byref(execute_info)):
            raise ctypes.WinError(ctypes.GetLastError())
        if not execute_info.hProcess:
            raise RuntimeError("系统没有返回可等待的进程。")

        kernel32.WaitForSingleObject(execute_info.hProcess, INFINITE)
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(execute_info.hProcess, ctypes.byref(exit_code)):
            raise ctypes.WinError()
        return int(exit_code.value)
    finally:
        try:
            if execute_info.hProcess:
                kernel32.CloseHandle(execute_info.hProcess)
        except Exception:
            pass
