"""Build a Windows onedir package and zip it for release publishing."""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

import PyInstaller.__main__
from PyInstaller.utils.hooks import collect_data_files


APP_NAME = "PeekAgent"
ARCHIVE_NAME = "PeekAgent-windows-amd64"
ROOT_DIR = Path(__file__).resolve().parent
DIST_DIR = ROOT_DIR / "dist"
BUILD_DIR = ROOT_DIR / "build"
APP_DIR = DIST_DIR / APP_NAME
ARCHIVE_PATH = DIST_DIR / f"{ARCHIVE_NAME}.zip"
CHANGELOG_PATH = ROOT_DIR / "CHANGELOG.md"
VERSION_PATH = ROOT_DIR / "version.txt"

QT_MODULE_EXCLUDES = [
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtBluetooth",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtHttpServer",
    "PySide6.QtLocation",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetworkAuth",
    "PySide6.QtNfc",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtSerialBus",
    "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio",
    "PySide6.QtSql",
    "PySide6.QtStateMachine",
    "PySide6.QtTest",
    "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools",
    "PySide6.QtVirtualKeyboard",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebSockets",
    "PySide6.QtWebView",
]


def _data_args() -> list[str]:
    entries: list[tuple[Path, str]] = [
        (ROOT_DIR / "src" / "resources", "src/resources"),
    ]
    if VERSION_PATH.exists():
        entries.append((VERSION_PATH, "."))
    for icon_name in ("icon.png", "icon.ico"):
        icon_path = ROOT_DIR / icon_name
        if icon_path.exists():
            entries.append((icon_path, "."))

    args: list[str] = []
    for src, dest in entries:
        args.append(f"--add-data={src}{os.pathsep}{dest}")

    for src, dest in collect_data_files("qfluentwidgets"):
        args.append(f"--add-data={src}{os.pathsep}{dest}")
    return args


def _build_args() -> list[str]:
    args = [
        "--noconfirm",
        "--clean",
        "--windowed",
        "--onedir",
        "--name",
        APP_NAME,
        "--hidden-import=PySide6.QtWebEngineCore",
        "--hidden-import=PySide6.QtWebEngineWidgets",
        "--hidden-import=PySide6.QtWebChannel",
    ]
    for module_name in QT_MODULE_EXCLUDES:
        args.append(f"--exclude-module={module_name}")
    icon_path = ROOT_DIR / "icon.ico"
    if icon_path.exists():
        args.extend(["--icon", str(icon_path)])
    args.extend(_data_args())
    args.append(str(ROOT_DIR / "main.py"))
    return args


def _clean() -> None:
    shutil.rmtree(BUILD_DIR, ignore_errors=True)
    shutil.rmtree(APP_DIR, ignore_errors=True)
    if ARCHIVE_PATH.exists():
        ARCHIVE_PATH.unlink()


def _extract_version() -> str:
    if not CHANGELOG_PATH.exists():
        return "development"
    first_line = CHANGELOG_PATH.read_text(encoding="utf-8").splitlines()[0].strip()
    if not first_line:
        return "development"
    version = re.sub(r"^\s*#+\s*", "", first_line).strip()
    version = re.sub(r"^\[(.*)\]$", r"\1", version).strip()
    return version or "development"


def _write_version_file() -> str:
    version = _extract_version()
    VERSION_PATH.write_text(version + "\n", encoding="utf-8")
    return version


def _zip_dist() -> Path:
    archive_base = DIST_DIR / ARCHIVE_NAME
    shutil.make_archive(str(archive_base), "zip", root_dir=DIST_DIR, base_dir=APP_NAME)
    return ARCHIVE_PATH


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("build_win.py is intended to run on Windows.")

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    _clean()
    version = _write_version_file()
    print(f"Version: {version}")
    print("Building with PyInstaller...")
    PyInstaller.__main__.run(_build_args())
    archive_path = _zip_dist()
    print(f"Build complete: {APP_DIR}")
    print(f"Release archive: {archive_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
