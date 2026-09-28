import unittest
from time import perf_counter

from backend.math_core.assignment import _minimum_cost_assignment, polish_route_assignment
from backend.math_core.local_search import (
    LocalSearchPlanner,
    _clone,
    _eliminate_routes,
    _evaluate,
    _insert_cheapest,
    _neighbors,
    _render,
)

from tests.helpers import make_problem, make_request, make_technician, make_travel


class LocalSearchPlannerTest(unittest.TestCase):
    def test_zero_budget_keeps_greedy_coverage(self) -> None:
        problem = make_problem(
            [make_request("REQ-1"), make_request("REQ-2")],
            [make_technician("TECH-1"), make_technician("TECH-2")],
        )
        result = LocalSearchPlanner(budget_s=0).plan(problem)
        self.assertEqual(result["algorithm"]["id"], "greedy-local-search")
        self.assertEqual(result["metrics"]["assigned_count"], 2)
        self.assertEqual(
            result["routes"][0]["stops"][0]["explanation"]["code"], "LOCAL_SEARCH"
        )
        self.assertEqual(result["diagnostics"]["warnings"], [])

    def test_first_improvement_and_operator_subset_stay_feasible(self) -> None:
        problem = make_problem(
            [
                make_request("REQ-1", location_id="A"),
                make_request("REQ-2", location_id="B"),
                make_request("REQ-3", location_id="C"),
            ],
            [make_technician()],
            make_travel(
                {
                    ("OFFICE", "A"): (5, 1.0),
                    ("OFFICE", "B"): (5, 1.0),
                    ("OFFICE", "C"): (5, 1.0),
                    ("A", "B"): (40, 8.0),
                    ("A", "C"): (5, 1.0),
                    ("B", "C"): (40, 8.0),
                }
            ),
        )
        first = LocalSearchPlanner(
            budget_s=0.5, strategy="first", operators=("two_opt", "relocate")
        ).plan(problem)
        best = LocalSearchPlanner(budget_s=0.5, strategy="best").plan(problem)
        for result in (first, best):
            assigned = [
                stop["request_id"] for route in result["routes"] for stop in route["stops"]
            ]
            self.assertEqual(sorted(assigned), ["REQ-1", "REQ-2", "REQ-3"])
            self.assertEqual(result["metrics"]["used_technicians"], 1)

    def test_assignment_polish_reassigns_complete_routes(self) -> None:
        problem = make_problem(
            [
                make_request("REQ-A", location_id="A"),
                make_request("REQ-B", location_id="B"),
            ],
            [
                make_technician("TECH-1", start_location_id="START-1"),
                make_technician("TECH-2", start_location_id="START-2"),
            ],
            make_travel(
                {
                    ("START-1", "A"): (50, 50.0),
                    ("START-1", "B"): (5, 5.0),
                    ("START-2", "A"): (5, 5.0),
                    ("START-2", "B"): (50, 50.0),
                }
            ),
        )

        candidate = polish_route_assignment(
            problem,
            {"TECH-1": ["REQ-A"], "TECH-2": ["REQ-B"]},
        )

        self.assertEqual(candidate, {"TECH-1": ["REQ-B"], "TECH-2": ["REQ-A"]})

    def test_assignment_solver_avoids_greedy_trap(self) -> None:
        self.assertEqual(_minimum_cost_assignment([[1, 2], [2, 100]]), [1, 0])


