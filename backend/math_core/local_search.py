"""Lexicographic local search polish on top of cheapest insertion."""

from __future__ import annotations

from time import perf_counter
from typing import Any, Iterable, Iterator

from backend.math_core.assignment import polish_route_assignment
from backend.math_core.greedy import GreedyPlanner
from backend.math_core.models import PlanningProblem
from backend.math_core.simulate import Simulation, compatible, lex_score, simulate_route

Score = tuple[int, int, int, int, int, int]
State = tuple[Score, dict[str, list[str]], list[str]]
RouteCache = dict[str, tuple[tuple[str, ...], Simulation | None]]


class LocalSearchPlanner:
    """Greedy construction plus relocate/swap/2-opt/reinsert.

    Moves are accepted only when they strictly improve the experimental
    lexicographic vector (urgent, high, normal, technicians, travel, distance).
    """

    algorithm_id = "greedy-local-search"
    algorithm_version = "1.2"

    def __init__(
        self,
        budget_s: float = 1.0,
        strategy: str = "best",
        operators: Iterable[str] | None = None,
        eliminate_routes: bool = True,
    ) -> None:
        self.budget_s = budget_s
        self.strategy = strategy
        self.operators = tuple(operators or ("reinsert", "relocate", "swap", "two_opt"))
        self.eliminate_routes = eliminate_routes
        self.greedy = GreedyPlanner()

    def plan(self, problem: PlanningProblem) -> dict[str, Any]:
        started = perf_counter()
        base = self.greedy.plan(problem)
        routes = {
            route["technician_id"]: [stop["request_id"] for stop in route["stops"]]
            for route in base["routes"]
        }
        unassigned = [row["request_id"] for row in base["unassigned"]]
        reasons = {row["request_id"]: row for row in base["unassigned"]}
        cache: RouteCache = {}
        current = _evaluate(problem, routes, unassigned, cache=cache, update_cache=True)
        if current is None:
            return base
        deadline = started + self.budget_s
        improved = True
        while improved and perf_counter() < deadline:
            improved = False
            best: tuple[Score, dict[str, list[str]], list[str], str] | None = None
            for operator in self.operators:
                for trial_routes, trial_unassigned, touched in _neighbors(
                    problem, current[1], current[2], operator, deadline
                ):
                    if perf_counter() >= deadline:
                        break
                    evaluated = _evaluate(
                        problem,
                        trial_routes,
                        trial_unassigned,
                        cache=cache,
                        touched=touched,
                    )
                    if evaluated is None or evaluated[0] >= current[0]:
                        continue
                    if self.strategy == "first":
                        current = evaluated
                        _evaluate(
                            problem,
                            current[1],
                            current[2],
                            cache=cache,
                            update_cache=True,
                        )
                        improved = True
                        break
                    if best is None or evaluated[0] < best[0]:
                        best = (evaluated[0], evaluated[1], evaluated[2], operator)
                if improved:
                    break
            if not improved and best is not None:
                current = (best[0], best[1], best[2])
                _evaluate(
                    problem,
                    current[1],
                    current[2],
                    cache=cache,
                    update_cache=True,
                )
                improved = True
        if self.eliminate_routes and perf_counter() < deadline:
            current = _eliminate_routes(problem, current, cache, deadline)
        assignment_candidate = polish_route_assignment(problem, current[1])
        if assignment_candidate is not None:
            polished = _evaluate(problem, assignment_candidate, current[2])
            if polished is not None and polished[0] < current[0]:
                current = polished
        return _render(problem, current[1], current[2], started, reasons)


def _clone(routes: dict[str, list[str]]) -> dict[str, list[str]]:
    return {tech_id: list(sequence) for tech_id, sequence in routes.items()}


