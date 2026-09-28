import sys
import unittest
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

from backend.math_core.contract import parse_problem
from backend.math_core.greedy import GreedyPlanner
from backend.math_core.routing import (
    RoutingPlanner,
    VEHICLE_FIXED,
    _infeasible,
    _minutes,
    _penalty_table,
    _render,
    _solve,
    _status_name,
    _with_runtime,
)

from tests.helpers import at, make_problem, make_request, make_technician, sample_payload


class RoutingPlannerTest(unittest.TestCase):
    def test_falls_back_when_ortools_is_missing(self) -> None:
        problem = parse_problem(sample_payload())
        with patch.dict(sys.modules, {"ortools": None, "ortools.constraint_solver": None}):
            result = RoutingPlanner(time_limit_s=1).plan(problem)
        self.assertEqual(result["algorithm"]["id"], "greedy-local-search")
        self.assertTrue(
            any(
                "ortools is not installed" in warning
                for warning in result["diagnostics"]["warnings"]
            )
        )
        self.assertGreaterEqual(result["diagnostics"]["runtime_ms"], 0)

    def test_falls_back_when_solver_reports_infeasible(self) -> None:
        problem = make_problem([make_request()], [make_technician()])
        fake_solver = SimpleNamespace(
            pywrapcp=SimpleNamespace(),
            routing_enums_pb2=SimpleNamespace(),
        )
        with patch.dict(
            sys.modules,
            {
                "ortools": SimpleNamespace(constraint_solver=fake_solver),
                "ortools.constraint_solver": fake_solver,
            },
        ):
            with patch(
                "backend.math_core.routing._solve",
                return_value={"status": "FAIL", "fallback": True, "plan": None},
            ):
                result = RoutingPlanner(time_limit_s=1).plan(problem)
        self.assertEqual(result["algorithm"]["id"], "greedy-local-search")
        self.assertTrue(
            any("routing status=FAIL" in warning for warning in result["diagnostics"]["warnings"])
        )

    def test_empty_requests_do_not_crash(self) -> None:
        result = RoutingPlanner(time_limit_s=1).plan(
            make_problem([], [make_technician()])
        )
        self.assertEqual(result["metrics"]["assigned_count"], 0)
        self.assertEqual(result["algorithm"]["id"], "ortools-routing")

    def test_sample_coverage_matches_greedy_when_solver_runs(self) -> None:
        problem = parse_problem(sample_payload())
        result = RoutingPlanner(time_limit_s=1).plan(problem)
        self.assertEqual(result["metrics"]["assigned_count"], 3)
        self.assertEqual(result["metrics"]["unassigned_count"], 1)

    def test_solve_skips_model_when_there_are_no_requests(self) -> None:
        problem = make_problem([], [make_technician()])
        greedy = GreedyPlanner().plan(problem)
        built = _solve(problem, greedy, 1, SimpleNamespace(), SimpleNamespace())
        self.assertEqual(built["status"], "trivial")
        self.assertFalse(built["fallback"])
        self.assertIs(built["plan"], greedy)


class RoutingHelpersTest(unittest.TestCase):
    def test_penalty_table_is_lexicographic(self) -> None:
        penalties = _penalty_table(3, 2)
        self.assertEqual(penalties["normal"], 2 * VEHICLE_FIXED + 1)
        self.assertGreater(penalties["high"], 3 * penalties["normal"])
        self.assertGreater(penalties["urgent"], 3 * penalties["high"])

    def test_minutes_and_status_name(self) -> None:
        self.assertEqual(_minutes(at(10, 30), at(9)), 90)
        routing = SimpleNamespace(ROUTING_SUCCESS=1, ROUTING_FAIL=2, status=lambda: 1)
        self.assertEqual(_status_name(routing), "SUCCESS")
        self.assertEqual(_status_name(SimpleNamespace(status=lambda: 99)), "99")

    def test_with_runtime_appends_warnings(self) -> None:
        started = perf_counter()
        result = _with_runtime(
            {"diagnostics": {"runtime_ms": 1, "warnings": ["keep"]}},
            started,
            ["added"],
        )
        self.assertEqual(result["diagnostics"]["warnings"], ["keep", "added"])
        self.assertGreaterEqual(result["diagnostics"]["runtime_ms"], 0)

    def test_infeasible_detects_time_and_compatibility(self) -> None:
        feasible = make_problem([make_request()], [make_technician()])
        self.assertFalse(_infeasible(feasible, {"TECH-1": ["REQ-1"]}))
        self.assertFalse(_infeasible(feasible, {"TECH-1": []}))
        missing_travel = make_problem(
            [make_request(location_id="client")],
            [make_technician()],
        )
        self.assertTrue(_infeasible(missing_travel, {"TECH-1": ["REQ-1"]}))
        mismatch = make_problem(
            [make_request(required_skills=frozenset({"satellite"}))],
            [make_technician()],
        )
        self.assertTrue(_infeasible(mismatch, {"TECH-1": ["REQ-1"]}))

    def test_render_explanations_and_unassigned(self) -> None:
        problem = make_problem(
            [
                make_request("REQ-1", locked_technician_id="TECH-1"),
                make_request("REQ-2", required_skills=frozenset({"satellite"})),
            ],
            [make_technician()],
        )
        result = _render(problem, {"TECH-1": ["REQ-1"]}, ["REQ-2"])
        self.assertEqual(result["algorithm"]["id"], "ortools-routing")
        self.assertEqual(
            result["routes"][0]["stops"][0]["explanation"]["code"],
            "MANUAL_ASSIGNMENT",
        )
        unlocked = make_problem([make_request()], [make_technician()])
        unlocked_result = _render(unlocked, {"TECH-1": ["REQ-1"]}, [])
        self.assertEqual(
            unlocked_result["routes"][0]["stops"][0]["explanation"]["code"],
            "ORTOOLS_ROUTING",
        )
        self.assertEqual(
            result["unassigned"][0]["reason"]["code"], "NO_FEASIBLE_TIME_SLOT"
        )

    def test_render_rejects_unschedulable_route(self) -> None:
        problem = make_problem(
            [make_request("REQ-1", location_id="client")],
            [make_technician()],
        )
        with self.assertRaises(RuntimeError):
            _render(problem, {"TECH-1": ["REQ-1"]}, [])


if __name__ == "__main__":
    unittest.main()
