"""Replace travel.legs with a contextual directed road matrix.

The input must already contain verified latitude/longitude values.  The command
is intended for a controlled OSRM deployment, not the public demo endpoint.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.travel_matrix import (
    GeoPoint,
    MatrixBuildError,
    OsrmRouteProvider,
    TravelMatrixBuilder,
)


def _mode_profiles(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        mode, separator, profile = value.partition("=")
        if not separator or not mode.strip() or not profile.strip():
            raise argparse.ArgumentTypeError(
                f"invalid mode mapping {value!r}; expected MODE=OSRM_PROFILE"
            )
        result[mode.strip()] = profile.strip()
    return result


def _departure(payload: dict[str, Any], explicit: str | None) -> datetime:
    if explicit is not None:
        value = explicit
    else:
        available = [
            row.get("available_from") for row in payload.get("technicians", [])
        ]
        if not available or any(not isinstance(item, str) for item in available):
            raise MatrixBuildError(
                "departure time is unavailable; pass --departure-at explicitly"
            )
        value = min(available)
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise MatrixBuildError("departure time must be ISO 8601") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise MatrixBuildError("departure time must include a UTC offset")
    return result


def _points(payload: dict[str, Any]) -> list[GeoPoint]:
    points = []
    for index, row in enumerate(payload.get("locations", [])):
        try:
            points.append(
                GeoPoint(
                    id=row["id"],
                    latitude=float(row["latitude"]),
                    longitude=float(row["longitude"]),
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise MatrixBuildError(
                f"locations[{index}] must have a valid id, latitude and longitude"
            ) from error
    return points


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="plan-input JSON with coordinates")
    parser.add_argument("output", type=Path, help="resulting plan-input JSON")
    parser.add_argument(
        "--manifest",
        type=Path,
        help="audit manifest (default: OUTPUT.travel-manifest.json)",
    )
    parser.add_argument("--osrm-url", default="http://127.0.0.1:5000")
    parser.add_argument(
        "--mode-profile",
        action="append",
        default=[],
        metavar="MODE=PROFILE",
        help="repeat for each transport mode (defaults: car=driving, pedestrian=foot)",
    )
    parser.add_argument("--departure-at", help="ISO 8601 context time")
    parser.add_argument("--alternatives", type=int, default=3)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    mappings = (
        _mode_profiles(args.mode_profile)
        if args.mode_profile
        else {"car": "driving", "pedestrian": "foot"}
    )
    modes = sorted(
        {
            row["vehicle"]
            for row in payload.get("technicians", [])
            if isinstance(row.get("vehicle"), str) and row["vehicle"].strip()
        }
    )
    missing = sorted(set(modes) - mappings.keys())
    if missing:
        raise MatrixBuildError(
            "missing --mode-profile mappings for: " + ", ".join(missing)
        )

    provider = OsrmRouteProvider(args.osrm_url, mappings)
    build = TravelMatrixBuilder(
        provider,
        alternatives=args.alternatives,
        max_workers=args.workers,
    ).build(_points(payload), modes, _departure(payload, args.departure_at))
    payload["travel"] = build.travel

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest_path = args.manifest or args.output.with_suffix(".travel-manifest.json")
    manifest_path.write_text(
        json.dumps(build.manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "manifest": str(manifest_path),
                "legs": len(build.travel["legs"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
