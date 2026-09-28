import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.audit_data import DATA, _office_address, _valid_rows, build_audit, main


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    path.write_text(
        "\n".join(";".join(row) for row in rows) + "\n",
        encoding="cp1251",
    )


def _region_files(folder: Path, region: str, *, same: bool = True) -> None:
    synthetic = [
        ["Заявка", "Начало", "Окончание", "Тип заявки BK", "Бригада"],
        ["Адрес офиса", f"{region} office", "", "", ""],
        ["1", "09:00", "10:00", "Авария", ""],
        ["skip", "09:00", "10:00", "Авария", ""],
        ["2", "10:00", "11:00", "Подключение", ""],
        ["3", "", "11:00", "Подключение", ""],
    ]
    control = [
        ["Заявка", "Начало", "Окончание", "Тип заявки BK", "Бригада"],
        ["1", "09:00", "10:00", "Авария", "Бригада 1"],
        ["2", "10:00", "11:00", "Подключение", ""],
    ]
    if not same:
        control.append(["4", "11:00", "12:00", "Ремонт", "Бригада 2"])
    _write_csv(folder / f"{region} Синтетические данные.csv", synthetic)
    _write_csv(folder / f"{region} Контрольное распределение.csv", control)


class DataAuditUnitTest(unittest.TestCase):
    def test_valid_rows_and_office_address(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "sample.csv"
            _write_csv(
                path,
                [
                    ["Заявка", "Начало", "Окончание"],
                    ["Адрес офиса", "ул. Тестовая, 1"],
                    ["abc", "09:00", "10:00"],
                    ["10", "", "10:00"],
                    ["11", "09:00", ""],
                    ["12", "09:00", "10:00"],
                ],
            )
            rows = _valid_rows(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Заявка"], "12")
            self.assertEqual(_office_address(path), "ул. Тестовая, 1")
            empty = Path(folder) / "empty.csv"
            _write_csv(empty, [["Заявка", "Начало", "Окончание"], ["1", "09:00", "10:00"]])
            self.assertEqual(_office_address(empty), "")

    def test_build_audit_aggregates_matching_regions(self) -> None:
        with TemporaryDirectory() as folder:
            data_dir = Path(folder)
            _region_files(data_dir, "Север")
            _region_files(data_dir, "Юг")
            result = build_audit(data_dir)
            self.assertEqual(result["totals"]["request_count"], 4)
            self.assertEqual(result["totals"]["control_technicians"], 2)
            self.assertEqual(result["totals"]["control_unassigned"], 2)
            self.assertTrue(all(region["same_cases"] for region in result["regions"]))
            self.assertEqual(
                {region["region"] for region in result["regions"]}, {"Север", "Юг"}
            )
            self.assertTrue(all(region["office_address"] for region in result["regions"]))

    def test_build_audit_detects_mismatched_control_set(self) -> None:
        with TemporaryDirectory() as folder:
            data_dir = Path(folder)
            _region_files(data_dir, "Север", same=False)
            result = build_audit(data_dir)
            self.assertFalse(result["regions"][0]["same_cases"])
            self.assertGreater(result["regions"][0]["control_count"], result["regions"][0]["request_count"])

    def test_main_return_codes(self) -> None:
        matching = {
            "regions": [{"same_cases": True}],
            "totals": {"request_count": 2},
        }
        stdout = StringIO()
        with patch("backend.audit_data.build_audit", return_value=matching):
            with redirect_stdout(stdout):
                self.assertEqual(main(), 0)
        self.assertEqual(json.loads(stdout.getvalue())["totals"]["request_count"], 2)

        mismatch = {
            "regions": [{"same_cases": False}],
            "totals": {"request_count": 2},
        }
        with patch("backend.audit_data.build_audit", return_value=mismatch):
            with redirect_stdout(StringIO()):
                self.assertEqual(main(), 1)

        empty = {"regions": [], "totals": {"request_count": 0}}
        with patch("backend.audit_data.build_audit", return_value=empty):
            with redirect_stdout(StringIO()):
                self.assertEqual(main(), 1)


class DataAuditFixtureTest(unittest.TestCase):
    @unittest.skipUnless(
        DATA.exists() and any(DATA.glob("*Синтетические данные.csv")),
        "hackathon CSV fixtures are not present",
    )
    def test_synthetic_and_control_sets_match(self) -> None:
        result = build_audit()
        self.assertEqual(result["totals"]["request_count"], 205)
        self.assertTrue(all(region["same_cases"] for region in result["regions"]))
        self.assertTrue(all(region["office_address"] for region in result["regions"]))


if __name__ == "__main__":
    unittest.main()
