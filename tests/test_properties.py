"""Property tests for the planning model, simulation, and planners."""

from __future__ import annotations

import copy
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from backend.math_core.contract import ContractError, parse_problem
from backend.math_core.greedy import GreedyPlanner
from backend.math_core.local_search import LocalSearchPlanner
from backend.math_core.routing import RoutingPlanner
from backend.math_core.models import (
    PlanningProblem,
    Request,
    Technician,
    TimeWindow,
    TravelLeg,
    TravelTable,
)
from backend.math_core.simulate import compatible, lex_score, simulate_route

from tests.helpers import DAY, minimal_payload

FAST = settings(max_examples=50, deadline=None)
PLANS = settings(max_examples=20, deadline=None)
ROUTING = settings(max_examples=8, deadline=None)

SKILLS = ("connection", "emergency", "satellite")
EQUIPMENT = ("router", "ladder", "tv-box")
VEHICLES = ("car", "pedestrian", "van")
PRIORITIES = ("normal", "high", "urgent")
LOCATIONS = ("S", "A", "B", "C", "D", "E")
REJECTION_CODES = frozenset(
    {
        "NO_SKILLED_TECHNICIAN",
        "NO_REQUIRED_VEHICLE",
        "NO_REQUIRED_EQUIPMENT",
        "LOCKED_TECHNICIAN_UNAVAILABLE",
        "TRAVEL_DATA_MISSING",
        "NO_FEASIBLE_TIME_SLOT",
    }
)


def at_minute(minute: int):
    return DAY + timedelta(minutes=minute)


def iso(minute: int) -> str:
    return at_minute(minute).isoformat()


skills = st.frozensets(st.sampled_from(SKILLS), max_size=3)
equipment = st.frozensets(st.sampled_from(EQUIPMENT), max_size=2)
modes = st.one_of(st.none(), st.sampled_from(VEHICLES))


@st.composite
def travel_queries(draw):
    names = draw(
        st.lists(st.sampled_from(LOCATIONS), min_size=1, max_size=4, unique=True)
    )
    legs: dict[tuple[str, str, str | None], TravelLeg] = {}
    for origin in names:
        for destination in names:
            if origin == destination:
                continue
            for mode in (None, draw(st.sampled_from(VEHICLES))):
                if draw(st.booleans()):
                    legs[(origin, destination, mode)] = TravelLeg(
                        draw(st.integers(0, 240)),
                        draw(st.integers(0, 400)) / 2,
                    )
    origin = draw(st.sampled_from(names))
    destination = draw(st.sampled_from(names))
    mode = draw(modes)
    symmetric = draw(st.booleans())
    return legs, symmetric, origin, destination, mode


def expected_leg(
    legs: dict[tuple[str, str, str | None], TravelLeg],
    symmetric: bool,
    origin: str,
    destination: str,
    mode: str | None,
) -> TravelLeg | None:
    if origin == destination:
        return TravelLeg(0, 0.0)
    keys = [(origin, destination, mode), (origin, destination, None)]
    if symmetric:
        keys.extend([(destination, origin, mode), (destination, origin, None)])
    for key in keys:
        if key in legs:
            return legs[key]
    return None


def minute_offset(moment: datetime, origin: datetime) -> int:
    seconds = int((moment - origin).total_seconds())
    if seconds % 60:
        raise AssertionError("reference clock expects whole minutes")
    return seconds // 60


@dataclass(frozen=True)
class ClockStop:
    arrival: datetime
    service_start: datetime
    service_end: datetime
    travel_minutes: int
    distance_km: float
    wait_minutes: int


@dataclass(frozen=True)
class Clock:
    """Route timeline computed in minute offsets, not by simulate_route."""

    stops: tuple[ClockStop, ...]
    travel_minutes: int
    distance_km: float
    wait_minutes: int
    finish_at: datetime
    failure: str
    failed_index: int | None


