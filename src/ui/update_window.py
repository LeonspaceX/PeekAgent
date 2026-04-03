"""Update progress dialogs."""

from __future__ import annotations

import hashlib
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
    cancelled = Signal()

    def __init__(
        self,
        release_info: ReleaseInfo,
        mirror_prefix: str,
        app_dir: str,
        executable_path: str,
        current_pid: int,
        parent=None,
    ):
        super().__init__(parent)
        self._release_info = release_info
        self._mirror_prefix = mirror_prefix
        self._app_dir = Path(app_dir)
        self._executable_path = Path(executable_path)
        self._current_pid = int(current_pid)

    def run(self):
        try:
            self._run_impl()
        except _UpdateCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))

    def _ensure_not_cancelled(self):
        if self.isInterruptionRequested():
            raise _UpdateCancelled()

    def _run_impl(self):
        temp_root = Path(tempfile.gettempdir()) / f"peekagent_update_{uuid.uuid4().hex}"
        temp_root.mkdir(parents=True, exist_ok=True)
        archive_path = temp_root / self._release_info.asset_name
        extract_root = temp_root / "extract"
        extract_root.mkdir(parents=True, exist_ok=True)

        self._ensure_not_cancelled()
        self._download_archive(archive_path)
        self._ensure_not_cancelled()
        self._verify_archive(archive_path)
        self._ensure_not_cancelled()
        package_dir = self._extract_archive(archive_path, extract_root)
        self._ensure_not_cancelled()
        script_path = self._write_update_script(temp_root, package_dir, archive_path)
        self.stage_changed.emit("收尾", 100, "更新脚本已准备完成，正在退出程序...")
        self.ready.emit(str(script_path), self._release_info.version)

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
                self._ensure_not_cancelled()
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
                self._ensure_not_cancelled()
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
                    self._ensure_not_cancelled()
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
        script_path = temp_root / "peekagent_apply_update.ps1"
        version = self._release_info.version
        script = textwrap.dedent(
            f"""\
            $ErrorActionPreference = "Stop"
            [Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
            [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
            $OutputEncoding = [System.Text.UTF8Encoding]::new($false)

            $AppDir = "{self._app_dir}"
            $PackageDir = "{package_dir}"
            $ArchivePath = "{archive_path}"
            $ScriptPath = $MyInvocation.MyCommand.Path
            $TargetExe = "{self._executable_path}"
            $TargetPid = {self._current_pid}
            $Version = "{version}"

            function Show-ProgressLine {{
                param(
                    [int]$Percent,
                    [string]$CurrentItem
                )

                $width = 30
                $filled = [Math]::Min($width, [Math]::Floor($Percent * $width / 100))
                $bar = ("#" * $filled).PadRight($width, "-")
                $suffix = if ([string]::IsNullOrWhiteSpace($CurrentItem)) {{ "" }} else {{ " " + $CurrentItem }}
                Write-Host -NoNewline ("`r[{0}] {1,3}%{2}" -f $bar, $Percent, $suffix)
            }}

            try {{
                while (Get-Process -Id $TargetPid -ErrorAction SilentlyContinue) {{
                    Start-Sleep -Seconds 2
                }}

                Start-Sleep -Seconds 2
                Write-Host "正在应用更新，请勿关闭此窗口..."

                $sourceRoot = [System.IO.Path]::GetFullPath($PackageDir)
                $destinationRoot = [System.IO.Path]::GetFullPath($AppDir)
                $sourceInternal = Join-Path $sourceRoot "_internal"
                $destinationInternal = Join-Path $destinationRoot "_internal"

                if (-not (Test-Path -LiteralPath $sourceRoot)) {{
                    throw "更新源目录不存在：$sourceRoot"
                }}

                New-Item -ItemType Directory -Force -Path $destinationRoot | Out-Null

                if (Test-Path -LiteralPath $sourceInternal) {{
                    Write-Host "正在清理旧版 _internal 目录..."
                    Remove-Item -LiteralPath $destinationInternal -Recurse -Force -ErrorAction SilentlyContinue
                }}

                foreach ($dir in Get-ChildItem -LiteralPath $sourceRoot -Recurse -Force -Directory) {{
                    $relativeDir = $dir.FullName.Substring($sourceRoot.Length).TrimStart('\')
                    if ($relativeDir) {{
                        New-Item -ItemType Directory -Force -Path (Join-Path $destinationRoot $relativeDir) | Out-Null
                    }}
                }}

                $files = @(Get-ChildItem -LiteralPath $sourceRoot -Recurse -Force -File)
                [long]$totalBytes = 0
                foreach ($file in $files) {{
                    $totalBytes += $file.Length
                }}

                if ($files.Count -eq 0) {{
                    Show-ProgressLine -Percent 100 -CurrentItem "没有需要复制的文件"
                    Write-Host ""
                }} else {{
                    [long]$copiedBytes = 0
                    $buffer = New-Object byte[] (1024 * 1024)

                    foreach ($file in $files) {{
                        $relativePath = $file.FullName.Substring($sourceRoot.Length).TrimStart('\')
                        $targetPath = Join-Path $destinationRoot $relativePath
                        $targetParent = Split-Path -Parent $targetPath
                        if ($targetParent) {{
                            New-Item -ItemType Directory -Force -Path $targetParent | Out-Null
                        }}

                        if ($false -and (Test-Path -LiteralPath $targetPath)) {{
                            $existingItem = Get-Item -LiteralPath $targetPath
                            if (-not $existingItem.PSIsContainer -and $existingItem.Length -eq $file.Length) {{
                                $copiedBytes += $file.Length
                                $percent = if ($totalBytes -gt 0) {{ [Math]::Min(100, [int]($copiedBytes * 100 / $totalBytes)) }} else {{ 100 }}
                                Show-ProgressLine -Percent $percent -CurrentItem ($relativePath + " (跳过)")
                                continue
                            }}
                        }}

                        if ($file.Length -eq 0) {{
                            [System.IO.File]::WriteAllBytes($targetPath, [byte[]]::new(0))
                            $targetItem = Get-Item -LiteralPath $targetPath
                            $targetItem.LastWriteTime = $file.LastWriteTime
                            $targetItem.Attributes = $file.Attributes
                            $copiedBytes += $file.Length
                            $percent = if ($totalBytes -gt 0) {{ [Math]::Min(100, [int]($copiedBytes * 100 / $totalBytes)) }} else {{ 100 }}
                            Show-ProgressLine -Percent $percent -CurrentItem $relativePath
                            continue
                        }}

                        $sourceStream = $null
                        $targetStream = $null
                        try {{
                            $sourceStream = [System.IO.File]::Open($file.FullName, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
                            $targetStream = [System.IO.File]::Open($targetPath, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)

                            while (($read = $sourceStream.Read($buffer, 0, $buffer.Length)) -gt 0) {{
                                $targetStream.Write($buffer, 0, $read)
                                $copiedBytes += $read
                                $percent = if ($totalBytes -gt 0) {{ [Math]::Min(100, [int]($copiedBytes * 100 / $totalBytes)) }} else {{ 100 }}
                                Show-ProgressLine -Percent $percent -CurrentItem $relativePath
                            }}
                        }} finally {{
                            if ($targetStream) {{ $targetStream.Dispose() }}
                            if ($sourceStream) {{ $sourceStream.Dispose() }}
                        }}

                        $targetItem = Get-Item -LiteralPath $targetPath
                        $targetItem.LastWriteTime = $file.LastWriteTime
                        $targetItem.Attributes = $file.Attributes
                    }}

                    Show-ProgressLine -Percent 100 -CurrentItem "复制完成"
                    Write-Host ""
                }}

                Start-Process -FilePath $TargetExe -ArgumentList "--update-finish=$Version"
                Start-Sleep -Seconds 2
                Remove-Item -LiteralPath $PackageDir -Recurse -Force -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath $ArchivePath -Force -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath $ScriptPath -Force -ErrorAction SilentlyContinue
                exit 0
            }} catch {{
                Write-Host ""
                Write-Host ("更新失败：" + $_.Exception.Message)
                Read-Host "按回车关闭"
                exit 1
            }}
            """
        )
        script_path.write_text(script, encoding="utf-8-sig", newline="\r\n")
        self.stage_changed.emit("收尾", 80, "升级脚本准备完成")
        return script_path