def _evaluate(
    problem: PlanningProblem,
    routes: dict[str, list[str]],
    unassigned: list[str],
    *,
    cache: RouteCache | None = None,
    touched: set[str] | None = None,
    update_cache: bool = False,
) -> State | None:
    techs = {tech.id: tech for tech in problem.technicians}
    requests = {request.id: request for request in problem.requests}
    used = 0
    travel = 0
    distance_km = 0.0
    assigned: set[str] = set()
    for tech_id, sequence in routes.items():
        if not sequence:
            if cache is not None and update_cache:
                cache[tech_id] = (tuple(), None)
            continue
        used += 1
        items = [requests[request_id] for request_id in sequence]
        route_key = tuple(sequence)
        simulated = None
        if cache is not None and (touched is None or tech_id not in touched):
            cached = cache.get(tech_id)
            if cached is not None and cached[0] == route_key:
                simulated = cached[1]
        if simulated is None:
            simulated, _failure = simulate_route(items, techs[tech_id], problem.travel)
        if simulated is None:
            return None
        if cache is not None and update_cache:
            cache[tech_id] = (route_key, simulated)
        for request in items:
            if request.id in assigned or not compatible(request, techs[tech_id]):
                return None
            assigned.add(request.id)
        travel += simulated.travel_minutes
        distance_km += simulated.distance_km
    leftover = [request.id for request in problem.requests if request.id not in assigned]
    _ = unassigned
    score = lex_score(problem.requests, assigned, used, travel, distance_km)
    return score, _clone(routes), leftover


def _neighbors(
    problem: PlanningProblem,
    routes: dict[str, list[str]],
    unassigned: list[str],
    operator: str,
    deadline: float,
) -> Iterator[tuple[dict[str, list[str]], list[str], set[str]]]:
    techs = {tech.id: tech for tech in problem.technicians}
    requests = {request.id: request for request in problem.requests}
    tech_ids = [tech.id for tech in problem.technicians]
    filled = {tech_id: list(routes.get(tech_id, [])) for tech_id in tech_ids}

    if operator == "reinsert":
        for request_id in unassigned:
            request = requests[request_id]
            for tech_id in tech_ids:
                if perf_counter() >= deadline:
                    return
                if not compatible(request, techs[tech_id]):
                    continue
                route = filled[tech_id]
                for position in range(len(route) + 1):
                    trial = _clone(filled)
                    trial[tech_id] = route[:position] + [request_id] + route[position:]
                    leftover = [item for item in unassigned if item != request_id]
                    yield trial, leftover, {tech_id}
        return

    if operator == "relocate":
        for src in tech_ids:
            src_route = filled[src]
            for index, request_id in enumerate(src_route):
                request = requests[request_id]
                lock = request.locked_technician_id
                for dst in tech_ids:
                    if perf_counter() >= deadline:
                        return
                    if lock is not None and lock != dst:
                        continue
                    if not compatible(request, techs[dst]):
                        continue
                    reduced = src_route[:index] + src_route[index + 1 :]
                    dst_route = filled[dst] if dst != src else reduced
                    for position in range(len(dst_route) + 1):
                        if dst == src:
                            original_position = position if position <= index else position + 1
                            if original_position == index:
                                continue
                        trial = _clone(filled)
                        trial[src] = reduced
                        trial[dst] = dst_route[:position] + [request_id] + dst_route[position:]
                        yield trial, list(unassigned), {src, dst}
        return

    if operator == "swap":
        for src_index, src in enumerate(tech_ids):
            for i, request_a in enumerate(filled[src]):
                for dst in tech_ids[src_index:]:
                    start_j = i + 1 if dst == src else 0
                    for j, request_b in enumerate(filled[dst][start_j:], start=start_j):
                        if perf_counter() >= deadline:
                            return
                        lock_a = requests[request_a].locked_technician_id
                        lock_b = requests[request_b].locked_technician_id
                        if lock_a is not None and lock_a != dst:
                            continue
                        if lock_b is not None and lock_b != src:
                            continue
                        if not compatible(requests[request_a], techs[dst]):
                            continue
                        if not compatible(requests[request_b], techs[src]):
                            continue
                        trial = _clone(filled)
                        trial[src][i] = request_b
                        trial[dst][j] = request_a
                        yield trial, list(unassigned), {src, dst}
        return

    if operator == "two_opt":
        for tech_id in tech_ids:
            route = filled[tech_id]
            n = len(route)
            if n < 3:
                continue
            for left in range(n - 1):
                for right in range(left + 2, n + 1):
                    if perf_counter() >= deadline:
                        return
                    trial = _clone(filled)
                    trial[tech_id] = (
                        route[:left] + list(reversed(route[left:right])) + route[right:]
                    )
                    yield trial, list(unassigned), {tech_id}


