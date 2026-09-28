"""Stable routing contract in front of one or more private OSRM runtimes."""

from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen


class RoutingError(RuntimeError):
    """A complete, trustworthy routing response could not be produced."""


@dataclass(frozen=True)
class RoutingPoint:
    id: str
    longitude: float
    latitude: float

    def __post_init__(self) -> None:
        if not self.id or not -180 <= self.longitude <= 180 or not -90 <= self.latitude <= 90:
            raise RoutingError(f"invalid routing point: {self.id!r}")


class OsrmTableClient:
    """Call OSRM Table/Route and reject partial or malformed responses."""

    def __init__(
        self,
        mode_urls: Mapping[str, str],
        *,
        timeout_seconds: float = 20.0,
        graph_version: str = "unversioned",
        max_table_size: int = 100,
    ) -> None:
        self._mode_urls = {key: value.rstrip("/") for key, value in mode_urls.items()}
        self._timeout = timeout_seconds
        self.graph_version = graph_version
        self.max_table_size = max_table_size

    def matrix(self, points: Sequence[RoutingPoint], mode: str) -> dict[str, Any]:
        if not points:
            raise RoutingError("matrix requires at least one coordinate")
        if len(points) > self.max_table_size:
            raise RoutingError(
                f"matrix contains {len(points)} coordinates; configured OSRM limit is "
                f"{self.max_table_size}"
            )
        base_url = self._url_for(mode)
        coordinates = ";".join(
            f"{point.longitude:.7f},{point.latitude:.7f}" for point in points
        )
        url = (
            f"{base_url}/table/v1/driving/{coordinates}"
            "?annotations=duration,distance"
        )
        payload = self._get_json(url)
        if payload.get("code") != "Ok":
            raise RoutingError(payload.get("message", "OSRM matrix failed"))
        durations = self._square_matrix(payload.get("durations"), len(points), "durations")
        distances = self._square_matrix(payload.get("distances"), len(points), "distances")
        sources = payload.get("sources", [])
        snap_distances = []
        if isinstance(sources, list) and len(sources) == len(points):
            for source in sources:
                distance = source.get("distance") if isinstance(source, dict) else None
                snap_distances.append(float(distance) if isinstance(distance, (int, float)) else None)
        return {
            "graph_version": self.graph_version,
            "engine": "osrm",
            "algorithm": "mld",
            "profile_version": mode,
            "point_ids": [point.id for point in points],
            "durations_seconds": durations,
            "distances_meters": distances,
            "snap_distances_meters": snap_distances,
        }

    def route(self, points: str, mode: str = "car") -> dict[str, Any]:
        base_url = self._url_for(mode)
        url = (
            f"{base_url}/route/v1/driving/{quote(points, safe=',;-.')}"
            "?overview=full&geometries=geojson&steps=false"
        )
        payload = self._get_json(url)
        if payload.get("code") != "Ok" or not payload.get("routes"):
            raise RoutingError(payload.get("message", "OSRM found no route"))
        route = payload["routes"][0]
        return {
            "provider": "osrm",
            "graph_version": self.graph_version,
            "profile": mode,
            "geometry": route["geometry"],
            "distance_km": round(float(route["distance"]) / 1000, 3),
            "duration_seconds": round(float(route["duration"]), 1),
            "duration_minutes": round(float(route["duration"]) / 60, 1),
        }

    def health(self) -> dict[str, Any]:
        points = [
            RoutingPoint("moscow-center", 37.6176, 55.7558),
            RoutingPoint("moscow-east", 37.6428, 55.7642),
        ]
        for mode in self._mode_urls:
            self.matrix(points, mode)
        return {
            "status": "ok",
            "engine": "osrm",
            "algorithm": "mld",
            "graph_version": self.graph_version,
            "modes": sorted(self._mode_urls),
        }

    def _url_for(self, mode: str) -> str:
        try:
            return self._mode_urls[mode]
        except KeyError as error:
            raise RoutingError(f"routing mode is not configured: {mode}") from error

    def _get_json(self, url: str) -> dict[str, Any]:
        request = Request(url, headers={"User-Agent": "BeelineRoutingGateway/1.0"})
        try:
            with urlopen(request, timeout=self._timeout) as response:
                payload = json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RoutingError(f"OSRM is unavailable: {error}") from error
        if not isinstance(payload, dict):
            raise RoutingError("OSRM returned a non-object response")
        return payload

    @staticmethod
    def _square_matrix(value: Any, size: int, name: str) -> list[list[float]]:
        if not isinstance(value, list) or len(value) != size:
            raise RoutingError(f"OSRM returned invalid {name} dimensions")
        result: list[list[float]] = []
        for row in value:
            if not isinstance(row, list) or len(row) != size:
                raise RoutingError(f"OSRM returned invalid {name} dimensions")
            parsed = []
            for item in row:
                if not isinstance(item, (int, float)) or isinstance(item, bool) or not math.isfinite(item) or item < 0:
                    raise RoutingError(f"OSRM returned incomplete {name}")
                parsed.append(float(item))
            result.append(parsed)
        return result


