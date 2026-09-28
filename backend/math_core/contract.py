"""JSON trust-boundary: validate plan-input 1.0 and build the solver model."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from backend.math_core.models import (
    ActiveWork,
    SCHEMA_VERSION,
    PLANNING_OVERNIGHT,
    PLANNING_REASONS,
    PlanningProblem,
    ReplanContext,
    Request,
    Technician,
    TimeWindow,
    TravelLeg,
    TravelTable,
)


class ContractError(ValueError):
    """The input does not satisfy the planning contract."""


def parse_problem(payload: dict[str, Any]) -> PlanningProblem:
    """Validate JSON at the trust boundary and create the solver model."""

    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"schema_version must be {SCHEMA_VERSION!r}")
    plan_id = _text(payload.get("plan_id"), "plan_id")
    planning_reason = payload.get("planning_reason", PLANNING_OVERNIGHT)
    if planning_reason not in PLANNING_REASONS:
        raise ContractError("planning_reason must be 'overnight' or 'replan'")

    location_rows = _list(payload.get("locations"), "locations")
    location_ids = [_text(row.get("id"), "locations[].id") for row in location_rows]
    _unique(location_ids, "location ids")

    requests = []
    for index, row in enumerate(_list(payload.get("requests"), "requests")):
        path = f"requests[{index}]"
        window = row.get("window")
        if not isinstance(window, dict):
            raise ContractError(f"{path}.window must be an object")
        start = _datetime(window.get("start"), f"{path}.window.start")
        end = _datetime(window.get("end"), f"{path}.window.end")
        if start > end:
            raise ContractError(f"{path}.window start must not be after end")
        location_id = _text(row.get("location_id"), f"{path}.location_id")
        if location_id not in location_ids:
            raise ContractError(f"{path}.location_id is unknown: {location_id}")
        duration = _positive_int(row.get("duration_minutes"), f"{path}.duration_minutes")
        skills = frozenset(_texts(row.get("required_skills"), f"{path}.required_skills"))
        vehicle = row.get("required_vehicle")
        if vehicle is not None:
            vehicle = _text(vehicle, f"{path}.required_vehicle")
        priority = row.get("priority")
        if priority not in {"normal", "high", "urgent"}:
            raise ContractError(
                f"{path}.priority must be 'normal', 'high' or 'urgent'"
            )
        equipment = frozenset(
            _optional_texts(row.get("required_equipment"), f"{path}.required_equipment")
        )
        locked_technician_id = row.get("locked_technician_id")
        if locked_technician_id is not None:
            locked_technician_id = _text(
                locked_technician_id, f"{path}.locked_technician_id"
            )
        requests.append(
            Request(
                id=_text(row.get("id"), f"{path}.id"),
                location_id=location_id,
                duration_minutes=duration,
                window=TimeWindow(start, end),
                required_skills=skills,
                required_vehicle=vehicle,
                required_equipment=equipment,
                locked_technician_id=locked_technician_id,
                priority=priority,
            )
        )
    _unique([request.id for request in requests], "request ids")

    technicians = []
    for index, row in enumerate(_list(payload.get("technicians"), "technicians")):
        path = f"technicians[{index}]"
        start_location_id = _text(
            row.get("start_location_id"), f"{path}.start_location_id"
        )
        if start_location_id not in location_ids:
            raise ContractError(
                f"{path}.start_location_id is unknown: {start_location_id}"
            )
        available_from = _datetime(row.get("available_from"), f"{path}.available_from")
        shift_end = _datetime(row.get("shift_end"), f"{path}.shift_end")
        if available_from >= shift_end:
            raise ContractError(f"{path}.available_from must be before shift_end")
        technicians.append(
            Technician(
                id=_text(row.get("id"), f"{path}.id"),
                name=_text(row.get("name"), f"{path}.name"),
                start_location_id=start_location_id,
                available_from=available_from,
                shift_end=shift_end,
                skills=frozenset(_texts(row.get("skills"), f"{path}.skills")),
                vehicle=_text(row.get("vehicle"), f"{path}.vehicle"),
                equipment=frozenset(
                    _optional_texts(row.get("equipment"), f"{path}.equipment")
                ),
            )
        )
    if not technicians:
        raise ContractError("technicians must not be empty")
    _unique([technician.id for technician in technicians], "technician ids")
    technician_ids = {technician.id for technician in technicians}
    for request in requests:
        if (
            request.locked_technician_id is not None
            and request.locked_technician_id not in technician_ids
        ):
            raise ContractError(
                f"request {request.id} refers to unknown locked technician: "
                f"{request.locked_technician_id}"
            )

    travel_row = payload.get("travel")
    if not isinstance(travel_row, dict):
        raise ContractError("travel must be an object")
    symmetric = travel_row.get("symmetric")
    if not isinstance(symmetric, bool):
        raise ContractError("travel.symmetric must be boolean")
    legs: dict[tuple[str, str, str | None], TravelLeg] = {}
    for index, row in enumerate(_list(travel_row.get("legs"), "travel.legs")):
        path = f"travel.legs[{index}]"
        origin = _text(row.get("from"), f"{path}.from")
        destination = _text(row.get("to"), f"{path}.to")
        if origin not in location_ids or destination not in location_ids:
            raise ContractError(f"{path} refers to an unknown location")
        mode = row.get("mode")
        if mode is not None:
            mode = _text(mode, f"{path}.mode")
        key = (origin, destination, mode)
        if key in legs:
            suffix = f" for {mode}" if mode else ""
            raise ContractError(
                f"duplicate travel leg: {origin} -> {destination}{suffix}"
            )
        legs[key] = TravelLeg(
            minutes=_nonnegative_int(row.get("minutes"), f"{path}.minutes"),
            distance_km=_nonnegative_number(
                row.get("distance_km"), f"{path}.distance_km"
            ),
        )

    replan_context = _replan_context(
        payload.get("replan_context"), planning_reason, requests, technicians, location_ids
    )

    return PlanningProblem(
        plan_id=plan_id,
        requests=tuple(requests),
        technicians=tuple(technicians),
        travel=TravelTable(legs, symmetric),
        planning_reason=planning_reason,
        replan_context=replan_context,
    )


def _replan_context(
    value: Any,
    planning_reason: str,
    requests: list[Request],
    technicians: list[Technician],
    location_ids: list[str],
) -> ReplanContext | None:
    if value is None:
        return None
    if planning_reason != "replan" or not isinstance(value, dict):
        raise ContractError("replan_context requires planning_reason 'replan' and an object")
    event_at = _datetime(value.get("event_at"), "replan_context.event_at")
    new_ids = _texts(value.get("new_request_ids"), "replan_context.new_request_ids")
    requests_by_id = {request.id: request for request in requests}
    if set(new_ids) - requests_by_id.keys():
        raise ContractError("replan_context.new_request_ids contains an unknown request")
    techs_by_id = {tech.id: tech for tech in technicians}
    for tech in technicians:
        if tech.available_from < event_at:
            raise ContractError(
                f"technician {tech.id} available_from must be at or after event_at"
            )

    previous_routes = []
    seen_techs: set[str] = set()
    seen_requests: set[str] = set()
    for index, row in enumerate(_list(value.get("previous_routes"), "replan_context.previous_routes")):
        path = f"replan_context.previous_routes[{index}]"
        tech_id = _text(row.get("technician_id"), f"{path}.technician_id")
        if tech_id not in techs_by_id or tech_id in seen_techs:
            raise ContractError(f"{path}.technician_id is unknown or repeated")
        seen_techs.add(tech_id)
        sequence = _optional_texts(row.get("request_ids"), f"{path}.request_ids")
        for request_id in sequence:
            if request_id not in requests_by_id or request_id in seen_requests or request_id in new_ids:
                raise ContractError(f"{path}.request_ids contains an unknown, repeated or new request")
            seen_requests.add(request_id)
        previous_routes.append((tech_id, tuple(sequence)))

    active_work = []
    seen_active_techs: set[str] = set()
    seen_active_requests: set[str] = set()
    for index, row in enumerate(_list(value.get("active_work", []), "replan_context.active_work")):
        path = f"replan_context.active_work[{index}]"
        tech_id = _text(row.get("technician_id"), f"{path}.technician_id")
        request_id = _text(row.get("request_id"), f"{path}.request_id")
        location_id = _text(row.get("location_id"), f"{path}.location_id")
        finish_at = _datetime(row.get("finish_at"), f"{path}.finish_at")
        if tech_id not in techs_by_id or tech_id in seen_active_techs:
            raise ContractError(f"{path}.technician_id is unknown or repeated")
        if request_id in requests_by_id or request_id in seen_active_requests:
            raise ContractError(f"{path}.request_id must be an active, non-pending request")
        if location_id not in location_ids or finish_at <= event_at:
            raise ContractError(f"{path} has an unknown location or invalid finish_at")
        tech = techs_by_id[tech_id]
        if tech.start_location_id != location_id or tech.available_from < finish_at:
            raise ContractError(f"{path} disagrees with the technician's next availability")
        active_work.append(ActiveWork(tech_id, request_id, location_id, finish_at))
        seen_active_techs.add(tech_id)
        seen_active_requests.add(request_id)

    return ReplanContext(event_at, tuple(new_ids), tuple(previous_routes), tuple(active_work))


def _list(value: Any, path: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ContractError(f"{path} must be an array of objects")
    return value


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{path} must be a non-empty string")
    return value.strip()


def _texts(value: Any, path: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ContractError(f"{path} must be a non-empty string array")
    result = [_text(item, f"{path}[]") for item in value]
    _unique(result, path)
    return result


def _optional_texts(value: Any, path: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ContractError(f"{path} must be a string array")
    result = [_text(item, f"{path}[]") for item in value]
    _unique(result, path)
    return result


def _datetime(value: Any, path: str) -> datetime:
    if not isinstance(value, str):
        raise ContractError(f"{path} must be an ISO 8601 timestamp")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise ContractError(f"{path} must be an ISO 8601 timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ContractError(f"{path} must include a UTC offset")
    return result


def _positive_int(value: Any, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ContractError(f"{path} must be a positive integer")
    return value


def _nonnegative_int(value: Any, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ContractError(f"{path} must be a non-negative integer")
    return value


def _nonnegative_number(value: Any, path: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ContractError(f"{path} must be a non-negative number")
    return float(value)


def _unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ContractError(f"{label} must be unique")
