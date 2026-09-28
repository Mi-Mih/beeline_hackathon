"""Apply the intraday event rules to a snapshot of remaining work."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from time import perf_counter
from typing import Any

from backend.math_core.contract import ContractError
from backend.math_core.greedy import GreedyPlanner
from backend.math_core.models import PlanningProblem, Request, TimeWindow
from backend.math_core.simulate import compatible, simulate_route


def plan_event(problem: PlanningProblem, routing_planner: Any) -> dict[str, Any]:
    context = problem.replan_context
    if context is None:
        raise ValueError("an intraday event requires replan_context")
    new_ids = set(context.new_request_ids)
    emergency_ids = frozenset(
        request.id for request in problem.requests
        if request.id in new_ids and request.priority == "urgent"
    )
    prepared = replace(
        problem,
        requests=tuple(
            replace(request, window=TimeWindow(max(request.window.start, context.event_at), request.window.end))
            if request.id in new_ids
            else request
            for request in problem.requests
        ),
        replan_context=None,
        reaction_at=context.event_at if emergency_ids else None,
        reaction_request_ids=emergency_ids,
    )

    if emergency_ids:
        result = routing_planner.plan(prepared)
        _report_reaction(result, context.event_at, emergency_ids)
        return result
    return _insert_ordinary(prepared, new_ids, context.previous_routes)


def _insert_ordinary(
    problem: PlanningProblem,
    new_ids: set[str],
    previous_routes: tuple[tuple[str, tuple[str, ...]], ...],
) -> dict[str, Any]:
    from backend.math_core.routing import _render

    started = perf_counter()
    requests = {request.id: request for request in problem.requests}
    routes = {tech.id: [] for tech in problem.technicians}
    technicians = {tech.id: tech for tech in problem.technicians}
    for tech_id, request_ids in previous_routes:
        route = [requests[request_id] for request_id in request_ids]
        if any(not compatible(request, technicians[tech_id]) for request in route):
            raise ContractError(f"previous route for {tech_id} is incompatible with the snapshot")
        simulation, failure = simulate_route(route, technicians[tech_id], problem.travel)
        if simulation is None:
            raise ContractError(f"previous route for {tech_id} is infeasible: {failure}")
        routes[tech_id] = list(request_ids)

    for request in sorted(
        (requests[request_id] for request_id in new_ids),
        key=lambda item: (
            {"high": 0, "normal": 1}[item.priority],
            item.window.end,
            item.id,
        ),
    ):
        candidates = []
        for tech in problem.technicians:
            if not compatible(request, tech):
                continue
            old_ids = routes[tech.id]
            old_sim, _ = simulate_route([requests[item] for item in old_ids], tech, problem.travel)
            for position in range(len(old_ids) + 1):
                trial = old_ids[:position] + [request.id] + old_ids[position:]
                simulation, _ = simulate_route([requests[item] for item in trial], tech, problem.travel)
                if simulation is None:
                    continue
                candidates.append((
                    (
                        not bool(old_ids),
                        simulation.travel_minutes - old_sim.travel_minutes,
                        simulation.distance_km - old_sim.distance_km,
                        simulation.finish_at,
                        tech.id,
                        position,
                    ),
                    tech.id,
                    trial,
                ))
        if candidates:
            _, tech_id, trial = min(candidates, key=lambda item: item[0])
            routes[tech_id] = trial

    assigned = {request_id for route in routes.values() for request_id in route}
    unassigned = [request.id for request in problem.requests if request.id not in assigned]
    result = _render(problem, routes, unassigned)
    result["algorithm"] = {"id": "stable-event-insertion", "version": "1.0"}
    for route in result["routes"]:
        for stop in route["stops"]:
            if stop["explanation"]["code"] == "ORTOOLS_ROUTING":
                stop["explanation"] = {
                    "code": "STABLE_EVENT_INSERTION",
                    "message": "Сохранены прежние назначения и порядок работ.",
                }
    result["diagnostics"]["runtime_ms"] = round((perf_counter() - started) * 1000, 3)
    # Preserve precise compatibility reasons for newly received work.
    for row in result["unassigned"]:
        request = requests[row["request_id"]]
        if request.id in new_ids:
            code = _reason(request, problem)
            row["reason"] = GreedyPlanner._unassigned(request, code)["reason"]
    return result


def _reason(request: Request, problem: PlanningProblem) -> str:
    skilled = [tech for tech in problem.technicians if request.required_skills <= tech.skills]
    if not skilled:
        return "NO_SKILLED_TECHNICIAN"
    vehicled = [tech for tech in skilled if request.required_vehicle is None or request.required_vehicle == tech.vehicle]
    if not vehicled:
        return "NO_REQUIRED_VEHICLE"
    equipped = [tech for tech in vehicled if request.required_equipment <= tech.equipment]
    if not equipped:
        return "NO_REQUIRED_EQUIPMENT"
    if not any(compatible(request, tech) for tech in equipped):
        return "LOCKED_TECHNICIAN_UNAVAILABLE"
    return "NO_FEASIBLE_TIME_SLOT"


def _report_reaction(result: dict[str, Any], event_at: datetime, new_ids: set[str]) -> None:
    urgent_stops = [
        stop
        for route in result["routes"]
        for stop in route["stops"]
        if stop["request_id"] in new_ids
    ]
    warnings = result["diagnostics"]["warnings"]
    for stop in urgent_stops:
        minutes = (datetime.fromisoformat(stop["service_start_at"]) - event_at).total_seconds() / 60
        if minutes > 120:
            warnings.append(
                f"{stop['request_id']}: reaction {minutes:.0f} min exceeds the 1–2 h guideline"
            )
