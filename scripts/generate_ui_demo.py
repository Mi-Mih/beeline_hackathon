"""Reproducible, entirely synthetic UI load fixture; no customer data is read."""
from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def generate(crews: int = 30, orders: int = 300, seed: int = 20260927) -> dict:
    if not 1 <= crews <= 200 or not crews <= orders <= 3000:
        raise ValueError("Require 1..200 crews and crews..3000 orders")
    rng = random.Random(seed)
    locations = [{"id": "office", "address": "Тестовый склад (синтетические данные)", "latitude": 55.675, "longitude": 37.762}]
    for i in range(60):
        locations.append({"id": f"point-{i+1:03}", "address": f"Тестовая точка {i+1:03}, сектор {i // 10 + 1}",
                          "latitude": round(55.635 + rng.random() * .065, 6),
                          "longitude": round(37.710 + rng.random() * .09, 6)})
    first_names = ["Анна", "Борис", "Виктор", "Дарья", "Елена", "Иван", "Мария", "Павел", "Сергей", "Юлия"]
    technicians = [{"id": f"BRG-{i+1:03}", "name": f"Бригада {i+1:03} · {first_names[i % len(first_names)]}",
                   "start_location_id": "office", "available_from": "2026-09-27T09:00:00+03:00",
                   "shift_end": "2026-09-27T18:00:00+03:00", "skills": ["connection", "emergency"],
                   "vehicle": "car", "equipment": ["router", "repair-kit", "tv-box"]} for i in range(crews)]
    requests = []
    for i in range(orders):
        kind = ["connection", "local_repair", "equipment_order", "emergency"][i % 4]
        start = 9 if i < crews else rng.choice([9, 10, 11, 12, 13, 14])
        request = {"id": f"TEST-{i+1:04}", "location_id": locations[1 + i % 60]["id"], "work_type": kind,
                   "duration_minutes": {"connection": 70, "local_repair": 30, "equipment_order": 20, "emergency": 60}[kind],
                   "window": {"start": f"2026-09-27T{start:02}:00:00+03:00", "end": f"2026-09-27T{min(17, start+4):02}:00:00+03:00"},
                   "required_skills": ["emergency" if kind == "emergency" else "connection"],
                   "required_vehicle": "car", "required_equipment": [],
                   "priority": "urgent" if kind == "emergency" else "high" if kind == "connection" else "normal"}
        if i < crews:
            request["locked_technician_id"] = technicians[i]["id"]
        elif i % 29 == 0:
            request["required_skills"] = ["satellite"]  # Intentional infeasible cases.
        requests.append(request)
    legs = []
    for i, origin in enumerate(locations):
        for destination in locations[i + 1:]:
            km = math.hypot((origin["latitude"]-destination["latitude"])*111,
                            (origin["longitude"]-destination["longitude"])*63) * 1.3
            legs.append({"from": origin["id"], "to": destination["id"],
                         "minutes": max(3, math.ceil(km / 25 * 60)), "distance_km": round(km, 3)})
    return {"schema_version": "1.0", "plan_id": f"synthetic-ui-{crews}-{orders}-{seed}",
            "locations": locations, "technicians": technicians, "requests": requests,
            "travel": {"symmetric": True, "legs": legs}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crews", type=int, default=30)
    parser.add_argument("--orders", type=int, default=300)
    parser.add_argument("--output", type=Path, default=ROOT / "examples" / "synthetic-ui-30-300.json")
    args = parser.parse_args()
    payload = generate(args.crews, args.orders)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(payload['technicians'])} crews, {len(payload['requests'])} orders: {args.output}")
