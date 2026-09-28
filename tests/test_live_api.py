"""Live-demo API checks without opening map/history databases or external services."""
import copy
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from backend.web import DispatcherHandler, DEMO_INPUT
from backend.geocoding import GeocodingError


class LiveDemoApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), DispatcherHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, path, body=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=15)
        try:
            connection.request("POST" if body else "GET", path,
                               body=json.dumps(body) if body else None,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_config_exposes_mode_and_normatives(self):
        for mode in ("input", "gateway"):
            with patch("backend.web.ROUTING_MATRIX_MODE", mode):
                status, config = self.request("/api/planning-config")
                self.assertEqual(status, 200)
                self.assertEqual(config["matrix_mode"], mode)
                self.assertEqual(config["normatives"]["emergency"]["duration_minutes"], 80)

    def test_geocode_api_contract_and_errors(self):
        point = {"address": "Тестовый адрес", "latitude": 55.7, "longitude": 37.7}
        with patch("backend.web.search_address", return_value=[point]) as search:
            status, result = self.request("/api/geocode", {"query": "Тестовый адрес"})
            self.assertEqual(status, 200)
            self.assertEqual(result, {"source": "local", "results": [point]})
            search.assert_called_once_with("Тестовый адрес")
        with patch("backend.web.search_address", side_effect=GeocodingError("Локальный сервис недоступен")):
            status, result = self.request("/api/geocode", {"query": "Тестовый адрес"})
            self.assertEqual(status, 503)
            self.assertIn("Локальный", result["error"])
        status, _ = self.request("/api/geocode", {"query": ""})
        self.assertEqual(status, 400)

    def test_manual_request_replans_through_unchanged_contract(self):
        source = json.loads(DEMO_INPUT.read_text(encoding="utf-8"))
        with patch("backend.web.ROUTING_MATRIX_MODE", "input"):
            status, before = self.request("/api/plan", source)
            self.assertEqual(status, 200)
            payload = copy.deepcopy(source)
            request = copy.deepcopy(source["requests"][1])
            request.update(id="LIVE-1", location_id="client-d", duration_minutes=30,
                           window={"start": "2026-08-17T09:00:00+03:00",
                                   "end": "2026-08-17T10:00:00+03:00"})
            payload["requests"].append(request)
            payload["planning_reason"] = "replan"
            payload["replan_context"] = {
                "event_at": "2026-08-17T09:00:00+03:00", "new_request_ids": ["LIVE-1"],
                "previous_routes": [{"technician_id": route["technician_id"],
                                     "request_ids": [stop["request_id"] for stop in route["stops"]]}
                                    for route in before["routes"]], "active_work": []}
            status, after = self.request("/api/plan", payload)
            self.assertEqual(status, 200)
            self.assertEqual(after["schema_version"], before["schema_version"])
            self.assertEqual(after["metrics"]["request_count"], 5)
            assignment = next(route for route in after["routes"]
                              if any(stop["request_id"] == "LIVE-1" for stop in route["stops"]))
            self.assertEqual(assignment["technician_id"], "TECH-2")
            old = next(stop for route in before["routes"] for stop in route["stops"] if stop["request_id"] == "REQ-2")
            new = next(stop for route in after["routes"] for stop in route["stops"] if stop["request_id"] == "REQ-2")
            self.assertNotEqual(old["service_start_at"], new["service_start_at"])
