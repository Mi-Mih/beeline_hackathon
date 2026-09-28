"""Build a directed, contextual road matrix before invoking the solver.

The module deliberately has no dependency on ``backend.math_core``.  It produces the
``travel`` object from plan-input 1.0 and a separate, auditable build manifest.
Network access is hidden behind ``RouteProvider`` so selection can be tested
without a routing service and OSRM can later be replaced without touching the
solver.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import quote
from urllib.request import Request, urlopen


class MatrixBuildError(RuntimeError):
    """A complete, trustworthy travel matrix could not be built."""


@dataclass(frozen=True)
class GeoPoint:
    id: str
    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("point id must not be empty")
        if not -90 <= self.latitude <= 90 or not -180 <= self.longitude <= 180:
            raise ValueError(f"point {self.id!r} has invalid coordinates")


@dataclass(frozen=True)
class RouteSegment:
    """A part of a candidate route with one routing-relevant road class."""

    distance_km: float
    road_class: str = "default"
    zone: str = "default"

    def __post_init__(self) -> None:
        if not math.isfinite(self.distance_km) or self.distance_km < 0:
            raise ValueError("segment distance must be a finite non-negative value")


@dataclass(frozen=True)
class RouteCandidate:
    id: str
    segments: tuple[RouteSegment, ...]
    turn_count: int = 0
    traffic_light_count: int = 0
    provider_duration_minutes: float | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("candidate id must not be empty")
        if not self.segments:
            raise ValueError("candidate must contain at least one route segment")
        if self.turn_count < 0 or self.traffic_light_count < 0:
            raise ValueError("route counters must be non-negative")
        if self.provider_duration_minutes is not None and (
            not math.isfinite(self.provider_duration_minutes)
            or self.provider_duration_minutes < 0
        ):
            raise ValueError("provider duration must be finite and non-negative")

    @property
    def distance_km(self) -> float:
        return sum(item.distance_km for item in self.segments)


class RouteProvider(Protocol):
    """Return distinct road routes; no straight-line fallback is permitted."""

    name: str

    def alternatives(
        self, origin: GeoPoint, destination: GeoPoint, mode: str, limit: int
    ) -> Sequence[RouteCandidate]: ...


@dataclass(frozen=True)
class ModeProfile:
    """Explainable route-time and reliability parameters for one mode."""

    speeds_kmh: Mapping[str, float]
    variability: Mapping[str, float]
    time_factors: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    zone_factors: Mapping[str, float] = field(default_factory=dict)
    turn_penalty_minutes: float = 0.12
    traffic_light_penalty_minutes: float = 0.35
    reliability_weight: float = 0.8
    distance_weight_minutes_per_km: float = 0.03

    def __post_init__(self) -> None:
        if self.speeds_kmh.get("default", 0) <= 0:
            raise ValueError("profile must define a positive default speed")
        if any(value <= 0 for value in self.speeds_kmh.values()):
            raise ValueError("profile speeds must be positive")
        if any(value < 0 for value in self.variability.values()):
            raise ValueError("profile variability must be non-negative")
        if any(
            value <= 0
            for values in self.time_factors.values()
            for value in values.values()
        ):
            raise ValueError("profile time factors must be positive")
        if any(value <= 0 for value in self.zone_factors.values()):
            raise ValueError("profile zone factors must be positive")
        if any(
            value < 0
            for value in (
                self.turn_penalty_minutes,
                self.traffic_light_penalty_minutes,
                self.reliability_weight,
                self.distance_weight_minutes_per_km,
            )
        ):
            raise ValueError("profile penalties and weights must be non-negative")


CAR_PROFILE = ModeProfile(
    speeds_kmh=MappingProxyType(
        {
            "motorway": 72.0,
            "trunk": 58.0,
            "primary": 43.0,
            "secondary": 34.0,
            "residential": 24.0,
            "service": 15.0,
            "default": 30.0,
        }
    ),
    variability=MappingProxyType(
        {
            "motorway": 0.18,
            "trunk": 0.22,
            "primary": 0.28,
            "secondary": 0.24,
            "residential": 0.16,
            "service": 0.12,
            "default": 0.25,
        }
    ),
    time_factors=MappingProxyType(
        {
            "morning_peak": MappingProxyType(
                {"motorway": 1.20, "trunk": 1.35, "primary": 1.45, "default": 1.30}
            ),
            "day": MappingProxyType(
                {"motorway": 1.05, "trunk": 1.10, "primary": 1.15, "default": 1.10}
            ),
            "evening_peak": MappingProxyType(
                {"motorway": 1.25, "trunk": 1.40, "primary": 1.50, "default": 1.35}
            ),
            "off_peak": MappingProxyType({"default": 1.0}),
        }
    ),
)

PEDESTRIAN_PROFILE = ModeProfile(
    speeds_kmh=MappingProxyType(
        {"footway": 4.8, "steps": 3.2, "service": 4.5, "default": 4.6}
    ),
    variability=MappingProxyType(
        {"footway": 0.08, "steps": 0.15, "service": 0.1, "default": 0.1}
    ),
    time_factors=MappingProxyType(
        {
            "morning_peak": MappingProxyType({"default": 1.0}),
            "day": MappingProxyType({"default": 1.0}),
            "evening_peak": MappingProxyType({"default": 1.0}),
            "off_peak": MappingProxyType({"default": 1.0}),
        }
    ),
    turn_penalty_minutes=0.03,
    traffic_light_penalty_minutes=0.2,
    reliability_weight=0.4,
    distance_weight_minutes_per_km=0.02,
)


@dataclass(frozen=True)
class RouteEstimate:
    candidate: RouteCandidate
    expected_minutes: float
    uncertainty_minutes: float
    score: float
    time_band: str


class ContextualRouteModel:
    """Score actual road alternatives instead of applying one mean speed."""

    def __init__(self, profiles: Mapping[str, ModeProfile] | None = None) -> None:
        self._profiles = dict(
            profiles or {"car": CAR_PROFILE, "pedestrian": PEDESTRIAN_PROFILE}
        )

    def estimate(
        self, candidate: RouteCandidate, mode: str, departure_at: datetime
    ) -> RouteEstimate:
        try:
            profile = self._profiles[mode]
        except KeyError as error:
            raise MatrixBuildError(
                f"no contextual profile configured for mode {mode!r}"
            ) from error

        band = time_band(departure_at)
        segment_minutes: list[float] = []
        segment_sigmas: list[float] = []
        for segment in candidate.segments:
            road_class = segment.road_class
            speed = profile.speeds_kmh.get(road_class, profile.speeds_kmh["default"])
            factor = self._factor(profile.time_factors.get(band, {}), road_class)
            factor *= profile.zone_factors.get(segment.zone, 1.0)
            minutes = segment.distance_km / speed * 60.0 * factor
            variability = profile.variability.get(
                road_class, profile.variability.get("default", 0.0)
            )
            segment_minutes.append(minutes)
            segment_sigmas.append(minutes * variability)

        expected = sum(segment_minutes)
        expected += candidate.turn_count * profile.turn_penalty_minutes
        expected += (
            candidate.traffic_light_count * profile.traffic_light_penalty_minutes
        )
        uncertainty = math.sqrt(sum(value * value for value in segment_sigmas))
        score = (
            expected
            + profile.reliability_weight * uncertainty
            + profile.distance_weight_minutes_per_km * candidate.distance_km
        )
        return RouteEstimate(candidate, expected, uncertainty, score, band)

    def choose(
        self,
        candidates: Sequence[RouteCandidate],
        mode: str,
        departure_at: datetime,
    ) -> tuple[RouteEstimate, tuple[RouteEstimate, ...]]:
        if not candidates:
            raise MatrixBuildError("routing provider returned no road alternatives")
        estimates = tuple(
            self.estimate(candidate, mode, departure_at) for candidate in candidates
        )
        ranked = tuple(
            sorted(
                estimates,
                key=lambda item: (
                    item.score,
                    item.expected_minutes,
                    item.candidate.distance_km,
                    item.candidate.id,
                ),
            )
        )
        return ranked[0], ranked

    @staticmethod
    def _factor(values: Mapping[str, float], road_class: str) -> float:
        factor = values.get(road_class, values.get("default", 1.0))
        if factor <= 0:
            raise MatrixBuildError("time factor must be positive")
        return factor


def time_band(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("departure_at must include a UTC offset")
    hour = value.hour
    if 7 <= hour < 10:
        return "morning_peak"
    if 10 <= hour < 17:
        return "day"
    if 17 <= hour < 20:
        return "evening_peak"
    return "off_peak"


@dataclass(frozen=True)
class MatrixBuild:
    travel: dict[str, Any]
    manifest: dict[str, Any]


class TravelMatrixBuilder:
    """Build every directed pair for every requested transport mode."""

    def __init__(
        self,
        provider: RouteProvider,
        model: ContextualRouteModel | None = None,
        *,
        alternatives: int = 3,
        max_workers: int = 8,
    ) -> None:
        if alternatives < 1:
            raise ValueError("alternatives must be positive")
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self._provider = provider
        self._model = model or ContextualRouteModel()
        self._alternatives = alternatives
        self._max_workers = max_workers

    def build(
        self,
        points: Sequence[GeoPoint],
        modes: Sequence[str],
        departure_at: datetime,
    ) -> MatrixBuild:
        point_ids = [point.id for point in points]
        if len(point_ids) != len(set(point_ids)):
            raise MatrixBuildError("location ids must be unique")
        clean_modes = sorted(set(modes))
        if not points or not clean_modes:
            raise MatrixBuildError("at least one point and one mode are required")
        # Validate timezone even for the degenerate one-point matrix.
        band = time_band(departure_at)

        jobs = [
            (mode, origin, destination)
            for mode in clean_modes
            for origin in points
            for destination in points
            if origin.id != destination.id
        ]
        selections: dict[
            tuple[str, str, str],
            tuple[RouteEstimate, tuple[RouteEstimate, ...]],
        ] = {}
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = {
                executor.submit(self._select, origin, destination, mode, departure_at): (
                    mode,
                    origin.id,
                    destination.id,
                )
                for mode, origin, destination in jobs
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    selections[key] = future.result()
                except Exception as error:  # collect all missing arcs before failing
                    failures.append(f"{key[1]} -> {key[2]} ({key[0]}): {error}")
        if failures:
            preview = "; ".join(sorted(failures)[:10])
            suffix = "" if len(failures) <= 10 else f"; and {len(failures) - 10} more"
            raise MatrixBuildError(f"matrix is incomplete: {preview}{suffix}")

        legs: list[dict[str, Any]] = []
        selection_rows: list[dict[str, Any]] = []
        for key in sorted(selections):
            mode, origin_id, destination_id = key
            selected, ranked = selections[key]
            legs.append(
                {
                    "from": origin_id,
                    "to": destination_id,
                    "minutes": (
                        0
                        if selected.candidate.distance_km == 0
                        else max(1, math.ceil(selected.expected_minutes))
                    ),
                    "distance_km": round(selected.candidate.distance_km, 3),
                    "mode": mode,
                }
            )
            selection_rows.append(
                {
                    "from": origin_id,
                    "to": destination_id,
                    "mode": mode,
                    "selected": selected.candidate.id,
                    "alternatives": [self._estimate_row(item) for item in ranked],
                }
            )

        digest_payload = json.dumps(
            legs, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        manifest = {
            "algorithm": "contextual-multiroute-v1",
            "provider": self._provider.name,
            "departure_at": departure_at.isoformat(),
            "time_band": band,
            "symmetric": False,
            "point_count": len(points),
            "mode_count": len(clean_modes),
            "leg_count": len(legs),
            "matrix_sha256": hashlib.sha256(digest_payload).hexdigest(),
            "selections": selection_rows,
        }
        return MatrixBuild({"symmetric": False, "legs": legs}, manifest)

    def _select(
        self,
        origin: GeoPoint,
        destination: GeoPoint,
        mode: str,
        departure_at: datetime,
    ) -> tuple[RouteEstimate, tuple[RouteEstimate, ...]]:
        candidates = self._provider.alternatives(
            origin, destination, mode, self._alternatives
        )
        return self._model.choose(candidates, mode, departure_at)

    @staticmethod
    def _estimate_row(item: RouteEstimate) -> dict[str, Any]:
        row = {
            "id": item.candidate.id,
            "distance_km": round(item.candidate.distance_km, 3),
            "expected_minutes": round(item.expected_minutes, 3),
            "uncertainty_minutes": round(item.uncertainty_minutes, 3),
            "score": round(item.score, 3),
        }
        if item.candidate.provider_duration_minutes is not None:
            row["provider_duration_minutes"] = round(
                item.candidate.provider_duration_minutes, 3
            )
        return row


class OsrmRouteProvider:
    """Fetch alternative road geometries from a controlled OSRM instance."""

    name = "osrm-route"

    def __init__(
        self,
        base_url: str,
        mode_profiles: Mapping[str, str],
        *,
        timeout_seconds: float = 15.0,
        user_agent: str = "BeelineTravelMatrix/1.0",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._mode_profiles = dict(mode_profiles)
        self._timeout = timeout_seconds
        self._user_agent = user_agent

    def alternatives(
        self, origin: GeoPoint, destination: GeoPoint, mode: str, limit: int
    ) -> Sequence[RouteCandidate]:
        try:
            profile = self._mode_profiles[mode]
        except KeyError as error:
            raise MatrixBuildError(
                f"no OSRM profile mapped for mode {mode!r}"
            ) from error
        coordinates = (
            f"{origin.longitude:.7f},{origin.latitude:.7f};"
            f"{destination.longitude:.7f},{destination.latitude:.7f}"
        )
        url = (
            f"{self._base_url}/route/v1/{quote(profile, safe='-_')}/"
            f"{coordinates}?alternatives={limit}&steps=true&overview=false"
        )
        request = Request(url, headers={"User-Agent": self._user_agent})
        with urlopen(request, timeout=self._timeout) as response:
            payload = json.load(response)
        if payload.get("code") != "Ok" or not payload.get("routes"):
            raise MatrixBuildError(payload.get("message", "OSRM found no route"))
        result = []
        for index, route in enumerate(payload["routes"][:limit]):
            segments = []
            turns = 0
            for leg in route.get("legs", []):
                for step in leg.get("steps", []):
                    distance_km = float(step.get("distance", 0)) / 1000.0
                    if distance_km <= 0:
                        continue
                    segments.append(
                        RouteSegment(
                            distance_km=distance_km,
                            road_class=_osrm_road_class(step, mode),
                        )
                    )
                    maneuver = step.get("maneuver", {}).get("type")
                    if maneuver not in {None, "depart", "arrive", "new name", "continue"}:
                        turns += 1
            if not segments:
                segments = [RouteSegment(float(route["distance"]) / 1000.0)]
            result.append(
                RouteCandidate(
                    id=f"osrm-{index}",
                    segments=tuple(segments),
                    turn_count=turns,
                    provider_duration_minutes=float(route["duration"]) / 60.0,
                )
            )
        return result


def _osrm_road_class(step: Mapping[str, Any], mode: str) -> str:
    if mode == "pedestrian":
        maneuver = str(step.get("maneuver", {}).get("type", "")).lower()
        name = str(step.get("name", "")).lower()
        if "step" in maneuver or "лест" in name:
            return "steps"
        return "footway"

    ref = str(step.get("ref", "")).upper()
    name = str(step.get("name", "")).lower()
    if re.search(r"(^|[;\s])[МM][-\s]?\d", ref):
        return "motorway"
    if re.search(r"(^|[;\s])[АA][-\s]?\d", ref):
        return "trunk"
    if re.search(r"(^|[;\s])[РPR][-\s]?\d", ref):
        return "primary"
    if any(token in name for token in ("проезд", "переулок", "тупик")):
        return "service"
    if any(token in name for token in ("шоссе", "проспект", "магистраль")):
        return "primary"
    if "улица" in name or "ул." in name:
        return "secondary"
    return "default"
