"""一次运行的阶段状态、外部调用耗时与可追踪日志。

运行记录是面向命令行和未来 GUI 的同一份事实来源：终端只展示简短阶段状态，
``output/runs/<run_id>.json`` 保存完整摘要，``.jsonl`` 保存按时间追加的事件。
记录不写入凭证、请求头或完整的外部响应，避免把密钥和行情原文带进日志。
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import re
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator


_ACTIVE: contextvars.ContextVar["RunTracker | None"] = contextvars.ContextVar(
    "research_helper_run_tracker", default=None)
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _seconds(start: float) -> float:
    return round(time.perf_counter() - start, 3)


def _emit(message: str) -> None:
    """独立模块也可在默认 GBK 控制台运行，日志提示不能反过来中断业务。"""
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(message.encode(encoding, errors="replace").decode(encoding, errors="replace"))


@dataclass
class Stage:
    key: str
    label: str
    status: str = "running"
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float | None = None
    error: str = ""
    detail: str = ""


@dataclass
class ExternalCall:
    name: str
    status: str
    duration_seconds: float
    attempt: int = 1
    detail: str = ""
    error: str = ""
    at: str = ""


@dataclass
class RunTracker:
    """持久化一次运行的状态；所有写入均可安全覆盖/追加。"""

    run_id: str
    request: str = ""
    mode: str = "brief"
    root: Path = field(default_factory=lambda: Path.cwd())
    status: str = "running"
    started_at: str = field(default_factory=_now)
    finished_at: str = ""
    duration_seconds: float | None = None
    error: str = ""
    stages: list[Stage] = field(default_factory=list)
    external_calls: list[ExternalCall] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)
    recovery_actions: list[str] = field(default_factory=list)
    _started: float = field(default_factory=time.perf_counter, repr=False)

    @classmethod
    def create(cls, *, root: str | Path, request: str = "", mode: str = "brief") -> "RunTracker":
        timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        tracker = cls(run_id=f"run-{timestamp}-{uuid.uuid4().hex[:6]}", request=request,
                      mode=mode, root=Path(root).resolve())
        tracker._persist(event={"type": "run_started", "at": tracker.started_at})
        return tracker

    @property
    def directory(self) -> Path:
        path = self.root / "output" / "runs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def summary_path(self) -> Path:
        return self.directory / f"{_SAFE_ID.sub('_', self.run_id)}.json"

    @property
    def event_path(self) -> Path:
        return self.directory / f"{_SAFE_ID.sub('_', self.run_id)}.jsonl"

    def payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "request": self.request,
            "mode": self.mode,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
            "stages": [asdict(item) for item in self.stages],
            "external_calls": [asdict(item) for item in self.external_calls],
            "artifacts": dict(self.artifacts),
            "metadata": dict(self.metadata),
            "recovery_actions": list(dict.fromkeys(self.recovery_actions)),
        }

    def _persist(self, *, event: dict[str, Any] | None = None) -> None:
        """日志失败不能影响报告生成，因此持久化错误有意吞掉。"""
        try:
            self.summary_path.write_text(json.dumps(self.payload(), ensure_ascii=False, indent=2),
                                         encoding="utf-8")
            if event is not None:
                with self.event_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"run_id": self.run_id, **event}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    @contextmanager
    def activate(self) -> Iterator["RunTracker"]:
        token = _ACTIVE.set(self)
        try:
            yield self
        finally:
            _ACTIVE.reset(token)

    @contextmanager
    def stage(self, key: str, label: str) -> Iterator[Stage]:
        stage = Stage(key=key, label=label, started_at=_now())
        start = time.perf_counter()
        self.stages.append(stage)
        self._persist(event={"type": "stage_started", "key": key, "label": label, "at": stage.started_at})
        _emit(f"  ▶ [{key}] {label}…")
        try:
            yield stage
        except Exception as error:
            stage.status = "failed"
            stage.error = f"{type(error).__name__}: {str(error)[:300]}"
            raise
        finally:
            if stage.status == "running":
                stage.status = "completed"
            stage.finished_at = _now()
            stage.duration_seconds = _seconds(start)
            symbol = "✓" if stage.status == "completed" else "⚠"
            suffix = f"：{stage.error}" if stage.error else ""
            _emit(f"  {symbol} [{key}] {stage.label}（{stage.duration_seconds:.1f}s）{suffix}")
            self._persist(event={"type": "stage_finished", "key": key, "status": stage.status,
                                 "duration_seconds": stage.duration_seconds, "error": stage.error,
                                 "at": stage.finished_at})

    def fail_stage(self, stage: Stage, error: str, *, detail: str = "") -> None:
        stage.status = "failed"
        stage.error = error[:500]
        stage.detail = detail[:500]

    def add_external(self, name: str, *, status: str, duration_seconds: float,
                     attempt: int = 1, detail: str = "", error: str = "") -> None:
        # 外部调用在开始时先写入 ``running``，结束后由同一 name/attempt 的记录原位
        # 更新。GUI 因而能在 HTTP 请求尚未返回时显示“正在等待”，而不会留下一个
        # 永远 running 的重复条目。
        if status != "running":
            for call in reversed(self.external_calls):
                if call.name == name and call.attempt == attempt and call.status == "running":
                    call.status = status
                    call.duration_seconds = round(duration_seconds, 3)
                    call.detail = detail[:300] or call.detail
                    call.error = error[:500]
                    self._persist(event={"type": "external_call", "state": "finished",
                                         **asdict(call)})
                    return
        call = ExternalCall(name=name, status=status, duration_seconds=round(duration_seconds, 3),
                            attempt=attempt, detail=detail[:300], error=error[:500], at=_now())
        self.external_calls.append(call)
        self._persist(event={"type": "external_call", **asdict(call)})

    def add_artifact(self, name: str, path: str | Path) -> None:
        self.artifacts[name] = str(path)
        self._persist(event={"type": "artifact", "name": name, "path": str(path), "at": _now()})

    def add_metadata(self, name: str, value: object) -> None:
        if value is None or value == "":
            return
        self.metadata[name] = str(value)
        self._persist(event={"type": "metadata", "name": name, "value": str(value), "at": _now()})

    def add_recovery(self, action: str) -> None:
        if action and action not in self.recovery_actions:
            self.recovery_actions.append(action)
            self._persist(event={"type": "recovery_action", "action": action, "at": _now()})

    def finish(self, *, status: str = "completed", error: str = "") -> None:
        if self.finished_at:
            return
        self.status = status
        self.error = error[:500]
        self.finished_at = _now()
        self.duration_seconds = _seconds(self._started)
        self._persist(event={"type": "run_finished", "status": status, "error": self.error,
                             "duration_seconds": self.duration_seconds, "at": self.finished_at})

    def print_summary(self) -> None:
        total = self.duration_seconds if self.duration_seconds is not None else _seconds(self._started)
        _emit(f"\n运行摘要｜{self.run_id}｜{self.status}｜总耗时 {total:.1f}s")
        for stage in self.stages:
            elapsed = stage.duration_seconds if stage.duration_seconds is not None else 0.0
            _emit(f"  · {stage.label}：{stage.status}（{elapsed:.1f}s）")
        if self.artifacts:
            _emit("  · 输出：" + "；".join(f"{name}={path}" for name, path in self.artifacts.items()))
        if self.metadata:
            _emit("  · 口径：" + "；".join(f"{name}={value}" for name, value in self.metadata.items()))
        if self.recovery_actions:
            _emit("  · 可操作下一步：" + "；".join(self.recovery_actions))
        _emit(f"  · 可追踪日志：{self.summary_path}")


def current() -> RunTracker | None:
    return _ACTIVE.get()


def record_external(name: str, *, status: str, duration_seconds: float,
                    attempt: int = 1, detail: str = "", error: str = "") -> None:
    tracker = current()
    if tracker is not None:
        tracker.add_external(name, status=status, duration_seconds=duration_seconds,
                             attempt=attempt, detail=detail, error=error)
