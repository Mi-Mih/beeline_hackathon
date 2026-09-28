"""Choose the production planner for a planning trigger."""

from __future__ import annotations

from backend.math_core.models import Planner
from backend.math_core.routing import RoutingPlanner


def planner_for(reason: str) -> Planner:
    """Use OR-Tools Routing for both supported planning modes.

    ``RoutingPlanner`` owns the local-search fallback for environments where
    OR-Tools cannot be imported or Routing does not return a feasible plan.
    The contract boundary validates the reason before this selector is called.
    """

    _ = reason
    return RoutingPlanner()
