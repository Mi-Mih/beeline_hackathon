import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from io import BytesIO
from unittest.mock import patch
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse

from backend.web import (
    DEMO_INPUT,
    MAX_BODY_BYTES,
    ROUTE_CACHE,
    DispatcherHandler,
    normalize_route_points,
)
from backend.map_manager import MapManager, MapStore
from backend.travel_history import TravelHistoryStore
import backend.web as web_module
from backend.math_core import ContractError

from tests.helpers import sample_payload


class NormalizeRoutePointsTest(unittest.TestCase):
    def test_normalizes_and_rejects_route_points(self) -> None:
        self.assertEqual(
            normalize_route_points("37.6,55.7;37.7,55.8"),
            "37.600000,55.700000;37.700000,55.800000",
        )
        self.assertEqual(
            normalize_route_points("-180,-90;180,90"),
            "-180.000000,-90.000000;180.000000,90.000000",
        )
        cases = [
            "200,55.7;37.7,55.8",
            "37.7,91;37.7,55.8",
            "37.6;37.7,55.8",
            "only-one",
            ";".join(["37.6,55.7"] * 26),
        ]
        for points in cases:
            with self.subTest(points):
                with self.assertRaises(ContractError):
                    normalize_route_points(points)


class DispatcherApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), DispatcherHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        ROUTE_CACHE.clear()
        self.routing_matrix_mode = web_module.ROUTING_MATRIX_MODE
        web_module.ROUTING_MATRIX_MODE = "input"
        self.temporary = tempfile.TemporaryDirectory()
        web_module._MAP_MANAGER = MapManager(
            MapStore(Path(self.temporary.name) / "routing.sqlite3")
        )
        web_module._TRAVEL_HISTORY_STORE = TravelHistoryStore(
            Path(self.temporary.name) / "routing.sqlite3"
        )

    def tearDown(self) -> None:
        ROUTE_CACHE.clear()
        web_module.ROUTING_MATRIX_MODE = self.routing_matrix_mode
        web_module._MAP_MANAGER = None
        web_module._TRAVEL_HISTORY_STORE = None
        self.temporary.cleanup()

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ):
        parsed = urlparse(self.base)
        connection = http.client.HTTPConnection(
            parsed.hostname, parsed.port, timeout=10
        )
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            raw = response.read()
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                payload = {}
            return response.status, payload
        finally:
            connection.close()

    def test_health_and_demo(self) -> None:
        status, payload = self._request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "ok", "contract": "plan-output/1.0"})

        status, payload = self._request("/api/demo")
        self.assertEqual(status, 200)
        self.assertEqual(payload["plan_id"], "demo-2026-08-17")
        self.assertEqual(payload, json.loads(DEMO_INPUT.read_text(encoding="utf-8")))

    def test_plan_accepts_sample_and_rejects_bad_bodies(self) -> None:
        raw = json.dumps(sample_payload()).encode("utf-8")
        status, payload = self._request(
            "/api/plan",
            method="POST",
            body=raw,
            headers={"Content-Type": "application/json", "Content-Length": str(len(raw))},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["schema_version"], "1.0")
        self.assertEqual(payload["metrics"]["assigned_count"], 3)

        status, payload = self._request(
            "/api/plan",
            method="POST",
            body=b"[]",
            headers={"Content-Length": "2"},
        )
        self.assertEqual(status, 400)
        self.assertIn("JSON object", payload["error"])

        status, payload = self._request(
            "/api/plan",
            method="POST",
            body=b"{",
            headers={"Content-Length": "1"},
        )
        self.assertEqual(status, 400)

        status, payload = self._request(
            "/api/plan",
            method="POST",
            body=b"\xff\xfe",
            headers={"Content-Length": "2"},
        )
        self.assertEqual(status, 400)

        status, payload = self._request(
            "/api/plan",
            method="POST",
            body=b"{}",
            headers={"Content-Length": "0"},
        )
        self.assertEqual(status, 400)
        self.assertIn("2 MB", payload["error"])

        oversized = str(MAX_BODY_BYTES + 1)
        status, payload = self._request(
            "/api/plan",
            method="POST",
            body=b"{}",
            headers={"Content-Length": oversized},
        )
        self.assertEqual(status, 400)

        status, _payload = self._request(
            "/api/missing",
            method="POST",
            body=b"{}",
            headers={"Content-Length": "2"},
        )
        self.assertEqual(status, 404)

    def test_route_uses_provider_and_cache(self) -> None:
        geometry = {
            "code": "Ok",
            "routes": [
                {
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[37.6, 55.7], [37.7, 55.8]],
                    },
                    "distance": 1500.0,
                    "duration": 180.0,
                }
            ],
        }

        def fake_urlopen(_request, timeout=10):
            return BytesIO(json.dumps(geometry).encode("utf-8"))

        with patch("backend.web.urlopen", side_effect=fake_urlopen) as mocked:
            status, payload = self._request("/api/route?points=37.6,55.7;37.7,55.8")
            self.assertEqual(status, 200)
            self.assertEqual(payload["provider"], "osrm")
            self.assertEqual(payload["distance_km"], 1.5)
            self.assertEqual(payload["duration_seconds"], 180.0)
            self.assertEqual(payload["duration_minutes"], 3.0)
            status, again = self._request("/api/route?points=37.6,55.7;37.7,55.8")
            self.assertEqual(status, 200)
            self.assertEqual(again, payload)
            self.assertEqual(mocked.call_count, 1)
            self.assertIn("mode=car", mocked.call_args.args[0].full_url)

            status, pedestrian = self._request(
                "/api/route?mode=pedestrian&points=37.6,55.7;37.7,55.8"
            )
            self.assertEqual(status, 200)
            self.assertEqual(pedestrian["profile"], "pedestrian")
            self.assertEqual(mocked.call_count, 2)
            self.assertIn("mode=pedestrian", mocked.call_args.args[0].full_url)

            status, again = self._request(
                "/api/route?mode=pedestrian&points=37.6,55.7;37.7,55.8"
            )
            self.assertEqual(status, 200)
            self.assertEqual(again, pedestrian)
            self.assertEqual(mocked.call_count, 2)

        with patch(
            "backend.web.urlopen",
            return_value=BytesIO(json.dumps({"code": "NoRoute", "message": "none"}).encode()),
        ):
            ROUTE_CACHE.clear()
            status, payload = self._request("/api/route?points=37.6,55.7;37.7,55.8")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "none")

        with patch("backend.web.urlopen", side_effect=URLError("down")):
            status, payload = self._request("/api/route?points=37.6,55.7;37.7,55.8")
        self.assertEqual(status, 502)
        self.assertIn("unavailable", payload["error"])

        status, payload = self._request("/api/route")
        self.assertEqual(status, 400)
        self.assertIn("between 2 and 25 points", payload["error"])

        status, payload = self._request("/api/route?mode=&points=37.6,55.7;37.7,55.8")
        self.assertEqual(status, 400)
        self.assertIn("mode must not be empty", payload["error"])

    def test_map_status_and_schedule_api(self) -> None:
        status, payload = self._request("/api/map/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload["coverage"], "Москва")
        self.assertEqual(payload["state"], "uninitialized")

        raw = json.dumps(
            {"enabled": True, "interval_days": 7, "local_time": "02:30"}
        ).encode()
        status, payload = self._request(
            "/api/map/schedule",
            method="PUT",
            body=raw,
            headers={"Content-Length": str(len(raw)), "Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["schedule"]["enabled"])

        status, payload = self._request("/api/map/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"jobs": []})

    def test_imports_travel_observations_idempotently(self) -> None:
        body = (
            "technician_id,departed_at,arrived_at,vehicle_mode,osrm_seconds,"
            "origin_latitude,origin_longitude,destination_latitude,destination_longitude\n"
            "TECH-1,2026-09-24T09:00:00+03:00,2026-09-24T09:18:30+03:00,"
            "car,900,55.75,37.61,55.76,37.64\n"
        ).encode()
        path = "/api/travel-observations/import?filename=movements.csv"
        headers = {"Content-Type": "text/csv", "Content-Length": str(len(body))}
        status, payload = self._request(path, method="POST", body=body, headers=headers)
        self.assertEqual(status, 201)
        self.assertEqual(payload["inserted"], 1)
        self.assertEqual(payload["duplicates"], 0)
        self.assertEqual(payload["technicians"], ["TECH-1"])

        status, payload = self._request(path, method="POST", body=body, headers=headers)
        self.assertEqual(status, 201)
        self.assertEqual(payload["inserted"], 0)
        self.assertEqual(payload["duplicates"], 1)

        status, payload = self._request("/api/travel-observations/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload["observation_count"], 1)
        self.assertEqual(payload["technician_count"], 1)
        self.assertEqual(payload["technicians"][0]["comparable_count"], 1)
        self.assertEqual(payload["technicians"][0]["experimental_factor"], 1.233)
        self.assertEqual(payload["technicians"][0]["median_error_seconds"], 210)
        self.assertFalse(payload["technicians"][0]["calibration_ready"])

    def test_rejects_invalid_travel_observations(self) -> None:
        body = json.dumps(
            [
                {
                    "technician_id": "TECH-1",
                    "departed_at": "2026-09-24T10:00:00+03:00",
                    "arrived_at": "2026-09-24T09:00:00+03:00",
                }
            ]
        ).encode()
        status, payload = self._request(
            "/api/travel-observations/import?filename=bad.json",
            method="POST",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("arrived_at must be after", payload["error"])

    def test_plan_can_apply_engineer_travel_history(self) -> None:
        web_module._TRAVEL_HISTORY_STORE.import_rows(
            [
                {
                    "technician_id": "TECH-1",
                    "departed_at": "2026-09-24T09:00:00+03:00",
                    "arrived_at": "2026-09-24T09:20:00+03:00",
                    "osrm_seconds": 600,
                }
            ],
            source_filename="test.json",
        )
        body = sample_payload()
        body["use_travel_history"] = True
        raw = json.dumps(body).encode()

        status, payload = self._request(
            "/api/plan",
            method="POST",
            body=raw,
            headers={"Content-Type": "application/json", "Content-Length": str(len(raw))},
        )

        self.assertEqual(status, 200)
        calibration = payload["travel_time_calculation"]
        self.assertEqual(calibration["mode"], "engineer_history")
        self.assertEqual(calibration["applied"][0]["technician_id"], "TECH-1")
        self.assertEqual(calibration["applied"][0]["factor"], 2.0)
        self.assertFalse(calibration["applied"][0]["calibration_ready"])

    def test_plan_rejects_non_boolean_travel_history_option(self) -> None:
        body = sample_payload()
        body["use_travel_history"] = "yes"
        raw = json.dumps(body).encode()
        status, payload = self._request(
            "/api/plan",
            method="POST",
            body=raw,
            headers={"Content-Type": "application/json", "Content-Length": str(len(raw))},
        )
        self.assertEqual(status, 400)
        self.assertIn("must be boolean", payload["error"])


if __name__ == "__main__":
    unittest.main()
