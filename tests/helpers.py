"""Shared builders for planning unit tests."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.math_core.models import (
    PLANNING_OVERNIGHT,
    PlanningProblem,
    Request,
    Technician,
    TimeWindow,
    TravelLeg,
    TravelTable,
)

ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=3))
DAY = datetime(2026, 8, 17, tzinfo=TZ)


def at(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


def make_request(
    request_id: str = "REQ-1",
    *,
    location_id: str = "OFFICE",
    duration_minutes: int = 30,
    window: TimeWindow | None = None,
    required_skills: frozenset[str] | None = None,
    required_vehicle: str | None = None,
    required_equipment: frozenset[str] | None = None,
    locked_technician_id: str | None = None,
    priority: str = "normal",
) -> Request:
    return Request(
        id=request_id,
        location_id=location_id,
        duration_minutes=duration_minutes,
        window=window or TimeWindow(at(9), at(18)),
        required_skills=required_skills or frozenset({"connection"}),
        required_vehicle=required_vehicle,
        required_equipment=required_equipment or frozenset(),
        locked_technician_id=locked_technician_id,
        priority=priority,
    )


def make_technician(
    technician_id: str = "TECH-1",
    *,
    name: str | None = None,
    start_location_id: str = "OFFICE",
    available_from: datetime | None = None,
    shift_end: datetime | None = None,
    skills: frozenset[str] | None = None,
    vehicle: str = "car",
    equipment: frozenset[str] | None = None,
    travel_time_factor: float = 1.0,
) -> Technician:
    return Technician(
        id=technician_id,
        name=name or technician_id,
        start_location_id=start_location_id,
        available_from=available_from or at(9),
        shift_end=shift_end or at(18),
        skills=skills or frozenset({"connection"}),
        vehicle=vehicle,
        equipment=equipment or frozenset(),
        travel_time_factor=travel_time_factor,
    )


def make_travel(
    legs: dict[tuple[str, str] | tuple[str, str, str | None], tuple[int, float]]
    | None = None,
    *,
    symmetric: bool = True,
) -> TravelTable:
    parsed: dict[tuple[str, str, str | None], TravelLeg] = {}
    for key, value in (legs or {}).items():
        if len(key) == 2:
            origin, destination = key
            mode = None
        else:
            origin, destination, mode = key
        minutes, distance_km = value
        parsed[(origin, destination, mode)] = TravelLeg(minutes, distance_km)
    return TravelTable(parsed, symmetric)


def make_problem(
    requests: list[Request],
    technicians: list[Technician],
    travel: TravelTable | None = None,
    *,
    plan_id: str = "unit-test",
    planning_reason: str = PLANNING_OVERNIGHT,
) -> PlanningProblem:
    return PlanningProblem(
        plan_id=plan_id,
        requests=tuple(requests),
        technicians=tuple(technicians),
        travel=travel if travel is not None else make_travel(),
        planning_reason=planning_reason,
    )


def sample_payload() -> dict:
    return json.loads((ROOT / "examples" / "sample-input.json").read_text(encoding="utf-8"))


def minimal_payload() -> dict:
    return copy.deepcopy(
        {
            "schema_version": "1.0",
            "plan_id": "unit-minimal",
            "locations": [{"id": "office"}, {"id": "client"}],
            "requests": [
                {
                    "id": "REQ-1",
                    "location_id": "client",
                    "duration_minutes": 30,
                    "window": {
                        "start": "2026-08-17T10:00:00+03:00",
                        "end": "2026-08-17T12:00:00+03:00",
                    },
                    "required_skills": ["connection"],
                    "required_vehicle": None,
                    "priority": "normal",
                }
            ],
            "technicians": [
                {
                    "id": "TECH-1",
                    "name": "Anna",
                    "start_location_id": "office",
                    "available_from": "2026-08-17T09:00:00+03:00",
                    "shift_end": "2026-08-17T18:00:00+03:00",
                    "skills": ["connection"],
                    "vehicle": "car",
                }
            ],
            "travel": {
                "symmetric": True,
                "legs": [
                    {
                        "from": "office",
                        "to": "client",
                        "minutes": 10,
                        "distance_km": 3.5,
                    }
                ],
            },
        }
    )