def schedule_route(route: list[Request], technician: Technician, travel: TravelTable) -> Clock:
    """Replay a route from the published rules, without calling simulate_route.

    Time is an integer offset from ``available_from``. Legs come from the same
    lookup order as the travel-table spec (``expected_leg``), not from
    ``TravelTable.get``.
    """

    origin = technician.available_from
    clock = 0
    location = technician.start_location_id
    shift_end = minute_offset(technician.shift_end, origin)
    stops: list[ClockStop] = []
    travel_minutes = 0
    distance_km = 0.0
    wait_minutes = 0
    for index, request in enumerate(route):
        leg = expected_leg(
            travel._legs,
            travel._symmetric,
            location,
            request.location_id,
            technician.vehicle,
        )
        if leg is None:
            return Clock(
                tuple(stops), travel_minutes, distance_km, wait_minutes,
                origin + timedelta(minutes=clock), "TRAVEL_DATA_MISSING", index,
            )
        arrival_min = clock + leg.minutes
        service_start_min = max(arrival_min, minute_offset(request.window.start, origin))
        service_end_min = service_start_min + request.duration_minutes
        window_end = minute_offset(request.window.end, origin)
        if service_start_min > window_end or service_end_min > shift_end:
            return Clock(
                tuple(stops), travel_minutes, distance_km, wait_minutes,
                origin + timedelta(minutes=clock), "NO_FEASIBLE_TIME_SLOT", index,
            )
        wait = service_start_min - arrival_min
        stops.append(
            ClockStop(
                arrival=origin + timedelta(minutes=arrival_min),
                service_start=origin + timedelta(minutes=service_start_min),
                service_end=origin + timedelta(minutes=service_end_min),
                travel_minutes=leg.minutes,
                distance_km=leg.distance_km,
                wait_minutes=wait,
            )
        )
        clock = service_end_min
        location = request.location_id
        travel_minutes += leg.minutes
        distance_km += leg.distance_km
        wait_minutes += wait
    return Clock(
        tuple(stops), travel_minutes, distance_km, wait_minutes,
        origin + timedelta(minutes=clock), "", None,
    )


def fits(request: Request, technician: Technician) -> bool:
    return (
        request.required_skills <= technician.skills
        and request.required_equipment <= technician.equipment
        and (
            request.required_vehicle is None
            or request.required_vehicle == technician.vehicle
        )
        and (
            request.locked_technician_id is None
            or request.locked_technician_id == technician.id
        )
    )


@st.composite
def compatibility_pairs(draw):
    required_skills = draw(st.frozensets(st.sampled_from(SKILLS), min_size=1, max_size=2))
    required_equipment = draw(equipment)
    required_vehicle = draw(st.one_of(st.none(), st.sampled_from(VEHICLES)))
    technician_id = draw(st.sampled_from(("TECH-1", "TECH-2")))
    locked = draw(st.one_of(st.none(), st.sampled_from(("TECH-1", "TECH-2", "OTHER"))))
    request = Request(
        id="REQ",
        location_id="A",
        duration_minutes=30,
        window=TimeWindow(at_minute(9 * 60), at_minute(18 * 60)),
        required_skills=required_skills,
        required_vehicle=required_vehicle,
        required_equipment=required_equipment,
        locked_technician_id=locked,
        priority=draw(st.sampled_from(PRIORITIES)),
    )
    technician = Technician(
        id=technician_id,
        name=technician_id,
        start_location_id="S",
        available_from=at_minute(9 * 60),
        shift_end=at_minute(18 * 60),
        skills=draw(skills),
        vehicle=draw(st.sampled_from(VEHICLES)),
        equipment=draw(equipment),
    )
    return request, technician


@st.composite
def route_cases(draw):
    vehicle = draw(st.sampled_from(VEHICLES))
    available = draw(st.integers(6 * 60, 12 * 60))
    shift_end = draw(st.integers(available + 1, 22 * 60))
    technician = Technician(
        id="TECH",
        name="TECH",
        start_location_id="S",
        available_from=at_minute(available),
        shift_end=at_minute(shift_end),
        skills=frozenset({"connection"}),
        vehicle=vehicle,
        equipment=frozenset(),
    )
    count = draw(st.integers(0, 6))
    route = [_request(draw, index) for index in range(count)]
    extra = _request(draw, count)
    legs: dict[tuple[str, str, str | None], TravelLeg] = {}
    for origin in LOCATIONS:
        for destination in LOCATIONS:
            if origin == destination:
                continue
            if draw(st.booleans()):
                legs[(origin, destination, None)] = _leg(draw)
            if draw(st.booleans()):
                legs[(origin, destination, vehicle)] = _leg(draw)
    travel = TravelTable(legs, draw(st.booleans()))
    return route, technician, travel, extra


