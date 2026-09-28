import unittest

from backend.math_core import ContractError, parse_problem, planner_for
from tests.helpers import minimal_payload


def event_payload(priority="normal"):
    payload = minimal_payload()
    payload["planning_reason"] = "replan"
    payload["locations"].append({"id": "far"})
    payload["requests"][0].update(
        id="OLD", location_id="client", duration_minutes=30,
        window={"start": "2026-08-17T11:00:00+03:00", "end": "2026-08-17T14:00:00+03:00"},
    )
    payload["requests"].append({
        "id": "NEW", "location_id": "far", "duration_minutes": 30,
        "window": {"start": "2026-08-17T10:00:00+03:00", "end": "2026-08-17T12:00:00+03:00"},
        "required_skills": ["connection"], "required_vehicle": None, "priority": priority,
    })
    payload["technicians"][0]["available_from"] = "2026-08-17T10:00:00+03:00"
    payload["travel"]["legs"].extend([
        {"from": "office", "to": "far", "minutes": 10, "distance_km": 5},
        {"from": "client", "to": "far", "minutes": 20, "distance_km": 10},
    ])
    payload["replan_context"] = {
        "event_at": "2026-08-17T10:00:00+03:00",
        "new_request_ids": ["NEW"],
        "previous_routes": [{"technician_id": "TECH-1", "request_ids": ["OLD"]}],
    }
    return payload


class ReplanTest(unittest.TestCase):
    def test_ordinary_insertion_preserves_existing_assignment_and_order(self):
        payload = event_payload()
        payload["requests"].insert(1, {
            **payload["requests"][0], "id": "SECOND",
            "window": {"start": "2026-08-17T12:00:00+03:00", "end": "2026-08-17T16:00:00+03:00"},
        })
        payload["replan_context"]["previous_routes"][0]["request_ids"].append("SECOND")
        result = planner_for("replan").plan(parse_problem(payload))
        ids = [stop["request_id"] for stop in result["routes"][0]["stops"]]
        self.assertEqual(ids, ["NEW", "OLD", "SECOND"])
        self.assertEqual(result["algorithm"]["id"], "stable-event-insertion")

    def test_active_work_delays_emergency_without_interruption(self):
        payload = event_payload("urgent")
        payload["technicians"][0]["start_location_id"] = "client"
        payload["technicians"][0]["available_from"] = "2026-08-17T11:00:00+03:00"
        payload["replan_context"]["active_work"] = [{
            "technician_id": "TECH-1", "request_id": "STARTED",
            "location_id": "client", "finish_at": "2026-08-17T11:00:00+03:00",
        }]
        result = planner_for("replan").plan(parse_problem(payload))
        stops = [stop for route in result["routes"] for stop in route["stops"]]
        emergency = next(stop for stop in stops if stop["request_id"] == "NEW")
        self.assertGreaterEqual(emergency["service_start_at"], "2026-08-17T11:00:00+03:00")
        self.assertNotIn("STARTED", [stop["request_id"] for stop in stops])

    def test_rejects_active_work_that_is_still_pending(self):
        payload = event_payload("urgent")
        payload["replan_context"]["active_work"] = [{
            "technician_id": "TECH-1", "request_id": "OLD", "location_id": "client",
            "finish_at": "2026-08-17T11:00:00+03:00",
        }]
        with self.assertRaises(ContractError):
            parse_problem(payload)

    def test_reaction_guideline_warns_without_dropping_emergency(self):
        payload = event_payload("urgent")
        payload["requests"][1]["window"]["end"] = "2026-08-17T17:00:00+03:00"
        payload["technicians"][0]["start_location_id"] = "client"
        payload["technicians"][0]["available_from"] = "2026-08-17T13:00:00+03:00"
        payload["replan_context"]["active_work"] = [{
            "technician_id": "TECH-1", "request_id": "STARTED",
            "location_id": "client", "finish_at": "2026-08-17T13:00:00+03:00",
        }]
        result = planner_for("replan").plan(parse_problem(payload))
        self.assertEqual(result["metrics"]["assigned_count"], 2)
        self.assertTrue(any("NEW: reaction" in warning for warning in result["diagnostics"]["warnings"]))

    def test_geography_does_not_block_assignment(self):
        payload = event_payload()
        result = planner_for("replan").plan(parse_problem(payload))
        self.assertEqual(result["metrics"]["assigned_count"], 2)
        self.assertEqual(result["unassigned"], [])


if __name__ == "__main__":
    unittest.main()
