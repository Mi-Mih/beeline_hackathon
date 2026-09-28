from __future__ import annotations

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from backend.routing_gateway import (
    OsrmTableClient,
    RoutingError,
    RoutingPoint,
    client_from_env,
    travel_from_gateway,
)
from tests.helpers import sample_payload


class OsrmTableClientTest(unittest.TestCase):
    def test_health_checks_both_routing_profiles(self) -> None:
        response = {
            "code": "Ok",
            "durations": [[0, 10], [11, 0]],
            "distances": [[0, 100], [110, 0]],
        }
        client = OsrmTableClient({"car": "http://car", "pedestrian": "http://foot"})
        with patch("backend.routing_gateway.urlopen", side_effect=lambda *_args, **_kwargs: BytesIO(json.dumps(response).encode())) as get:
            result = client.health()
        self.assertEqual(result["modes"], ["car", "pedestrian"])
        self.assertEqual(get.call_count, 2)
        self.assertIn(";", get.call_args_list[0].args[0].full_url)

    def test_manifest_version_takes_priority_over_stale_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text('{"version":"active-v2"}', encoding="utf-8")
            with patch.dict("os.environ", {"ROUTING_MANIFEST_PATH": str(manifest), "ROUTING_GRAPH_VERSION": "old-v1"}):
                self.assertEqual(client_from_env().graph_version, "active-v2")

    def test_builds_complete_directed_matrix(self) -> None:
        response = {
            "code": "Ok",
            "durations": [[0, 61], [125, 0]],
            "distances": [[0, 1000], [1300, 0]],
        }
        client = OsrmTableClient({"car": "http://osrm"}, graph_version="v1")
        with patch("backend.routing_gateway.urlopen", return_value=BytesIO(json.dumps(response).encode())):
            result = client.matrix(
                [RoutingPoint("a", 37.6, 55.7), RoutingPoint("b", 37.7, 55.8)],
                "car",
            )
        self.assertEqual(result["graph_version"], "v1")
        self.assertEqual(result["durations_seconds"][1][0], 125.0)

    def test_rejects_null_and_unknown_mode(self) -> None:
        client = OsrmTableClient({"car": "http://osrm"})
        with self.assertRaisesRegex(RoutingError, "not configured"):
            client.matrix([RoutingPoint("a", 37.6, 55.7)], "pedestrian")
        response = {"code": "Ok", "durations": [[0, None], [1, 0]], "distances": [[0, 1], [1, 0]]}
        with patch("backend.routing_gateway.urlopen", return_value=BytesIO(json.dumps(response).encode())):
            with self.assertRaisesRegex(RoutingError, "incomplete"):
                client.matrix([RoutingPoint("a", 37.6, 55.7), RoutingPoint("b", 37.7, 55.8)], "car")

    def test_converts_gateway_units_without_averaging(self) -> None:
        payload = sample_payload()
        payload["technicians"] = [payload["technicians"][0]]
        size = len(payload["locations"])
        matrix = {
            "graph_version": "v1",
            "engine": "osrm",
            "algorithm": "mld",
            "profile_version": "car",
            "durations_seconds": [[0 if row == column else 61 + row for column in range(size)] for row in range(size)],
            "distances_meters": [[0 if row == column else 1500 + row for column in range(size)] for row in range(size)],
        }
        with patch("backend.routing_gateway.urlopen", return_value=BytesIO(json.dumps(matrix).encode())):
            travel, manifests = travel_from_gateway(payload, "http://gateway")
        self.assertFalse(travel["symmetric"])
        self.assertEqual(len(travel["legs"]), size * (size - 1))
        self.assertEqual(travel["legs"][0]["minutes"], 2)
        self.assertEqual(manifests[0]["graph_version"], "v1")


if __name__ == "__main__":
    unittest.main()
