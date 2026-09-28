#!/usr/bin/env python3
"""Build and activate an immutable Moscow OSRM dataset.

External programs are invoked as argument arrays. No uploaded/user value is
interpolated into a shell command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SOURCE_URL = "https://download.geofabrik.de/russia/central-fed-district-latest.osm.pbf"
MOSCOW_RELATION = "r102269"


def run(
    arguments: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> None:
    subprocess.run(arguments, cwd=cwd, env=env, check=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(target: Path) -> None:
    request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "BeelineMapBuilder/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)


def osrm(image: str, data: Path, *arguments: str) -> None:
    run([
        "docker", "run", "--rm", "-t", "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{data.resolve()}:/data",
        image,
        *arguments,
    ])


def osmium(image: str, data: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    command = ["docker", "run", "--rm", "-v", f"{data.resolve()}:/data", image, *arguments]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        print(result.stdout, end="")
        print(result.stderr, end="")
        raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)
    return result


def source_timestamp(image: str, directory: Path, filename: str) -> str | None:
    result = osmium(
        image,
        directory,
        "fileinfo",
        "-g",
        "header.option.osmosis_replication_timestamp",
        f"/data/{filename}",
    )
    value = result.stdout.strip()
    return value or None


def record_activation(database: Path, version: str, timestamp: str | None, manifest: dict) -> None:
    activated = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO map_versions(id,source_kind,source_timestamp,created_at,activated_at,status,manifest_json) VALUES(?,?,?,?,?,?,?)",
            (version, manifest["source_kind"], timestamp, manifest["created_at"], activated, "active", json.dumps(manifest)),
        )
        connection.execute(
            "INSERT INTO settings(key,value) VALUES('active_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (version,),
        )


def canary_points(root: Path) -> list[dict[str, object]]:
    points: dict[tuple[float, float], dict[str, object]] = {}
    if not root.exists():
        return []
    for path in root.rglob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for location in payload.get("locations", []) if isinstance(payload, dict) else []:
            latitude = location.get("latitude")
            longitude = location.get("longitude")
            if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
                key = (float(longitude), float(latitude))
                points[key] = {"id": str(location.get("id", len(points))), "longitude": key[0], "latitude": key[1]}
    return list(points.values())


def select_moscow_polygon(exported: Path, target: Path) -> None:
    payload = json.loads(exported.read_text(encoding="utf-8"))
    features = payload.get("features", []) if isinstance(payload, dict) else []
    matches = [
        feature
        for feature in features
        if isinstance(feature, dict)
        and isinstance(feature.get("properties"), dict)
        and feature["properties"].get("ISO3166-2") == "RU-MOW"
        and feature.get("geometry", {}).get("type") in {"Polygon", "MultiPolygon"}
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one RU-MOW polygon, found {len(matches)}")
    target.write_text(json.dumps(matches[0], ensure_ascii=False), encoding="utf-8")


def verify_gateway(points: list[dict[str, object]], modes: list[str], maximum_snap: float) -> None:
    if not points:
        raise RuntimeError("no geocoded object coordinates were found for canary validation")
    gateway_url = os.getenv("ROUTING_GATEWAY_URL", "http://127.0.0.1:4180").rstrip("/")
    last_error: Exception | None = None
    for _ in range(60):
        try:
            for mode in modes:
                body = json.dumps({"coverage_id": "moscow", "mode": mode, "coordinates": points}).encode()
                request = urllib.request.Request(
                    f"{gateway_url}/internal/routing/v1/matrix",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    result = json.load(response)
                snaps = result.get("snap_distances_meters", [])
                if len(snaps) != len(points) or any(value is None or value > maximum_snap for value in snaps):
                    raise RuntimeError(f"{mode} has object coordinates farther than {maximum_snap:g} m from its graph: {snaps}")
            return
        except Exception as error:  # runtime may still be starting
            last_error = error
            time.sleep(1)
    raise RuntimeError(f"routing canary failed: {last_error}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--root", type=Path, default=Path(os.getenv("ROUTING_DATA_ROOT", "/srv/beeline-routing")))
    parser.add_argument("--image", default=os.getenv("OSRM_IMAGE", "ghcr.io/project-osrm/osrm-backend:26.9.0-debian@sha256:8a1b1bc938412f15f9b5b32d794c4ec6bf4a85dfbbabfa0a014b70b187edb53b"))
    parser.add_argument("--osmium-image", default=os.getenv("OSMIUM_IMAGE", "beeline-osmium:local"))
    parser.add_argument("--compose-file", type=Path, default=Path("deploy/routing/compose.yml"))
    parser.add_argument("--with-foot", action="store_true", default=os.getenv("ROUTING_WITH_FOOT") == "1")
    parser.add_argument("--canary-root", type=Path, default=Path("examples"))
    parser.add_argument("--max-snap-meters", type=float, default=float(os.getenv("ROUTING_MAX_SNAP_METERS", "1000")))
    args = parser.parse_args()

    root = args.root.resolve()
    sources = root / "sources"
    versions = root / "versions"
    sources.mkdir(parents=True, exist_ok=True)
    versions.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc).isoformat()
    version = f"moscow-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{args.job_id[:8]}"
    final = versions / version

    with tempfile.TemporaryDirectory(prefix="moscow-build-", dir=root) as temporary:
        work = Path(temporary)
        source = work / "source.osm.pbf"
        source_kind = "upload" if args.source else "managed_download"
        if args.source:
            shutil.copyfile(args.source, source)
        else:
            cached_source = sources / "central-fed-district-latest.osm.pbf"
            if not cached_source.exists():
                partial = sources / ".central-fed-district-latest.osm.pbf.part"
                download(partial)
                partial.replace(cached_source)
            os.link(cached_source, source)
        osmium(args.osmium_image, work, "fileinfo", "/data/source.osm.pbf")

        configured_polygon = root / "config" / "moscow.geojson"
        polygon = work / "moscow.geojson"
        if configured_polygon.exists():
            shutil.copyfile(configured_polygon, polygon)
        else:
            boundary = work / "moscow-boundary.osm.pbf"
            exported = work / "moscow-export.geojson"
            osmium(args.osmium_image, work, "getid", "-r", "/data/source.osm.pbf", MOSCOW_RELATION, "-o", "/data/moscow-boundary.osm.pbf", "--overwrite")
            osmium(args.osmium_image, work, "export", "/data/moscow-boundary.osm.pbf", "--geometry-types=polygon", "-o", "/data/moscow-export.geojson", "--overwrite")
            select_moscow_polygon(exported, polygon)

        clipped = work / "moscow.osm.pbf"
        osmium(args.osmium_image, work, "extract", "-p", "/data/moscow.geojson", "/data/source.osm.pbf", "-o", "/data/moscow.osm.pbf", "--overwrite", "--strategy", "complete_ways")
        osmium(args.osmium_image, work, "fileinfo", "/data/moscow.osm.pbf")

        modes = [("car", "/opt/car.lua")]
        if args.with_foot:
            modes.append(("foot", "/opt/foot.lua"))
        for mode, profile in modes:
            mode_dir = work / mode
            mode_dir.mkdir()
            shutil.copyfile(clipped, mode_dir / "moscow.osm.pbf")
            osrm(args.image, mode_dir, "osrm-extract", "-p", profile, "/data/moscow.osm.pbf")
            osrm(args.image, mode_dir, "osrm-partition", "/data/moscow.osrm")
            osrm(args.image, mode_dir, "osrm-customize", "/data/moscow.osrm")
            osrm(args.image, mode_dir, "osrm-routed", "--algorithm", "mld", "--trial", "true", "/data/moscow.osrm")
            (mode_dir / "moscow.osm.pbf").unlink()

        timestamp = source_timestamp(args.osmium_image, work, "source.osm.pbf")
        manifest = {
            "version": version,
            "job_id": args.job_id,
            "created_at": created_at,
            "source_kind": source_kind,
            "source_url": None if args.source else SOURCE_URL,
            "source_timestamp": timestamp,
            "source_sha256": sha256(source),
            "clipped_sha256": sha256(clipped),
            "polygon_sha256": sha256(polygon),
            "engine": "osrm",
            "algorithm": "mld",
            "image": args.image,
            "modes": ["pedestrian" if mode == "foot" else mode for mode, _ in modes],
        }
        (work / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.copytree(work, final)

    active = root / "active"
    previous = root / "previous"
    if active.is_symlink():
        old_target = os.readlink(active)
        replacement = root / ".previous-new"
        replacement.unlink(missing_ok=True)
        replacement.symlink_to(old_target)
        replacement.replace(previous)
    replacement = root / ".active-new"
    replacement.unlink(missing_ok=True)
    replacement.symlink_to(final)
    replacement.replace(active)

    environment = {**os.environ, "ROUTING_DATA_ROOT": str(root), "ROUTING_GRAPH_VERSION": version}
    if args.with_foot:
        environment["OSRM_FOOT_URL"] = "http://osrm-foot:5000"
    compose = ["docker", "compose", "-f", str(args.compose_file)]
    if args.with_foot:
        compose.extend(("--profile", "foot"))
    compose.extend(("up", "-d", "--force-recreate", "osrm-car"))
    if args.with_foot:
        compose.append("osrm-foot")
    compose.append("routing-gateway")
    try:
        run(compose, env=environment)
        verify_gateway(
            canary_points(args.canary_root),
            ["car", "pedestrian"] if args.with_foot else ["car"],
            args.max_snap_meters,
        )
    except Exception:
        if previous.is_symlink():
            restore = root / ".active-restore"
            restore.unlink(missing_ok=True)
            restore.symlink_to(os.readlink(previous))
            restore.replace(active)
            run(compose, env={**environment, "ROUTING_GRAPH_VERSION": "rollback"})
        raise
    database = Path(os.getenv("ROUTING_STATE_DB", root / "state" / "routing.sqlite3"))
    record_activation(database, version, timestamp, manifest)


if __name__ == "__main__":
    main()
