import json
import unittest
from collections import Counter
from io import BytesIO

from openpyxl import load_workbook

from tools.evaluate_tool_usage import (
    _extra_escaped_quotes, _fixture_xlsx, _successful_formula_test, balanced_sample, evaluate_item,
)


def validation_row(index, category):
    return {"id": f"val-{category}-{index}", "category": category}


class ToolUsageEvaluationTests(unittest.TestCase):
    def test_balanced_sample_is_deterministic_and_caps_at_rare_category_size(self):
        categories = ("propose_test_final", "error_repair", "read_then_test", "authorized_write_test_read")
        rows = [validation_row(index, category) for category, count in zip(categories, (50, 30, 10, 10))
                for index in range(count)]
        sample = balanced_sample(rows, 12)
        self.assertEqual(Counter(row["category"] for row in sample), Counter({category: 3 for category in categories}))
        self.assertEqual([row["id"] for row in sample], [row["id"] for row in balanced_sample(rows, 12)])
        balanced = Counter(row["category"] for row in balanced_sample(rows, 80))
        self.assertEqual(balanced, Counter({categories[0]: 30, categories[1]: 30,
                                            categories[2]: 10, categories[3]: 10}))
        full = Counter(row["category"] for row in balanced_sample(rows, 100))
        self.assertEqual(full, Counter({categories[0]: 50, categories[1]: 30, categories[2]: 10, categories[3]: 10}))

    def test_workbook_keeps_formula_like_input_as_literal_text_and_restores_headers(self):
        row = {
            "id": "val-literal-001",
            "messages": [{"role": "system", "content": ""},
                         {"role": "user", "content": "Sheet ToolValid; columns A Item | B Note"}],
            "fixture": {
                "sheet": "ToolValid", "input_range": "A1:B2", "formula_cell": "C2",
                "input_cells": [{"address": "A1", "value": "Item"}, {"address": "B1", "value": "Note"},
                                {"address": "A2", "value": "ITEM-1"}, {"address": "B2", "value": "=1+1"}],
            },
        }
        workbook = load_workbook(BytesIO(_fixture_xlsx(row)), data_only=False)
        sheet = workbook["ToolValid"]
        self.assertEqual(sheet["A1"].value, "Item")
        self.assertEqual(sheet["B1"].value, "Note")
        self.assertEqual(sheet["B2"].value, "=1+1")
        self.assertEqual(sheet["B2"].data_type, "s")
        workbook.close()

    def test_counts_success_only_when_final_formula_was_tested_by_excel(self):
        valid = {
            "name": "excel_test_formula",
            "arguments": {"formula": "=SUM(A1:A2)"},
            "result": {"ok": True, "result": {"ok": True, "engine": "Microsoft Excel",
                                                "formula": "=SUM(A1:A2)", "error": None}},
        }
        self.assertTrue(_successful_formula_test(valid, "=sum(A1:A2)"))
        valid["result"]["result"]["engine"] = "LibreOffice"
        self.assertFalse(_successful_formula_test(valid, "=SUM(A1:A2)"))
        valid["result"]["result"]["engine"] = "Microsoft Excel"
        valid["result"]["ok"] = False
        self.assertFalse(_successful_formula_test(valid, "=SUM(A1:A2)"))

    def test_generation_receives_only_the_user_turn_and_tool_instruction(self):
        expected = "=SUM(A2:A3)"
        teacher_call = '<tool_call>{"name":"excel_test_formula","arguments":{"formula":"=SUM(A2:A3)"}}</tool_call>'
        row = {
            "id": "val-no-leak-001", "source_id": "synthetic-tool-usage:validation:1",
            "category": "propose_test_final",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "Given this Excel spreadsheet context:\n\nSheet: Sales\nA: Units\nSample:\n2 | 1\n3 | 2\n\nInstruction:\nSum the Units values."},
                {"role": "assistant", "content": teacher_call},
                {"role": "tool", "name": "excel_test_formula", "content": '{"ok":true}'},
                {"role": "assistant", "content": expected},
            ],
            "fixture": {"sheet": "Sales", "input_range": "A1:A3", "formula_cell": "B2",
                        "input_cells": [{"address": "A2", "value": 1}, {"address": "A3", "value": 2}]},
        }

        class Events:
            def check(self):
                return None

        seen = []

        def generate(history, tools):
            seen.append(history)
            self.assertEqual([message["role"] for message in history], ["user"])
            self.assertNotIn(teacher_call, json.dumps(history))
            self.assertNotIn(expected, json.dumps(history))
            self.assertEqual(history[0]["content"].count("test your proposed formula"), 1)
            return expected

        item = evaluate_item(
            row, generate, lambda name, content: {"id": "123e4567-e89b-12d3-a456-426614174000"},
            lambda attachment_id: {"id": "223e4567-e89b-12d3-a456-426614174000"},
            lambda workbook_id, sheet, cell, formula: {
                "ok": True, "engine": "Microsoft Excel", "formula": formula, "error": None,
            }, Events(),
        )
        self.assertNotIn("error", item, item)
        self.assertEqual(len(seen), 1)
        self.assertTrue(item["formula_match"])

    def test_quote_metric_counts_invalid_backslash_escaping_not_excel_doubled_quotes(self):
        self.assertEqual(_extra_escaped_quotes(r'="valid "" quote"', r'="valid "" quote"'), 0)
        self.assertEqual(_extra_escaped_quotes(r'="invalid \" quote"', r'="valid "" quote"'), 1)


if __name__ == "__main__":
    unittest.main()
