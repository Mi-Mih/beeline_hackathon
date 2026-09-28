import unittest

from backend.math_core.models import TravelLeg, TravelTable


class TravelTableTest(unittest.TestCase):
    def setUp(self) -> None:
        self.table = TravelTable(
            {
                ("office", "client", None): TravelLeg(12, 4.0),
                ("office", "client", "pedestrian"): TravelLeg(25, 2.1),
                ("a", "b", None): TravelLeg(8, 1.5),
            },
            symmetric=True,
        )

    def test_same_location_is_zero(self) -> None:
        self.assertEqual(self.table.get("office", "office"), TravelLeg(0, 0.0))
        self.assertEqual(
            self.table.get("office", "office", "pedestrian"), TravelLeg(0, 0.0)
        )

    def test_prefers_mode_specific_leg_then_generic(self) -> None:
        self.assertEqual(
            self.table.get("office", "client", "pedestrian"), TravelLeg(25, 2.1)
        )
        self.assertEqual(self.table.get("office", "client", "car"), TravelLeg(12, 4.0))
        self.assertEqual(self.table.get("office", "client"), TravelLeg(12, 4.0))

    def test_symmetric_lookup_uses_reverse_leg(self) -> None:
        self.assertEqual(
            self.table.get("client", "office", "pedestrian"), TravelLeg(25, 2.1)
        )
        self.assertEqual(self.table.get("b", "a"), TravelLeg(8, 1.5))

    def test_asymmetric_table_does_not_reverse(self) -> None:
        table = TravelTable(
            {("office", "client", None): TravelLeg(12, 4.0)},
            symmetric=False,
        )
        self.assertEqual(table.get("office", "client"), TravelLeg(12, 4.0))
        self.assertIsNone(table.get("client", "office"))

    def test_missing_leg_returns_none(self) -> None:
        self.assertIsNone(self.table.get("office", "unknown"))


if __name__ == "__main__":
    unittest.main()
