"""Local HTTP shell for the dispatcher prototype.

The browser only knows the stable JSON contract. Overnight vs replan
selects the planner; the HTTP response shape stays the same.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from backend.map_manager import MapManager, MapManagerError, manager_from_env
from backend.routing_gateway import RoutingError, travel_from_gateway
from backend.travel_history import (
    MAX_TRAVEL_IMPORT_BYTES,
    TravelHistoryError,
    TravelHistoryStore,
    parse_movement_upload,
    store_from_env,
)
from backend.math_core import ContractError, parse_problem
from backend.math_core.select import planner_for
from backend.demo_routing import demo_geometry
from backend.geocoding import GeocodingError, search_address, geocoding_mode

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "frontend"
DEMO_INPUT = REPO_ROOT / "examples" / "sample-input.json"
LARGE_DEMO_INPUT = REPO_ROOT / "examples" / "synthetic-ui-30-300.json"
MAX_BODY_BYTES = 2_000_000
ROUTING_GATEWAY_URL = os.getenv("ROUTING_GATEWAY_URL", "http://127.0.0.1:4180")
ROUTING_MATRIX_MODE = os.getenv("ROUTING_MATRIX_MODE", "gateway")
ROUTE_CACHE: dict[str, dict[str, Any]] = {}
_MAP_MANAGER: MapManager | None = None
_TRAVEL_HISTORY_STORE: TravelHistoryStore | None = None


def map_manager() -> MapManager:
    global _MAP_MANAGER
    if _MAP_MANAGER is None:
        _MAP_MANAGER = manager_from_env()
    return _MAP_MANAGER


def travel_history_store() -> TravelHistoryStore:
    global _TRAVEL_HISTORY_STORE
    if _TRAVEL_HISTORY_STORE is None:
        _TRAVEL_HISTORY_STORE = store_from_env()
    return _TRAVEL_HISTORY_STORE


def normalize_route_points(points: str) -> str:
    rows = points.split(";")
    if not 2 <= len(rows) <= 25:
        raise ContractError("route requires between 2 and 25 points")
    normalized = []
    for row in rows:
        try:
            longitude, latitude = (float(value) for value in row.split(","))
        except (TypeError, ValueError) as error:
            raise ContractError("route points must use longitude,latitude") from error
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise ContractError("route point is outside valid coordinate bounds")
        normalized.append(f"{longitude:.6f},{latitude:.6f}")
    return ";".join(normalized)


class DispatcherHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(FRONTEND), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        path = urlparse(self.path).path
        if path == "/api/health":
            self._json({"status": "ok", "contract": "plan-output/1.0"})
            return
        if path == "/api/demo":
            large = parse_qs(urlparse(self.path).query).get("size") == ["large"]
            self._json(json.loads((LARGE_DEMO_INPUT if large else DEMO_INPUT).read_text(encoding="utf-8")))
            return
        if path == "/api/planning-config":
            self._json({"matrix_mode": ROUTING_MATRIX_MODE,
                        "geocoding_mode": geocoding_mode(),
                        "normatives": json.loads((REPO_ROOT / "config/work-normatives.json").read_text(encoding="utf-8"))["items"]})
            return
        if path == "/api/map/status":
            self._json(map_manager().store.status())
            return
        if path == "/api/map/jobs":
            self._json({"jobs": map_manager().store.jobs()})
            return
        if path == "/api/travel-observations/status":
            self._json(travel_history_store().status())
            return
        if path == "/api/route":
            try:
                points = self._route_points()
                mode = parse_qs(urlparse(self.path).query, keep_blank_values=True).get("mode", ["car"])[0]
                if not mode:
                    raise ContractError("route mode must not be empty")
                demo = parse_qs(urlparse(self.path).query).get("demo", [None])[0]
                self._json(demo_geometry(points, mode, demo) if demo is not None else self._route_geometry(points, mode))
            except ContractError as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
                self._json(
                    {"error": f"routing provider is unavailable: {error}"},
                    HTTPStatus.BAD_GATEWAY,
                )
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        path = urlparse(self.path).path
        if path == "/api/geocode":
            try:
                payload = self._read_json(4096)
                self._json({"results": search_address(payload.get("query")), "source": geocoding_mode()})
            except (ContractError, ValueError, UnicodeDecodeError) as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            except GeocodingError as error:
                self._json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        if path == "/api/map/updates":
            self._json(map_manager().start(), HTTPStatus.ACCEPTED)
            return
        if path == "/api/map/uploads":
            try:
                upload = self._save_upload()
                self._json(
                    map_manager().start("manual_upload", source_path=upload),
                    HTTPStatus.ACCEPTED,
                )
            except (ContractError, OSError) as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/travel-observations/import":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_TRAVEL_IMPORT_BYTES:
                    raise TravelHistoryError("movement file must contain up to 5 MB")
                body = self.rfile.read(length)
                rows = parse_movement_upload(
                    body, self.headers.get("Content-Type", "")
                )
                filename = parse_qs(urlparse(self.path).query).get(
                    "filename", ["movement-upload"]
                )[0]
                filename = Path(filename).name[:255] or "movement-upload"
                self._json(
                    travel_history_store().import_rows(
                        rows, source_filename=filename
                    ),
                    HTTPStatus.CREATED,
                )
            except (TravelHistoryError, OSError, ValueError) as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if path != "/api/plan":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ContractError("request body must contain up to 2 MB of JSON")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ContractError("input must be a JSON object")
            use_travel_history = payload.get("use_travel_history", False)
            if not isinstance(use_travel_history, bool):
                raise ContractError("use_travel_history must be boolean")
            if ROUTING_MATRIX_MODE == "gateway":
                travel, routing_manifest = travel_from_gateway(payload, ROUTING_GATEWAY_URL)
                payload = {**payload, "travel": travel}
            problem = parse_problem(payload)
            applied_calibrations = []
            if use_travel_history:
                profiles = travel_history_store().calibration_profiles()
                technicians = []
                for technician in problem.technicians:
                    profile = profiles.get(technician.id)
                    if profile is None:
                        technicians.append(technician)
                        continue
                    technicians.append(
                        replace(
                            technician,
                            travel_time_factor=profile["factor"],
                        )
                    )
                    applied_calibrations.append(
                        {
                            "technician_id": technician.id,
                            **profile,
                        }
                    )
                problem = replace(problem, technicians=tuple(technicians))
            result = planner_for(problem.planning_reason).plan(problem)
            result["travel_time_calculation"] = {
                "mode": "engineer_history" if use_travel_history else "osrm",
                "applied": applied_calibrations,
            }
            if ROUTING_MATRIX_MODE == "gateway":
                result["routing"] = routing_manifest
        except (ContractError, RoutingError, json.JSONDecodeError, UnicodeDecodeError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        self._json(result)

    def do_PUT(self) -> None:  # noqa: N802 - stdlib callback name
        if urlparse(self.path).path != "/api/map/schedule":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_json(100_000)
            self._json({"schedule": map_manager().store.set_schedule(payload)})
        except (ContractError, MapManagerError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib callback name
        path = urlparse(self.path).path
        prefix = "/api/map/jobs/"
        if not path.startswith(prefix):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            self._json(map_manager().cancel(path[len(prefix):]))
        except (MapManagerError, OSError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def _route_points(self) -> str:
        points = parse_qs(urlparse(self.path).query).get("points", [""])[0]
        return normalize_route_points(points)

    @staticmethod
    def _route_geometry(points: str, mode: str) -> dict[str, Any]:
        active_version = map_manager().store.status().get("active_version") or "uninitialized"
        cache_key = f"{active_version}:{mode}:{points}"
        cached = ROUTE_CACHE.get(cache_key)
        if cached is not None:
            return cached
        url = (
            f"{ROUTING_GATEWAY_URL}/internal/routing/v1/route"
            f"?mode={quote(mode, safe='')}&points={quote(points, safe=',;-.')}"
        )
        request = UrlRequest(
            url,
            headers={"User-Agent": "SmenaDispatcherPrototype/1.0 (hackathon MVP)"},
        )
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
        if payload.get("provider") == "osrm" and payload.get("geometry"):
            result = dict(payload)
            if result.get("duration_seconds") is None and result.get("duration_minutes") is not None:
                result["duration_seconds"] = round(float(result["duration_minutes"]) * 60, 1)
        elif payload.get("code") == "Ok" and payload.get("routes"):
            route = payload["routes"][0]
            result = {
                "provider": "osrm",
                "profile": mode,
                "geometry": route["geometry"],
                "distance_km": round(route["distance"] / 1000, 3),
                "duration_seconds": round(route["duration"], 1),
                "duration_minutes": round(route["duration"] / 60, 1),
            }
        else:
            raise ContractError(payload.get("message", "routing provider found no route"))
        ROUTE_CACHE[cache_key] = result
        return result

    def _read_json(self, maximum: int) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > maximum:
            raise ContractError(f"request body must contain up to {maximum} bytes of JSON")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ContractError("input must be a JSON object")
        return payload

    def _save_upload(self) -> Path:
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/octet-stream":
            raise ContractError("map upload must use application/octet-stream")
        length = int(self.headers.get("Content-Length", "0"))
        maximum = int(os.getenv("ROUTING_MAX_UPLOAD_BYTES", str(2_000_000_000)))
        if length <= 0 or length > maximum:
            raise ContractError("uploaded PBF has an invalid size")
        upload_dir = Path(os.getenv("ROUTING_UPLOAD_DIR", ".runtime/uploads"))
        upload_dir.mkdir(parents=True, exist_ok=True)
        target = upload_dir / f"{os.urandom(16).hex()}.osm.pbf"
        remaining = length
        try:
            with target.open("xb") as output:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ContractError("uploaded PBF ended unexpectedly")
                    output.write(chunk)
                    remaining -= len(chunk)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return target

    def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the dispatcher prototype")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4173)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), DispatcherHandler)
    print(f"Dispatcher prototype: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
