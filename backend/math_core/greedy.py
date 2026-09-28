"""Cheapest feasible insertion for the technician CVRPTW-style model."""

from __future__ import annotations

from datetime import datetime
from time import perf_counter
from typing import Any

from backend.math_core.models import (
    SCHEMA_VERSION,
    PlanningProblem,
    Request,
)
from backend.math_core.simulate import compatible as technician_fits, simulate_route


class GreedyPlanner:
    """Cheapest feasible insertion, constrained jobs first.

    This is a baseline, not an optimal solver. It uses active technicians before
    opening a new route, which approximates the MVP objective of using fewer
    people and then reducing travel.
    """

    algorithm_id = "greedy-cheapest-insertion"
    algorithm_version = "1.1"

    def plan(self, problem: PlanningProblem) -> dict[str, Any]:
        started = perf_counter()
        routes: dict[str, list[Request]] = {tech.id: [] for tech in problem.technicians}
        unassigned: list[dict[str, Any]] = []
        tech_by_id = {tech.id: tech for tech in problem.technicians}

        def compatible_count(request: Request) -> int:
            return sum(technician_fits(request, tech) for tech in problem.technicians)

        ordered = sorted(
            problem.requests,
            key=lambda request: (
                {"urgent": 0, "high": 1, "normal": 2}[request.priority],
                compatible_count(request),
                request.window.end,
                request.window.start,
                -request.duration_minutes,
                request.id,
            ),
        )

        for request in ordered:
            skilled = [
                tech
                for tech in problem.technicians
                if request.required_skills <= tech.skills
            ]
            if not skilled:
                unassigned.append(self._unassigned(request, "NO_SKILLED_TECHNICIAN"))
                continue

            compatible = [
                tech
                for tech in skilled
                if request.required_vehicle is None
                or request.required_vehicle == tech.vehicle
            ]
            if not compatible:
                unassigned.append(self._unassigned(request, "NO_REQUIRED_VEHICLE"))
                continue

            equipped = [
                tech
                for tech in compatible
                if request.required_equipment <= tech.equipment
            ]
            if not equipped:
                unassigned.append(self._unassigned(request, "NO_REQUIRED_EQUIPMENT"))
                continue

            available = [
                tech
                for tech in equipped
                if request.locked_technician_id is None
                or request.locked_technician_id == tech.id
            ]
            if not available:
                unassigned.append(
                    self._unassigned(request, "LOCKED_TECHNICIAN_UNAVAILABLE")
                )
                continue

            candidates: list[tuple[tuple[Any, ...], str, list[Request]]] = []
            failures: list[str] = []
            for tech in available:
                old_route = routes[tech.id]
                old_sim, _ = simulate_route(old_route, tech, problem.travel)
                old_distance = old_sim.distance_km if old_sim else 0.0
                old_travel = old_sim.travel_minutes if old_sim else 0
                for position in range(len(old_route) + 1):
                    trial = old_route[:position] + [request] + old_route[position:]
                    simulation, failure = simulate_route(trial, tech, problem.travel)
                    if simulation is None:
                        failures.append(failure)
                        continue
                    request_start = next(
                        stop.service_start
                        for stop in simulation.stops
                        if stop.request.id == request.id
                    )
                    key = (
                        request_start
                        if request.priority in {"urgent", "high"}
                        else datetime.max.replace(tzinfo=request_start.tzinfo),
                        not bool(old_route),
                        round(simulation.distance_km - old_distance, 6),
                        simulation.travel_minutes - old_travel,
                        simulation.finish_at,
                        tech.id,
                        position,
                    )
                    candidates.append((key, tech.id, trial))

            if not candidates:
                code = (
                    "TRAVEL_DATA_MISSING"
                    if failures and set(failures) == {"TRAVEL_DATA_MISSING"}
                    else "NO_FEASIBLE_TIME_SLOT"
                )
                unassigned.append(self._unassigned(request, code))
                continue

            _, technician_id, best_route = min(candidates, key=lambda item: item[0])
            routes[technician_id] = best_route

        rendered_routes: list[dict[str, Any]] = []
        assigned_count = 0
        total_distance = 0.0
        total_travel = 0
        total_service = 0
        total_wait = 0

        for technician_id in sorted(routes):
            route = routes[technician_id]
            if not route:
                continue
            tech = tech_by_id[technician_id]
            simulation, failure = simulate_route(route, tech, problem.travel)
            if simulation is None:  # pragma: no cover - internal invariant
                raise RuntimeError(f"Final route became invalid: {failure}")

            stops = []
            for sequence, stop in enumerate(simulation.stops, start=1):
                stops.append(
                    {
                        "sequence": sequence,
                        "request_id": stop.request.id,
                        "location_id": stop.request.location_id,
                        "arrival_at": stop.arrival.isoformat(),
                        "service_start_at": stop.service_start.isoformat(),
                        "service_end_at": stop.service_end.isoformat(),
                        "travel_minutes": stop.travel_minutes,
                        "distance_km": round(stop.distance_km, 3),
                        "wait_minutes": stop.wait_minutes,
                        "explanation": {
                            "code": (
                                "MANUAL_ASSIGNMENT"
                                if stop.request.locked_technician_id
                                else "FEASIBLE_GREEDY_INSERTION"
                            ),
                            "message": (
                                "Назначено диспетчером; ограничения соблюдены."
                                if stop.request.locked_technician_id
                                else "Подходит по навыкам, оснащению и времени."
                            ),
                        },
                    }
                )

            service_minutes = sum(item.duration_minutes for item in route)
            rendered_routes.append(
                {
                    "technician_id": tech.id,
                    "technician_name": tech.name,
                    "start_location_id": tech.start_location_id,
                    "finish_at": simulation.finish_at.isoformat(),
                    "stops": stops,
                    "metrics": {
                        "request_count": len(route),
                        "distance_km": round(simulation.distance_km, 3),
                        "travel_minutes": simulation.travel_minutes,
                        "service_minutes": service_minutes,
                        "wait_minutes": simulation.wait_minutes,
                    },
                }
            )
            assigned_count += len(route)
            total_distance += simulation.distance_km
            total_travel += simulation.travel_minutes
            total_service += service_minutes
            total_wait += simulation.wait_minutes

        return {
            "schema_version": SCHEMA_VERSION,
            "plan_id": problem.plan_id,
            "algorithm": {
                "id": self.algorithm_id,
                "version": self.algorithm_version,
            },
            "routes": rendered_routes,
            "unassigned": sorted(unassigned, key=lambda item: item["request_id"]),
            "metrics": {
                "request_count": len(problem.requests),
                "assigned_count": assigned_count,
                "unassigned_count": len(unassigned),
                "used_technicians": len(rendered_routes),
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

    @staticmethod
    def _unassigned(request: Request, code: str) -> dict[str, Any]:
        messages = {
            "NO_SKILLED_TECHNICIAN": "Нет инженера со всеми требуемыми навыками.",
            "NO_REQUIRED_VEHICLE": "Нет подходящего инженера с требуемым транспортом.",
            "NO_REQUIRED_EQUIPMENT": "Нет инженера со всем нужным оборудованием.",
            "LOCKED_TECHNICIAN_UNAVAILABLE": "Выбранный диспетчером инженер не подходит для заявки.",
            "NO_FEASIBLE_TIME_SLOT": "Заявка не помещается в доступные маршруты, окна и смены.",
            "TRAVEL_DATA_MISSING": "Не хватает данных о времени в пути до заявки.",
        }
        return {
            "request_id": request.id,
            "reason": {
                "code": code,
                "message": messages[code],
                "details": {
                    "required_skills": sorted(request.required_skills),
                    "required_vehicle": request.required_vehicle,
                    "required_equipment": sorted(request.required_equipment),
                    "locked_technician_id": request.locked_technician_id,
                },
            },
        }
