"""Shared feasible-route simulation used by constructive and local-search planners."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.math_core.models import Request, Technician, TravelTable


@dataclass(frozen=True)
class Stop:
    request: Request
    arrival: datetime
    service_start: datetime
    service_end: datetime
    travel_minutes: int
    distance_km: float
    wait_minutes: int


@dataclass(frozen=True)
class Simulation:
    stops: tuple[Stop, ...]
    travel_minutes: int
    distance_km: float
    wait_minutes: int
    finish_at: datetime


def compatible(request: Request, technician: Technician) -> bool:
    return (
        request.required_skills <= technician.skills
        and request.required_equipment <= technician.equipment
        and (
            request.required_vehicle is None
            or request.required_vehicle == technician.vehicle
        )
        and (
            request.locked_technician_id is None
            or request.locked_technician_id == technician.id
        )
    )


def calibrated_travel_minutes(minutes: int, factor: float) -> int:
    """Scale an OSRM estimate while preserving zero-length legs."""
    if minutes <= 0:
        return 0
    return max(1, math.ceil(minutes * factor))


def simulate_route(
    route: list[Request], technician: Technician, travel: TravelTable
) -> tuple[Simulation | None, str]:
    moment = technician.available_from
    location = technician.start_location_id
    stops: list[Stop] = []
    travel_minutes = 0
    distance_km = 0.0
    wait_minutes = 0

    for request in route:
        leg = travel.get(location, request.location_id, technician.vehicle)
        if leg is None:
            return None, "TRAVEL_DATA_MISSING"
        leg_minutes = calibrated_travel_minutes(
            leg.minutes, technician.travel_time_factor
        )
        arrival = moment + timedelta(minutes=leg_minutes)
        service_start = max(arrival, request.window.start)
        wait = int((service_start - arrival).total_seconds() // 60)
        service_end = service_start + timedelta(minutes=request.duration_minutes)
        if service_start > request.window.end or service_end > technician.shift_end:
            return None, "NO_FEASIBLE_TIME_SLOT"
        stops.append(
            Stop(
                request=request,
                arrival=arrival,
                service_start=service_start,
                service_end=service_end,
                travel_minutes=leg_minutes,
                distance_km=leg.distance_km,
                wait_minutes=wait,
            )
        )
        moment = service_end
        location = request.location_id
        travel_minutes += leg_minutes
        distance_km += leg.distance_km
        wait_minutes += wait

    return (
        Simulation(
            stops=tuple(stops),
            travel_minutes=travel_minutes,
            distance_km=distance_km,
            wait_minutes=wait_minutes,
            finish_at=moment,
        ),
        "",
    )


def lex_score(
    problem_requests: tuple[Request, ...],
    assigned_ids: set[str],
    used_technicians: int,
    travel_minutes: int,
    distance_km: float,
) -> tuple[int, int, int, int, int, int]:
    counts = {"urgent": 0, "high": 0, "normal": 0}
    for request in problem_requests:
        if request.id not in assigned_ids:
            counts[request.priority] += 1
    return (
        counts["urgent"],
        counts["high"],
        counts["normal"],
        used_technicians,
        travel_minutes,
        int(round(distance_km * 1000)),
    )