def points_from_payload(payload: Mapping[str, Any]) -> list[RoutingPoint]:
    result = []
    for row in payload.get("locations", []):
        try:
            result.append(
                RoutingPoint(
                    id=str(row["id"]),
                    longitude=float(row["longitude"]),
                    latitude=float(row["latitude"]),
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RoutingError("every location must have valid latitude and longitude") from error
    if not result:
        raise RoutingError("payload has no locations")
    if len({point.id for point in result}) != len(result):
        raise RoutingError("location ids must be unique")
    return result


def modes_from_payload(payload: Mapping[str, Any]) -> list[str]:
    modes = sorted({str(row.get("vehicle", "car")) for row in payload.get("technicians", [])})
    return modes or ["car"]


def travel_from_gateway(payload: Mapping[str, Any], gateway_url: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Replace user-supplied travel with complete matrices from the gateway."""
    points = points_from_payload(payload)
    legs: list[dict[str, Any]] = []
    manifests = []
    for mode in modes_from_payload(payload):
        request_payload = json.dumps(
            {
                "coverage_id": "moscow",
                "mode": mode,
                "coordinates": [point.__dict__ for point in points],
                "sources": "all",
                "destinations": "all",
            }
        ).encode("utf-8")
        request = Request(
            f"{gateway_url.rstrip('/')}/internal/routing/v1/matrix",
            data=request_payload,
            headers={"Content-Type": "application/json", "User-Agent": "BeelinePlanner/1.0"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                matrix = json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RoutingError(f"routing gateway is unavailable: {error}") from error
        durations = matrix.get("durations_seconds")
        distances = matrix.get("distances_meters")
        OsrmTableClient._square_matrix(durations, len(points), "durations")
        OsrmTableClient._square_matrix(distances, len(points), "distances")
        for origin_index, origin in enumerate(points):
            for destination_index, destination in enumerate(points):
                if origin_index == destination_index:
                    continue
                seconds = float(durations[origin_index][destination_index])
                meters = float(distances[origin_index][destination_index])
                legs.append(
                    {
                        "from": origin.id,
                        "to": destination.id,
                        "mode": mode,
                        "minutes": 0 if meters == 0 else max(1, math.ceil(seconds / 60)),
                        "distance_km": round(meters / 1000, 3),
                    }
                )
        manifests.append(
            {key: matrix.get(key) for key in ("graph_version", "engine", "algorithm", "profile_version")}
        )
    return {"symmetric": False, "legs": legs}, manifests


def client_from_env() -> OsrmTableClient:
    urls = {"car": os.getenv("OSRM_CAR_URL", "http://127.0.0.1:5000")}
    foot = os.getenv("OSRM_FOOT_URL")
    if foot:
        urls["pedestrian"] = foot
    graph_version = None
    manifest_path = os.getenv("ROUTING_MANIFEST_PATH")
    if manifest_path:
        try:
            graph_version = json.loads(Path(manifest_path).read_text(encoding="utf-8"))["version"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return OsrmTableClient(
        urls,
        graph_version=graph_version or os.getenv("ROUTING_GRAPH_VERSION") or "unversioned",
        max_table_size=int(os.getenv("OSRM_MAX_TABLE_SIZE", "100")),
    )


class RoutingGatewayHandler(BaseHTTPRequestHandler):
    client: OsrmTableClient = client_from_env()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/health":
                self._json(self.client.health())
                return
            if parsed.path == "/internal/routing/v1/route":
                query = parse_qs(parsed.query)
                points = query.get("points", [""])[0]
                mode = query.get("mode", ["car"])[0]
                if len(points.split(";")) < 2:
                    raise RoutingError("route requires at least two points")
                self._json(self.client.route(points, mode))
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except RoutingError as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/internal/routing/v1/matrix":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2_000_000:
                raise RoutingError("matrix body must contain up to 2 MB of JSON")
            payload = json.loads(self.rfile.read(length))
            points = [
                RoutingPoint(str(row["id"]), float(row["longitude"]), float(row["latitude"]))
                for row in payload.get("coordinates", [])
            ]
            self._json(self.client.matrix(points, str(payload.get("mode", "car"))))
        except (RoutingError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the private OSRM routing gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4180)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), RoutingGatewayHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
