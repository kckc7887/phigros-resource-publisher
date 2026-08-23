from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from threading import Lock
from typing import Any


@dataclass
class TaskSnapshot:
    status: str = "idle"
    stage: str = "idle"
    message: str = "等待开始"
    progress: float = 0.0
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    logs: list[str] = field(default_factory=list)


class TaskState:
    """Thread-safe in-memory state exposed to the local GUI."""

    def __init__(self, max_logs: int = 500) -> None:
        self._lock = Lock()
        self._logs: deque[str] = deque(maxlen=max_logs)
        self._snapshot = TaskSnapshot()

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def reset(self) -> None:
        with self._lock:
            self._logs.clear()
            self._snapshot = TaskSnapshot(
                status="running",
                stage="starting",
                message="正在初始化任务",
                started_at=self._now(),
            )

    def update(
        self,
        stage: str,
        message: str,
        progress: float | None = None,
        *,
        log: bool = True,
    ) -> None:
        with self._lock:
            self._snapshot.stage = stage
            self._snapshot.message = message
            if progress is not None:
                self._snapshot.progress = max(0.0, min(100.0, progress))
            if log:
                self._logs.append(f"[{self._now()}] {message}")

    def complete(self, result: dict[str, Any]) -> None:
        with self._lock:
            self._snapshot.status = "done"
            self._snapshot.stage = "done"
            self._snapshot.message = "流程执行完成"
            self._snapshot.progress = 100.0
            self._snapshot.finished_at = self._now()
            self._snapshot.result = result
            self._logs.append(f"[{self._now()}] 流程执行完成")

    def fail(self, error: BaseException) -> None:
        with self._lock:
            self._snapshot.status = "error"
            self._snapshot.stage = "error"
            self._snapshot.message = "流程执行失败"
            self._snapshot.finished_at = self._now()
            self._snapshot.error = str(error)
            self._logs.append(f"[{self._now()}] ERROR: {error}")

    def is_running(self) -> bool:
        with self._lock:
            return self._snapshot.status == "running"

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            result = asdict(self._snapshot)
            result["logs"] = list(self._logs)
            return result

