import json
import unittest
from io import BytesIO
from unittest.mock import patch

from backend.demo_routing import demo_geometry, _cache
from backend.math_core import ContractError, parse_problem
from backend.web import normalize_route_points
from scripts.generate_ui_demo import generate


class DemoRoutingTest(unittest.TestCase):
    def setUp(self):
        _cache.clear()

    def test_fixture_is_repeatable_and_valid(self):
        fixture = generate()
        self.assertEqual(fixture, generate())
        self.assertEqual(len(fixture["technicians"]), 30)
        self.assertEqual(len(fixture["requests"]), 300)
        parse_problem(fixture)

    @patch("backend.demo_routing.urlopen")
    def test_unknown_coordinates_never_leave_server(self, upstream):
        with self.assertRaises(ContractError):
            demo_geometry(normalize_route_points("37.1,55.1;37.2,55.2"), "car", "synthetic")
        with self.assertRaises(ContractError):
            demo_geometry(normalize_route_points("37.1,55.1;37.2,55.2"), "car", "uploaded")
        upstream.assert_not_called()

    @patch("backend.demo_routing.time.sleep")
    @patch("backend.demo_routing.urlopen")
    def test_demo_returns_road_geometry_and_caches(self, upstream, sleep):
        payload = {"code": "Ok", "routes": [{"geometry": {"type": "LineString", "coordinates": [[37.762,55.675],[37.75,55.68],[37.7447,55.6814]]}, "distance": 2500, "duration": 300}]}
        upstream.return_value = BytesIO(json.dumps(payload).encode())
        points = normalize_route_points("37.762,55.675;37.7447,55.6814")
        result = demo_geometry(points, "car", "sample")
        self.assertEqual(len(result["geometry"]["coordinates"]), 3)
        self.assertTrue(result["synthetic_only"])
        self.assertEqual(demo_geometry(points, "car", "sample"), result)
        upstream.assert_called_once()
        self.assertTrue(upstream.call_args.args[0].full_url.startswith("https://router.project-osrm.org/"))


if __name__ == "__main__":
    unittest.main()
