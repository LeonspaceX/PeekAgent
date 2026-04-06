"""Background task execution for long-running PowerShell commands."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Callable


@dataclass
class BackgroundTaskResult:
    task_id: str
    title: str
    command: str
    context_id: str | None
    timeout_seconds: int
    session_id: str | None
    status: str
    exit_code: int | None
    output: str
    duration_seconds: float


class BackgroundTaskManager:
    def __init__(
        self,
        run_command: Callable[[str, str | None, int | None], tuple[str, bool, int | None, str | None]],
    ):
        self._run_command = run_command
        self._lock = threading.Lock()
        self._pending_results: deque[BackgroundTaskResult] = deque()
        self._threads: dict[str, threading.Thread] = {}
        self._closed = False
        self._completion_callback: Callable[[], None] | None = None

    def set_completion_callback(self, callback: Callable[[], None] | None):
        with self._lock:
            self._completion_callback = callback

    def close(self):
        with self._lock:
            self._closed = True

    def start_task(
        self,
        *,
        title: str,
        command: str,
        context_id: str | None,
        timeout_seconds: int,
        session_id: str | None,
    ) -> str:
        task_id = f"bg_{uuid.uuid4().hex[:10]}"
        worker = threading.Thread(
            target=self._run_task,
            kwargs={
                "task_id": task_id,
                "title": title,
                "command": command,
                "context_id": context_id,
                "timeout_seconds": timeout_seconds,
                "session_id": session_id,
            },
            daemon=True,
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("background task manager already closed")
            self._threads[task_id] = worker
        worker.start()
        return task_id

    def pop_completed_results(self, session_id: str | None = None) -> list[BackgroundTaskResult]:
        with self._lock:
            if session_id is None:
                results = list(self._pending_results)
                self._pending_results.clear()
                return results

            kept: deque[BackgroundTaskResult] = deque()
            matched: list[BackgroundTaskResult] = []
            while self._pending_results:
                item = self._pending_results.popleft()
                if item.session_id == session_id:
                    matched.append(item)
                else:
                    kept.append(item)
            self._pending_results = kept
            return matched

    def _run_task(
        self,
        *,
        task_id: str,
        title: str,
        command: str,
        context_id: str | None,
        timeout_seconds: int,
        session_id: str | None,
    ):
        started_at = time.monotonic()
        try:
            output, timed_out, exit_code, effective_context_id = self._run_command(command, context_id, timeout_seconds)
            if timed_out:
                status = "error"
                if exit_code is None:
                    exit_code = -1
            else:
                status = "success"
        except Exception as exc:
            output = str(exc)
            timed_out = False
            status = "error"
            exit_code = -1
            effective_context_id = context_id
        duration_seconds = max(0.0, time.monotonic() - started_at)
        result = BackgroundTaskResult(
            task_id=task_id,
            title=title,
            command=command,
            context_id=effective_context_id,
            timeout_seconds=timeout_seconds,
            session_id=session_id,
            status=status,
            exit_code=exit_code,
            output=output,
            duration_seconds=duration_seconds,
        )
        with self._lock:
            self._threads.pop(task_id, None)
            callback = self._completion_callback
            if not self._closed:
                self._pending_results.append(result)
            else:
                callback = None
        if callback is not None:
            callback()
