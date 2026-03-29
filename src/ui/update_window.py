"""Update progress dialogs."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import textwrap
import uuid
import zipfile
from pathlib import Path

import requests
from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout
from qfluentwidgets import BodyLabel, FluentIcon, PrimaryPushButton, ProgressBar, PushButton, SubtitleLabel, ToolButton

from src.update_manager import ReleaseInfo, build_mirrored_url


class UpdateWorker(QThread):
    stage_changed = Signal(str, int, str)
    failed = Signal(str)
    ready = Signal(str, str)

    def __init__(
        self,
        release_info: ReleaseInfo,
        mirror_prefix: str,
        app_dir: str,
        executable_path: str,
        parent=None,
    ):
        super().__init__(parent)
        self._release_info = release_info
        self._mirror_prefix = mirror_prefix
        self._app_dir = Path(app_dir)
        self._executable_path = Path(executable_path)

    def run(self):
        try:
            self._run_impl()
        except Exception as exc:
            self.failed.emit(str(exc))

    def _run_impl(self):
        temp_root = Path(tempfile.gettempdir()) / f"peekagent_update_{uuid.uuid4().hex}"
        temp_root.mkdir(parents=True, exist_ok=True)
        archive_path = temp_root / self._release_info.asset_name
        extract_root = temp_root / "extract"
        extract_root.mkdir(parents=True, exist_ok=True)

        self._download_archive(archive_path)
        self._verify_archive(archive_path)
        package_dir = self._extract_archive(archive_path, extract_root)
        batch_path = self._write_update_script(temp_root, package_dir, archive_path)
        self.stage_changed.emit("收尾", 100, "更新脚本已准备完成，正在退出程序...")
        self.ready.emit(str(batch_path), self._release_info.version)

    def _download_archive(self, archive_path: Path):
        download_url = build_mirrored_url(self._release_info.download_url, self._mirror_prefix)
        self.stage_changed.emit("下载", 0, "正在下载更新包...")
        try:
            response = requests.get(download_url, stream=True, timeout=(10, 60))
            response.raise_for_status()
        except Exception as exc:
            raise RuntimeError(f"下载阶段失败：{exc}") from exc

        total = int(response.headers.get("Content-Length") or 0)
        written = 0
        with archive_path.open("wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 128):
                if not chunk:
                    continue
                file.write(chunk)
                written += len(chunk)
                progress = min(99, int(written * 100 / total)) if total > 0 else 0
                self.stage_changed.emit("下载", progress, f"正在下载更新包... {written // 1024} KB")
        self.stage_changed.emit("下载", 100, "更新包下载完成")

    def _verify_archive(self, archive_path: Path):
        self.stage_changed.emit("校验", 0, "正在校验更新包 sha256...")
        expected_sha256 = self._release_info.sha256.strip().lower()
        file_size = archive_path.stat().st_size
        processed = 0
        digest = hashlib.sha256()
        with archive_path.open("rb") as file:
            while True:
                chunk = file.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                processed += len(chunk)
                progress = min(99, int(processed * 100 / file_size)) if file_size > 0 else 0
                self.stage_changed.emit("校验", progress, "正在校验更新包 sha256...")
        actual_sha256 = digest.hexdigest().lower()
        if actual_sha256 != expected_sha256:
            raise RuntimeError(
                "校验阶段失败：sha256 不匹配，已终止更新。\n"
                f"期望：{expected_sha256}\n实际：{actual_sha256}"
            )
        self.stage_changed.emit("校验", 100, "sha256 校验通过")

    def _extract_archive(self, archive_path: Path, extract_root: Path) -> Path:
        self.stage_changed.emit("解压", 0, "正在解压更新包...")
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                members = archive.infolist()
                if not members:
                    raise RuntimeError("压缩包为空")
                for index, member in enumerate(members, start=1):
                    archive.extract(member, extract_root)
                    progress = int(index * 100 / len(members))
                    self.stage_changed.emit("解压", progress, f"正在解压更新包... {index}/{len(members)}")
        except Exception as exc:
            raise RuntimeError(f"解压阶段失败：{exc}") from exc

        package_dir = extract_root / "PeekAgent"
        if not package_dir.exists():
            children = [item for item in extract_root.iterdir() if item.is_dir()]
            if len(children) == 1:
                package_dir = children[0]
        if not (package_dir / "PeekAgent.exe").is_file():
            raise RuntimeError("解压阶段失败：压缩包内缺少 PeekAgent.exe")
        if not (package_dir / "_internal").is_dir():
            raise RuntimeError("解压阶段失败：压缩包内缺少 _internal 文件夹")
        self.stage_changed.emit("解压", 100, "解压完成")
        return package_dir

    def _write_update_script(self, temp_root: Path, package_dir: Path, archive_path: Path) -> Path:
        self.stage_changed.emit("收尾", 10, "正在准备升级脚本...")
        script_path = temp_root / "peekagent_apply_update.bat"
        version = self._release_info.version
        script = textwrap.dedent(
            f"""\
            @echo off
            setlocal enableextensions

            set "APP_DIR={self._app_dir}"
            set "PACKAGE_DIR={package_dir}"
            set "ARCHIVE_PATH={archive_path}"
            set "SCRIPT_PATH=%~f0"
            set "TARGET_EXE={self._executable_path}"
            set "PROCESS_NAME={self._executable_path.name}"

            :wait_process
            tasklist /FI "IMAGENAME eq %PROCESS_NAME%" | find /I "%PROCESS_NAME%" >nul
            if not errorlevel 1 (
                timeout /t 2 /nobreak >nul
                goto wait_process
            )

            timeout /t 2 /nobreak >nul

            robocopy "%PACKAGE_DIR%" "%APP_DIR%" /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >nul
            set "ROBOCOPY_EXIT=%ERRORLEVEL%"
            if %ROBOCOPY_EXIT% GEQ 8 (
                echo 更新失败：覆盖文件时 robocopy 返回 %ROBOCOPY_EXIT%
                pause
                exit /b %ROBOCOPY_EXIT%
            )

            start "" "%TARGET_EXE%" --update-finish={version}

            timeout /t 2 /nobreak >nul
            rmdir /s /q "{package_dir}" >nul 2>nul
            del /f /q "{archive_path}" >nul 2>nul
            del /f /q "%SCRIPT_PATH%" >nul 2>nul
            exit /b 0
            """
        )
        script_path.write_text(script, encoding="utf-8", newline="\r\n")
        self.stage_changed.emit("收尾", 80, "升级脚本准备完成")
        return script_path


class UpdateDialog(QDialog):
    update_apply_requested = Signal(str)

    def __init__(
        self,
        current_version: str,
        release_info: ReleaseInfo,
        mirror_prefix: str,
        app_dir: str,
        executable_path: str,
        parent=None,
    ):
        super().__init__(parent)
        self._worker = UpdateWorker(release_info, mirror_prefix, app_dir, executable_path, self)
        self._worker.stage_changed.connect(self._on_stage_changed)
        self._worker.failed.connect(self._on_failed)
        self._worker.ready.connect(self._on_ready)

        self.setWindowTitle("版本升级")
        self.setModal(True)
        self.resize(460, 220)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title_row = QHBoxLayout()
        self.icon_btn = ToolButton(FluentIcon.UPDATE, self)
        self.icon_btn.setEnabled(False)
        title_row.addWidget(self.icon_btn)
        title_row.addWidget(SubtitleLabel("正在准备升级", self), 1)
        layout.addLayout(title_row)

        layout.addWidget(BodyLabel(f"当前版本：{current_version}", self))
        layout.addWidget(BodyLabel(f"目标版本：{release_info.version}", self))

        self.stage_label = BodyLabel("阶段：等待开始", self)
        layout.addWidget(self.stage_label)

        self.status_label = BodyLabel("正在初始化升级任务...", self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.progress_bar = ProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.percent_label = BodyLabel("0%", self)
        layout.addWidget(self.percent_label)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.close_btn = PushButton("关闭", self)
        self.close_btn.clicked.connect(self.reject)
        button_row.addWidget(self.close_btn)
        layout.addLayout(button_row)

        self._worker.start()

    def reject(self):
        if self._worker.isRunning():
            return
        super().reject()

    def _on_stage_changed(self, stage: str, progress: int, status: str):
        self.stage_label.setText(f"阶段：{stage}")
        self.status_label.setText(status)
        self.progress_bar.setValue(max(0, min(100, progress)))
        self.percent_label.setText(f"{max(0, min(100, progress))}%")

    def _on_failed(self, message: str):
        self.stage_label.setText("阶段：失败")
        self.status_label.setText(message)
        self.progress_bar.setValue(0)
        self.percent_label.setText("0%")
        self.close_btn.setEnabled(True)

    def _on_ready(self, batch_path: str, version: str):
        self.status_label.setText(f"更新已准备完成，正在退出并应用 {version}...")
        self.close_btn.setEnabled(False)
        self.update_apply_requested.emit(batch_path)


class UpdateCompleteDialog(QDialog):
    def __init__(self, version: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("升级完成")
        self.setModal(True)
        self.resize(380, 170)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title_row = QHBoxLayout()
        icon_btn = ToolButton(FluentIcon.ACCEPT_MEDIUM, self)
        icon_btn.setEnabled(False)
        title_row.addWidget(icon_btn)
        title_row.addWidget(SubtitleLabel("PeekAgent 已升级完成", self), 1)
        layout.addLayout(title_row)

        layout.addWidget(BodyLabel(f"当前版本：{version}", self))
        layout.addWidget(BodyLabel("更新已完成，程序已重新启动。", self))
        layout.addStretch(1)

        close_btn = PrimaryPushButton("知道了", self)
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignRight)