def _request(draw, index: int) -> Request:
    start = draw(st.integers(0, 16 * 60))
    end = draw(st.integers(start, 22 * 60))
    return Request(
        id=f"REQ-{index}",
        location_id=draw(st.sampled_from(LOCATIONS)),
        duration_minutes=draw(st.integers(1, 180)),
        window=TimeWindow(at_minute(start), at_minute(end)),
        required_skills=frozenset({"connection"}),
        required_vehicle=None,
        required_equipment=frozenset(),
        locked_technician_id=None,
        priority=draw(st.sampled_from(PRIORITIES)),
    )


def _leg(draw) -> TravelLeg:
    return TravelLeg(draw(st.integers(0, 90)), float(draw(st.integers(0, 80))))


@st.composite
def score_cases(draw):
    count = draw(st.integers(0, 6))
    requests = tuple(
        Request(
            id=f"REQ-{index}",
            location_id="A",
            duration_minutes=30,
            window=TimeWindow(at_minute(9 * 60), at_minute(18 * 60)),
            required_skills=frozenset({"connection"}),
            required_vehicle=None,
            required_equipment=frozenset(),
            locked_technician_id=None,
            priority=draw(st.sampled_from(PRIORITIES)),
        )
        for index in range(count)
    )
    ids = [request.id for request in requests]
    chosen = (
        draw(st.lists(st.sampled_from(ids), max_size=count, unique=True)) if ids else []
    )
    if draw(st.booleans()):
        chosen.append(draw(st.sampled_from(("GHOST", "OTHER"))))
    used = draw(st.integers(0, 4))
    travel_minutes = draw(st.integers(0, 400))
    distance_km = float(draw(st.integers(0, 5_000)))
    return requests, set(chosen), used, travel_minutes, distance_km


@st.composite
def planning_problems(
    draw,
    min_technicians: int = 1,
    max_technicians: int = 4,
    min_requests: int = 0,
    max_requests: int = 8,
):
    open_schedule = draw(st.booleans())
    technician_count = draw(st.integers(min_technicians, max_technicians))
    request_count = draw(st.integers(min_requests, max_requests))
    technicians = []
    for index in range(technician_count):
        if open_schedule:
            available, shift_end = 8 * 60, 18 * 60
            tech_skills = frozenset(SKILLS)
            tech_equipment = frozenset(EQUIPMENT)
        else:
            available = draw(st.integers(7 * 60, 12 * 60))
            shift_end = draw(st.integers(available + 30, 21 * 60))
            tech_skills = draw(st.frozensets(st.sampled_from(SKILLS), min_size=1, max_size=3))
            tech_equipment = draw(equipment)
        technicians.append(
            Technician(
                id=f"TECH-{index}",
                name=f"TECH-{index}",
                start_location_id="S",
                available_from=at_minute(available),
                shift_end=at_minute(shift_end),
                skills=tech_skills,
                vehicle=draw(st.sampled_from(VEHICLES)),
                equipment=tech_equipment,
            )
        )
    requests = []
    for index in range(request_count):
        if open_schedule:
            window = TimeWindow(at_minute(8 * 60), at_minute(18 * 60))
            duration = draw(st.integers(15, 40))
            required_skills = frozenset({"connection"})
            required_equipment = frozenset()
            required_vehicle = None
        else:
            start = draw(st.integers(7 * 60, 15 * 60))
            window = TimeWindow(at_minute(start), at_minute(draw(st.integers(start, 20 * 60))))
            duration = draw(st.integers(15, 120))
            required_skills = draw(
                st.frozensets(st.sampled_from(SKILLS), min_size=1, max_size=2)
            )
            required_equipment = draw(equipment)
            required_vehicle = draw(st.one_of(st.none(), st.sampled_from(VEHICLES)))
        locked = None
        if draw(st.booleans()):
            locked = draw(st.sampled_from([tech.id for tech in technicians] + ["OTHER"]))
        requests.append(
            Request(
                id=f"REQ-{index}",
                location_id=draw(st.sampled_from(LOCATIONS)),
                duration_minutes=duration,
                window=window,
                required_skills=required_skills,
                required_vehicle=required_vehicle,
                required_equipment=required_equipment,
                locked_technician_id=locked,
                priority=draw(st.sampled_from(PRIORITIES)),
            )
        )
    vehicles = {tech.vehicle for tech in technicians}
    legs: dict[tuple[str, str, str | None], TravelLeg] = {}
    for origin in LOCATIONS:
        for destination in LOCATIONS:
            if origin == destination:
                continue
            if open_schedule or draw(st.booleans()):
                legs[(origin, destination, None)] = TravelLeg(
                    draw(st.integers(0, 20)) if open_schedule else draw(st.integers(0, 60)),
                    float(draw(st.integers(0, 30))),
                )
            for vehicle in vehicles:
                if draw(st.booleans()):
                    legs[(origin, destination, vehicle)] = TravelLeg(
                        draw(st.integers(0, 60)),
                        float(draw(st.integers(0, 30))),
                    )
    travel = TravelTable(legs, symmetric=draw(st.booleans()) or open_schedule)
    return PlanningProblem("property", tuple(requests), tuple(technicians), travel)


