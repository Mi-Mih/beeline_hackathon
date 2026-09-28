"""Domain objects for technician routing with time windows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

SCHEMA_VERSION = "1.0"
PLANNING_OVERNIGHT = "overnight"
PLANNING_REPLAN = "replan"
PLANNING_REASONS = frozenset({PLANNING_OVERNIGHT, PLANNING_REPLAN})


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Request:
    id: str
    location_id: str
    duration_minutes: int
    window: TimeWindow
    required_skills: frozenset[str]
    required_vehicle: str | None
    required_equipment: frozenset[str]
    locked_technician_id: str | None
    priority: str


@dataclass(frozen=True)
class Technician:
    id: str
    name: str
    start_location_id: str
    available_from: datetime
    shift_end: datetime
    skills: frozenset[str]
    vehicle: str
    equipment: frozenset[str]
    travel_time_factor: float = 1.0


@dataclass(frozen=True)
class TravelLeg:
    minutes: int
    distance_km: float


class TravelTable:
    def __init__(
        self, legs: dict[tuple[str, str, str | None], TravelLeg], symmetric: bool
    ):
        self._legs = legs
        self._symmetric = symmetric

    def get(
        self, origin: str, destination: str, mode: str | None = None
    ) -> TravelLeg | None:
        if origin == destination:
            return TravelLeg(0, 0.0)
        leg = self._legs.get((origin, destination, mode))
        if leg is None:
            leg = self._legs.get((origin, destination, None))
        if leg is None and self._symmetric:
            leg = self._legs.get((destination, origin, mode))
        if leg is None and self._symmetric:
            leg = self._legs.get((destination, origin, None))
        return leg


@dataclass(frozen=True)
class ActiveWork:
    technician_id: str
    request_id: str
    location_id: str
    finish_at: datetime


@dataclass(frozen=True)
class ReplanContext:
    event_at: datetime
    new_request_ids: tuple[str, ...]
    previous_routes: tuple[tuple[str, tuple[str, ...]], ...]
    active_work: tuple[ActiveWork, ...] = ()


@dataclass(frozen=True)
class PlanningProblem:
    plan_id: str
    requests: tuple[Request, ...]
    technicians: tuple[Technician, ...]
    travel: TravelTable
    planning_reason: str = PLANNING_OVERNIGHT
    replan_context: ReplanContext | None = None
    reaction_at: datetime | None = None
    reaction_request_ids: frozenset[str] = frozenset()


class Planner(Protocol):
    """Replacement planners implement only this method."""

    def plan(self, problem: PlanningProblem) -> dict[str, Any]: ...
