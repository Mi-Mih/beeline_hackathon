"""Nominatim adapter: local by default, explicitly enabled public demo mode."""
from __future__ import annotations

import ipaddress
import json
import math
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class GeocodingError(Exception):
    pass


_public_lock = threading.Lock()


def geocoding_mode() -> str:
    mode = os.getenv("GEOCODING_MODE", "local")
    if mode not in ("local", "public_demo"):
        raise GeocodingError("GEOCODING_MODE: допустимы local или public_demo")
    return mode


def public_endpoint() -> str:
    # Deployment configuration, never a URL received from the browser.
    url = os.getenv("PUBLIC_GEOCODER_URL", "https://nominatim.openstreetmap.org/search")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise GeocodingError("PUBLIC_GEOCODER_URL должен быть HTTPS-адресом Search API")
    return url


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GeocodingError("Геокодер перенаправляет запрос. Перенаправления запрещены.")


def local_endpoint() -> str:
    url = os.getenv("LOCAL_GEOCODER_URL", "http://127.0.0.1:4190")
    parsed = urlsplit(url)
    host = parsed.hostname
    if host == "localhost":
        host = "127.0.0.1"
    try:
        allowed = ipaddress.ip_address(host or "").is_loopback
        port = parsed.port
    except ValueError:
        allowed, port = False, None
    if not allowed or parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise GeocodingError("LOCAL_GEOCODER_URL должен указывать на локальный адрес 127.0.0.1 или ::1. Публичные геокодеры запрещены.")
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    return urlunsplit((parsed.scheme, authority, parsed.path.rstrip("/") + "/search", "", ""))


def search_address(query: str) -> list[dict]:
    if not isinstance(query, str) or not 3 <= len(query.strip()) <= 300:
        raise ValueError("Введите адрес от 3 до 300 символов")
    if geocoding_mode() == "public_demo":
        return search_public_address(query.strip())
    return fetch_address(local_endpoint(), query.strip(), public=False)


def search_public_address(query: str) -> list[dict]:
    endpoint = public_endpoint()
    cache_path = Path(os.getenv("GEOCODER_CACHE_PATH", str(Path(__file__).resolve().parents[1] / ".runtime/geocoding.sqlite3")))
    if not _public_lock.acquire(blocking=False):
        raise GeocodingError("Другой адрес ещё ищется. Повторите через несколько секунд.")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(cache_path, timeout=1)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS results (provider TEXT, query TEXT, payload TEXT, created REAL, PRIMARY KEY(provider, query))")
            db.execute("CREATE TABLE IF NOT EXISTS throttle (id INTEGER PRIMARY KEY, next_at REAL)")
            now = time.time()
            key = " ".join(query.casefold().split())
            cached = db.execute("SELECT payload FROM results WHERE provider=? AND query=? AND created>?", (endpoint, key, now - 30 * 86400)).fetchone()
            if cached:
                return json.loads(cached[0])
            # Persistent global throttle covers every browser tab of this demo server.
            db.execute("BEGIN IMMEDIATE")
            next_at = db.execute("SELECT next_at FROM throttle WHERE id=1").fetchone()
            if next_at and now < next_at[0]:
                raise GeocodingError("Лимит бесплатного поиска. Подождите несколько секунд и повторите.")
            db.execute("INSERT OR REPLACE INTO throttle VALUES (1, ?)", (now + 1.1,))
            db.commit()
            try:
                result = fetch_address(endpoint, query, public=True)
            except GeocodingError:
                # Do not hammer an unavailable/throttled public service.
                db.execute("UPDATE throttle SET next_at=? WHERE id=1", (time.time() + 60,))
                db.commit()
                raise
            db.execute("INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?)", (endpoint, key, json.dumps(result, ensure_ascii=False), now))
            db.execute("DELETE FROM results WHERE rowid NOT IN (SELECT rowid FROM results ORDER BY created DESC LIMIT 1000)")
            db.commit()
            return result
    except (OSError, sqlite3.Error) as error:
        raise GeocodingError("Не удалось открыть кэш адресов. Поиск не выполнен; проверьте доступ к .runtime.") from error
    finally:
        _public_lock.release()


def fetch_address(endpoint: str, query: str, *, public: bool) -> list[dict]:
    # No environment proxy or redirects. The public provider is explicit and replaceable.
    opener = build_opener(ProxyHandler({}), NoRedirects())
    request = Request(endpoint + "?" + urlencode({"q": query, "format": "jsonv2", "limit": 8, "accept-language": "ru"}),
                      headers={"User-Agent": "SmenaDispatcherDemo/1.0 (+https://github.com/Mi-Mih/beeline_hack)", "Accept": "application/json"})
    try:
        with opener.open(request, timeout=6) as response:
            raw = response.read(256_001)
        if len(raw) > 256_000:
            raise ValueError("oversized geocoder response")
        payload = json.loads(raw)
        if not isinstance(payload, list):
            raise ValueError("invalid geocoder response")
        results = []
        for row in payload[:8]:
            latitude, longitude = float(row["lat"]), float(row["lon"])
            address = row["display_name"]
            if not math.isfinite(latitude) or not math.isfinite(longitude) or abs(latitude) > 90 or abs(longitude) > 180 or not isinstance(address, str) or not address.strip() or len(address) > 300:
                raise ValueError("invalid geocoder result")
            results.append({"address": address, "latitude": latitude, "longitude": longitude})
        return results
    except HTTPError as error:
        if public:
            raise GeocodingError("Бесплатный геокодер отклонил запрос. Повторите через минуту или укажите координаты вручную.") from error
        raise GeocodingError("Локальный геокодер отклонил запрос. Проверьте его настройки.") from error
    except (URLError, TimeoutError, OSError) as error:
        if public:
            raise GeocodingError("Бесплатный геокодер недоступен. Повторите через минуту или укажите координаты вручную.") from error
        raise GeocodingError("Локальный геокодер недоступен. Запустите Nominatim на порту 4190 или укажите координаты вручную. Адрес не отправлен во внешний сервис.") from error
    except (ValueError, KeyError, TypeError) as error:
        raise GeocodingError("Геокодер вернул некорректный ответ. Укажите координаты вручную.") from error
