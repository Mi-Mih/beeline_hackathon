"""Validated storage for observed technician travel times."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence


MAX_TRAVEL_IMPORT_BYTES = 5_000_000
CALIBRATION_MIN_OBSERVATIONS = 20


class TravelHistoryError(ValueError):
    """An uploaded movement file cannot be safely imported."""


class TravelHistoryStore:
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS travel_observations (
                    fingerprint TEXT PRIMARY KEY,
                    imported_at TEXT NOT NULL,
                    source_filename TEXT NOT NULL,
                    technician_id TEXT NOT NULL,
                    plan_id TEXT,
                    request_id TEXT,
                    origin_location_id TEXT,
                    destination_location_id TEXT,
                    departed_at TEXT NOT NULL,
                    arrived_at TEXT NOT NULL,
                    actual_seconds INTEGER NOT NULL,
                    vehicle_mode TEXT,
                    osrm_seconds REAL,
                    osrm_distance_meters REAL,
                    origin_latitude REAL,
                    origin_longitude REAL,
                    destination_latitude REAL,
                    destination_longitude REAL,
                    graph_version TEXT,
                    raw_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS travel_observations_technician_time "
                "ON travel_observations(technician_id, departed_at)"
            )

    def import_rows(
        self, rows: Sequence[Mapping[str, Any]], *, source_filename: str
    ) -> dict[str, Any]:
        if not rows:
            raise TravelHistoryError("movement file contains no rows")
        normalized = [normalize_observation(row, index + 1) for index, row in enumerate(rows)]
        imported_at = datetime.now(timezone.utc).isoformat()
        inserted = 0
        with self.connect() as connection:
            for row in normalized:
                fingerprint = observation_fingerprint(row)
                values = {
                    **row,
                    "fingerprint": fingerprint,
                    "imported_at": imported_at,
                    "source_filename": source_filename,
                    "raw_json": json.dumps(row, ensure_ascii=False, sort_keys=True),
                }
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO travel_observations(
                        fingerprint, imported_at, source_filename, technician_id,
                        plan_id, request_id, origin_location_id, destination_location_id,
                        departed_at, arrived_at, actual_seconds, vehicle_mode,
                        osrm_seconds, osrm_distance_meters,
                        origin_latitude, origin_longitude,
                        destination_latitude, destination_longitude,
                        graph_version, raw_json
                    ) VALUES (
                        :fingerprint, :imported_at, :source_filename, :technician_id,
                        :plan_id, :request_id, :origin_location_id, :destination_location_id,
                        :departed_at, :arrived_at, :actual_seconds, :vehicle_mode,
                        :osrm_seconds, :osrm_distance_meters,
                        :origin_latitude, :origin_longitude,
                        :destination_latitude, :destination_longitude,
                        :graph_version, :raw_json
                    )
                    """,
                    values,
                )
                inserted += cursor.rowcount
        return {
            "received": len(normalized),
            "inserted": inserted,
            "duplicates": len(normalized) - inserted,
            "technicians": sorted({row["technician_id"] for row in normalized}),
            "status": self.status(),
        }

    def status(self) -> dict[str, Any]:
        with self.connect() as connection:
            totals = connection.execute(
                "SELECT COUNT(*) AS total, COUNT(DISTINCT technician_id) AS engineers "
                "FROM travel_observations"
            ).fetchone()
            rows = connection.execute(
                """
                SELECT technician_id, COUNT(*) AS observation_count,
                       SUM(CASE WHEN osrm_seconds > 0 THEN 1 ELSE 0 END) AS comparable_count,
                       MAX(arrived_at) AS latest_arrived_at
                FROM travel_observations
                GROUP BY technician_id
                ORDER BY technician_id
                """
            ).fetchall()
            comparable = connection.execute(
                """
                SELECT technician_id, actual_seconds, osrm_seconds
                FROM travel_observations
                WHERE osrm_seconds > 0
                ORDER BY departed_at
                """
            ).fetchall()
        samples: dict[str, list[tuple[float, float]]] = {}
        for item in comparable:
            ratio = item["actual_seconds"] / item["osrm_seconds"]
            if 0.25 <= ratio <= 4.0:
                samples.setdefault(item["technician_id"], []).append(
                    (ratio, item["actual_seconds"] - item["osrm_seconds"])
                )
        technicians = []
        for row in rows:
            valid = samples.get(row["technician_id"], [])
            technicians.append(
                {
                    **dict(row),
                    "preview_observation_count": len(valid),
                    "experimental_factor": (
                        round(median(item[0] for item in valid), 3) if valid else None
                    ),
                    "median_error_seconds": (
                        int(round(median(item[1] for item in valid))) if valid else None
                    ),
                    "calibration_ready": len(valid)
                    >= CALIBRATION_MIN_OBSERVATIONS,
                }
            )
        return {
            "observation_count": totals["total"],
            "technician_count": totals["engineers"],
            "calibration_min_observations": CALIBRATION_MIN_OBSERVATIONS,
            "technicians": technicians,
        }

    def calibration_profiles(self) -> dict[str, dict[str, Any]]:
        """Return preview factors that can be applied by the optional planner mode."""
        profiles: dict[str, dict[str, Any]] = {}
        for technician in self.status()["technicians"]:
            factor = technician["experimental_factor"]
            if factor is None:
                continue
            profiles[technician["technician_id"]] = {
                "factor": factor,
                "observation_count": technician["preview_observation_count"],
                "calibration_ready": technician["calibration_ready"],
            }
        return profiles


