"""Exact reassignment of complete routes between technicians."""

from __future__ import annotations

from backend.math_core.models import PlanningProblem
from backend.math_core.simulate import compatible, simulate_route

INFEASIBLE = 10**30


def polish_route_assignment(
    problem: PlanningProblem,
    routes: dict[str, list[str]],
) -> dict[str, list[str]] | None:
    """Return a better assignment candidate, or ``None`` when the gate is closed.

    Route sequences are kept intact.  The caller remains responsible for a full
    lexicographic evaluation and for publishing only a strict improvement.
    """

    active = [
        (tech_id, list(sequence))
        for tech_id, sequence in routes.items()
        if sequence
    ]
    technicians = list(problem.technicians)
    if len(active) < 2 or len(technicians) < 2:
        return None

    tech_index = {tech.id: index for index, tech in enumerate(technicians)}
    requests = {request.id: request for request in problem.requests}
    feasible = [[False] * len(technicians) for _ in active]
    travel = [[0] * len(technicians) for _ in active]
    distance_m = [[0] * len(technicians) for _ in active]
    edges: set[tuple[str, str]] = set()
    has_nonworse_alternative = False

    for row, (owner, sequence) in enumerate(active):
        items = [requests[request_id] for request_id in sequence]
        owner_column = tech_index[owner]
        owner_cost: tuple[int, int] | None = None
        for column, technician in enumerate(technicians):
            if any(not compatible(request, technician) for request in items):
                continue
            simulated, _failure = simulate_route(items, technician, problem.travel)
            if simulated is None:
                continue
            feasible[row][column] = True
            travel[row][column] = simulated.travel_minutes
            distance_m[row][column] = int(round(simulated.distance_km * 1000))
            if column == owner_column:
                owner_cost = (travel[row][column], distance_m[row][column])

        # The incumbent route must remain available in the assignment matrix.
        if owner_cost is None:
            return None
        for column, technician in enumerate(technicians):
            if column == owner_column or not feasible[row][column]:
                continue
            edges.add((owner, technician.id))
            alternative = (travel[row][column], distance_m[row][column])
            if alternative <= owner_cost:
                has_nonworse_alternative = True

    if not has_nonworse_alternative or not _has_cycle(edges):
        return None

    # Travel minutes dominate total distance exactly.  The scale is larger than
    # any possible sum of finite per-route distances in this matrix.
    distance_bound = sum(
        max(
            distance_m[row][column]
            for column in range(len(technicians))
            if feasible[row][column]
        )
        for row in range(len(active))
    )
    distance_scale = distance_bound + 1
    costs = [
        [
            travel[row][column] * distance_scale + distance_m[row][column]
            if feasible[row][column]
            else INFEASIBLE
            for column in range(len(technicians))
        ]
        for row in range(len(active))
    ]
    assignment = _minimum_cost_assignment(costs)
    if assignment is None:
        return None

    candidate = {technician.id: [] for technician in technicians}
    for row, column in enumerate(assignment):
        if not feasible[row][column]:
            return None
        candidate[technicians[column].id] = list(active[row][1])
    return candidate


def _has_cycle(edges: set[tuple[str, str]]) -> bool:
    graph: dict[str, set[str]] = {}
    for source, destination in edges:
        graph.setdefault(source, set()).add(destination)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        visiting.add(node)
        for destination in graph.get(node, ()):
            if destination in visiting:
                return True
            if destination not in visited and visit(destination):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph if node not in visited)


def _minimum_cost_assignment(costs: list[list[int]]) -> list[int] | None:
    """Solve a rectangular min-cost assignment for rows <= columns.

    This is the shortest augmenting-path form of the Hungarian algorithm.
    The result contains the selected column for every row.
    """

    row_count = len(costs)
    column_count = len(costs[0]) if costs else 0
    if row_count == 0:
        return []
    if row_count > column_count or any(len(row) != column_count for row in costs):
        return None

    row_potential = [0] * (row_count + 1)
    column_potential = [0] * (column_count + 1)
    matching = [0] * (column_count + 1)
    predecessor = [0] * (column_count + 1)

    for row in range(1, row_count + 1):
        matching[0] = row
        current_column = 0
        minimum = [INFEASIBLE] * (column_count + 1)
        used = [False] * (column_count + 1)
        while True:
            used[current_column] = True
            current_row = matching[current_column]
            delta = INFEASIBLE
            next_column = 0
            for column in range(1, column_count + 1):
                if used[column]:
                    continue
                reduced = (
                    costs[current_row - 1][column - 1]
                    - row_potential[current_row]
                    - column_potential[column]
                )
                if reduced < minimum[column]:
                    minimum[column] = reduced
                    predecessor[column] = current_column
                if minimum[column] < delta:
                    delta = minimum[column]
                    next_column = column
            if delta >= INFEASIBLE:
                return None
            for column in range(column_count + 1):
                if used[column]:
                    row_potential[matching[column]] += delta
                    column_potential[column] -= delta
                else:
                    minimum[column] -= delta
            current_column = next_column
            if matching[current_column] == 0:
                break
        while True:
            previous_column = predecessor[current_column]
            matching[current_column] = matching[previous_column]
            current_column = previous_column
            if current_column == 0:
                break

    assignment = [-1] * row_count
    for column in range(1, column_count + 1):
        if matching[column] != 0:
            assignment[matching[column] - 1] = column - 1
    if any(
        column < 0 or costs[row][column] >= INFEASIBLE
        for row, column in enumerate(assignment)
    ):
        return None
    return assignment
