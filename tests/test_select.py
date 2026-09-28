import unittest

from backend.math_core.routing import RoutingPlanner
from backend.math_core.select import planner_for


class PlannerForTest(unittest.TestCase):
    def test_all_triggers_use_routing(self) -> None:
        self.assertIsInstance(planner_for("overnight"), RoutingPlanner)
        self.assertIsInstance(planner_for("replan"), RoutingPlanner)
        self.assertIsInstance(planner_for("anything-else"), RoutingPlanner)


if __name__ == "__main__":
    unittest.main()
