import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.math_core.cli import main

from tests.helpers import ROOT, sample_payload


class CliTest(unittest.TestCase):
    def test_writes_plan_from_file(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([str(ROOT / "examples" / "sample-input.json")])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        plan = json.loads(stdout.getvalue())
        self.assertEqual(plan["plan_id"], "demo-2026-08-17")
        self.assertIn(plan["algorithm"]["id"], {"ortools-routing", "greedy-local-search"})
        if plan["algorithm"]["id"] == "greedy-local-search":
            self.assertTrue(plan["diagnostics"]["warnings"])
        self.assertEqual(plan["metrics"]["assigned_count"], 3)

    def test_reason_flag_selects_replan_solver(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout), redirect_stderr(StringIO()):
            code = main(
                [str(ROOT / "examples" / "sample-input.json"), "--reason", "replan"]
            )
        self.assertEqual(code, 0)
        plan = json.loads(stdout.getvalue())
        self.assertIn(plan["algorithm"]["id"], {"ortools-routing", "greedy-local-search"})
        if plan["algorithm"]["id"] == "greedy-local-search":
            self.assertTrue(plan["diagnostics"]["warnings"])

    def test_reads_stdin(self) -> None:
        stdout = StringIO()
        stdin = StringIO(json.dumps(sample_payload()))
        with patch("sys.stdin", stdin):
            with redirect_stdout(stdout), redirect_stderr(StringIO()):
                code = main(["-"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["plan_id"], "demo-2026-08-17")

    def test_input_errors_return_two(self) -> None:
        cases = {
            "missing_file": ["/no/such/plan.json"],
            "invalid_json": None,
            "contract": None,
        }
        with TemporaryDirectory() as folder:
            broken = Path(folder) / "broken.json"
            broken.write_text("{", encoding="utf-8")
            contract = Path(folder) / "contract.json"
            contract.write_text('{"schema_version": "nope"}', encoding="utf-8")
            cases["invalid_json"] = [str(broken)]
            cases["contract"] = [str(contract)]
            for name, argv in cases.items():
                with self.subTest(name):
                    stderr = StringIO()
                    with redirect_stdout(StringIO()), redirect_stderr(stderr):
                        code = main(argv)
                    self.assertEqual(code, 2)
                    self.assertTrue(stderr.getvalue().startswith("Input error:"))


if __name__ == "__main__":
    unittest.main()
