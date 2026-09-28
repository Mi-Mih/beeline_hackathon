import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

from backend.math_core import (
    ContractError,
    GreedyPlanner,
    LocalSearchPlanner,
    RoutingPlanner,
    parse_problem,
    planner_for,
)
from backend.math_core.local_search import _eliminate_routes, _evaluate
from backend.math_core.models import PlanningProblem, Request, Technician, TimeWindow, TravelTable


ROOT = Path(__file__).resolve().parents[1]


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(
            (ROOT / "examples" / "sample-input.json").read_text(encoding="utf-8")
        )

    def test_builds_feasible_routes_and_explains_unassigned(self):
        result = GreedyPlanner().plan(parse_problem(self.payload))

        self.assertEqual(
            set(result),
            {"schema_version", "plan_id", "algorithm", "routes", "unassigned", "metrics", "diagnostics"},
        )
        self.assertEqual(result["schema_version"], "1.0")
        self.assertEqual(result["metrics"]["request_count"], 4)
        self.assertEqual(result["metrics"]["assigned_count"], 3)
        self.assertEqual(result["metrics"]["used_technicians"], 2)
        self.assertEqual(
            result["unassigned"][0]["reason"]["code"], "NO_SKILLED_TECHNICIAN"
        )

        request_by_id = {row["id"]: row for row in self.payload["requests"]}
        assigned = set()
        for route in result["routes"]:
            for expected_sequence, stop in enumerate(route["stops"], start=1):
                self.assertEqual(stop["sequence"], expected_sequence)
                self.assertNotIn(stop["request_id"], assigned)
                assigned.add(stop["request_id"])
                service_start = stop["service_start_at"]
                window = request_by_id[stop["request_id"]]["window"]
                self.assertGreaterEqual(service_start, window["start"])
                self.assertLessEqual(service_start, window["end"])
        self.assertEqual(assigned, {"REQ-1", "REQ-2", "REQ-3"})

    def test_local_search_keeps_coverage_and_is_not_worse(self):
        problem = parse_problem(self.payload)
        greedy = GreedyPlanner().plan(problem)
        polished = LocalSearchPlanner(budget_s=0.2).plan(problem)
        self.assertEqual(polished["algorithm"]["id"], "greedy-local-search")
        self.assertEqual(
            polished["metrics"]["assigned_count"], greedy["metrics"]["assigned_count"]
        )
        self.assertLessEqual(
            polished["metrics"]["used_technicians"], greedy["metrics"]["used_technicians"]
        )
        if polished["metrics"]["used_technicians"] == greedy["metrics"]["used_technicians"]:
            self.assertLessEqual(
                polished["metrics"]["travel_minutes"], greedy["metrics"]["travel_minutes"]
            )

    def test_delta_evaluation_matches_full_evaluation(self):
        problem = self._route_elimination_problem()
        routes = {"TECH-1": ["REQ-1"], "TECH-2": ["REQ-2"], "TECH-3": ["REQ-3"]}
        cache = {}
        self.assertIsNotNone(
            _evaluate(problem, routes, [], cache=cache, update_cache=True)
        )
        trial = {"TECH-1": ["REQ-1", "REQ-3"], "TECH-2": ["REQ-2"], "TECH-3": []}

        full = _evaluate(problem, trial, [])
        delta = _evaluate(problem, trial, [], cache=cache, touched={"TECH-1", "TECH-3"})

        self.assertEqual(delta, full)

    def test_route_elimination_is_atomic_and_reduces_used_technicians(self):
        problem = self._route_elimination_problem()
        routes = {"TECH-1": ["REQ-1"], "TECH-2": ["REQ-2"], "TECH-3": ["REQ-3"]}
        cache = {}
        current = _evaluate(problem, routes, [], cache=cache, update_cache=True)
        self.assertIsNotNone(current)

        improved = _eliminate_routes(problem, current, cache, perf_counter() + 1.0)

        self.assertEqual(improved[0][:3], (0, 0, 0))
        self.assertEqual(improved[0][3], 1)
        self.assertEqual(
            sorted(request_id for route in improved[1].values() for request_id in route),
            ["REQ-1", "REQ-2", "REQ-3"],
        )

    def test_rejects_unknown_location_at_contract_boundary(self):
        self.payload["requests"][0]["location_id"] = "missing"
        with self.assertRaises(ContractError):
            parse_problem(self.payload)

    def test_manual_assignment_and_mode_specific_travel(self):
        request = self.payload["requests"][0]
        request["required_vehicle"] = None
        request["required_equipment"] = []
        request["locked_technician_id"] = "TECH-2"
        self.payload["travel"]["legs"].append(
            {
                "from": "client-a",
                "to": "client-b",
                "mode": "pedestrian",
                "minutes": 7,
                "distance_km": 1.2,
            }
        )

        result = GreedyPlanner().plan(parse_problem(self.payload))

        route = next(row for row in result["routes"] if row["technician_id"] == "TECH-2")
        stop = next(row for row in route["stops"] if row["request_id"] == "REQ-1")
        self.assertEqual(stop["explanation"]["code"], "MANUAL_ASSIGNMENT")
        self.assertEqual(stop["travel_minutes"], 7)

    def test_rejects_unknown_manual_technician(self):
        self.payload["requests"][0]["locked_technician_id"] = "missing"
        with self.assertRaises(ContractError):
            parse_problem(self.payload)

    def test_overnight_and_replan_use_routing(self):
        overnight = parse_problem(self.payload)
        self.assertEqual(overnight.planning_reason, "overnight")
        self.assertIsInstance(planner_for(overnight.planning_reason), RoutingPlanner)

        self.payload["planning_reason"] = "replan"
        replan = parse_problem(self.payload)
        self.assertIsInstance(planner_for(replan.planning_reason), RoutingPlanner)

        self.payload["planning_reason"] = "midshift"
        with self.assertRaises(ContractError):
            parse_problem(self.payload)

    def test_replan_keeps_sample_coverage(self):
        self.payload["planning_reason"] = "replan"
        result = planner_for("replan").plan(parse_problem(self.payload))
        self.assertEqual(result["metrics"]["assigned_count"], 3)
        self.assertEqual(result["metrics"]["unassigned_count"], 1)
        if result["algorithm"]["id"] == "ortools-routing":
            self.assertEqual(result["diagnostics"]["warnings"], [])
        else:
            self.assertEqual(result["algorithm"]["id"], "greedy-local-search")
            self.assertTrue(result["diagnostics"]["warnings"])

    @staticmethod
    def _route_elimination_problem():
        tz = timezone(timedelta(hours=3))
        start = datetime(2026, 8, 17, 9, 0, tzinfo=tz)
        end = datetime(2026, 8, 17, 18, 0, tzinfo=tz)
        requests = tuple(
            Request(
                id=f"REQ-{index}",
                location_id="OFFICE",
                duration_minutes=30,
                window=TimeWindow(start, end),
                required_skills=frozenset({"connection"}),
                required_vehicle=None,
                required_equipment=frozenset(),
                locked_technician_id=None,
                priority="normal",
            )
            for index in range(1, 4)
        )
        technicians = tuple(
            Technician(
                id=f"TECH-{index}",
                name=f"Technician {index}",
                start_location_id="OFFICE",
                available_from=start,
                shift_end=end,
                skills=frozenset({"connection"}),
                vehicle="car",
                equipment=frozenset(),
            )
            for index in range(1, 4)
        )
        return PlanningProblem(
            plan_id="route-elimination-test",
            requests=requests,
            technicians=technicians,
            travel=TravelTable({}, symmetric=True),
        )


if __name__ == "__main__":
    unittest.main()
