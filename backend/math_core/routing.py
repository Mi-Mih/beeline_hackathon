"""Primary open-VRPTW planner implemented with OR-Tools Routing."""

from __future__ import annotations

from datetime import datetime, timedelta
from time import perf_counter
from typing import Any

from backend.math_core.greedy import GreedyPlanner
from backend.math_core.local_search import LocalSearchPlanner
from backend.math_core.models import PlanningProblem
from backend.math_core.simulate import (
    calibrated_travel_minutes,
    compatible,
    simulate_route,
)

MAX_TRAVEL_MINUTES = 20_000
MAX_DISTANCE_METERS = 10_000_000
W_TRAVEL = MAX_DISTANCE_METERS + 1
VEHICLE_FIXED = MAX_TRAVEL_MINUTES * W_TRAVEL + MAX_DISTANCE_METERS + 1


class RoutingPlanner:
    """OR-Tools RoutingModel with lexicographic drop penalties.

    Falls back to local search if OR-Tools is missing or the model returns an
    infeasible assignment.
    """

    algorithm_id = "ortools-routing"
    algorithm_version = "1.0"

    def __init__(self, time_limit_s: int = 5) -> None:
        self.time_limit_s = time_limit_s

    def plan(self, problem: PlanningProblem) -> dict[str, Any]:
        if problem.replan_context is not None:
            from backend.math_core.replan import plan_event

            return plan_event(problem, self)
        started = perf_counter()
        if not problem.requests:
            plan = _render(
                problem,
                {technician.id: [] for technician in problem.technicians},
                [],
            )
            plan["diagnostics"]["runtime_ms"] = round(
                (perf_counter() - started) * 1000, 3
            )
            return plan

        fallback = LocalSearchPlanner()
        try:
            from ortools.constraint_solver import pywrapcp, routing_enums_pb2
        except ImportError:
            plan = fallback.plan(problem)
            return _with_runtime(
                plan,
                started,
                ["ortools is not installed; used greedy-local-search"],
            )

        greedy_plan = GreedyPlanner().plan(problem)
        built = _solve(
            problem,
            greedy_plan,
            self.time_limit_s,
            pywrapcp,
            routing_enums_pb2,
        )
        if built["fallback"] or built["plan"] is None:
            plan = fallback.plan(problem)
            return _with_runtime(
                plan,
                started,
                [f"routing status={built['status']}; used greedy-local-search"],
            )
        plan = built["plan"]
        plan["diagnostics"]["runtime_ms"] = round((perf_counter() - started) * 1000, 3)
        return plan


def _with_runtime(
    plan: dict[str, Any], started: float, warnings: list[str]
) -> dict[str, Any]:
    result = dict(plan)
    diagnostics = dict(result.get("diagnostics") or {})
    diagnostics["runtime_ms"] = round((perf_counter() - started) * 1000, 3)
    diagnostics["warnings"] = list(diagnostics.get("warnings") or []) + warnings
    result["diagnostics"] = diagnostics
    return result


def _penalty_table(n_requests: int, n_vehicles: int) -> dict[str, int]:
    p_normal = n_vehicles * VEHICLE_FIXED + 1
    p_high = n_requests * p_normal + 1
    p_urgent = n_requests * p_high + 1
    return {
        "normal": p_normal,
        "high": p_high,
        "urgent": p_urgent,
    }


