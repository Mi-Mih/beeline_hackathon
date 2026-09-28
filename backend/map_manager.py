"""Durable single-node state and background jobs for Moscow map updates."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MapManagerError(RuntimeError):
    pass


class MapStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS map_versions (
                    id TEXT PRIMARY KEY,
                    source_kind TEXT NOT NULL,
                    source_timestamp TEXT,
                    created_at TEXT NOT NULL,
                    activated_at TEXT,
                    status TEXT NOT NULL,
                    manifest_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS update_jobs (
                    id TEXT PRIMARY KEY,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error TEXT,
                    version_id TEXT
                );
                """
            )

    def status(self) -> dict[str, Any]:
        with self.connect() as connection:
            active_id = self._setting(connection, "active_version")
            active = connection.execute(
                "SELECT * FROM map_versions WHERE id = ?", (active_id,)
            ).fetchone() if active_id else None
            current = connection.execute(
                "SELECT * FROM update_jobs WHERE status IN ('queued','running','cancelling') "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            schedule = json.loads(self._setting(connection, "schedule") or '{"enabled":false,"interval_days":7,"local_time":"02:00"}')
        return {
            "coverage": "Москва",
            "state": "updating" if current else ("ready" if active else "uninitialized"),
            "active_version": active["id"] if active else None,
            "source_timestamp": active["source_timestamp"] if active else None,
            "activated_at": active["activated_at"] if active else None,
            "modes": json.loads(active["manifest_json"]).get("modes", []) if active else [],
            "schedule": schedule,
            "current_job": dict(current) if current else None,
        }

    def jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM update_jobs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_job(self, trigger: str) -> dict[str, Any]:
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM update_jobs WHERE status IN ('queued','running','cancelling') LIMIT 1"
            ).fetchone()
            if existing:
                return dict(existing)
            job_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO update_jobs(id,trigger,status,stage,created_at) VALUES(?,?,?,?,?)",
                (job_id, trigger, "queued", "queued", utc_now()),
            )
        return self.job(job_id)

    def job(self, job_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM update_jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise MapManagerError("update job not found")
        return dict(row)

    def update_job(self, job_id: str, **values: Any) -> None:
        allowed = {"status", "stage", "started_at", "finished_at", "error", "version_id"}
        if not values or not set(values) <= allowed:
            raise ValueError("invalid job update")
        assignments = ",".join(f"{key} = ?" for key in values)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE update_jobs SET {assignments} WHERE id = ?",  # noqa: S608 - keys are allowlisted
                (*values.values(), job_id),
            )

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.job(job_id)
        if job["status"] not in {"queued", "running"}:
            raise MapManagerError("only an active update can be cancelled")
        self.update_job(job_id, status="cancelling", stage="cancelling")
        return self.job(job_id)

    def set_schedule(self, value: dict[str, Any]) -> dict[str, Any]:
        enabled = value.get("enabled")
        interval = value.get("interval_days")
        local_time = value.get("local_time")
        if not isinstance(enabled, bool) or not isinstance(interval, int) or not 1 <= interval <= 90:
            raise MapManagerError("schedule interval_days must be between 1 and 90")
        try:
            hour, minute = (int(item) for item in str(local_time).split(":"))
        except (TypeError, ValueError) as error:
            raise MapManagerError("schedule local_time must use HH:MM") from error
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise MapManagerError("schedule local_time must use HH:MM")
        schedule = {"enabled": enabled, "interval_days": interval, "local_time": f"{hour:02d}:{minute:02d}", "timezone": "Europe/Moscow"}
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO settings(key,value) VALUES('schedule',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(schedule),),
            )
        return schedule

    @staticmethod
    def _setting(connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


class MapManager:
    """Run exactly one fixed update program; never interpolate user shell input."""

    def __init__(self, store: MapStore, command: Sequence[str] | None = None) -> None:
        self.store = store
        self.command = tuple(command or ())
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._started: set[str] = set()
        self._lock = threading.Lock()

    def start(
        self,
        trigger: str = "manual_download",
        *,
        source_path: Path | None = None,
        background: bool = True,
    ) -> dict[str, Any]:
        job = self.store.create_job(trigger)
        with self._lock:
            should_start = job["status"] == "queued" and job["id"] not in self._started
            if should_start:
                self._started.add(job["id"])
        if should_start:
            if background:
                threading.Thread(
                    target=self._run, args=(job["id"], source_path), daemon=True
                ).start()
            else:
                self._run(job["id"], source_path)
        return job

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.store.cancel(job_id)
        with self._lock:
            process = self._processes.get(job_id)
        if process and process.poll() is None:
            process.terminate()
        return job

    def _run(self, job_id: str, source_path: Path | None) -> None:
        if self.store.job(job_id)["status"] == "cancelling":
            self.store.update_job(job_id, status="cancelled", stage="cancelled", finished_at=utc_now())
            with self._lock:
                self._started.discard(job_id)
            return
        self.store.update_job(job_id, status="running", stage="building", started_at=utc_now())
        if not self.command:
            self.store.update_job(
                job_id,
                status="failed",
                stage="failed",
                finished_at=utc_now(),
                error="ROUTING_UPDATE_COMMAND is not configured",
            )
            with self._lock:
                self._started.discard(job_id)
            return
        try:
            arguments = [*self.command, "--job-id", job_id]
            if source_path is not None:
                arguments.extend(("--source", str(source_path)))
            process = subprocess.Popen(
                arguments,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            with self._lock:
                self._processes[job_id] = process
            output, _ = process.communicate()
            current = self.store.job(job_id)
            if current["status"] == "cancelling":
                self.store.update_job(job_id, status="cancelled", stage="cancelled", finished_at=utc_now())
            elif process.returncode:
                self.store.update_job(job_id, status="failed", stage="failed", finished_at=utc_now(), error=(output or "map build failed")[-4000:])
            else:
                self.store.update_job(job_id, status="ready", stage="ready", finished_at=utc_now())
        except OSError as error:
            self.store.update_job(job_id, status="failed", stage="failed", finished_at=utc_now(), error=str(error))
        finally:
            with self._lock:
                self._processes.pop(job_id, None)
                self._started.discard(job_id)


def manager_from_env() -> MapManager:
    state_path = Path(os.getenv("ROUTING_STATE_DB", ".runtime/routing.sqlite3"))
    command_value = os.getenv("ROUTING_UPDATE_COMMAND", "")
    command = command_value.split() if command_value else ()
    return MapManager(MapStore(state_path), command)


def scheduler_main() -> None:
    """Hourly systemd entry point; create a scheduled job only when it is due."""
    manager = manager_from_env()
    status = manager.store.status()
    schedule = status["schedule"]
    if not schedule.get("enabled") or status["current_job"]:
        return
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    hour, minute = (int(item) for item in schedule["local_time"].split(":"))
    if (now.hour, now.minute) < (hour, minute):
        return
    scheduled_jobs = [job for job in manager.store.jobs(100) if job["trigger"] == "schedule"]
    if scheduled_jobs:
        last = datetime.fromisoformat(scheduled_jobs[0]["created_at"])
        if datetime.now(timezone.utc) - last < timedelta(days=schedule["interval_days"]):
            return
    manager.start("schedule", background=False)