@st.composite
def valid_payloads(draw):
    location_count = draw(st.integers(1, 5))
    locations = [f"L{index}" for index in range(location_count)]
    technician_count = draw(st.integers(1, 4))
    technicians = []
    for index in range(technician_count):
        available = draw(st.integers(7 * 60, 11 * 60))
        technicians.append(
            {
                "id": f"TECH-{index}",
                "name": f"  Tech {index}  ",
                "start_location_id": draw(st.sampled_from(locations)),
                "available_from": iso(available),
                "shift_end": iso(draw(st.integers(available + 1, 22 * 60))),
                "skills": draw(
                    st.lists(st.sampled_from(SKILLS), min_size=1, max_size=2, unique=True)
                ),
                "vehicle": draw(st.sampled_from(VEHICLES)),
                "equipment": draw(
                    st.lists(st.sampled_from(EQUIPMENT), max_size=2, unique=True)
                ),
            }
        )
    request_count = draw(st.integers(0, 8))
    requests = []
    for index in range(request_count):
        start = draw(st.integers(8 * 60, 16 * 60))
        row = {
            "id": f"  REQ-{index}  ",
            "location_id": draw(st.sampled_from(locations)),
            "duration_minutes": draw(st.integers(1, 180)),
            "window": {
                "start": iso(start),
                "end": iso(draw(st.integers(start, 22 * 60))),
            },
            "required_skills": draw(
                st.lists(st.sampled_from(SKILLS), min_size=1, max_size=2, unique=True)
            ),
            "required_vehicle": draw(st.one_of(st.none(), st.sampled_from(VEHICLES))),
            "required_equipment": draw(
                st.lists(st.sampled_from(EQUIPMENT), max_size=2, unique=True)
            ),
            "priority": draw(st.sampled_from(PRIORITIES)),
        }
        if draw(st.booleans()):
            locked = draw(st.sampled_from([tech["id"] for tech in technicians]))
            row["locked_technician_id"] = f"  {locked}  "
        requests.append(row)
    symmetric = draw(st.booleans())
    pairs = [
        (origin, destination)
        for origin in locations
        for destination in locations
        if origin != destination and (not symmetric or origin < destination)
    ]
    chosen = (
        draw(st.lists(st.sampled_from(pairs), max_size=len(pairs), unique=True))
        if pairs
        else []
    )
    legs = []
    for origin, destination in chosen:
        mode = draw(modes)
        leg = {
            "from": origin,
            "to": destination,
            "minutes": draw(st.integers(0, 90)),
            "distance_km": draw(st.integers(0, 40)),
        }
        if mode is not None:
            leg["mode"] = mode
        legs.append(leg)
    core_id = draw(st.text(alphabet="plan-id", min_size=1, max_size=8))
    return {
        "schema_version": "1.0",
        "plan_id": f"  {core_id}  ",
        "planning_reason": draw(st.sampled_from(("overnight", "replan"))),
        "locations": [{"id": location_id} for location_id in locations],
        "requests": requests,
        "technicians": technicians,
        "travel": {"symmetric": symmetric, "legs": legs},
    }