class EvaluateTest(unittest.TestCase):
    def test_rebuilds_leftover_and_rejects_duplicates(self) -> None:
        problem = make_problem(
            [make_request("REQ-1"), make_request("REQ-2")],
            [make_technician("TECH-1"), make_technician("TECH-2")],
        )
        empty = _evaluate(problem, {"TECH-1": [], "TECH-2": []}, ["ignored"])
        self.assertEqual(empty[2], ["REQ-1", "REQ-2"])
        self.assertEqual(empty[0][3], 0)

        cache = {}
        assigned = _evaluate(
            problem,
            {"TECH-1": ["REQ-1"], "TECH-2": []},
            [],
            cache=cache,
            update_cache=True,
        )
        self.assertEqual(assigned[2], ["REQ-2"])
        self.assertEqual(cache["TECH-2"], ((), None))

        self.assertIsNone(
            _evaluate(problem, {"TECH-1": ["REQ-1"], "TECH-2": ["REQ-1"]}, [])
        )
        incompatible = make_problem(
            [make_request("REQ-1", required_skills=frozenset({"satellite"}))],
            [make_technician()],
        )
        self.assertIsNone(_evaluate(incompatible, {"TECH-1": ["REQ-1"]}, []))

    def test_clone_is_a_shallow_copy_of_sequences(self) -> None:
        routes = {"TECH-1": ["REQ-1"]}
        cloned = _clone(routes)
        cloned["TECH-1"].append("REQ-2")
        self.assertEqual(routes["TECH-1"], ["REQ-1"])


class NeighborTest(unittest.TestCase):
    def _problem(self):
        return make_problem(
            [
                make_request("REQ-1"),
                make_request("REQ-2"),
                make_request("REQ-3"),
                make_request("REQ-4", locked_technician_id="TECH-1"),
            ],
            [make_technician("TECH-1"), make_technician("TECH-2")],
        )

    def test_reinsert_skips_incompatible_and_yields_positions(self) -> None:
        problem = make_problem(
            [
                make_request("REQ-1"),
                make_request("REQ-2", required_skills=frozenset({"satellite"})),
            ],
            [make_technician("TECH-1"), make_technician("TECH-2")],
        )
        trials = list(
            _neighbors(
                problem,
                {"TECH-1": ["REQ-1"], "TECH-2": []},
                ["REQ-2"],
                "reinsert",
                perf_counter() + 1,
            )
        )
        self.assertEqual(trials, [])
        compatible = make_problem(
            [make_request("REQ-1"), make_request("REQ-2")],
            [make_technician("TECH-1")],
        )
        insertions = list(
            _neighbors(
                compatible,
                {"TECH-1": ["REQ-1"]},
                ["REQ-2"],
                "reinsert",
                perf_counter() + 1,
            )
        )
        sequences = [routes["TECH-1"] for routes, leftover, _touched in insertions]
        self.assertEqual(sequences, [["REQ-2", "REQ-1"], ["REQ-1", "REQ-2"]])
        self.assertTrue(all(leftover == [] for _routes, leftover, _touched in insertions))

    def test_relocate_respects_lock_and_skips_identity(self) -> None:
        problem = self._problem()
        routes = {"TECH-1": ["REQ-4"], "TECH-2": ["REQ-1"]}
        moves = list(_neighbors(problem, routes, [], "relocate", perf_counter() + 1))
        moved_ids = {
            (tuple(trial["TECH-1"]), tuple(trial["TECH-2"]))
            for trial, _leftover, _touched in moves
        }
        self.assertNotIn(((), ("REQ-1", "REQ-4")), moved_ids)
        self.assertIn((("REQ-1", "REQ-4"), ()), moved_ids)

    def test_swap_and_two_opt(self) -> None:
        problem = self._problem()
        swaps = list(
            _neighbors(
                problem,
                {"TECH-1": ["REQ-1"], "TECH-2": ["REQ-2"]},
                [],
                "swap",
                perf_counter() + 1,
            )
        )
        self.assertEqual(swaps[0][0]["TECH-1"], ["REQ-2"])
        self.assertEqual(swaps[0][0]["TECH-2"], ["REQ-1"])

        two_opt = list(
            _neighbors(
                problem,
                {"TECH-1": ["REQ-1", "REQ-2", "REQ-3"], "TECH-2": []},
                [],
                "two_opt",
                perf_counter() + 1,
            )
        )
        reversed_routes = [trial["TECH-1"] for trial, _leftover, _touched in two_opt]
        self.assertIn(["REQ-2", "REQ-1", "REQ-3"], reversed_routes)
        self.assertIn(["REQ-3", "REQ-2", "REQ-1"], reversed_routes)
        self.assertIn(["REQ-1", "REQ-3", "REQ-2"], reversed_routes)
        self.assertEqual(
            list(
                _neighbors(
                    problem, {"TECH-1": ["REQ-1"], "TECH-2": []}, [], "unknown", 0
                )
            ),
            [],
        )

    def test_neighbors_stop_when_deadline_passed(self) -> None:
        problem = self._problem()
        self.assertEqual(
            list(
                _neighbors(
                    problem,
                    {"TECH-1": ["REQ-1"], "TECH-2": []},
                    ["REQ-2"],
                    "reinsert",
                    0,
                )
            ),
            [],
        )


