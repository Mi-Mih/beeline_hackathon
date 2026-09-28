from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from backend.travel_matrix import (
    ContextualRouteModel,
    GeoPoint,
    MatrixBuildError,
    RouteCandidate,
    RouteSegment,
    TravelMatrixBuilder,
    time_band,
)
from backend.math_core import parse_problem
from tests.helpers import minimal_payload


TZ = timezone(timedelta(hours=3))


def at(hour: int) -> datetime:
    return datetime(2026, 9, 21, hour, tzinfo=TZ)


class FakeProvider:
    name = "fake-roads"

    def alternatives(self, origin, destination, mode, limit):
        direction_bonus = 1 if origin.id > destination.id else 0
        return [
            RouteCandidate(
                id=f"{origin.id}-{destination.id}-short",
                segments=(RouteSegment(10 + direction_bonus, "primary"),),
                turn_count=5,
            ),
            RouteCandidate(
                id=f"{origin.id}-{destination.id}-stable",
                segments=(RouteSegment(12 + direction_bonus, "motorway"),),
                turn_count=1,
            ),
        ][:limit]


class EmptyProvider:
    name = "empty"

    def alternatives(self, origin, destination, mode, limit):
        return []


class CoLocatedProvider:
    name = "co-located"

    def alternatives(self, origin, destination, mode, limit):
        return [
            RouteCandidate(
                id=f"{origin.id}-{destination.id}-same-place",
                segments=(RouteSegment(0),),
                provider_duration_minutes=0,
            )
        ]


class ContextualRouteModelTests(unittest.TestCase):
    def test_time_bands_are_contextual(self):
        self.assertEqual(time_band(at(8)), "morning_peak")
        self.assertEqual(time_band(at(12)), "day")
        self.assertEqual(time_band(at(18)), "evening_peak")
        self.assertEqual(time_band(at(22)), "off_peak")

    def test_reliable_route_can_beat_shortest_route(self):
        candidates = FakeProvider().alternatives(
            GeoPoint("a", 55.7, 37.5), GeoPoint("b", 55.8, 37.6), "car", 3
        )
        selected, ranked = ContextualRouteModel().choose(candidates, "car", at(8))

        self.assertEqual(selected.candidate.id, "a-b-stable")
        self.assertEqual(len(ranked), 2)
        self.assertGreater(selected.candidate.distance_km, candidates[0].distance_km)
        self.assertLess(selected.score, ranked[1].score)


class TravelMatrixBuilderTests(unittest.TestCase):
    def test_builds_complete_directed_mode_specific_matrix(self):
        points = [GeoPoint("a", 55.7, 37.5), GeoPoint("b", 55.8, 37.6)]
        result = TravelMatrixBuilder(FakeProvider(), max_workers=2).build(
            points, ["car"], at(8)
        )

        self.assertFalse(result.travel["symmetric"])
        self.assertEqual(len(result.travel["legs"]), 2)
        forward, reverse = result.travel["legs"]
        self.assertEqual((forward["from"], forward["to"]), ("a", "b"))
        self.assertEqual((reverse["from"], reverse["to"]), ("b", "a"))
        self.assertEqual(forward["mode"], "car")
        self.assertNotEqual(forward["distance_km"], reverse["distance_km"])
        self.assertEqual(result.manifest["algorithm"], "contextual-multiroute-v1")
        self.assertEqual(result.manifest["leg_count"], 2)
        self.assertEqual(len(result.manifest["matrix_sha256"]), 64)
        self.assertEqual(
            result.manifest["selections"][0]["selected"], "a-b-stable"
        )

    def test_result_is_accepted_by_plan_input_contract(self):
        points = [
            GeoPoint("office", 55.7, 37.5),
            GeoPoint("client", 55.8, 37.6),
        ]
        result = TravelMatrixBuilder(FakeProvider()).build(points, ["car"], at(8))
        payload = minimal_payload()
        payload["travel"] = result.travel

        problem = parse_problem(payload)

        leg = problem.travel.get("office", "client", "car")
        self.assertIsNotNone(leg)
        self.assertGreater(leg.distance_km, 0)

    def test_fails_instead_of_using_straight_line_fallback(self):
        points = [GeoPoint("a", 55.7, 37.5), GeoPoint("b", 55.8, 37.6)]
        with self.assertRaisesRegex(MatrixBuildError, "matrix is incomplete"):
            TravelMatrixBuilder(EmptyProvider()).build(points, ["car"], at(8))

    def test_distinct_ids_at_same_coordinates_have_zero_leg(self):
        points = [GeoPoint("a", 55.7, 37.5), GeoPoint("b", 55.7, 37.5)]
        result = TravelMatrixBuilder(CoLocatedProvider()).build(
            points, ["car"], at(8)
        )

        self.assertEqual(result.travel["legs"][0]["distance_km"], 0)
        self.assertEqual(result.travel["legs"][0]["minutes"], 0)

    def test_rejects_unknown_mode_profile(self):
        points = [GeoPoint("a", 55.7, 37.5), GeoPoint("b", 55.8, 37.6)]
        with self.assertRaisesRegex(MatrixBuildError, "no contextual profile"):
            TravelMatrixBuilder(FakeProvider()).build(points, ["bike"], at(8))


if __name__ == "__main__":
    unittest.main()