def _solve(
    problem: PlanningProblem,
    greedy_plan: dict[str, Any],
    time_limit_s: int,
    pywrapcp: Any,
    routing_enums_pb2: Any,
) -> dict[str, Any]:
    requests = list(problem.requests)
    techs = list(problem.technicians)
    n = len(requests)
    v = len(techs)
    if n == 0:
        return {"status": "trivial", "fallback": False, "plan": greedy_plan}

    t0 = min(tech.available_from for tech in techs)
    horizon = max(_minutes(tech.shift_end, t0) for tech in techs) + 1
    start_nodes = list(range(n, n + v))
    end_nodes = list(range(n + v, n + 2 * v))
    n_nodes = n + 2 * v
    manager = pywrapcp.RoutingIndexManager(n_nodes, v, start_nodes, end_nodes)
    routing = pywrapcp.RoutingModel(manager)
    penalties = _penalty_table(max(n, 1), max(v, 1))

    def node_location(node: int, vehicle: int) -> str | None:
        if node < n:
            return requests[node].location_id
        if node < n + v:
            return techs[node - n].start_location_id
        return None

    def travel(
        vehicle: int, origin: str | None, destination: str | None
    ) -> tuple[int, int] | None:
        if origin is None or destination is None or origin == destination:
            return (0, 0)
        leg = problem.travel.get(origin, destination, techs[vehicle].vehicle)
        if leg is None:
            return None
        minutes = calibrated_travel_minutes(
            leg.minutes, techs[vehicle].travel_time_factor
        )
        return (minutes, int(round(leg.distance_km * 1000)))

    cost_evaluators = []
    time_evaluators = []
    for vehicle in range(v):

        def make_cost(vehicle_index: int):
            def cb(from_index: int, to_index: int) -> int:
                frm = manager.IndexToNode(from_index)
                to = manager.IndexToNode(to_index)
                if to >= n + v:
                    return 0
                origin = node_location(frm, vehicle_index)
                dest = node_location(to, vehicle_index)
                if origin is None or dest is None:
                    return 0
                if to < n and not compatible(requests[to], techs[vehicle_index]):
                    return VEHICLE_FIXED
                leg = travel(vehicle_index, origin, dest)
                if leg is None:
                    return VEHICLE_FIXED
                minutes, meters = leg
                return minutes * W_TRAVEL + meters

            return cb

        def make_time(vehicle_index: int):
            def cb(from_index: int, to_index: int) -> int:
                frm = manager.IndexToNode(from_index)
                to = manager.IndexToNode(to_index)
                origin = node_location(frm, vehicle_index)
                dest = node_location(to, vehicle_index)
                service = requests[frm].duration_minutes if frm < n else 0
                if to < n and not compatible(requests[to], techs[vehicle_index]):
                    return horizon + 1
                if dest is None:
                    return service
                if origin is None:
                    return 0
                leg = travel(vehicle_index, origin, dest)
                if leg is None:
                    return horizon + 1
                return service + leg[0]

            return cb

        cost_evaluators.append(routing.RegisterTransitCallback(make_cost(vehicle)))
        time_evaluators.append(routing.RegisterTransitCallback(make_time(vehicle)))
        routing.SetArcCostEvaluatorOfVehicle(cost_evaluators[-1], vehicle)
        routing.SetFixedCostOfVehicle(VEHICLE_FIXED, vehicle)

    routing.AddDimensionWithVehicleTransits(time_evaluators, horizon, horizon, False, "Time")
    time_dim = routing.GetDimensionOrDie("Time")
    for vehicle, tech in enumerate(techs):
        start_index = routing.Start(vehicle)
        end_index = routing.End(vehicle)
        available = _minutes(tech.available_from, t0)
        shift = _minutes(tech.shift_end, t0)
        time_dim.CumulVar(start_index).SetRange(available, shift)
        time_dim.CumulVar(end_index).SetRange(available, shift)

    for i, request in enumerate(requests):
        index = manager.NodeToIndex(i)
        start = max(0, _minutes(request.window.start, t0))
        end = min(horizon, _minutes(request.window.end, t0))
        if start > end:
            # The window misses [0, horizon]. A negative pin makes OR-Tools
            # raise CP Solver fail; keep an in-domain instant instead.
            # A visit there is still outside the real window, so _infeasible
            # rejects it and the disjunction can drop the node.
            start = min(max(end, 0), horizon)
            end = start
        time_dim.CumulVar(index).SetRange(start, end)
        routing.AddDisjunction([index], penalties[request.priority])
        if problem.reaction_at is not None and request.id in problem.reaction_request_ids:
            deadline = max(0, _minutes(problem.reaction_at + timedelta(hours=2), t0))
            time_dim.SetCumulVarSoftUpperBound(index, deadline, 10 * W_TRAVEL)

    search = pywrapcp.DefaultRoutingSearchParameters()
    search.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.DESCRIPTOR.enum_values_by_name[
            "PARALLEL_CHEAPEST_INSERTION"
        ].number
    )
    search.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.DESCRIPTOR.enum_values_by_name[
            "GUIDED_LOCAL_SEARCH"
        ].number
    )
    search.time_limit.FromSeconds(max(1, time_limit_s))

    assignment = routing.SolveWithParameters(search)
    status = _status_name(routing)
    if assignment is None:
        return {"status": status, "fallback": True, "plan": None}

    routes: dict[str, list[str]] = {tech.id: [] for tech in techs}
    for vehicle, tech in enumerate(techs):
        index = routing.Start(vehicle)
        sequence = []
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node < n:
                sequence.append(requests[node].id)
            index = assignment.Value(routing.NextVar(index))
        routes[tech.id] = sequence
    unassigned = [
        request.id
        for request in requests
        if all(request.id not in routes[tech.id] for tech in techs)
    ]
    if _infeasible(problem, routes):
        return {"status": status, "fallback": True, "plan": None}
    return {
        "status": status,
        "fallback": False,
        "plan": _render(problem, routes, unassigned),
    }