class RouteEliminationTest(unittest.TestCase):
    def test_skips_locked_source_and_insert_cheapest_can_fail(self) -> None:
        problem = make_problem(
            [
                make_request("REQ-1", locked_technician_id="TECH-1"),
                make_request("REQ-2", required_skills=frozenset({"emergency"})),
                make_request("REQ-3", required_skills=frozenset({"emergency"})),
            ],
            [
                make_technician("TECH-1"),
                make_technician("TECH-2", skills=frozenset({"emergency"})),
                make_technician("TECH-3", skills=frozenset({"emergency"})),
            ],
        )
        cache = {}
        current = _evaluate(
            problem,
            {"TECH-1": ["REQ-1"], "TECH-2": ["REQ-2"], "TECH-3": ["REQ-3"]},
            [],
            cache=cache,
            update_cache=True,
        )
        improved = _eliminate_routes(problem, current, cache, perf_counter() + 1)
        self.assertEqual(improved[1]["TECH-1"], ["REQ-1"])
        self.assertEqual(improved[0][3], 2)
        self.assertEqual(
            sorted(request_id for route in improved[1].values() for request_id in route),
            ["REQ-1", "REQ-2", "REQ-3"],
        )
        self.assertEqual(_eliminate_routes(problem, current, cache, 0), current)

        routes = {"TECH-1": ["REQ-1"], "TECH-2": []}
        self.assertFalse(
            _insert_cheapest(
                make_problem(
                    [make_request("REQ-1"), make_request("REQ-2", location_id="client")],
                    [make_technician("TECH-1"), make_technician("TECH-2")],
                ),
                routes,
                "REQ-2",
                "TECH-1",
                perf_counter() + 1,
            )
        )


class RenderTest(unittest.TestCase):
    def test_keeps_greedy_unassigned_reasons_and_manual_code(self) -> None:
        problem = make_problem(
            [
                make_request("REQ-1", locked_technician_id="TECH-1"),
                make_request("REQ-2", required_skills=frozenset({"satellite"})),
            ],
            [make_technician()],
        )
        reasons = {
            "REQ-2": {
                "request_id": "REQ-2",
                "reason": {
                    "code": "NO_SKILLED_TECHNICIAN",
                    "message": "kept",
                    "details": {},
                },
            }
        }
        result = _render(problem, {"TECH-1": ["REQ-1"]}, ["REQ-2"], perf_counter(), reasons)
        self.assertEqual(
            result["unassigned"][0]["reason"]["code"], "NO_SKILLED_TECHNICIAN"
        )
        self.assertEqual(
            result["routes"][0]["stops"][0]["explanation"]["code"], "MANUAL_ASSIGNMENT"
        )

    def test_fallback_reason_when_local_search_drops_a_request(self) -> None:
        problem = make_problem(
            [make_request("REQ-1"), make_request("REQ-2")],
            [make_technician()],
        )
        result = _render(problem, {"TECH-1": ["REQ-1"]}, ["REQ-2"], perf_counter(), {})
        self.assertEqual(
            result["unassigned"][0]["reason"]["code"], "NO_FEASIBLE_TIME_SLOT"
        )

    def test_infeasible_final_route_raises(self) -> None:
        problem = make_problem(
            [make_request("REQ-1", location_id="client")],
            [make_technician()],
        )
        with self.assertRaises(RuntimeError):
            _render(problem, {"TECH-1": ["REQ-1"]}, [], perf_counter(), {})


if __name__ == "__main__":
    unittest.main()