def hard_rejection(request: Request, technicians: tuple[Technician, ...]) -> str | None:
    skilled = [tech for tech in technicians if request.required_skills <= tech.skills]
    if not skilled:
        return "NO_SKILLED_TECHNICIAN"
    with_vehicle = [
        tech
        for tech in skilled
        if request.required_vehicle is None or request.required_vehicle == tech.vehicle
    ]
    if not with_vehicle:
        return "NO_REQUIRED_VEHICLE"
    equipped = [
        tech for tech in with_vehicle if request.required_equipment <= tech.equipment
    ]
    if not equipped:
        return "NO_REQUIRED_EQUIPMENT"
    available = [
        tech
        for tech in equipped
        if request.locked_technician_id is None
        or request.locked_technician_id == tech.id
    ]
    if not available:
        return "LOCKED_TECHNICIAN_UNAVAILABLE"
    return None


def assert_matches_clock(test: unittest.TestCase, simulation, clock: Clock) -> None:
    test.assertEqual(simulation is None, bool(clock.failure))
    if clock.failure:
        return
    test.assertEqual(simulation.finish_at, clock.finish_at)
    test.assertEqual(simulation.travel_minutes, clock.travel_minutes)
    test.assertEqual(simulation.distance_km, clock.distance_km)
    test.assertEqual(simulation.wait_minutes, clock.wait_minutes)
    test.assertEqual(len(simulation.stops), len(clock.stops))
    for produced, expected in zip(simulation.stops, clock.stops, strict=True):
        test.assertEqual(produced.arrival, expected.arrival)
        test.assertEqual(produced.service_start, expected.service_start)
        test.assertEqual(produced.service_end, expected.service_end)
        test.assertEqual(produced.travel_minutes, expected.travel_minutes)
        test.assertEqual(produced.distance_km, expected.distance_km)
        test.assertEqual(produced.wait_minutes, expected.wait_minutes)


def assert_plan(
    test: unittest.TestCase,
    problem: PlanningProblem,
    result: dict,
    *,
    reasons: bool,
) -> None:
    request_ids = [request.id for request in problem.requests]
    assigned: list[str] = []
    techs = {tech.id: tech for tech in problem.technicians}
    requests = {request.id: request for request in problem.requests}
    test.assertEqual(result["schema_version"], "1.0")
    test.assertEqual(result["plan_id"], problem.plan_id)
    test.assertEqual(result["metrics"]["request_count"], len(problem.requests))
    route_techs = [route["technician_id"] for route in result["routes"]]
    test.assertEqual(len(route_techs), len(set(route_techs)))
    test.assertEqual(result["metrics"]["used_technicians"], len(result["routes"]))

    total_travel = 0
    total_distance = 0.0
    total_service = 0
    total_wait = 0
    for route in result["routes"]:
        technician = techs[route["technician_id"]]
        sequence = [requests[stop["request_id"]] for stop in route["stops"]]
        clock = schedule_route(sequence, technician, problem.travel)
        test.assertEqual(clock.failure, "")
        test.assertEqual(route["finish_at"], clock.finish_at.isoformat())
        test.assertEqual(route["metrics"]["travel_minutes"], clock.travel_minutes)
        test.assertEqual(route["metrics"]["wait_minutes"], clock.wait_minutes)
        test.assertEqual(route["metrics"]["distance_km"], round(clock.distance_km, 3))
        test.assertEqual(
            route["metrics"]["service_minutes"],
            sum(request.duration_minutes for request in sequence),
        )
        for sequence_no, stop, tick, request in zip(
            range(1, len(sequence) + 1), route["stops"], clock.stops, sequence, strict=True
        ):
            test.assertEqual(stop["sequence"], sequence_no)
            test.assertEqual(stop["request_id"], request.id)
            test.assertTrue(fits(request, technician))
            if request.locked_technician_id is not None:
                test.assertEqual(request.locked_technician_id, technician.id)
            test.assertEqual(stop["arrival_at"], tick.arrival.isoformat())
            test.assertEqual(stop["service_start_at"], tick.service_start.isoformat())
            test.assertEqual(stop["service_end_at"], tick.service_end.isoformat())
            test.assertEqual(stop["travel_minutes"], tick.travel_minutes)
            test.assertEqual(stop["distance_km"], round(tick.distance_km, 3))
            test.assertEqual(stop["wait_minutes"], tick.wait_minutes)
            assigned.append(request.id)
        total_travel += clock.travel_minutes
        total_distance += clock.distance_km
        total_service += route["metrics"]["service_minutes"]
        total_wait += clock.wait_minutes

    unassigned = [row["request_id"] for row in result["unassigned"]]
    test.assertEqual(sorted(assigned + unassigned), sorted(request_ids))
    test.assertEqual(len(assigned), len(set(assigned)))
    test.assertEqual(result["metrics"]["assigned_count"], len(assigned))
    test.assertEqual(result["metrics"]["unassigned_count"], len(unassigned))
    test.assertEqual(result["metrics"]["travel_minutes"], total_travel)
    test.assertEqual(result["metrics"]["service_minutes"], total_service)
    test.assertEqual(result["metrics"]["wait_minutes"], total_wait)
    test.assertEqual(result["metrics"]["distance_km"], round(total_distance, 3))

    if not reasons:
        return
    for row in result["unassigned"]:
        request = requests[row["request_id"]]
        code = row["reason"]["code"]
        test.assertIn(code, REJECTION_CODES)
        expected = hard_rejection(request, problem.technicians)
        if expected is None:
            test.assertIn(code, {"TRAVEL_DATA_MISSING", "NO_FEASIBLE_TIME_SLOT"})
        else:
            test.assertEqual(code, expected)


