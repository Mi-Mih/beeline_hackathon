import unittest

from backend.math_core.simulate import compatible, lex_score, simulate_route

from tests.helpers import TimeWindow, at, make_request, make_technician, make_travel


class CompatibleTest(unittest.TestCase):
    def test_skills_equipment_vehicle_and_lock(self) -> None:
        tech = make_technician(
            skills=frozenset({"connection", "emergency"}),
            vehicle="car",
            equipment=frozenset({"router", "ladder"}),
        )
        self.assertTrue(compatible(make_request(), tech))
        self.assertTrue(
            compatible(
                make_request(
                    required_skills=frozenset({"emergency"}),
                    required_vehicle="car",
                    required_equipment=frozenset({"router"}),
                    locked_technician_id="TECH-1",
                ),
                tech,
            )
        )
        self.assertFalse(
            compatible(make_request(required_skills=frozenset({"satellite"})), tech)
        )
        self.assertFalse(compatible(make_request(required_vehicle="van"), tech))
        self.assertFalse(
            compatible(
                make_request(required_equipment=frozenset({"router", "tv-box"})),
                tech,
            )
        )
        self.assertFalse(compatible(make_request(locked_technician_id="TECH-2"), tech))


class SimulateRouteTest(unittest.TestCase):
    def test_empty_route_stays_at_start(self) -> None:
        tech = make_technician()
        simulation, failure = simulate_route([], tech, make_travel())
        self.assertEqual(failure, "")
        self.assertIsNotNone(simulation)
        self.assertEqual(simulation.stops, ())
        self.assertEqual(simulation.finish_at, tech.available_from)
        self.assertEqual(simulation.travel_minutes, 0)
        self.assertEqual(simulation.wait_minutes, 0)

    def test_waits_for_window_and_records_travel(self) -> None:
        request = make_request(
            window=TimeWindow(at(11), at(18)),
            location_id="client",
            duration_minutes=40,
        )
        tech = make_technician()
        travel = make_travel({("OFFICE", "client"): (20, 5.5)})
        simulation, failure = simulate_route([request], tech, travel)
        self.assertEqual(failure, "")
        stop = simulation.stops[0]
        self.assertEqual(stop.arrival, at(9, 20))
        self.assertEqual(stop.service_start, at(11))
        self.assertEqual(stop.service_end, at(11, 40))
        self.assertEqual(stop.wait_minutes, 100)
        self.assertEqual(stop.travel_minutes, 20)
        self.assertEqual(stop.distance_km, 5.5)
        self.assertEqual(simulation.finish_at, at(11, 40))
        self.assertEqual(simulation.wait_minutes, 100)

    def test_uses_vehicle_mode_then_generic_leg(self) -> None:
        request = make_request(location_id="client")
        pedestrian = make_technician(vehicle="pedestrian")
        car = make_technician(vehicle="car")
        travel = make_travel(
            {
                ("OFFICE", "client"): (10, 4.0),
                ("OFFICE", "client", "pedestrian"): (22, 1.8),
            }
        )
        walked, _ = simulate_route([request], pedestrian, travel)
        driven, _ = simulate_route([request], car, travel)
        self.assertEqual(walked.stops[0].travel_minutes, 22)
        self.assertEqual(driven.stops[0].travel_minutes, 10)

    def test_applies_engineer_travel_time_factor(self) -> None:
        request = make_request(location_id="client")
        technician = make_technician(travel_time_factor=1.23)
        travel = make_travel({("OFFICE", "client"): (10, 4.0)})

        simulation, failure = simulate_route([request], technician, travel)

        self.assertEqual(failure, "")
        self.assertEqual(simulation.stops[0].travel_minutes, 13)
        self.assertEqual(simulation.stops[0].arrival, at(9, 13))

    def test_missing_travel_data(self) -> None:
        request = make_request(location_id="client")
        simulation, failure = simulate_route([request], make_technician(), make_travel())
        self.assertIsNone(simulation)
        self.assertEqual(failure, "TRAVEL_DATA_MISSING")

    def test_rejects_start_after_window_end(self) -> None:
        request = make_request(window=TimeWindow(at(9), at(10)))
        tech = make_technician(available_from=at(11))
        simulation, failure = simulate_route([request], tech, make_travel())
        self.assertIsNone(simulation)
        self.assertEqual(failure, "NO_FEASIBLE_TIME_SLOT")

    def test_service_may_leave_window_but_not_shift(self) -> None:
        request = make_request(
            duration_minutes=90,
            window=TimeWindow(at(10), at(10, 30)),
        )
        ok_tech = make_technician(available_from=at(10), shift_end=at(12))
        late_tech = make_technician(available_from=at(10), shift_end=at(11))
        ok, ok_failure = simulate_route([request], ok_tech, make_travel())
        late, late_failure = simulate_route([request], late_tech, make_travel())
        self.assertEqual(ok_failure, "")
        self.assertEqual(ok.stops[0].service_end, at(11, 30))
        self.assertIsNone(late)
        self.assertEqual(late_failure, "NO_FEASIBLE_TIME_SLOT")

    def test_chains_stops_from_previous_location(self) -> None:
        first = make_request("REQ-1", location_id="A", duration_minutes=30)
        second = make_request("REQ-2", location_id="B", duration_minutes=20)
        travel = make_travel(
            {
                ("OFFICE", "A"): (10, 2.0),
                ("A", "B"): (15, 3.0),
            }
        )
        simulation, failure = simulate_route([first, second], make_technician(), travel)
        self.assertEqual(failure, "")
        self.assertEqual(simulation.stops[1].arrival, at(9, 55))
        self.assertEqual(simulation.travel_minutes, 25)
        self.assertEqual(simulation.distance_km, 5.0)


class LexScoreTest(unittest.TestCase):
    def test_counts_unassigned_priorities_then_resources(self) -> None:
        requests = (
            make_request("U", priority="urgent"),
            make_request("H", priority="high"),
            make_request("N1", priority="normal"),
            make_request("N2", priority="normal"),
        )
        self.assertEqual(
            lex_score(requests, {"U", "H", "N1"}, 2, 40, 1.2345),
            (0, 0, 1, 2, 40, 1234),
        )
        self.assertEqual(lex_score(requests, set(), 0, 0, 0.0), (1, 1, 2, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
