import json
import hashlib
import unittest

from benchmark.prompting import SYSTEM_PROMPT
from dashboard.excel_tools import tool_definitions
from dashboard.tool_training import _encode_tool_row, _validate_tool_row


class FakeTokenizer:
    eos_token_id = 2

    def apply_chat_template(self, messages, tools=None, tokenize=True, add_generation_prompt=False, return_dict=True):
        ids = [900]
        if tools is not None:
            ids.extend(ord(character) + 1000 for character in json.dumps(tools, sort_keys=True))
        for message in messages:
            role = message["role"]
            ids.append({"system": 10, "user": 11, "assistant": 12, "tool": 13}[role])
            if role == "tool":
                ids.extend(ord(character) + 1000 for character in message["name"])
            ids.extend(ord(character) + 1000 for character in message["content"])
            ids.extend((self.eos_token_id, 3))
        if add_generation_prompt:
            ids.append(12)
        return {"input_ids": ids}


def tool_row():
    formula = "=SUM(A1:A2)"
    result = {
        "ok": True,
        "result": {
            "ok": True, "formula": formula, "formula_local": formula,
            "value": 3, "error": None, "sheet": "Sales", "cell": "D1",
            "engine": "Microsoft Excel",
        },
    }
    canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sha = lambda value: hashlib.sha256(value).hexdigest()
    workbook_hash = "a" * 64
    input_cells = [{"address": "A1", "value": 1}, {"address": "A2", "value": 2}]
    input_hash = sha(canonical(input_cells))
    source_id = "synthetic-workbook-000001"
    verification_id = sha(f"{source_id}\n{formula}\n{workbook_hash}".encode("utf-8"))
    formula_evidence = {
        "value": 3, "error": None, "formula_local": formula,
        "result_sha256": sha(canonical({"formula": formula, "value": 3, "formula_local": formula})),
    }
    return {
        "id": "excel-tool-train-000001",
        "source_id": source_id,
        "category": "propose_test_final",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Given this Excel spreadsheet context:\nSales\n\nInstruction:\nSum A1:A2.\n\nReturn only the Excel formula."},
            {"role": "assistant", "content": '<tool_call>{"name":"excel_test_formula","arguments":{"sheet":"Sales","cell":"D1","formula":"=SUM(A1:A2)"}}</tool_call>'},
            {"role": "tool", "name": "excel_test_formula", "content": json.dumps(result, separators=(",", ":"))},
            {"role": "assistant", "content": formula},
        ],
        "tools": tool_definitions(),
        "provenance": {"split": "train", "worksheet": "ToolTrain", "text_variant": "T2609", "seed": 20261009},
        "fixture": {"formula_cell": "D1", "formula": formula, "input_cells": input_cells},
        "formula_evidence": {
            "formula": formula,
            "engine": "Microsoft Excel",
            "worker_module": "dashboard.excel_worker",
            "operation": "mutate_native_write_batch_then_native_read_range",
            "verification_id": verification_id,
            "bank_workbook_id": "123e4567-e89b-12d3-a456-426614174000",
            "bank_workbook_sha256": workbook_hash,
            "input_cells_sha256": input_hash,
            "formula_cell": "D1",
            "formula_result": formula_evidence,
            "candidate_result": None,
            "tool_results": [{
                "tool": "excel_test_formula", "result_sha256": sha(canonical(result)),
                "native_verification_id": verification_id,
            }],
        },
    }


class ToolTrajectoryTests(unittest.TestCase):
    def test_masks_only_assistant_content_and_end_token(self):
        row = tool_row()
        encoded = _encode_tool_row(FakeTokenizer(), row, row["tools"])
        supervised = [index for index, token in enumerate(encoded["labels"]) if token != -100]
        self.assertGreater(len(supervised), 10)
        self.assertNotEqual(encoded["labels"][supervised[-1]], 3)
        self.assertEqual(encoded["labels"][supervised[0]], encoded["input_ids"][supervised[0]])
        self.assertEqual(encoded["labels"][supervised[-1]], 2)

    def test_accepts_only_real_excel_tool_calls_and_matching_verified_final_formula(self):
        _validate_tool_row(tool_row(), tool_definitions())

    def test_rejects_unknown_tools_invalid_arguments_and_unverified_formula(self):
        row = tool_row()
        row["messages"][2]["content"] = '<tool_call>{"name":"not_excel","arguments":{}}</tool_call>'
        row["messages"][3]["name"] = "not_excel"
        with self.assertRaisesRegex(ValueError, "chamada de ferramenta inválida"):
            _validate_tool_row(row, tool_definitions())

        row = tool_row()
        row["messages"][2]["content"] = '<tool_call>{"name":"excel_test_formula","arguments":{"sheet":"Sales","cell":"XFE1","formula":"=SUM(A1:A2)"}}</tool_call>'
        with self.assertRaisesRegex(ValueError, "chamada de ferramenta inválida"):
            _validate_tool_row(row, tool_definitions())

        row = tool_row()
        row["messages"][-1]["content"] = "=SUM(A1:A3)"
        row["formula_evidence"]["formula"] = "=SUM(A1:A3)"
        row["fixture"]["formula"] = "=SUM(A1:A3)"
        row["formula_evidence"]["formula_result"]["result_sha256"] = hashlib.sha256(json.dumps({
            "formula": "=SUM(A1:A3)", "value": 3, "formula_local": "=SUM(A1:A2)",
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        row["formula_evidence"]["verification_id"] = hashlib.sha256(
            f"{row['source_id']}\n=SUM(A1:A3)\n{'a' * 64}".encode("utf-8")
        ).hexdigest()
        for item in row["formula_evidence"]["tool_results"]:
            item["native_verification_id"] = row["formula_evidence"]["verification_id"]
        with self.assertRaisesRegex(ValueError, "não foi testada com sucesso"):
            _validate_tool_row(row, tool_definitions())

    def test_rejects_formula_evidence_mismatch(self):
        row = tool_row()
        evidence = row["formula_evidence"]["formula_result"]
        evidence["value"] = 4
        evidence["result_sha256"] = hashlib.sha256(json.dumps({
            "formula": "=SUM(A1:A2)", "value": 4, "formula_local": "=SUM(A1:A2)",
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        with self.assertRaisesRegex(ValueError, "resultado do teste final diverge"):
            _validate_tool_row(row, tool_definitions())


if __name__ == "__main__":
    unittest.main()
