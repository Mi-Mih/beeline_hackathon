import copy
import unittest

from backend.math_core.contract import ContractError, parse_problem

from tests.helpers import minimal_payload, sample_payload


class ParseProblemTest(unittest.TestCase):
    def test_parses_sample_and_defaults_overnight(self) -> None:
        problem = parse_problem(sample_payload())
        self.assertEqual(problem.plan_id, "demo-2026-08-17")
        self.assertEqual(problem.planning_reason, "overnight")
        self.assertEqual(len(problem.requests), 4)
        self.assertEqual(len(problem.technicians), 2)
        self.assertEqual(problem.requests[0].required_equipment, frozenset({"router"}))
        self.assertIsNone(problem.requests[1].required_vehicle)

    def test_accepts_optional_fields_and_strips_identifiers(self) -> None:
        payload = minimal_payload()
        payload["plan_id"] = "  plan-1  "
        payload["planning_reason"] = "replan"
        payload["requests"][0]["required_equipment"] = None
        payload["requests"][0]["locked_technician_id"] = " TECH-1 "
        payload["technicians"][0]["equipment"] = []
        payload["travel"]["legs"].append(
            {
                "from": "office",
                "to": "client",
                "mode": "pedestrian",
                "minutes": 0,
                "distance_km": 0,
            }
        )
        problem = parse_problem(payload)
        self.assertEqual(problem.plan_id, "plan-1")
        self.assertEqual(problem.planning_reason, "replan")
        self.assertEqual(problem.requests[0].locked_technician_id, "TECH-1")
        self.assertEqual(problem.requests[0].required_equipment, frozenset())
        self.assertEqual(problem.technicians[0].equipment, frozenset())
        self.assertEqual(problem.travel.get("office", "client", "pedestrian").minutes, 0)

    def test_window_start_equal_to_end_is_allowed(self) -> None:
        payload = minimal_payload()
        payload["requests"][0]["window"]["end"] = payload["requests"][0]["window"]["start"]
        problem = parse_problem(payload)
        self.assertEqual(problem.requests[0].window.start, problem.requests[0].window.end)

    def test_rejects_invalid_payloads(self) -> None:
        cases = {
            "schema": lambda row: row.__setitem__("schema_version", "2.0"),
            "empty_plan_id": lambda row: row.__setitem__("plan_id", "  "),
            "reason": lambda row: row.__setitem__("planning_reason", "midshift"),
            "locations_type": lambda row: row.__setitem__("locations", "office"),
            "duplicate_locations": lambda row: row["locations"].append({"id": "office"}),
            "requests_type": lambda row: row.__setitem__("requests", [{"id": "x"}, 1]),
            "window_type": lambda row: row["requests"][0].__setitem__("window", "soon"),
            "window_order": lambda row: row["requests"][0]["window"].__setitem__(
                "end", "2026-08-17T09:00:00+03:00"
            ),
            "unknown_request_location": lambda row: row["requests"][0].__setitem__(
                "location_id", "missing"
            ),
            "duration_zero": lambda row: row["requests"][0].__setitem__(
                "duration_minutes", 0
            ),
            "duration_bool": lambda row: row["requests"][0].__setitem__(
                "duration_minutes", True
            ),
            "empty_skills": lambda row: row["requests"][0].__setitem__(
                "required_skills", []
            ),
            "duplicate_skills": lambda row: row["requests"][0].__setitem__(
                "required_skills", ["connection", "connection"]
            ),
            "empty_vehicle": lambda row: row["requests"][0].__setitem__(
                "required_vehicle", ""
            ),
            "priority": lambda row: row["requests"][0].__setitem__("priority", "low"),
            "equipment_type": lambda row: row["requests"][0].__setitem__(
                "required_equipment", "router"
            ),
            "duplicate_equipment": lambda row: row["requests"][0].__setitem__(
                "required_equipment", ["kit", "kit"]
            ),
            "duplicate_requests": lambda row: row["requests"].append(
                copy.deepcopy(row["requests"][0])
            ),
            "empty_technicians": lambda row: row.__setitem__("technicians", []),
            "unknown_start": lambda row: row["technicians"][0].__setitem__(
                "start_location_id", "missing"
            ),
            "shift_equal": lambda row: row["technicians"][0].__setitem__(
                "available_from", row["technicians"][0]["shift_end"]
            ),
            "empty_tech_skills": lambda row: row["technicians"][0].__setitem__(
                "skills", []
            ),
            "duplicate_technicians": lambda row: row["technicians"].append(
                copy.deepcopy(row["technicians"][0])
            ),
            "unknown_lock": lambda row: row["requests"][0].__setitem__(
                "locked_technician_id", "TECH-9"
            ),
            "travel_type": lambda row: row.__setitem__("travel", []),
            "symmetric_type": lambda row: row["travel"].__setitem__("symmetric", "yes"),
            "unknown_leg": lambda row: row["travel"]["legs"][0].__setitem__(
                "to", "missing"
            ),
            "duplicate_leg": lambda row: row["travel"]["legs"].append(
                copy.deepcopy(row["travel"]["legs"][0])
            ),
            "negative_minutes": lambda row: row["travel"]["legs"][0].__setitem__(
                "minutes", -1
            ),
            "negative_distance": lambda row: row["travel"]["legs"][0].__setitem__(
                "distance_km", -0.1
            ),
            "naive_time": lambda row: row["technicians"][0].__setitem__(
                "available_from", "2026-08-17T09:00:00"
            ),
            "bad_time": lambda row: row["technicians"][0].__setitem__(
                "available_from", "not-a-date"
            ),
            "empty_name": lambda row: row["technicians"][0].__setitem__("name", ""),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                payload = minimal_payload()
                mutate(payload)
                with self.assertRaises(ContractError):
                    parse_problem(payload)

    def test_duplicate_mode_specific_leg_is_rejected(self) -> None:
        payload = minimal_payload()
        payload["travel"]["legs"].append(
            {
                "from": "office",
                "to": "client",
                "mode": "car",
                "minutes": 8,
                "distance_km": 2.0,
            }
        )
        payload["travel"]["legs"].append(copy.deepcopy(payload["travel"]["legs"][-1]))
        with self.assertRaises(ContractError) as raised:
            parse_problem(payload)
        self.assertIn("duplicate travel leg", str(raised.exception))
        self.assertIn("car", str(raised.exception))

    def test_shift_must_be_strictly_after_available_from(self) -> None:
        payload = minimal_payload()
        payload["technicians"][0]["available_from"] = "2026-08-17T19:00:00+03:00"
        with self.assertRaises(ContractError) as raised:
            parse_problem(payload)
        self.assertIn("available_from must be before shift_end", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
