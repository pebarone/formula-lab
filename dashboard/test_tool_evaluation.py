import io
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from dashboard.runtime import Cancelled
from dashboard.tool_evaluation import compare_example, sample_workbook, summarize, validate_tool_eval


class ToolEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.row = {"id": "one", "instruction": "Sum A2:A3", "context_text":
                    "Sheet: Test\nA: Value\nB: Literal\nSample:\n2 | 10 | =BAD()\n3 | 20 | x", "expected": "=SUM(A2:A3)"}

    def test_parameters(self):
        self.assertEqual(validate_tool_eval({}), {"test_count": 10, "max_tool_calls": 4})
        for raw in ({"test_count": True}, {"test_count": 501}, {"max_tool_calls": 0}, {"extra": 1}, []):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_tool_eval(raw)

    def test_sample_preserves_addresses_and_literal_formulas(self):
        name, content = sample_workbook(self.row["context_text"])
        book = load_workbook(io.BytesIO(content))
        self.assertEqual(name, "Test")
        self.assertEqual(book[name]["A3"].value, 20)
        self.assertEqual(book[name]["B2"].value, "=BAD()")
        self.assertEqual(book[name]["B2"].data_type, "s")
        book.close()

    def test_ambiguous_sample_rejected(self):
        for context in ("Sheet: Bad/Name\nA: v\nSample:\n2 | 1", "Sheet: Test\nA: v\nSample:\n2 | a | b", "Sheet: Test\nA: v\nSample:\n2 | 1\n2 | 2"):
            with self.subTest(context=context), self.assertRaises(ValueError):
                sample_workbook(context)

    def test_paired_inputs_no_oracle_and_execution_is_not_accuracy(self):
        calls = []

        def generate(messages, tools):
            calls.append((messages, tools))
            self.assertNotIn(self.row["expected"], str(messages))
            return {"text": "=SUM(A2:A3)" if tools is None else "=A2+A3"}

        item = compare_example(self.row, generate, "e0beeb91-b5aa-4cda-a41e-43596697a2ed", lambda formula: {"ok": True})
        self.assertTrue(item["without_tools"]["match"])
        self.assertFalse(item["with_tools"]["match"])
        self.assertTrue(item["with_tools"]["execution_ok"])
        self.assertIsNone(calls[0][1])
        self.assertTrue(calls[1][1])
        self.assertIn(self.row["context_text"], calls[1][0][-1]["content"])
        report = summarize([item], 1, {})
        self.assertEqual(report["comparison"], {"delta_pp": -100, "gained": 0, "lost": 1})
        self.assertEqual(report["with_tools"]["tool_calls"], 0)

    def test_tool_feedback_can_repair_without_answer_leak(self):
        answers = iter(["=SUMM(A2:A3)", '<tool_call>{"name":"excel_test_formula","arguments":{"sheet":"Test","cell":"D1","formula":"=SUMM(A2:A3)"}}</tool_call>', "=SUM(A2:A3)"])
        with patch("dashboard.excel_tools.execute_excel_tool", return_value={"ok": False, "error": "#NAME?"}):
            item = compare_example(self.row, lambda messages, tools: {"text": next(answers)},
                                   "e0beeb91-b5aa-4cda-a41e-43596697a2ed", lambda formula: {"ok": formula == self.row["expected"]})
        self.assertFalse(item["without_tools"]["match"])
        self.assertTrue(item["with_tools"]["match"])
        self.assertEqual(item["with_tools"]["tool_calls"], 1)

    def test_cancellation_propagates(self):
        def generate(messages, tools):
            raise Cancelled("stop")
        with self.assertRaises(Cancelled):
            compare_example(self.row, generate, "e0beeb91-b5aa-4cda-a41e-43596697a2ed", lambda formula: {})


if __name__ == "__main__":
    unittest.main()