def reference_score(problem: PlanningProblem, result: dict) -> tuple[int, int, int, int, int, int]:
    techs = {tech.id: tech for tech in problem.technicians}
    requests = {request.id: request for request in problem.requests}
    assigned: set[str] = set()
    travel_minutes = 0
    distance_km = 0.0
    for route in result["routes"]:
        technician = techs[route["technician_id"]]
        sequence = [requests[stop["request_id"]] for stop in route["stops"]]
        clock = schedule_route(sequence, technician, problem.travel)
        if clock.failure:
            raise AssertionError(clock.failure)
        assigned.update(request.id for request in sequence)
        travel_minutes += clock.travel_minutes
        distance_km += clock.distance_km
    counts = {"urgent": 0, "high": 0, "normal": 0}
    for request in problem.requests:
        if request.id not in assigned:
            counts[request.priority] += 1
    return (
        counts["urgent"],
        counts["high"],
        counts["normal"],
        len(result["routes"]),
        travel_minutes,
        int(round(distance_km * 1000)),
    )


class TravelTablePropertyTest(unittest.TestCase):
    @FAST
    @given(travel_queries())
    def test_lookup_follows_mode_then_generic_then_reverse(self, query) -> None:
        legs, symmetric, origin, destination, mode = query
        table = TravelTable(legs, symmetric)
        found = table.get(origin, destination, mode)
        self.assertEqual(found, expected_leg(legs, symmetric, origin, destination, mode))
        self.assertEqual(found, table.get(origin, destination, mode))
        self.assertEqual(table.get(origin, origin, mode), TravelLeg(0, 0.0))


class CompatiblePropertyTest(unittest.TestCase):
    @FAST
    @given(compatibility_pairs())
    def test_compatibility_is_the_four_subset_checks(self, pair) -> None:
        request, technician = pair
        self.assertEqual(
            compatible(request, technician),
            request.required_skills <= technician.skills
            and request.required_equipment <= technician.equipment
            and (
                request.required_vehicle is None
                or request.required_vehicle == technician.vehicle
            )
            and (
                request.locked_technician_id is None
                or request.locked_technician_id == technician.id
            ),
        )

    @FAST
    @given(compatibility_pairs(), st.sampled_from(SKILLS), st.sampled_from(EQUIPMENT))
    def test_extra_technician_capabilities_keep_a_match(
        self, pair, skill: str, tool: str
    ) -> None:
        request, technician = pair
        if not compatible(request, technician):
            return
        richer = Technician(
            id=technician.id,
            name=technician.name,
            start_location_id=technician.start_location_id,
            available_from=technician.available_from,
            shift_end=technician.shift_end,
            skills=technician.skills | {skill},
            vehicle=technician.vehicle,
            equipment=technician.equipment | {tool},
        )
        self.assertTrue(compatible(request, richer))


