import json
import threading
import unittest
import tempfile
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from backend.geocoding import GeocodingError, local_endpoint, search_address


class LocalGeocoderTest(unittest.TestCase):
    def test_public_hosts_and_credentials_are_rejected(self):
        for url in ("https://nominatim.openstreetmap.org", "http://8.8.8.8", "http://user:pass@127.0.0.1", "http://127.0.0.1?url=external", "file:///tmp"):
            with self.subTest(url=url), patch.dict("os.environ", {"LOCAL_GEOCODER_URL": url}):
                with self.assertRaises(GeocodingError):
                    local_endpoint()

    def test_invalid_queries(self):
        for query in (None, 123, "", "ab", "x" * 301):
            with self.assertRaises(ValueError):
                search_address(query)

    def test_local_search_and_refused_redirect(self):
        seen = []
        class Stub(BaseHTTPRequestHandler):
            def do_GET(self):
                query = parse_qs(urlsplit(self.path).query)
                seen.append(query)
                if query["q"] == ["redirect"]:
                    self.send_response(302)
                    self.send_header("Location", "https://nominatim.openstreetmap.org/search")
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps([{"display_name":"Локальный адрес, 1", "lat":"55.7", "lon":"37.7"}]).encode())
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict("os.environ", {"LOCAL_GEOCODER_URL": f"http://127.0.0.1:{server.server_port}", "HTTP_PROXY":"http://invalid:9999"}):
                self.assertEqual(search_address("Локальный адрес, 1")[0]["latitude"], 55.7)
                self.assertEqual(seen[0]["q"], ["Локальный адрес, 1"])
                with self.assertRaisesRegex(GeocodingError, "Перенаправления запрещены"):
                    search_address("redirect")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class PublicDemoGeocoderTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict("os.environ", {"GEOCODING_MODE":"public_demo", "GEOCODER_CACHE_PATH":str(Path(self.temp.name) / "cache.sqlite3")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_cache_and_application_wide_rate_limit(self):
        result = [{"address":"Public test", "latitude":55.7, "longitude":37.7}]
        with patch("backend.geocoding.fetch_address", return_value=result) as fetch, patch("backend.geocoding.time.time", return_value=1000):
            self.assertEqual(search_address("Public test"), result)
            self.assertEqual(search_address(" public   TEST "), result)
            fetch.assert_called_once()
            with self.assertRaisesRegex(GeocodingError, "Лимит"):
                search_address("Another public test")
        with patch("backend.geocoding.fetch_address", return_value=[]) as fetch, patch("backend.geocoding.time.time", return_value=1002):
            self.assertEqual(search_address("Another public test"), [])
            self.assertEqual(search_address("Another public test"), [])
            fetch.assert_called_once()

    def test_unavailable_provider_backs_off_without_automatic_retry(self):
        with patch("backend.geocoding.fetch_address", side_effect=GeocodingError("offline")) as fetch, patch("backend.geocoding.time.time", return_value=1000):
            with self.assertRaisesRegex(GeocodingError, "offline"):
                search_address("Public test")
            with self.assertRaisesRegex(GeocodingError, "Лимит"):
                search_address("Public test")
            fetch.assert_called_once()

    def test_provider_is_replaceable_without_frontend_change(self):
        with patch.dict("os.environ", {"PUBLIC_GEOCODER_URL":"https://geocoder.example/search"}), patch("backend.geocoding.fetch_address", return_value=[]) as fetch:
            search_address("Public test")
            self.assertEqual(fetch.call_args.args[0], "https://geocoder.example/search")
