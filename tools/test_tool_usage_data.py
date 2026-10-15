import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

from tools.tool_usage_data import (
    CATEGORIES,
    FORMULA_FAMILIES,
    TRAIN_SEED,
    VALIDATION_SEED,
    _category_schedule,
    _context_and_user,
    _formula_read_range,
    _generate_cases,
    _token_lengths,
    _tool_call,
    _write_bank,
    build_dataset,
)


class ToolUsageDatasetTests(unittest.TestCase):
    def test_category_schedule_has_exact_deterministic_ratios(self):
        first = _category_schedule(1000)
        self.assertEqual(first, _category_schedule(1000))
        self.assertEqual({category: first.count(category) for category in CATEGORIES}, {
            "propose_test_final": 500,
            "error_repair": 300,
            "read_then_test": 100,
            "authorized_write_test_read": 100,
        })
        with self.assertRaises(ValueError):
            _category_schedule(99)

    def test_splits_generalize_to_disjoint_cells_and_cover_formula_families(self):
        train = _generate_cases("train", 120, TRAIN_SEED)
        validation = _generate_cases("validation", 120, VALIDATION_SEED)
        self.assertEqual({case["formula_family"] for case in train}, set(FORMULA_FAMILIES))
        self.assertEqual({case["formula_family"] for case in validation}, set(FORMULA_FAMILIES))
        self.assertTrue({case["source_id"] for case in train}.isdisjoint(case["source_id"] for case in validation))
        self.assertTrue({case["worksheet"] for case in train}.isdisjoint(case["worksheet"] for case in validation))
        train_values = {value for case in train for record in case["records"] for value in record
                        if isinstance(value, (int, float)) and not isinstance(value, bool)}
        validation_values = {value for case in validation for record in case["records"] for value in record
                             if isinstance(value, (int, float)) and not isinstance(value, bool)}
        self.assertTrue(train_values.isdisjoint(validation_values))
        self.assertEqual({case["seed"] for case in train}, {TRAIN_SEED})
        self.assertEqual({case["seed"] for case in validation}, {VALIDATION_SEED})

    def test_read_ranges_are_small_and_category_prompts_are_explicit(self):
        cases = _generate_cases("train", 120, TRAIN_SEED)
        for case in cases:
            address = _formula_read_range(case)
            self.assertLessEqual(_range_cell_count(address), 600)
        error = next(case for case in cases if case["category"] == "error_repair")
        write = next(case for case in cases if case["category"] == "authorized_write_test_read")
        read = next(case for case in cases if case["category"] == "read_then_test")
        self.assertIn(error["error_formula"], _context_and_user(error)[1])
        self.assertIn("I authorize writing", _context_and_user(write)[1])
        self.assertIn(write["formula_cell"], _context_and_user(write)[1])
        self.assertIn(read["read_range"], _context_and_user(read)[1])
        injected = cases[0]
        self.assertIn("=HYPERLINK(", _context_and_user(injected)[1])

    def test_tool_arguments_are_checked_against_production_protocol(self):
        call = _tool_call("excel_test_formula", {
            "sheet": "ToolTrain", "cell": "H2", "formula": "=SUM(C2:C7)",
        })
        self.assertTrue(call.startswith("<tool_call>{"))
        with self.assertRaises(ValueError):
            _tool_call("excel_test_formula", {
                "sheet": "ToolTrain", "cell": "H2", "formula": "=SUM(C2:C7)", "extra": True,
            })
        with self.assertRaises(ValueError):
            _tool_call("excel_write_cells", {"changes": []})

    def test_formula_like_untrusted_input_is_stored_as_text(self):
        case = _generate_cases("train", 10, TRAIN_SEED)[0]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bank.xlsx"
            _write_bank([case], "train", path)
            book = load_workbook(path, data_only=False)
            cell = book[case["worksheet"]][f"G{case['data_start']}"]
            self.assertTrue(cell.value.startswith("=HYPERLINK("))
            self.assertEqual(cell.data_type, "s")
            book.close()

    def test_verifier_cannot_be_replaced_with_a_fake_callback(self):
        import inspect

        self.assertNotIn("verifier", inspect.signature(build_dataset).parameters)
        self.assertNotIn("verifier", inspect.signature(_generate_cases).parameters)

    def test_token_length_reader_handles_mapping_like_batch_encodings(self):
        class BatchEncodingLike:
            def keys(self):
                return ("input_ids",)

            def get(self, key, default=None):
                return [1, 2, 3, 4] if key == "input_ids" else default

        class Tokenizer:
            def apply_chat_template(self, *args, **kwargs):
                return BatchEncodingLike()

        row = {"messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "raw request"},
            {"role": "assistant", "content": "=SUM(A1:A2)"},
        ]}
        result = _token_lengths({"train": [row], "validation": []}, tokenizer=Tokenizer())
        self.assertEqual(result["max"], 4)
        self.assertEqual(result["rows"], 1)

    def test_token_measurement_uses_production_schema_order_after_jsonl_roundtrip(self):
        from dashboard.excel_tools import tool_definitions

        captured = []

        class Tokenizer:
            def apply_chat_template(self, messages, **kwargs):
                captured.append(json.dumps(kwargs["tools"]))
                return [1, 2, 3]

        row = json.loads(json.dumps({
            "tools": tool_definitions(),
            "messages": [{"role": "system", "content": "system"},
                         {"role": "user", "content": "raw request"}],
        }, sort_keys=True))
        _token_lengths({"train": [row]}, tokenizer=Tokenizer())
        self.assertEqual(captured, [json.dumps(tool_definitions())])


def _range_cell_count(address):
    from openpyxl.utils.cell import range_boundaries

    min_col, min_row, max_col, max_row = range_boundaries(address)
    return (max_col - min_col + 1) * (max_row - min_row + 1)


if __name__ == "__main__":
    unittest.main()
