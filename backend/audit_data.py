"""Check that synthetic requests and control distributions describe the same cases."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


DATA = Path(__file__).resolve().parents[1] / "data"


def _valid_rows(path: Path) -> list[dict[str, str]]:
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
    return ""


def build_audit(data_dir: Path = DATA) -> dict[str, Any]:
    regions = []
    for synthetic_path in sorted(data_dir.glob("*Синтетические данные.csv")):
        region = synthetic_path.name.removesuffix(" Синтетические данные.csv")
        control_path = next(
            data_dir.glob(f"{region} Контрольное распределение*.csv")
        )
        requests = _valid_rows(synthetic_path)
        control = _valid_rows(control_path)
        request_types = Counter(row["Тип заявки BK"] for row in requests)
        control_types = Counter(row["Тип заявки BK"] for row in control)
        technicians = {row.get("Бригада", "").strip() for row in control}
        technicians.discard("")
        regions.append(
            {
                "region": region,
                "office_address": _office_address(synthetic_path),
                "request_count": len(requests),
                "control_count": len(control),
                "request_types": dict(sorted(request_types.items())),
                "control_technicians": len(technicians),
                "control_unassigned": sum(
                    not row.get("Бригада", "").strip() for row in control
                ),
                "same_cases": len(requests) == len(control)
                and request_types == control_types,
            }
        )
    return {
        "regions": regions,
        "totals": {
            "request_count": sum(row["request_count"] for row in regions),
            "control_technicians": sum(
                row["control_technicians"] for row in regions
            ),
            "control_unassigned": sum(row["control_unassigned"] for row in regions),
        },
    }


def main() -> int:
    result = build_audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["regions"] and all(row["same_cases"] for row in result["regions"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
