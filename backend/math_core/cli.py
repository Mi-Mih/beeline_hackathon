"""CLI for the planning core: JSON in, plan-output JSON out."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from backend.math_core.contract import ContractError, parse_problem
from backend.math_core.select import planner_for


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a technician route plan")
    parser.add_argument("input", help="UTF-8 plan-input JSON file, or - for stdin")
    parser.add_argument(
        "--reason",
        choices=["overnight", "replan"],
        help="planning trigger; both modes use OR-Tools Routing with local-search fallback",
    )
    args = parser.parse_args(argv)
    try:
        if args.input == "-":
            payload = json.load(sys.stdin)
        else:
            payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        problem = parse_problem(payload)
        if args.reason:
            problem = replace(problem, planning_reason=args.reason)
        result = planner_for(problem.planning_reason).plan(problem)
    except (ContractError, json.JSONDecodeError, OSError) as error:
        print(f"Input error: {error}", file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0
