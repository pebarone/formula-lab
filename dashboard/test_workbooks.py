"""CPU-only validation tests for isolated workbook storage and Excel requests."""

import tempfile
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import excel_engine, workbooks
from dashboard.excel_worker import (
    EXCEL_PROCESS_MEMORY_LIMIT, _add_spill_result, _assign_excel_job,
    _csv_numeric_value, _error_for, _snapshot, _write_csv, _set_formula, _formula_value, _local_formula,
)
from dashboard.config import read_json, write_json


class FormulaSafetyTests(unittest.TestCase):
    def test_builtin_dynamic_array_and_table_formulas_are_accepted(self):
        self.assertEqual(excel_engine._formula_text("=SUM(Table1[Sales])"), "=SUM(Table1[Sales])")
        self.assertEqual(excel_engine._formula_text("=FILTER(A2:A10,B2:B10=\"ok\")"), "=FILTER(A2:A10,B2:B10=\"ok\")")
        self.assertEqual(excel_engine._formula_text("=LET(x,1,x+1)"), "=LET(x,1,x+1)")
        self.assertEqual(excel_engine._formula_text("=XLOOKUP(A1,Table1[Key],Table1[Value])"), "=XLOOKUP(A1,Table1[Key],Table1[Value])")

    def test_external_workbook_references_are_rejected(self):
        formulas = (
            "=[1]Sheet1!A1",
            "='[Budget.xlsx]Sheet 1'!A1",
            "=SUM('[archive.xlsm]Sheet1'!A1:A2)",
            "=SUM('C:\\reports\\budget.xlsx'!A1:A2)",
            "=SUM('https://example.test/book.xlsx'!A1:A2)",
        )
        for formula in formulas:
            with self.subTest(formula=formula), self.assertRaises(ValueError):
                excel_engine._formula_text(formula)

    def test_dde_is_rejected(self):
        with self.assertRaises(ValueError):
            excel_engine._formula_text("=cmd|' /C calc'!A0")

    def test_unsafe_and_unknown_functions_are_rejected(self):
        for formula in ('=WEBSERVICE("https://example.test")', '=INDIRECT("A1")', '=MYUDF(A1)'):
            with self.subTest(formula=formula), self.assertRaises(ValueError):
                excel_engine._formula_text(formula)


class WorkbookStorageTests(unittest.TestCase):
    def test_workbook_ids_are_confined_to_storage_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            workbook_id, directory = workbooks.new_workbook_directory(temporary)
            self.assertEqual(directory.parent, (Path(temporary) / "workbooks").resolve())
            self.assertEqual(workbooks.workbook_directory(workbook_id, temporary), directory)
            with self.assertRaises(ValueError):
                workbooks.workbook_directory("..", temporary)

    def test_manifest_revision_rejects_boolean(self):
        with tempfile.TemporaryDirectory() as temporary:
            workbook_id, directory = workbooks.new_workbook_directory(temporary)
            (directory / "workbook.xlsx").write_bytes(b"placeholder")
            write_json(directory / "manifest.json", {"id": workbook_id, "name": "test", "revision": True})
            with self.assertRaises(ValueError):
                workbooks.read_manifest(workbook_id, temporary)

    def test_csv_import_preserves_declared_delimiter_and_limits(self):
        self.assertEqual(workbooks._csv_rows(b"a;b\n1;2\n", ".csv"), [["a", "b"], ["1", "2"]])
        self.assertEqual(workbooks._csv_rows(b"a\tb\n1\t2\n", ".tsv"), [["a", "b"], ["1", "2"]])
        content = ("x\n" * (workbooks.MAX_IMPORT_ROWS + 1)).encode("utf-8")
        with self.assertRaises(ValueError):
            workbooks._csv_rows(content, ".csv")


