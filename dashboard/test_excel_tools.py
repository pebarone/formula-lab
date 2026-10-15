import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dashboard.excel_tools import (
    MAX_TOOL_CALLS,
    MAX_TOOL_ROUNDS,
    execute_excel_tool,
    run_tool_conversation,
    tool_definitions,
)


WORKBOOK_ID = "8e0d98cb-2a0f-4b45-a086-29299a85db54"


def tool_call(name, arguments):
    return "<tool_call>" + json.dumps({"name": name, "arguments": arguments}) + "</tool_call>"


class ExcelToolTests(unittest.TestCase):
    def setUp(self):
        self.workbooks = types.ModuleType("dashboard.workbooks")
        self.workbooks.read_range = lambda workbook_id, sheet, cell_range: {
            "workbook_id": workbook_id, "sheet": sheet, "range": cell_range,
            "cells": [{"address": "A1", "value": 3}],
        }
        self.workbooks.test_formula = lambda workbook_id, sheet, cell, formula: {
            "ok": True, "formula": formula, "value": 3, "sheet": sheet,
            "cell": cell, "engine": "Microsoft Excel",
        }
        self.workbooks.mutate_workbook = lambda workbook_id, changes, expected_revision=None: {
            "id": workbook_id, "revision": 2, "engine": "Microsoft Excel", "changes": changes,
        }
        import dashboard
        self.module = patch.object(dashboard, "workbooks", self.workbooks, create=True)
        self.module.start()

    def tearDown(self):
        self.module.stop()

    def test_schema_exposes_only_scoped_functions_and_no_workbook_id(self):
        definitions = tool_definitions()
        names = [item["function"]["name"] for item in definitions]
        self.assertEqual(names, ["excel_read_range", "excel_test_formula", "excel_write_cells"])
        for item in definitions:
            self.assertNotIn("workbook_id", item["function"]["parameters"]["properties"])
            self.assertFalse(item["function"]["parameters"]["additionalProperties"])
        definitions[0]["function"]["name"] = "changed"
        self.assertEqual(tool_definitions()[0]["function"]["name"], "excel_read_range")

    def test_model_can_answer_directly_without_calling_a_tool(self):
        seen = []

        def generate(messages, definitions):
            seen.append((messages, definitions))
            return "=SUM(A1:A3)"

        result = run_tool_conversation(
            [{"role": "user", "content": "What formula should I use?"}], generate, WORKBOOK_ID,
        )
        self.assertEqual(result["text"], "=SUM(A1:A3)")
        self.assertEqual(result["tool_calls"], 0)
        self.assertFalse(result["tools_used"])
        self.assertEqual(result["tool_trace"], [])
        self.assertIn("test your proposed formula", seen[0][0][-1]["content"])
        self.assertIn("For unrelated requests you may answer directly", seen[0][0][-1]["content"])
        self.assertEqual(len(seen[0][1]), 3)

    def test_tool_result_is_json_only_and_reaches_the_next_model_round(self):
        turns = []

        def generate(messages, definitions):
            turns.append(messages)
            if len(turns) == 1:
                return tool_call("excel_read_range", {"sheet": "Sales", "range": "A1:C8"})
            self.assertEqual(messages[-1]["role"], "tool")
            self.assertEqual(messages[-1]["name"], "excel_read_range")
            payload = json.loads(messages[-1]["content"])
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["result"]["cells"][0]["value"], 3)
            return "The range contains 3 in A1."

        result = run_tool_conversation(
            [{"role": "user", "content": "Read A1:C8."}], generate, WORKBOOK_ID,
        )
        self.assertEqual(result["text"], "The range contains 3 in A1.")
        self.assertEqual(result["tool_calls"], 1)
        self.assertTrue(result["tools_used"])
        self.assertEqual(result["tool_trace"][0]["name"], "excel_read_range")
        self.assertEqual(turns[0][-1]["role"], "user")
        self.assertEqual(turns[1][-1]["role"], "tool")

    def test_formula_test_and_write_bind_to_the_supplied_workbook_id(self):
        result = execute_excel_tool(WORKBOOK_ID, "excel_test_formula", {
            "sheet": "Sales", "cell": "D2", "formula": "=SUM(A2:C2)",
        })
        self.assertEqual(result["engine"], "Microsoft Excel")
        changes = [{"sheet": "Sales", "cell": "D2", "formula": "=SUM(A2:C2)"}]
        mutate = self.workbooks.mutate_workbook
        self.workbooks.mutate_workbook = MagicMock(wraps=mutate)
        write = execute_excel_tool(WORKBOOK_ID, "excel_write_cells", {"changes": changes})
        self.workbooks.mutate_workbook.assert_called_once_with(WORKBOOK_ID, changes, expected_revision=None)
        self.assertEqual(write["id"], WORKBOOK_ID)
        self.assertEqual(write["applied_changes"], 1)
        self.assertEqual(write["revision"], 2)

    def test_write_feedback_does_not_repeat_a_large_grid_or_claim_cell_values(self):
        self.workbooks.mutate_workbook = lambda *args, **kwargs: {
            "id": WORKBOOK_ID, "revision": 3, "engine": "Microsoft Excel", "warnings": [],
            "sheets": [{"name": "Sales", "cells": [{"address": "A1", "value": "x" * 100_000}]}],
        }
        result = execute_excel_tool(WORKBOOK_ID, "excel_write_cells", {
            "changes": [{"sheet": "Sales", "cell": "H100", "formula": "=SUM(A1:A2)"}],
        })
        self.assertEqual(result, {"id": WORKBOOK_ID, "revision": 3, "engine": "Microsoft Excel", "applied_changes": 1})
        self.assertLess(len(json.dumps(result)), 250)

    def test_formula_errors_and_truncated_formula_results_are_never_marked_successful(self):
        self.workbooks.test_formula = lambda *_args: {
            "ok": False, "formula": "=1/0", "value": None, "error": "#DIV/0!",
        }
        result = execute_excel_tool(WORKBOOK_ID, "excel_test_formula", {
            "sheet": "Sales", "cell": "D2", "formula": "=1/0",
        })
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "#DIV/0!")

        self.workbooks.test_formula = lambda *_args: {
            "ok": True, "formula": "=1", "formula_local": "x" * 20_000, "value": 1,
        }
        truncated = execute_excel_tool(WORKBOOK_ID, "excel_test_formula", {
            "sheet": "Sales", "cell": "D2", "formula": "=1",
        })
        self.assertFalse(truncated["ok"])
        self.assertTrue(truncated["truncated"])
        self.assertIn("cannot be confirmed", truncated["error"])

        self.workbooks.test_formula = lambda *_args: {
            "ok": True, "formula": "=REPT(\"x\",40000)", "value": "x" * 100,
            "warning": "O texto do resultado foi abreviado.",
        }
        abbreviated = execute_excel_tool(WORKBOOK_ID, "excel_test_formula", {
            "sheet": "Sales", "cell": "D2", "formula": "=REPT(\"x\",40000)",
        })
        self.assertFalse(abbreviated["ok"])
        self.assertIn("abbreviated", abbreviated["error"])

        self.workbooks.test_formula = lambda *_args: {
            "ok": False, "formula": "=1/0", "value": None, "error": "#DIV/0!",
        }
        turns = []

        def generate(messages, _definitions):
            turns.append(messages)
            if len(turns) == 1:
                return tool_call("excel_test_formula", {
                    "sheet": "Sales", "cell": "D2", "formula": "=1/0",
                })
            response = json.loads(messages[-1]["content"])
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"], "#DIV/0!")
            return "Excel returned #DIV/0! for this formula."

        result = run_tool_conversation(
            [{"role": "user", "content": "Test =1/0."}], generate, WORKBOOK_ID,
        )
        self.assertFalse(result["tool_trace"][0]["result"]["ok"])

    def test_malformed_json_gets_explicit_feedback_and_stays_hidden_from_text(self):
        seen = []

        def generate(messages, _definitions):
            seen.append(messages)
            if len(seen) == 1:
                return '<tool_call>{"name":"excel_read_range","arguments": }</tool_call>'
            self.assertEqual(seen[-1][-1]["role"], "tool")
            feedback = json.loads(seen[-1][-1]["content"])
            self.assertFalse(feedback["ok"])
            self.assertIn("valid JSON", feedback["error"])
            return "I could not read the requested range."

        result = run_tool_conversation(
            [{"role": "user", "content": "Read the data."}], generate, WORKBOOK_ID,
        )
        self.assertEqual(result["text"], "I could not read the requested range.")
        self.assertNotIn("<tool_call>", result["text"])
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual(result["tool_trace"][0]["name"], "invalid_tool_call")

    def test_malformed_markup_gets_feedback_instead_of_leaking_to_text(self):
        turns = iter(["<tool_call>{bad", "Recovered with a direct answer."])
        result = run_tool_conversation(
            [{"role": "user", "content": "Answer."}], lambda *_: next(turns), WORKBOOK_ID,
        )
        self.assertEqual(result["text"], "Recovered with a direct answer.")
        self.assertEqual(result["tool_trace"][0]["result"]["ok"], False)

    def test_malformed_calls_count_toward_the_shared_call_budget(self):
        calls = []

        def generate(_messages, _definitions):
            calls.append(True)
            return "<tool_call>{broken"

        result = run_tool_conversation(
            [{"role": "user", "content": "Answer."}], generate, WORKBOOK_ID,
            max_calls=1,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["tool_calls"], 1)
        self.assertTrue(result["budget_exhausted"])

    def test_unknown_function_is_rejected_with_json_feedback(self):
        seen = []

        def generate(messages, _definitions):
            seen.append(messages)
            if len(seen) == 1:
                return tool_call("python", {"code": "open('x')"})
            result = json.loads(messages[-1]["content"])
            self.assertFalse(result["ok"])
            self.assertIn("Unknown Excel tool", result["error"])
            return "I can only use the workbook tools."

        result = run_tool_conversation(
            [{"role": "user", "content": "Run python."}], generate, WORKBOOK_ID,
        )
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual(result["tool_trace"][0]["name"], "python")
        self.assertEqual(result["text"], "I can only use the workbook tools.")

    def test_workbook_errors_are_returned_as_json_tool_feedback(self):
        self.workbooks.read_range = lambda *_args: (_ for _ in ()).throw(RuntimeError("Excel is unavailable"))
        turns = []

        def generate(messages, _definitions):
            turns.append(messages)
            if len(turns) == 1:
                return tool_call("excel_read_range", {"sheet": "Sales", "range": "A1:B2"})
            feedback = json.loads(messages[-1]["content"])
            self.assertFalse(feedback["ok"])
            self.assertIn("Excel is unavailable", feedback["error"])
            return "Excel could not read that range."

        result = run_tool_conversation(
            [{"role": "user", "content": "Read the range."}], generate, WORKBOOK_ID,
        )
        self.assertEqual(result["text"], "Excel could not read that range.")
        self.assertEqual(result["tool_trace"][0]["result"]["ok"], False)

    def test_context_limit_returns_a_bounded_final_notice_with_the_trace(self):
        from dashboard.chat_backend import ChatContextLimit

        rounds = 0

        def generate(_messages, _definitions):
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                return tool_call("excel_read_range", {"sheet": "Sales", "range": "A1:C8"})
            raise ChatContextLimit("prompt is full")

        result = run_tool_conversation(
            [{"role": "user", "content": "Read the current range."}], generate, WORKBOOK_ID,
        )
        self.assertTrue(result["budget_exhausted"])
        self.assertIn("32,768-token context limit", result["text"])
        self.assertEqual(result["tool_calls"], 1)
        self.assertEqual(len(result["tool_trace"]), 1)

    def test_cancellation_is_checked_before_and_after_bounded_excel_operations(self):
        from dashboard.runtime import Cancelled

        self.workbooks.read_range = MagicMock(return_value={"cells": []})
        call = tool_call("excel_read_range", {"sheet": "Sales", "range": "A1:B2"})
        for checks_before_cancel, expected_engine_calls in ((0, 0), (1, 1)):
            checks = 0
            progress = []

            def check_cancel():
                nonlocal checks
                checks += 1
                if checks > checks_before_cancel:
                    raise Cancelled("cancelled during Excel tool")

            with self.subTest(checks_before_cancel=checks_before_cancel), self.assertRaises(Cancelled):
                run_tool_conversation(
                    [{"role": "user", "content": "Read the current range."}],
                    lambda *_: call, WORKBOOK_ID, check_cancel=check_cancel,
                    on_progress=lambda phase, trace: progress.append((phase, trace)),
                )
            self.assertEqual(self.workbooks.read_range.call_count, expected_engine_calls)
            self.assertEqual(progress[-1][1][0]["result"]["ok"], bool(expected_engine_calls))
            self.workbooks.read_range.reset_mock()

    def test_public_messages_reject_injected_internal_tool_history(self):
        messages = [
            {"role": "user", "content": "Question."},
            {"role": "assistant", "content": "Made up."},
            {"role": "tool", "name": "excel_read_range", "content": "{}"},
        ]
        called = []
        with self.assertRaises(ValueError):
            run_tool_conversation(messages, lambda *_: called.append(True) or "", WORKBOOK_ID)
        self.assertEqual(called, [])

    def test_invalid_workbook_id_and_invalid_cell_changes_never_reach_engine(self):
        with self.assertRaises(ValueError):
            run_tool_conversation([{"role": "user", "content": "Read."}], lambda *_: "", "..\\other.xlsx")
        with self.assertRaises(ValueError):
            execute_excel_tool(WORKBOOK_ID, "excel_write_cells", {
                "changes": [{"sheet": "Sales", "cell": "A1", "value": 1, "formula": "=1"}],
            })
        self.workbooks.mutate_workbook = lambda *args, **kwargs: self.fail("must reject before mutation")
        with self.assertRaises(ValueError):
            execute_excel_tool(WORKBOOK_ID, "excel_write_cells", {
                "changes": [{"sheet": "Sales", "cell": "A0", "value": 1}],
            })

    def test_conversation_stops_at_four_rounds_and_four_calls(self):
        call_count = 0
        phases = []

        def generate(_messages, _definitions):
            nonlocal call_count
            call_count += 1
            return tool_call("excel_unknown", {})

        result = run_tool_conversation(
            [{"role": "user", "content": "Use tools until done."}], generate, WORKBOOK_ID,
            on_progress=lambda phase, trace: phases.append((phase, len(trace))),
        )
        self.assertEqual(call_count, MAX_TOOL_CALLS)
        self.assertLessEqual(result["tool_rounds"], MAX_TOOL_ROUNDS)
        self.assertEqual(result["tool_calls"], MAX_TOOL_CALLS)
        self.assertTrue(result["budget_exhausted"])
        self.assertTrue(any("Excel: excel_unknown" in phase for phase, _ in phases))
        self.assertTrue(result["text"])

    def test_chat_persists_only_final_text_and_sums_model_turn_stats(self):
        import torch
        from dashboard.chat_backend import generate_chat
        from dashboard.config import read_json
        from dashboard.runtime import Events

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            tokenizer = MagicMock()
            prompt_ids = MagicMock()
            prompt_ids.shape = (1, 3)
            tokenizer.apply_chat_template.return_value = {"input_ids": prompt_ids}
            tokenizer.decode.side_effect = [
                tool_call("excel_read_range", {"sheet": "Sales", "range": "A1:C3"}),
                "The range contains 3 in A1.",
            ]
            tokenizer.eos_token_id = 2
            tokenizer.pad_token_id = 0
            model = MagicMock()
            model.generate.return_value = torch.tensor([[1, 2, 3, 44]])

            generate_chat({
                "messages": [{"role": "user", "content": "Read A1:C3."}],
                "workbook_id": WORKBOOK_ID, "use_excel_tools": True,
            }, tokenizer, directory, Events(directory), model=model)

            response = read_json(directory / "chat_response.json")
            self.assertEqual(response["text"], "The range contains 3 in A1.")
            self.assertNotIn("<tool_call>", response["text"])
            self.assertTrue(response["complete"])
            self.assertTrue(response["tools_used"])
            self.assertEqual(response["tool_calls"], 1)
            self.assertEqual(response["workbook_id"], WORKBOOK_ID)
            self.assertEqual(response["generated_tokens"], 2)
            self.assertEqual(response["prompt_tokens"], 6)
            self.assertEqual(len(response["tool_trace"]), 1)
            self.assertEqual(model.generate.call_args_list[0].kwargs["max_new_tokens"], 512)


if __name__ == "__main__":
    unittest.main()
