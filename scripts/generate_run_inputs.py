"""Build reproducible plan-input fixtures from the hackathon CSV files.

The source data has requests, customer windows, addresses, and a control list of
technician names.  It does not contain coordinates, a road matrix, shifts, or
technician capabilities.  This generator keeps source-backed fields intact and
fills the missing planning fields with deterministic test assumptions described
in ``examples/generated-inputs/README.md``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data"
DEFAULT_OUTPUT = ROOT / "examples" / "generated-inputs"
MOSCOW_TZ = timezone(timedelta(hours=3))

TYPE_MAP = {
    "Подключение": ("connection", "high"),
    "Глобальная проблема": ("emergency", "urgent"),
    "Дозаказ": ("equipment_order", "normal"),
    "Локальная заявка": ("local_repair", "normal"),
}
SCENARIO_SIZES = {"small": 12, "medium": 32, "full": None}
TECHNICIAN_LIMITS = {"small": 4, "medium": 8, "full": None}


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="cp1251", newline="") as source:
        rows = list(csv.DictReader(source, delimiter=";"))
    return [
        row
        for row in rows
        if row.get("Заявка", "").strip().isdigit()
        and row.get("Начало", "").strip()
        and row.get("Окончание", "").strip()
    ]


def _office_address(path: Path) -> str:
    with path.open(encoding="cp1251", newline="") as source:
        rows = csv.reader(source, delimiter=";")
        next(rows, None)
        for row in rows:
            if row and row[0].strip().lower().startswith("адрес офиса"):
                return row[1].strip()
    raise ValueError(f"Office address not found in {path}")


def _parse_time(value: str) -> datetime:
    return datetime.strptime(value.strip(), "%d.%m.%Y %H:%M").replace(
        tzinfo=MOSCOW_TZ
    )


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _stable_number(*parts: str) -> int:
    payload = "\x1f".join(parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _ordered_rows(rows: list[dict[str, str]], region: str) -> list[dict[str, str]]:
    """Return a deterministic nested order with every available type up front."""

    buckets: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        buckets[row["Тип заявки BK"].strip()].append(row)
    for work_type, bucket in buckets.items():
        bucket.sort(
            key=lambda row: _stable_number(region, work_type, row["Заявка"].strip())
        )

    ordered: list[dict[str, str]] = []
    known_types = [source_type for source_type in TYPE_MAP if buckets[source_type]]
    for source_type in known_types:
        ordered.append(buckets[source_type].pop(0))
    remaining = [row for bucket in buckets.values() for row in bucket]
    remaining.sort(key=lambda row: _stable_number(region, row["Заявка"].strip()))
    return ordered + remaining


def _point(region: str, district: str, address: str) -> tuple[float, float]:
    """Create a synthetic point in kilometres, clustering equal districts."""

    district_seed = _stable_number(region, district)
    angle = math.radians(district_seed % 36000 / 100)
    radius = 3.0 + ((district_seed >> 16) % 1500) / 100
    center_x = radius * math.cos(angle)
    center_y = radius * math.sin(angle)

    address_seed = _stable_number(region, district, address)
    jitter_angle = math.radians(address_seed % 36000 / 100)
    jitter_radius = 0.2 + ((address_seed >> 16) % 130) / 100
    return (
        center_x + jitter_radius * math.cos(jitter_angle),
        center_y + jitter_radius * math.sin(jitter_angle),
    )


def _travel_legs(
    region: str, rows: list[dict[str, str]]
) -> list[dict[str, Any]]:
    points: dict[str, tuple[float, float]] = {"office": (0.0, 0.0)}
    for row in rows:
        request_id = row["Заявка"].strip()
        points[f"request-{request_id}"] = _point(
            region, row.get("Район", "").strip(), row.get("Адрес", "").strip()
        )

    location_ids = list(points)
    legs: list[dict[str, Any]] = []
    for origin_index, origin in enumerate(location_ids):
        origin_x, origin_y = points[origin]
        for destination in location_ids[origin_index + 1 :]:
            destination_x, destination_y = points[destination]
            straight_km = math.hypot(
                origin_x - destination_x, origin_y - destination_y
            )
            road_km = max(0.3, straight_km * 1.25)
            minutes = max(2, math.ceil(road_km / 25 * 60 + 3))
            legs.append(
                {
                    "from": origin,
                    "to": destination,
                    "minutes": minutes,
                    "distance_km": round(road_km, 2),
                }
            )
    return legs


def _technician_names(control_path: Path) -> list[str]:
    names = {
        row.get("Бригада", "").strip()
        for row in _read_rows(control_path)
        if row.get("Бригада", "").strip()
    }
    return sorted(names)


def _build_payload(
    *,
    region: str,
    rows: list[dict[str, str]],
    office_address: str,
    technician_names: list[str],
    durations: dict[str, int],
    scenario: str,
) -> dict[str, Any]:
    windows = [
        (_parse_time(row["Начало"]), _parse_time(row["Окончание"])) for row in rows
    ]
    earliest_start = min(start for start, _ in windows)
    latest_end = max(end for _, end in windows)
    available_from = earliest_start - timedelta(hours=1)
    shift_end = latest_end + timedelta(hours=2)

    technician_limit = TECHNICIAN_LIMITS[scenario]
    if technician_limit is not None:
        technician_names = technician_names[:technician_limit]

    locations = [{"id": "office", "address": office_address}]
    requests = []
    for row, (window_start, window_end) in zip(rows, windows, strict=True):
        source_type = row["Тип заявки BK"].strip()
        work_type, priority = TYPE_MAP[source_type]
        request_id = row["Заявка"].strip()
        location_id = f"request-{request_id}"
        locations.append(
            {"id": location_id, "address": row.get("Адрес", "").strip()}
        )
        requests.append(
            {
                "id": f"REQ-{request_id}",
                "location_id": location_id,
                "work_type": work_type,
                "duration_minutes": durations[work_type],
                "window": {"start": _iso(window_start), "end": _iso(window_end)},
                "required_skills": [work_type],
                "required_vehicle": None,
                "required_equipment": [],
                "priority": priority,
            }
        )

    all_skills = sorted(work_type for work_type, _ in TYPE_MAP.values())
    technicians = [
        {
            "id": f"TECH-{index:02d}",
            "name": name,
            "start_location_id": "office",
            "available_from": _iso(available_from),
            "shift_end": _iso(shift_end),
            "skills": all_skills,
            "vehicle": "car",
            "equipment": [],
        }
        for index, name in enumerate(technician_names, start=1)
    ]

    slug = region.lower().replace(" ", "-")
    return {
        "schema_version": "1.0",
        "plan_id": f"data-{slug}-{scenario}-2026-08-17",
        "planning_reason": "overnight",
        "locations": locations,
        "requests": requests,
        "technicians": technicians,
        "travel": {
            "symmetric": True,
            "legs": _travel_legs(region, rows),
        },
    }


def generate(data_dir: Path, output_dir: Path) -> list[dict[str, Any]]:
    normatives = json.loads(
        (ROOT / "config" / "work-normatives.json").read_text(encoding="utf-8")
    )
    durations = {
        work_type: item["duration_minutes"]
        for work_type, item in normatives["items"].items()
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []

    for synthetic_path in sorted(data_dir.glob("*Синтетические данные.csv")):
        region = synthetic_path.name.removesuffix(" Синтетические данные.csv")
        control_path = next(data_dir.glob(f"{region} Контрольное распределение*.csv"))
        ordered = _ordered_rows(_read_rows(synthetic_path), region)
        names = _technician_names(control_path)
        for scenario, size in SCENARIO_SIZES.items():
            selected = ordered if size is None else ordered[:size]
            payload = _build_payload(
                region=region,
                rows=selected,
                office_address=_office_address(synthetic_path),
                technician_names=names,
                durations=durations,
                scenario=scenario,
            )
            filename = f"{region.lower().replace(' ', '-')}-{scenario}.json"
            (output_dir / filename).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            manifest.append(
                {
                    "file": filename,
                    "region": region,
                    "scenario": scenario,
                    "request_count": len(payload["requests"]),
                    "technician_count": len(payload["technicians"]),
                    "travel_leg_count": len(payload["travel"]["legs"]),
                }
            )

    (output_dir / "manifest.json").write_text(
        json.dumps({"scenarios": manifest}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    manifest = generate(args.data_dir, args.output_dir)
    print(json.dumps({"scenarios": manifest}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