def parse_movement_upload(body: bytes, content_type: str) -> list[dict[str, Any]]:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise TravelHistoryError("movement file must use UTF-8") from error
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type == "application/json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise TravelHistoryError("movement JSON is invalid") from error
        if isinstance(payload, dict):
            payload = payload.get("movements")
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise TravelHistoryError(
                "movement JSON must be an array or an object with a movements array"
            )
        return payload
    if media_type in {"text/csv", "application/csv", "application/vnd.ms-excel"}:
        first_line = text.splitlines()[0] if text.splitlines() else ""
        delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
        try:
            return [dict(row) for row in csv.DictReader(io.StringIO(text), delimiter=delimiter)]
        except csv.Error as error:
            raise TravelHistoryError("movement CSV is invalid") from error
    raise TravelHistoryError("movement upload must be CSV or JSON")


def normalize_observation(value: Mapping[str, Any], row_number: int) -> dict[str, Any]:
    technician_id = _required_text(value.get("technician_id"), row_number, "technician_id")
    departed = _timestamp(value.get("departed_at"), row_number, "departed_at")
    arrived = _timestamp(value.get("arrived_at"), row_number, "arrived_at")
    actual_seconds = int((arrived - departed).total_seconds())
    if actual_seconds <= 0 or actual_seconds > 24 * 60 * 60:
        raise TravelHistoryError(
            f"row {row_number}: arrived_at must be after departed_at by at most 24 hours"
        )
    result: dict[str, Any] = {
        "technician_id": technician_id,
        "plan_id": _optional_text(value.get("plan_id")),
        "request_id": _optional_text(value.get("request_id")),
        "origin_location_id": _optional_text(value.get("origin_location_id")),
        "destination_location_id": _optional_text(value.get("destination_location_id")),
        "departed_at": departed.isoformat(),
        "arrived_at": arrived.isoformat(),
        "actual_seconds": actual_seconds,
        "vehicle_mode": _optional_text(value.get("vehicle_mode")),
        "osrm_seconds": _optional_number(value.get("osrm_seconds"), row_number, "osrm_seconds"),
        "osrm_distance_meters": _optional_number(
            value.get("osrm_distance_meters"), row_number, "osrm_distance_meters"
        ),
        "origin_latitude": _optional_coordinate(value.get("origin_latitude"), row_number, "origin_latitude", -90, 90),
        "origin_longitude": _optional_coordinate(value.get("origin_longitude"), row_number, "origin_longitude", -180, 180),
        "destination_latitude": _optional_coordinate(value.get("destination_latitude"), row_number, "destination_latitude", -90, 90),
        "destination_longitude": _optional_coordinate(value.get("destination_longitude"), row_number, "destination_longitude", -180, 180),
        "graph_version": _optional_text(value.get("graph_version")),
    }
    for prefix in ("origin", "destination"):
        latitude = result[f"{prefix}_latitude"]
        longitude = result[f"{prefix}_longitude"]
        if (latitude is None) != (longitude is None):
            raise TravelHistoryError(
                f"row {row_number}: {prefix}_latitude and {prefix}_longitude must be provided together"
            )
    return result


def observation_fingerprint(row: Mapping[str, Any]) -> str:
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def store_from_env() -> TravelHistoryStore:
    path = Path(
        os.getenv("TRAVEL_HISTORY_DB")
        or os.getenv("ROUTING_STATE_DB", ".runtime/routing.sqlite3")
    )
    return TravelHistoryStore(path)


def _required_text(value: Any, row: int, field: str) -> str:
    result = _optional_text(value)
    if result is None:
        raise TravelHistoryError(f"row {row}: {field} is required")
    if len(result) > 200:
        raise TravelHistoryError(f"row {row}: {field} is too long")
    return result


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _timestamp(value: Any, row: int, field: str) -> datetime:
    text = _required_text(value, row, field)
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise TravelHistoryError(f"row {row}: {field} must be ISO 8601") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise TravelHistoryError(f"row {row}: {field} must include a UTC offset")
    return result


def _optional_number(value: Any, row: int, field: str) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise TravelHistoryError(f"row {row}: {field} must be a number") from error
    if not math.isfinite(result) or result < 0:
        raise TravelHistoryError(f"row {row}: {field} must be finite and non-negative")
    return result


def _optional_coordinate(
    value: Any, row: int, field: str, minimum: float, maximum: float
) -> float | None:
    result = _optional_number(value, row, field)
    if result is not None and not minimum <= result <= maximum:
        raise TravelHistoryError(f"row {row}: {field} is outside valid bounds")
    return result
