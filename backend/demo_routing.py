"""Opt-in public road geometry, restricted to bundled synthetic coordinates.

Never a production fallback. A request cannot select an arbitrary upstream URL
or transmit coordinates outside the demo allowlist. No matrix calls are made.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from backend.math_core import ContractError

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = {"sample": ROOT / "examples/sample-input.json",
            "synthetic": ROOT / "examples/synthetic-ui-30-300.json"}
PROFILES = {"car": "https://router.project-osrm.org/route/v1/driving/",
            "pedestrian": "https://routing.openstreetmap.de/routed-foot/route/v1/driving/"}
_lock = threading.Lock()
_last_request = 0.0
_cache: dict[tuple[str, str, str], dict] = {}


def demo_geometry(points: str, mode: str, dataset: str) -> dict:
    """`points` must already have passed normalize_route_points."""
    global _last_request
    if dataset not in FIXTURES or mode not in PROFILES:
        raise ContractError("Public routing supports bundled demos only")
    fixture = json.loads(FIXTURES[dataset].read_text(encoding="utf-8"))
    allowed = {f"{point['longitude']:.6f},{point['latitude']:.6f}" for point in fixture["locations"]}
    if any(point not in allowed for point in points.split(";")):
        raise ContractError("Coordinates outside the synthetic demo: public routing refused")
    key = (dataset, mode, points)
    if not _lock.acquire(timeout=3):
        raise URLError("Demo routing is busy; retry shortly")
    try:
        if key in _cache:
            return _cache[key]
        # Public demo policy: at most one request per second, across profiles.
        time.sleep(max(0.0, 1.05 - (time.monotonic() - _last_request)))
        _last_request = time.monotonic()
        url = PROFILES[mode] + points + "?overview=full&geometries=geojson&steps=false"
        request = Request(url, headers={"User-Agent": "SmenaSyntheticDemo/1.0 (non-commercial UI test)"})
        with urlopen(request, timeout=8) as response:
            payload = json.load(response)
        if payload.get("code") != "Ok" or not payload.get("routes"):
            raise URLError("Public demo returned no road route")
        route = payload["routes"][0]
        result = {"provider": "osrm-public-demo", "profile": mode, "geometry": route["geometry"],
                  "distance_km": round(route["distance"] / 1000, 3),
                  "duration_seconds": round(route["duration"], 1), "synthetic_only": True}
        if len(_cache) >= 200:
            _cache.pop(next(iter(_cache)))
        _cache[key] = result
        return result
    finally:
        _lock.release()