class SimulateRoutePropertyTest(unittest.TestCase):
    @FAST
    @given(route_cases())
    def test_simulation_matches_reference_clock(self, case) -> None:
        route, technician, travel, _extra = case
        simulation, failure = simulate_route(route, technician, travel)
        clock = schedule_route(route, technician, travel)
        self.assertEqual(failure, clock.failure)
        assert_matches_clock(self, simulation, clock)
        for size in range(len(route) + 1):
            prefix, prefix_failure = simulate_route(route[:size], technician, travel)
            prefix_clock = schedule_route(route[:size], technician, travel)
            self.assertEqual(prefix_failure, prefix_clock.failure)
            assert_matches_clock(self, prefix, prefix_clock)
            if clock.failure == "" or size <= clock.failed_index:
                self.assertEqual(prefix_failure, "")

    @FAST
    @given(route_cases())
    def test_failed_prefix_rejects_any_extension(self, case) -> None:
        route, technician, travel, extra = case
        failure = schedule_route(route, technician, travel).failure
        extended = schedule_route(route + [extra], technician, travel)
        _simulation, produced = simulate_route(route, technician, travel)
        _extended, produced_extension = simulate_route(route + [extra], technician, travel)
        self.assertEqual(produced, failure)
        self.assertEqual(produced_extension, extended.failure)
        if failure:
            self.assertEqual(extended.failure, failure)
        elif extended.failure == "":
            self.assertEqual(extended.stops[: len(route)], schedule_route(route, technician, travel).stops)


class LexScorePropertyTest(unittest.TestCase):
    @FAST
    @given(score_cases())
    def test_vector_orders_unassigned_priorities_before_cost(self, case) -> None:
        requests, assigned, used, travel_minutes, distance_km = case
        counts = {"urgent": 0, "high": 0, "normal": 0}
        for request in requests:
            if request.id not in assigned:
                counts[request.priority] += 1
        meters = int(round(distance_km * 1000))
        self.assertEqual(
            lex_score(requests, assigned, used, travel_minutes, distance_km),
            (counts["urgent"], counts["high"], counts["normal"], used, travel_minutes, meters),
        )

    @FAST
    @given(score_cases())
    def test_assigning_every_request_improves_or_preserves_the_vector(self, case) -> None:
        requests, _assigned, used, travel_minutes, distance_km = case
        none = lex_score(requests, set(), used, travel_minutes, distance_km)
        every = lex_score(
            requests,
            {request.id for request in requests},
            used,
            travel_minutes,
            distance_km,
        )
        if requests:
            self.assertLess(every, none)
        else:
            self.assertEqual(every, none)
        self.assertEqual(every[:3], (0, 0, 0))