def _status_name(routing: Any) -> str:
    status_map = {}
    for name in (
        "ROUTING_NOT_SOLVED",
        "ROUTING_SUCCESS",
        "ROUTING_PARTIAL_SUCCESS_LOCAL_OPTIMUM_NOT_REACHED",
        "ROUTING_FAIL",
        "ROUTING_FAIL_TIMEOUT",
        "ROUTING_INVALID",
        "ROUTING_INFEASIBLE",
    ):
        value = getattr(routing, name, None)
        if value is not None:
            status_map[value] = name.replace("ROUTING_", "")
    return status_map.get(routing.status(), str(routing.status()))


def _infeasible(problem: PlanningProblem, routes: dict[str, list[str]]) -> bool:
    techs = {tech.id: tech for tech in problem.technicians}
    requests = {request.id: request for request in problem.requests}
    for tech_id, sequence in routes.items():
        if not sequence:
            continue
        items = [requests[request_id] for request_id in sequence]
        simulated, _failure = simulate_route(items, techs[tech_id], problem.travel)
        if simulated is None:
            return True
        if any(not compatible(request, techs[tech_id]) for request in items):
            return True
    return False


def _render(
    problem: PlanningProblem,
    routes: dict[str, list[str]],
    unassigned: list[str],
) -> dict[str, Any]:
    requests = {request.id: request for request in problem.requests}
    techs = {tech.id: tech for tech in problem.technicians}
    rendered = []
    assigned_count = 0
    total_distance = 0.0
    total_travel = 0
    total_service = 0
    total_wait = 0
    for tech in problem.technicians:
        sequence = routes.get(tech.id, [])
        if not sequence:
            continue
        items = [requests[request_id] for request_id in sequence]
        simulated, _failure = simulate_route(items, tech, problem.travel)
        if simulated is None:
            raise RuntimeError("routing produced an unschedulable route")
        stops = []
        for sequence_no, stop in enumerate(simulated.stops, start=1):
            request = stop.request
            stops.append(
                {
                    "sequence": sequence_no,
                    "request_id": request.id,
                    "location_id": request.location_id,
                    "arrival_at": stop.arrival.isoformat(),
                    "service_start_at": stop.service_start.isoformat(),
                    "service_end_at": stop.service_end.isoformat(),
                    "travel_minutes": stop.travel_minutes,
                    "distance_km": round(stop.distance_km, 3),
                    "wait_minutes": stop.wait_minutes,
                    "explanation": {
                        "code": (
                            "MANUAL_ASSIGNMENT"
                            if request.locked_technician_id
                            else "ORTOOLS_ROUTING"
                        ),
                        "message": (
                            "Назначено диспетчером; ограничения соблюдены."
                            if request.locked_technician_id
                            else "Подходит по навыкам, оснащению и времени; маршрут построен OR-Tools Routing."
                        ),
                    },
                }
            )
        rendered.append(
            {
                "technician_id": tech.id,
                "technician_name": tech.name,
                "start_location_id": tech.start_location_id,
                "finish_at": simulated.finish_at.isoformat(),
                "stops": stops,
                "metrics": {
                    "request_count": len(sequence),
                    "distance_km": round(simulated.distance_km, 3),
                    "travel_minutes": simulated.travel_minutes,
                    "service_minutes": sum(item.duration_minutes for item in items),
                    "wait_minutes": simulated.wait_minutes,
                },
            }
        )
        service_minutes = sum(item.duration_minutes for item in items)
        assigned_count += len(sequence)
        total_distance += simulated.distance_km
        total_travel += simulated.travel_minutes
        total_service += service_minutes
        total_wait += simulated.wait_minutes

    unassigned_rows = [
        GreedyPlanner._unassigned(requests[request_id], "NO_FEASIBLE_TIME_SLOT")
        for request_id in sorted(unassigned)
    ]
    return {
        "schema_version": "1.0",
        "plan_id": problem.plan_id,
        "algorithm": {
            "id": RoutingPlanner.algorithm_id,
            "version": RoutingPlanner.algorithm_version,
        },
        "routes": rendered,
        "unassigned": unassigned_rows,
        "metrics": {
            "request_count": len(problem.requests),
            "assigned_count": assigned_count,
            "unassigned_count": len(unassigned_rows),
            "used_technicians": len(rendered),
            "distance_km": round(total_distance, 3),
            "travel_minutes": total_travel,
            "service_minutes": total_service,
            "wait_minutes": total_wait,
        },
        "diagnostics": {"runtime_ms": 0.0, "warnings": []},
    }


def _minutes(moment: datetime, origin: datetime) -> int:
    return int((moment - origin).total_seconds() // 60)
