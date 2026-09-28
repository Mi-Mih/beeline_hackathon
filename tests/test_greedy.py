import unittest

from backend.math_core.greedy import GreedyPlanner
from backend.math_core.models import TimeWindow

from tests.helpers import at, make_problem, make_request, make_technician, make_travel


def unassigned_codes(result: dict) -> dict[str, str]:
    return {row["request_id"]: row["reason"]["code"] for row in result["unassigned"]}


class GreedyPlannerTest(unittest.TestCase):
    def test_empty_requests_return_zero_metrics(self) -> None:
        result = GreedyPlanner().plan(make_problem([], [make_technician()]))
        self.assertEqual(result["algorithm"]["id"], "greedy-cheapest-insertion")
        self.assertEqual(result["routes"], [])
        self.assertEqual(result["unassigned"], [])
        self.assertEqual(
            result["metrics"],
            {
                "request_count": 0,
                "assigned_count": 0,
                "unassigned_count": 0,
                "used_technicians": 0,
                "distance_km": 0.0,
                "travel_minutes": 0,
                "service_minutes": 0,
                "wait_minutes": 0,
            },
        )

    def test_prefers_urgent_when_only_one_job_fits(self) -> None:
        tech = make_technician(shift_end=at(11))
        urgent = make_request(
            "REQ-U",
            duration_minutes=90,
            window=TimeWindow(at(9), at(11)),
            priority="urgent",
        )
        normal = make_request(
            "REQ-N",
            duration_minutes=90,
            window=TimeWindow(at(9), at(11)),
            priority="normal",
        )
        result = GreedyPlanner().plan(make_problem([normal, urgent], [tech]))
        self.assertEqual(result["metrics"]["assigned_count"], 1)
        self.assertEqual(result["routes"][0]["stops"][0]["request_id"], "REQ-U")
        self.assertEqual(unassigned_codes(result), {"REQ-N": "NO_FEASIBLE_TIME_SLOT"})
        self.assertEqual(
            result["routes"][0]["stops"][0]["explanation"]["code"],
            "FEASIBLE_GREEDY_INSERTION",
        )

    def test_packs_onto_an_open_route_before_opening_another(self) -> None:
        requests = [
            make_request("REQ-1", duration_minutes=20),
            make_request("REQ-2", duration_minutes=20),
        ]
        techs = [make_technician("TECH-1"), make_technician("TECH-2")]
        result = GreedyPlanner().plan(make_problem(requests, techs))
        self.assertEqual(result["metrics"]["used_technicians"], 1)
        self.assertEqual(result["metrics"]["assigned_count"], 2)
        self.assertEqual(result["routes"][0]["technician_id"], "TECH-1")

    def test_unassigned_reason_codes(self) -> None:
        cases = {
            "NO_SKILLED_TECHNICIAN": make_problem(
                [make_request(required_skills=frozenset({"satellite"}))],
                [make_technician()],
            ),
            "NO_REQUIRED_VEHICLE": make_problem(
                [make_request(required_vehicle="van")],
                [make_technician(vehicle="car")],
            ),
            "NO_REQUIRED_EQUIPMENT": make_problem(
                [make_request(required_equipment=frozenset({"router"}))],
                [make_technician(equipment=frozenset({"ladder"}))],
            ),
            "LOCKED_TECHNICIAN_UNAVAILABLE": make_problem(
                [make_request(locked_technician_id="TECH-2")],
                [
                    make_technician("TECH-1"),
                    make_technician(
                        "TECH-2", skills=frozenset({"emergency"}), vehicle="car"
                    ),
                ],
            ),
            "TRAVEL_DATA_MISSING": make_problem(
                [make_request(location_id="client")],
                [make_technician()],
            ),
            "NO_FEASIBLE_TIME_SLOT": make_problem(
                [
                    make_request(
                        duration_minutes=120,
                        window=TimeWindow(at(9), at(10)),
                    )
                ],
                [make_technician(shift_end=at(10))],
            ),
        }
        planner = GreedyPlanner()
        for code, problem in cases.items():
            with self.subTest(code):
                result = planner.plan(problem)
                self.assertEqual(unassigned_codes(result), {"REQ-1": code})
                details = result["unassigned"][0]["reason"]["details"]
                request = problem.requests[0]
                self.assertEqual(details["required_skills"], sorted(request.required_skills))
                self.assertEqual(details["required_vehicle"], request.required_vehicle)
                self.assertEqual(
                    details["required_equipment"], sorted(request.required_equipment)
                )
                self.assertEqual(
                    details["locked_technician_id"], request.locked_technician_id
                )

    def test_mixed_simulate_failures_are_time_slot_not_missing_travel(self) -> None:
        request = make_request(location_id="client", window=TimeWindow(at(9), at(10)))
        problem = make_problem(
            [request],
            [
                make_technician("TECH-1", start_location_id="YARD"),
                make_technician("TECH-2", available_from=at(12), shift_end=at(18)),
            ],
            make_travel({("OFFICE", "client"): (10, 1.0)}),
        )
        result = GreedyPlanner().plan(problem)
        self.assertEqual(unassigned_codes(result), {"REQ-1": "NO_FEASIBLE_TIME_SLOT"})

    def test_manual_assignment_explanation(self) -> None:
        request = make_request(locked_technician_id="TECH-1")
        result = GreedyPlanner().plan(make_problem([request], [make_technician()]))
        self.assertEqual(
            result["routes"][0]["stops"][0]["explanation"]["code"],
            "MANUAL_ASSIGNMENT",
        )

    def test_route_metrics_include_wait_and_service(self) -> None:
        request = make_request(
            location_id="client",
            duration_minutes=40,
            window=TimeWindow(at(11), at(18)),
        )
        result = GreedyPlanner().plan(
            make_problem(
                [request],
                [make_technician()],
                make_travel({("OFFICE", "client"): (15, 2.25)}),
            )
        )
        metrics = result["routes"][0]["metrics"]
        self.assertEqual(metrics["service_minutes"], 40)
        self.assertEqual(metrics["travel_minutes"], 15)
        self.assertEqual(metrics["wait_minutes"], 105)
        self.assertEqual(metrics["distance_km"], 2.25)
        self.assertEqual(result["metrics"]["service_minutes"], 40)


if __name__ == "__main__":
    unittest.main()