class ParseProblemPropertyTest(unittest.TestCase):
    @FAST
    @given(valid_payloads())
    def test_valid_payload_round_trips_identifiers_and_legs(self, payload) -> None:
        problem = parse_problem(payload)
        self.assertEqual(problem.plan_id, payload["plan_id"].strip())
        self.assertEqual(problem.planning_reason, payload["planning_reason"])
        self.assertEqual(
            [request.id for request in problem.requests],
            [row["id"].strip() for row in payload["requests"]],
        )
        self.assertEqual(
            [technician.name for technician in problem.technicians],
            [row["name"].strip() for row in payload["technicians"]],
        )
        for row, request in zip(payload["requests"], problem.requests, strict=True):
            self.assertEqual(request.required_skills, frozenset(row["required_skills"]))
            self.assertEqual(request.required_equipment, frozenset(row["required_equipment"]))
            self.assertEqual(request.priority, row["priority"])
            locked = row.get("locked_technician_id")
            self.assertEqual(
                request.locked_technician_id, None if locked is None else locked.strip()
            )
        for leg in payload["travel"]["legs"]:
            mode = leg.get("mode")
            found = problem.travel.get(leg["from"], leg["to"], mode)
            self.assertEqual(found.minutes, leg["minutes"])
            self.assertEqual(found.distance_km, float(leg["distance_km"]))
            if payload["travel"]["symmetric"]:
                reverse = problem.travel.get(leg["to"], leg["from"], mode)
                self.assertEqual(reverse, found)

    @FAST
    @given(st.one_of(st.integers(min_value=-500, max_value=0), st.just(True), st.just(False)))
    def test_non_positive_duration_is_rejected(self, duration) -> None:
        payload = minimal_payload()
        payload["requests"][0]["duration_minutes"] = duration
        with self.assertRaises(ContractError):
            parse_problem(payload)

    @FAST
    @given(st.integers(min_value=1, max_value=600))
    def test_inverted_window_is_rejected(self, gap: int) -> None:
        payload = minimal_payload()
        start = at_minute(12 * 60)
        payload["requests"][0]["window"]["start"] = start.isoformat()
        payload["requests"][0]["window"]["end"] = (start - timedelta(minutes=gap)).isoformat()
        with self.assertRaises(ContractError):
            parse_problem(payload)

    @FAST
    @given(
        st.integers(min_value=-500, max_value=-1),
        st.sampled_from(("minutes", "distance_km")),
    )
    def test_negative_travel_is_rejected(self, amount: int, field: str) -> None:
        payload = minimal_payload()
        payload["travel"]["legs"][0][field] = amount
        with self.assertRaises(ContractError):
            parse_problem(payload)

    @FAST
    @given(st.sampled_from(("", "1", "1.1", "2.0", "1.0 ")))
    def test_unexpected_schema_version_is_rejected(self, schema_version: str) -> None:
        payload = minimal_payload()
        payload["schema_version"] = schema_version
        with self.assertRaises(ContractError):
            parse_problem(payload)


class PlannerPropertyTest(unittest.TestCase):
    @PLANS
    @given(planning_problems())
    def test_greedy_plan_is_feasible_and_deterministic(self, problem) -> None:
        planner = GreedyPlanner()
        first = planner.plan(problem)
        second = planner.plan(problem)
        assert_plan(self, problem, first, reasons=True)
        self.assertEqual(
            {key: value for key, value in first.items() if key != "diagnostics"},
            {key: value for key, value in second.items() if key != "diagnostics"},
        )

    @PLANS
    @given(planning_problems())
    def test_local_search_stays_feasible_and_lex_dominates_greedy(self, problem) -> None:
        greedy = GreedyPlanner().plan(problem)
        polished = LocalSearchPlanner(budget_s=0.05).plan(problem)
        assert_plan(self, problem, polished, reasons=True)
        self.assertLessEqual(reference_score(problem, polished), reference_score(problem, greedy))

    @ROUTING
    @given(planning_problems(min_technicians=2, min_requests=3))
    def test_routing_plan_matches_reference_clock(self, problem) -> None:
        result = RoutingPlanner(time_limit_s=1).plan(problem)
        solved_here = result["algorithm"]["id"] == RoutingPlanner.algorithm_id
        assert_plan(self, problem, result, reasons=not solved_here)
        if solved_here:
            self.assertEqual(result["diagnostics"]["warnings"], [])
            for row in result["unassigned"]:
                self.assertEqual(row["reason"]["code"], "NO_FEASIBLE_TIME_SLOT")
            return
        self.assertEqual(result["algorithm"]["id"], "greedy-local-search")
        self.assertTrue(
            any("greedy-local-search" in warning for warning in result["diagnostics"]["warnings"])
        )


class MinimalPayloadMutationPropertyTest(unittest.TestCase):
    @FAST
    @given(st.text(alphabet=" \t", min_size=0, max_size=4))
    def test_blank_plan_id_is_rejected(self, padding: str) -> None:
        payload = copy.deepcopy(minimal_payload())
        payload["plan_id"] = padding
        with self.assertRaises(ContractError):
            parse_problem(payload)


if __name__ == "__main__":
    unittest.main()