class _UpdateCancelled(Exception):
    pass


class UpdateDialog(QDialog):
    update_apply_requested = Signal(str)

    def __init__(
        self,
        current_version: str,
        release_info: ReleaseInfo,
        mirror_prefix: str,
        app_dir: str,
        executable_path: str,
        current_pid: int,
        parent=None,
    ):
        super().__init__(parent)
        self._worker = UpdateWorker(release_info, mirror_prefix, app_dir, executable_path, current_pid, self)
        self._worker.stage_changed.connect(self._on_stage_changed)
        self._worker.failed.connect(self._on_failed)
        self._worker.ready.connect(self._on_ready)
        self._worker.cancelled.connect(self._on_cancelled)

        self.setWindowTitle("版本升级")
        self.setModal(True)
        self.resize(420, 160)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        layout.addWidget(SubtitleLabel("正在准备升级", self))

        self.status_label = BodyLabel("正在初始化升级任务...", self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.progress_bar = ProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self.cancel_btn = PushButton("取消", self)
        self.cancel_btn.clicked.connect(self._cancel_update)
        button_row.addWidget(self.cancel_btn)
        layout.addLayout(button_row)

        self._worker.start()

    def reject(self):
        if self._worker.isRunning():
            return
        super().reject()

    def _on_stage_changed(self, stage: str, progress: int, status: str):
        self.status_label.setText(f"{stage}: {status}")
        self.progress_bar.setValue(max(0, min(100, progress)))

    def _on_failed(self, message: str):
        self.status_label.setText(message)
        self.progress_bar.setValue(0)
        self.cancel_btn.setText("关闭")
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.clicked.disconnect()
        self.cancel_btn.clicked.connect(self.accept)

    def _on_ready(self, script_path: str, version: str):
        self.status_label.setText(f"更新已准备完成，正在退出并应用 {version}...")
        self.cancel_btn.setEnabled(False)
        self.update_apply_requested.emit(script_path)

    def _on_cancelled(self):
        self.status_label.setText("已取消升级")
        self.progress_bar.setValue(0)
        self.cancel_btn.setText("关闭")
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.clicked.disconnect()
        self.cancel_btn.clicked.connect(self.accept)

    def _cancel_update(self):
        if not self._worker.isRunning():
            self.accept()
            return
        self.cancel_btn.setEnabled(False)
        self.status_label.setText("正在取消升级...")
        self._worker.requestInterruption()


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