def _eliminate_routes(
    problem: PlanningProblem,
    current: State,
    cache: RouteCache,
    deadline: float,
) -> State:
    """Atomically redistribute a complete route when that improves the lex score."""

    requests = {request.id: request for request in problem.requests}
    skipped: set[str] = set()
    while perf_counter() < deadline:
        candidates = sorted(
            (len(sequence), tech_id)
            for tech_id, sequence in current[1].items()
            if sequence and tech_id not in skipped
        )
        if not candidates:
            return current

        improved = False
        for _length, source_id in candidates:
            if perf_counter() >= deadline:
                return current
            source_route = current[1][source_id]
            if any(
                requests[request_id].locked_technician_id == source_id
                for request_id in source_route
            ):
                skipped.add(source_id)
                continue

            trial = _clone(current[1])
            trial[source_id] = []
            for request_id in source_route:
                if perf_counter() >= deadline or not _insert_cheapest(
                    problem, trial, request_id, source_id, deadline
                ):
                    break
            else:
                evaluated = _evaluate(problem, trial, current[2])
                if evaluated is not None and evaluated[0] < current[0]:
                    current = evaluated
                    _evaluate(
                        problem,
                        current[1],
                        current[2],
                        cache=cache,
                        update_cache=True,
                    )
                    skipped.clear()
                    improved = True
                    break
            skipped.add(source_id)

        if not improved:
            return current
    return current


def _insert_cheapest(
    problem: PlanningProblem,
    routes: dict[str, list[str]],
    request_id: str,
    source_id: str,
    deadline: float,
) -> bool:
    techs = {tech.id: tech for tech in problem.technicians}
    requests = {request.id: request for request in problem.requests}
    request = requests[request_id]
    candidates: list[tuple[tuple[Any, ...], str, list[str]]] = []
    for tech_id in sorted(routes):
        if perf_counter() >= deadline:
            return False
        if tech_id == source_id or not compatible(request, techs[tech_id]):
            continue
        route = routes[tech_id]
        old_items = [requests[item] for item in route]
        old_sim, _failure = simulate_route(old_items, techs[tech_id], problem.travel)
        old_travel = old_sim.travel_minutes if old_sim is not None else 0
        old_distance = old_sim.distance_km if old_sim is not None else 0.0
        for position in range(len(route) + 1):
            trial = route[:position] + [request_id] + route[position:]
            items = [requests[item] for item in trial]
            simulated, _failure = simulate_route(items, techs[tech_id], problem.travel)
            if simulated is None:
                continue
            key = (
                not bool(route),
                simulated.travel_minutes - old_travel,
                round(simulated.distance_km - old_distance, 6),
                simulated.finish_at,
                tech_id,
                position,
            )
            candidates.append((key, tech_id, trial))
    if not candidates:
        return False
    _key, technician_id, best_route = min(candidates, key=lambda item: item[0])
    routes[technician_id] = best_route
    return True


def _render(
    problem: PlanningProblem,
    routes: dict[str, list[str]],
    unassigned: list[str],
    started: float,
    reasons: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    techs = {tech.id: tech for tech in problem.technicians}
    requests = {request.id: request for request in problem.requests}
    rendered = []
    assigned_count = 0
    total_distance = 0.0
    total_travel = 0
    total_service = 0
    total_wait = 0
    for tech_id in sorted(routes):
        sequence = routes[tech_id]
        if not sequence:
            continue
        tech = techs[tech_id]
        items = [requests[request_id] for request_id in sequence]
        simulated, failure = simulate_route(items, tech, problem.travel)
        if simulated is None:
            raise RuntimeError(f"local search produced an infeasible route: {failure}")
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
                            else "LOCAL_SEARCH"
                        ),
                        "message": (
                            "Назначено диспетчером; ограничения соблюдены."
                            if request.locked_technician_id
                            else "Подходит по навыкам, оснащению и времени; порядок улучшен локальным поиском."
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

    unassigned_rows = []
    for request_id in sorted(unassigned):
        if request_id in reasons:
            unassigned_rows.append(reasons[request_id])
        else:
            unassigned_rows.append(
                GreedyPlanner._unassigned(requests[request_id], "NO_FEASIBLE_TIME_SLOT")
            )
    return {
        "schema_version": "1.0",
        "plan_id": problem.plan_id,
        "algorithm": {
            "id": LocalSearchPlanner.algorithm_id,
            "version": LocalSearchPlanner.algorithm_version,
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
        "diagnostics": {
            "runtime_ms": round((perf_counter() - started) * 1000, 3),
            "warnings": [],
        },
    }