class RequestValidationTests(unittest.TestCase):
    def test_mutation_contract_and_coordinate_bounds(self):
        self.assertEqual(excel_engine._validate_changes([{"sheet": "Sheet1", "cell": "$a$1", "value": "ok"}]),
                         [{"sheet": "Sheet1", "cell": "A1", "value": "ok"}])
        for changes in (
            [{"sheet": "Sheet1", "cell": "XFE1", "value": 1}],
            [{"sheet": "Sheet1", "cell": "A1", "value": 1, "formula": "=1"}],
            [{"sheet": "Sheet1", "cell": "A1", "formula": "=cmd|' /C calc'!A0"}],
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                excel_engine._validate_changes(changes)

    def test_range_reads_are_bounded(self):
        for address in ("A1:XFD1048576", "XFE1", "A1048577", "B2:A1"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                excel_engine.read_range("00000000-0000-0000-0000-000000000000", "Sheet1", address, runs_root=".")

    def test_localized_formula_uses_excel_local_property(self):
        class Cell:
            Formula2 = Formula2Local = None
        english, portuguese = Cell(), Cell()
        _set_formula(english, "=SUM(A1:A2)")
        _set_formula(portuguese, "=SOMA(A1:A2)")
        self.assertEqual(english.Formula2, "=SUM(A1:A2)")
        self.assertEqual(portuguese.Formula2Local, "=SOMA(A1:A2)")

    def test_literal_equals_text_is_not_a_formula(self):
        class Cell:
            HasFormula = False
            Formula2 = Formula2Local = "=1+1"
        self.assertIsNone(_formula_value(Cell()))
        self.assertIsNone(_local_formula(Cell()))

    def test_excel_hresult_errors_are_returned_as_excel_errors(self):
        class Cell:
            Text = "-2146826281"

        self.assertEqual(_error_for(Cell(), -2146826281, "=1/0"), "#DIV/0!")
        spill_hresult = (0x800A0000 | 2045) - (1 << 32)
        self.assertEqual(_error_for(Cell(), spill_hresult, "=SEQUENCE(3)"), "#SPILL!")


class ExcelWorkerBoundsTests(unittest.TestCase):
    def test_excel_process_job_has_memory_limit_and_kills_on_close(self):
        job_handle, process_handle = object(), object()
        state = {"flags": 0, "assigned": None, "closed": []}
        win32job = types.ModuleType("win32job")
        win32job.JobObjectExtendedLimitInformation = 9
        win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
        win32job.CreateJobObject = lambda security, name: (state.update(job_name=name) or job_handle)
        win32job.QueryInformationJobObject = lambda *_: {
            "BasicLimitInformation": {"LimitFlags": 0}, "ProcessMemoryLimit": 0,
        }
        win32job.SetInformationJobObject = lambda _job, _kind, info: state.update(
            flags=info["BasicLimitInformation"]["LimitFlags"],
            memory=info["ProcessMemoryLimit"],
        )
        win32job.AssignProcessToJobObject = lambda job, process: state.update(assigned=(job, process))
        win32api = types.ModuleType("win32api")
        win32api.OpenProcess = lambda _rights, _inherit, pid: (state.update(pid=pid) or process_handle)
        win32api.CloseHandle = lambda handle: state["closed"].append(handle)
        win32con = types.ModuleType("win32con")
        win32con.PROCESS_SET_QUOTA = 0x100
        win32con.PROCESS_TERMINATE = 0x1
        with patch.dict(sys.modules, {"win32job": win32job, "win32api": win32api, "win32con": win32con}):
            self.assertIs(_assign_excel_job(1234), job_handle)
        self.assertEqual(state["memory"], 1 << 30)
        self.assertEqual(state["memory"], EXCEL_PROCESS_MEMORY_LIMIT)
        self.assertEqual(state["flags"], 0x2100)
        self.assertEqual(state["assigned"], (job_handle, process_handle))
        self.assertEqual(state["closed"], [process_handle])
        self.assertEqual(state["job_name"], "")

    def test_snapshot_preview_is_anchored_at_a1_and_reports_actual_extent(self):
        class Count:
            def __init__(self, value):
                self.Count = value

        class Used:
            Row, Column = 8, 4
            Rows, Columns = Count(2), Count(2)

        class Coordinate:
            def __init__(self, row, column):
                self.row, self.column = row, column

        class Region:
            def __init__(self, sheet, bounds):
                self.sheet, self.bounds = sheet, bounds
                self.Text = ""

            def _matrix(self, key):
                min_col, min_row, max_col, max_row = self.bounds
                values = []
                for row in range(min_row, max_row + 1):
                    values.append(tuple(self.sheet.data.get((row, col), {}).get(key) for col in range(min_col, max_col + 1)))
                return values[0][0] if min_row == max_row and min_col == max_col else tuple(values)

            @property
            def Value2(self):
                return self._matrix("value")

            @property
            def Formula2(self):
                return self._matrix("formula")

            @property
            def Formula2Local(self):
                return self._matrix("formula_local")

        class Sheet:
            Name, UsedRange = "Dados", Used()

            def __init__(self):
                self.string_ranges = []
                self.data = {(8, 4): {"value": 42, "formula": "=SUM(A1:A2)",
                                      "formula_local": "=SOMA(A1:A2)"}}

            def Cells(self, row, column):
                return Coordinate(row, column)

            def Range(self, *args):
                if len(args) == 2:
                    start, end = args
                    bounds = (start.column, start.row, end.column, end.row)
                else:
                    self.string_ranges.append(args[0])
                    from openpyxl.utils.cell import range_boundaries
                    bounds = range_boundaries(args[0])
                return Region(self, bounds)

        sheet = Sheet()
        class Sheets:
            Count = 1
            def __call__(self, _index):
                return sheet

        class Book:
            Worksheets = Sheets()

        result = _snapshot(Book())
        item = result["sheets"][0]
        self.assertEqual((item["rows"], item["columns"]), (9, 5))
        cells = {cell["address"]: cell for cell in item["cells"]}
        self.assertEqual(len(cells), 45)
        self.assertEqual(cells["A1"]["value"], None)
        self.assertEqual(cells["D8"]["value"], 42)
        self.assertEqual(cells["D8"]["formula"], "=SUM(A1:A2)")
        self.assertEqual(cells["D8"]["formula_local"], "=SOMA(A1:A2)")
        self.assertEqual(cells["E9"]["value"], None)
        self.assertEqual(sheet.string_ranges, [])
        self.assertFalse(result["warnings"])

    def test_large_snapshot_preview_is_capped_to_a1_l50(self):
        class Count:
            def __init__(self, value):
                self.Count = value

        class Used:
            Row, Column = 1, 1
            Rows, Columns = Count(60), Count(20)

        class Coordinate:
            def __init__(self, row, column):
                self.row, self.column = row, column

        class Region:
            Value2 = Formula2 = Formula2Local = None
            Text = ""

        class Sheet:
            Name, UsedRange = "Grande", Used()
            def Cells(self, row, column):
                return Coordinate(row, column)
            def Range(self, *_args):
                return Region()

        sheet = Sheet()
        class Sheets:
            Count = 1
            def __call__(self, _index):
                return sheet

        class Book:
            Worksheets = Sheets()

        result = _snapshot(Book())
        item = result["sheets"][0]
        self.assertEqual((item["rows"], item["columns"]), (60, 20))
        self.assertEqual(len(item["cells"]), 600)
        self.assertEqual((item["cells"][0]["address"], item["cells"][-1]["address"]), ("A1", "L50"))
        self.assertTrue(any("A1:L50" in warning for warning in result["warnings"]))

    def test_large_spill_is_explicitly_incomplete_and_not_read(self):
        class Spill:
            def Address(self, *_args):
                return "A1:Y25"

        class Cell:
            HasSpill = True
            SpillingToRange = Spill()

        result = {"ok": True}
        with patch("dashboard.excel_worker._read_range", side_effect=AssertionError("oversized read")):
            _add_spill_result(result, None, Cell(), "Sheet1")
        self.assertFalse(result["ok"])
        self.assertIn("625", result["error"])
        self.assertIn("matricial", result["warning"])

    def test_scalar_formula_does_not_gain_spill_warning(self):
        class Cell:
            HasSpill = False

            @property
            def SpillingToRange(self):
                raise AssertionError("scalar formula has no spill range")

        result = {"ok": True}
        _add_spill_result(result, None, Cell(), "Sheet1")
        self.assertTrue(result["ok"])
        self.assertNotIn("warning", result)


class CsvImportTests(unittest.TestCase):
    def test_only_unambiguous_decimal_fields_become_numbers(self):
        self.assertEqual(_csv_numeric_value("42"), 42)
        self.assertEqual(_csv_numeric_value("-12.5"), -12.5)
        for text in ("0007", "2025-04-02", "1e3", "+5", "-1234567890123456", "=SUM(A1:A3)"):
            with self.subTest(text=text):
                self.assertEqual(_csv_numeric_value(text), text)

    def test_csv_matrix_writes_numeric_values_and_keeps_formula_text(self):
        class Coordinate:
            def __init__(self, row, column):
                self.row, self.column = row, column

        class Region:
            NumberFormat = None
            Value2 = None

        class Sheet:
            Name = "Sheet1"
            def __init__(self):
                self.region = Region()
                self.text_cells = []
            def Cells(self, row, column):
                return Coordinate(row, column)
            def Range(self, *_args):
                return self.region
            def set_text(self, coordinate):
                self.text_cells.append((coordinate.row, coordinate.column))

        sheet = Sheet()
        class Worksheets:
            Count = 1
            def __call__(self, _index):
                return sheet

        class Book:
            def __init__(self):
                self.Worksheets = Worksheets()
            def SaveAs(self, *_args, **_kwargs):
                pass

        class Workbooks:
            def Add(self):
                return Book()

        class App:
            def __init__(self):
                self.Workbooks = Workbooks()

        # Track setting NumberFormat on literal formula cells while allowing the bulk write.
        class TrackedCell:
            def __init__(self, row, column):
                self.row, self.column = row, column
            @property
            def NumberFormat(self):
                return None
            @NumberFormat.setter
            def NumberFormat(self, value):
                if value == "@":
                    sheet.set_text(self)
        sheet.Cells = lambda row, column: TrackedCell(row, column)
        book = _write_csv(App(), Path("unused.xlsx"), [["2", "3", "=SUM(A1:B1)", "0007", "2025-04-02"]])
        self.assertEqual(book.Worksheets(1).region.NumberFormat, "@")
        self.assertEqual(book.Worksheets(1).region.Value2,
                         ((2, 3, "=SUM(A1:B1)", "0007", "2025-04-02"),))
        self.assertEqual(sheet.text_cells, [])


class SnapshotCacheTests(unittest.TestCase):
    def test_compact_tool_context_keeps_headers_dimensions_and_a_clear_sample_limit(self):
        snapshot = {"name": "Inventory", "revision": 1, "warnings": [], "sheets": [{"name": "Stock", "rows": 1001, "columns": 2, "cells": [
            {"address": "A1", "value": "Unit Cost"}, {"address": "B1", "value": "Retail Price"},
            {"address": "A2", "value": 20}, {"address": "A6", "value": "outside sample"},
        ]}]}
        with patch.object(excel_engine, "get_workbook", return_value=snapshot):
            compact = excel_engine.workbook_context("id", compact=True)
            full = excel_engine.workbook_context("id")
        self.assertIn("Unit Cost", compact)
        self.assertIn("1001 linhas", compact)
        self.assertIn("excel_read_range", compact)
        self.assertNotIn("outside sample", compact)
        self.assertIn("outside sample", full)

    def test_failed_create_removes_only_its_uncommitted_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(excel_engine, "engine_status", return_value={"available": True}), \
                 patch.object(excel_engine, "_run_worker", side_effect=RuntimeError("worker failed")):
                with self.assertRaises(RuntimeError):
                    excel_engine.create_workbook(runs_root=temporary)
            self.assertEqual(list((Path(temporary) / "workbooks").iterdir()), [])

    def test_create_workbook_stores_its_initial_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = {"engine": "Microsoft Excel", "sheets": [], "warnings": []}

            def create_with_fake_worker(workbook_id, runs_root, _operation, options):
                directory = workbooks.workbook_directory(workbook_id, runs_root)
                (directory / options["file_name"]).write_bytes(b"workbook")
                return snapshot

            with patch.object(excel_engine, "engine_status", return_value={"available": True}), \
                 patch.object(excel_engine, "_run_worker", side_effect=create_with_fake_worker):
                created = excel_engine.create_workbook(runs_root=temporary)
            cached = read_json(Path(temporary) / "workbooks" / created["id"] / "snapshot.json")
            self.assertEqual(cached["revision"], 0)
            self.assertEqual(cached["snapshot"], snapshot)

    def test_get_workbook_uses_revision_matched_snapshot_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            workbook_id, directory = workbooks.new_workbook_directory(temporary)
            (directory / "workbook.xlsx").write_bytes(b"workbook")
            manifest = {"id": workbook_id, "name": "Cache", "revision": 3, "warnings": []}
            workbooks.write_manifest(directory, manifest)
            snapshot = {"engine": "Microsoft Excel", "sheets": [{"name": "Sheet1", "rows": 1,
                         "columns": 1, "cells": []}], "warnings": []}
            excel_engine._write_snapshot_cache(directory, 3, snapshot)
            with patch.object(excel_engine, "_run_worker", side_effect=AssertionError("cache miss")):
                result = excel_engine.get_workbook(workbook_id, runs_root=temporary)
            self.assertEqual(result["revision"], 3)
            self.assertEqual(result["sheets"], snapshot["sheets"])

    def test_mutation_refreshes_cache_for_the_new_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            workbook_id, directory = workbooks.new_workbook_directory(temporary)
            (directory / "workbook.xlsx").write_bytes(b"workbook")
            workbooks.write_manifest(directory, {"id": workbook_id, "name": "Cache", "revision": 0, "warnings": []})
            snapshot = {"engine": "Microsoft Excel", "sheets": [], "warnings": []}
            with patch.object(excel_engine, "_run_worker", return_value=snapshot):
                result = excel_engine.mutate_workbook(workbook_id, [], 0, runs_root=temporary)
            self.assertEqual(result["revision"], 1)
            with patch.object(excel_engine, "_run_worker", side_effect=AssertionError("stale cache")):
                cached = excel_engine.get_workbook(workbook_id, runs_root=temporary)
            self.assertEqual(cached["revision"], 1)


if __name__ == "__main__":
    unittest.main()
