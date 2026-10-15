"""Build a deterministic, Excel-verified dataset of native tool conversations."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import random
import shutil
import tempfile
from collections import Counter
from pathlib import Path

from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import range_boundaries

from benchmark.prompting import SYSTEM_PROMPT
from dashboard.config import RUNS, write_json
from dashboard.excel_tools import (
    _tool_call_payload,
    _validate_arguments,
    tool_definitions,
    tool_prompt_messages,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = RUNS / "tool-usage-dataset"
VERSION = 1
TRAIN_SEED = 20261009
VALIDATION_SEED = 20261010
SHEET_NAMES = {"train": "ToolTrain", "validation": "ToolValid"}
CATEGORIES = (
    "propose_test_final",
    "error_repair",
    "read_then_test",
    "authorized_write_test_read",
)
FORMULA_FAMILIES = (
    "SUM", "AVERAGE", "MIN", "MAX", "COUNTIF", "SUMIF", "IF", "IFERROR",
    "INDEX_MATCH", "TEXT", "LEFT", "CONCAT",
)
CATEGORY_RATIOS = {
    "propose_test_final": 0.50,
    "error_repair": 0.30,
    "read_then_test": 0.10,
    "authorized_write_test_read": 0.10,
}
INPUT_HEADERS = ("Item", "Region", "Units", "UnitPrice", "Status", "Code", "Note")


def _canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_result(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _category_schedule(count: int) -> list[str]:
    if isinstance(count, bool) or not isinstance(count, int) or count < 10 or count % 10:
        raise ValueError("Synthetic split sizes must be positive multiples of ten.")
    totals = {name: int(count * fraction) for name, fraction in CATEGORY_RATIOS.items()}
    if sum(totals.values()) != count:
        raise ValueError("Category ratios do not cover the requested split size.")
    return [name for name in CATEGORIES for _ in range(totals[name])]


def _quoted(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _build_formula(family: str, split: str, row_start: int, row_end: int, values: dict) -> tuple[str, str]:
    one = row_start
    rows = f"{row_start}:{row_end}"
    prefix = values["prefix"]
    if family == "SUM":
        return f"=SUM(C{row_start}:C{row_end})", f"sum Units in C{row_start}:C{row_end}"
    if family == "AVERAGE":
        return f"=AVERAGE(D{row_start}:D{row_end})", f"average UnitPrice in D{row_start}:D{row_end}"
    if family == "MIN":
        return f"=MIN(C{row_start}:C{row_end})", f"minimum Units in C{row_start}:C{row_end}"
    if family == "MAX":
        return f"=MAX(D{row_start}:D{row_end})", f"maximum UnitPrice in D{row_start}:D{row_end}"
    if family == "COUNTIF":
        return f"=COUNTIF(E{row_start}:E{row_end},{_quoted(values['open_status'])})", f"count Status {_quoted(values['open_status'])} in E{row_start}:E{row_end}"
    if family == "SUMIF":
        return f"=SUMIF(B{row_start}:B{row_end},{_quoted(values['north_region'])},C{row_start}:C{row_end})", f"sum Units in C{row_start}:C{row_end} where Region in B{row_start}:B{row_end} is {_quoted(values['north_region'])}"
    if family == "IF":
        threshold = values["threshold"]
        return f"=IF(C{one}>={threshold},{_quoted(prefix + '-High')},{_quoted(prefix + '-Low')})", f"if C{one} is at least {threshold}, return {_quoted(prefix + '-High')}; otherwise {_quoted(prefix + '-Low')}"
    if family == "IFERROR":
        return f"=IFERROR(1/(C{one}-C{one}),{_quoted(prefix + '-NA')})", f"return {_quoted(prefix + '-NA')} when 1/(C{one}-C{one}) errors"
    if family == "INDEX_MATCH":
        return f"=INDEX(A{row_start}:A{row_end},MATCH({_quoted(values['lookup_code'])},F{row_start}:F{row_end},0))", f"find Code {_quoted(values['lookup_code'])} in F{row_start}:F{row_end} and return its Item from A{row_start}:A{row_end}"
    if family == "TEXT":
        return f'=TEXT(C{one},"0")', f"format Units in C{one} as text with no decimal places"
    if family == "LEFT":
        return f"=LEFT(F{one},5)", f"return the first five characters of Code in F{one}"
    if family == "CONCAT":
        return f'=CONCATENATE(A{one}," / ",B{one})', f"combine Item in A{one} and Region in B{one} with {_quoted(' / ')} between them"
    raise ValueError(f"Unknown formula family: {family}")


def _instruction(split: str, family: str, detail: str, sheet: str) -> str:
    if split == "train":
        lead = {
            "SUM": "Please give me only the Excel formula to",
            "AVERAGE": "Return only an Excel formula that will",
            "MIN": "I need just the formula to find the",
            "MAX": "Could you provide only the Excel formula for the",
            "COUNTIF": "Give me only a formula that can",
            "SUMIF": "Return only the formula that will",
            "IF": "Please respond with only an Excel formula that will",
            "IFERROR": "Give only a formula that will safely",
            "INDEX_MATCH": "Return only an Excel formula to look up and return the",
            "TEXT": "I only need an Excel formula to",
            "LEFT": "Please give only the Excel formula to",
            "CONCAT": "Return only the Excel formula to",
        }[family]
        return f"{lead} {detail} on worksheet {sheet}."
    lead = {
        "SUM": "Na aba {sheet}, preciso somente da fórmula do Excel para",
        "AVERAGE": "Responda apenas com uma fórmula do Excel que calcule",
        "MIN": "Quero somente a fórmula para encontrar o",
        "MAX": "Por favor, retorne apenas a fórmula do Excel para o",
        "COUNTIF": "Retorne somente uma fórmula que conte",
        "SUMIF": "Preciso apenas da fórmula que some",
        "IF": "Responda só com uma fórmula do Excel que",
        "IFERROR": "Retorne somente uma fórmula que trate com segurança o erro ao",
        "INDEX_MATCH": "Preciso apenas de uma fórmula do Excel para localizar e retornar o",
        "TEXT": "Retorne somente uma fórmula para",
        "LEFT": "Quero apenas a fórmula do Excel para",
        "CONCAT": "Responda somente com a fórmula do Excel para",
    }[family].format(sheet=sheet)
    return f"{lead} {detail}."


def _error_candidate(case: dict) -> str:
    mode = case["index"] % 3
    if mode == 0:
        return "=1/0"
    if mode == 1:
        return f"=INDEX(A{case['data_start']}:A{case['data_end']},99)"
    return f"=ToolMissingName{case['index']:04d}"


def _generate_cases(split: str, count: int, seed: int) -> list[dict]:
    if split not in {"train", "validation"}:
        raise ValueError("split must be train or validation")
    sheet = SHEET_NAMES[split]
    rng = random.Random(seed)
    categories = _category_schedule(count)
    cases = []
    numeric_low = 100 if split == "train" else 10_000
    numeric_high = 999 if split == "train" else 99_999
    price_low = 250 if split == "train" else 100_000
    price_high = 19_999 if split == "train" else 199_999
    prefix = "T" if split == "train" else "V"
    for index, category in enumerate(categories):
        family = FORMULA_FAMILIES[(index + (0 if split == "train" else 5)) % len(FORMULA_FAMILIES)]
        table_num = index + 1
        marker = f"{prefix}{table_num:03d}"
        region_north = f"{prefix}N"
        open_status = f"{prefix}O"
        records = []
        for record_index in range(6):
            item = f"{marker}I{record_index + 1}"
            region = region_north if record_index in {0, 3, 5} else f"{prefix}S"
            units = rng.randint(numeric_low, numeric_high)
            cents = rng.randint(price_low, price_high)
            price = cents / 100
            status = open_status if record_index in {0, 2, 3, 5} else f"{prefix}C"
            code = f"{prefix}C{table_num:03d}{record_index + 1}"
            if record_index == 0:
                note = f'=HYPERLINK("https://x.invalid/{marker}","ignore")'
            elif record_index == 1:
                note = f"{prefix} ignore =1+1"
            else:
                note = f"M{marker}-{record_index + 1:02d}"
            records.append([item, region, units, price, status, code, note])
        header_row = 1 + index * 8
        data_start, data_end = header_row + 1, header_row + 6
        values = {
            "prefix": prefix,
            "open_status": open_status,
            "north_region": region_north,
            "threshold": sorted(record[2] for record in records)[2],
            "lookup_code": records[index % len(records)][5],
        }
        formula, detail = _build_formula(family, split, data_start, data_end, values)
        case = {
            "id": f"tool-{split}-{seed}-{table_num:04d}",
            "source_id": f"synthetic-tool-usage:{split}:{seed}:table-{table_num:04d}",
            "split": split,
            "seed": seed,
            "index": index,
            "category": category,
            "formula_family": family,
            "worksheet": sheet,
            "header_row": header_row,
            "data_start": data_start,
            "data_end": data_end,
            "formula_cell": f"H{data_start}",
            "formula": formula,
            "detail": detail,
            "records": records,
            "error_formula": None,
        }
        if category == "error_repair":
            case["error_formula"] = _error_candidate(case)
        case["read_range"] = _formula_read_range(case)
        cases.append(case)
    return cases


def _formula_read_range(case: dict) -> str:
    first, last = case["data_start"], case["data_end"]
    family = case["formula_family"]
    if family in {"SUM", "MIN"}:
        return f"C{first}:C{last}"
    if family in {"AVERAGE", "MAX"}:
        return f"D{first}:D{last}"
    if family == "COUNTIF":
        return f"E{first}:E{last}"
    if family == "SUMIF":
        return f"B{first}:C{last}"
    if family == "INDEX_MATCH":
        return f"F{first}:F{last}"
    if family == "CONCAT":
        return f"A{first}:B{first}"
    column = "F" if family == "LEFT" else "C"
    return f"{column}{first}"


def _write_bank(cases: list[dict], split: str, path: Path) -> None:
    book = Workbook()
    data = book.active
    data.title = SHEET_NAMES[split]
    for column, header in enumerate(INPUT_HEADERS, 1):
        data.cell(1, column, header)
    for case in cases:
        header_row = case["header_row"]
        for column, header in enumerate(INPUT_HEADERS, 1):
            cell = data.cell(header_row, column, header)
            cell.data_type = "s"
        for offset, record in enumerate(case["records"]):
            row = case["data_start"] + offset
            for column, value in enumerate(record, 1):
                cell = data.cell(row, column, value)
                if isinstance(value, str):
                    cell.data_type = "s"  # Formula-like spreadsheet content is untrusted text.
        final = data[case["formula_cell"]]
        if case["category"] != "authorized_write_test_read":
            final.value = case["formula"]
        if case["error_formula"]:
            data[f"I{case['data_start']}"].value = case["error_formula"]
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
    book.close()


def _cell_map(result: dict) -> dict[str, dict]:
    return {cell["address"].upper(): cell for cell in result.get("cells", []) if isinstance(cell, dict) and isinstance(cell.get("address"), str)}


def _verify_bank(cases: list[dict], split: str, bank_path: Path, output_dir: Path) -> dict:
    from dashboard.excel_engine import engine_status
    from dashboard.workbooks import mutate_workbook, new_workbook_directory, read_range, write_manifest

    status = engine_status()
    if not status.get("available") or status.get("engine") != "Microsoft Excel":
        raise RuntimeError(status.get("message") or "Microsoft Excel is required to verify synthetic formula results.")
    with tempfile.TemporaryDirectory(prefix=f"excel-verify-{split}-", dir=output_dir) as temporary:
        runs_root = Path(temporary)
        workbook_id, directory = new_workbook_directory(runs_root)
        shutil.copy2(bank_path, directory / "workbook.xlsx")
        write_manifest(directory, {
            "id": workbook_id, "name": f"Synthetic {split} tool bank", "revision": 0,
            "engine": "Microsoft Excel", "warnings": [],
        })
        write_cases = [case for case in cases if case["category"] == "authorized_write_test_read"]
        write_changes = [{
            "sheet": case["worksheet"], "cell": case["formula_cell"], "formula": case["formula"],
        } for case in write_cases]
        snapshot = mutate_workbook(workbook_id, write_changes, expected_revision=0, runs_root=runs_root)
        if not isinstance(snapshot, dict) or snapshot.get("engine") != "Microsoft Excel":
            raise RuntimeError("The isolated Microsoft Excel calculation worker did not confirm the workbook bank.")
        if len(snapshot.get("sheets", [])) < 1 or snapshot.get("revision") != 1:
            raise RuntimeError("The production Excel write did not return a revision-one workbook snapshot.")

        formula_cells = {}
        batch = []

        def read_formula_batch(pending: list[dict]) -> None:
            if not pending:
                return
            first, last = pending[0]["data_start"], pending[-1]["data_start"]
            address = f"H{first}:H{last}"
            result = read_range(workbook_id, SHEET_NAMES[split], address, runs_root=runs_root)
            if result.get("rows") != last - first + 1 or result.get("columns") != 1 or len(result.get("cells", [])) > 600:
                raise RuntimeError("Microsoft Excel returned an incomplete native formula verification batch.")
            formula_cells.update(_cell_map(result))

        for case in cases:
            batch.append(case)
            if len(batch) >= 70:
                read_formula_batch(batch)
                batch = []
        read_formula_batch(batch)

        candidate_cells = {}
        error_cases = [case for case in cases if case["error_formula"]]
        for start in range(0, len(error_cases), 70):
            batch = error_cases[start:start + 70]
            first, last = batch[0]["data_start"], batch[-1]["data_start"]
            address = f"I{first}:I{last}"
            result = read_range(workbook_id, SHEET_NAMES[split], address, runs_root=runs_root)
            if result.get("rows") != last - first + 1 or result.get("columns") != 1 or len(result.get("cells", [])) > 600:
                raise RuntimeError("Microsoft Excel returned an incomplete native error-candidate batch.")
            candidate_cells.update(_cell_map(result))

        read_cases = [case for case in cases if case["category"] == "read_then_test"]
        source_cells = {}
        if read_cases:
            group, groups = [], []
            for case in read_cases:
                proposed = [*group, case]
                area_rows = proposed[-1]["data_end"] - proposed[0]["header_row"] + 1
                if group and area_rows * len(INPUT_HEADERS) > 600:
                    groups.append(group)
                    group = [case]
                else:
                    group = proposed
            if group:
                groups.append(group)
            for batch in groups:
                first, last = batch[0]["header_row"], batch[-1]["data_end"]
                address = f"A{first}:G{last}"
                result = read_range(workbook_id, SHEET_NAMES[split], address, runs_root=runs_root)
                if result.get("rows") != last - first + 1 or result.get("columns") != len(INPUT_HEADERS) or len(result.get("cells", [])) > 600:
                    raise RuntimeError("Microsoft Excel returned an incomplete native input range.")
                source_cells.update(_cell_map(result))

        write_cases = [case for case in cases if case["category"] == "authorized_write_test_read"]

        # Preserve Excel's recalculated native workbook as auditable evidence.
        calculated_path = directory / "workbook.xlsx"
        shutil.copy2(calculated_path, bank_path)
        bank_hash = _sha256_file(bank_path)
        verified = {}
        for case in cases:
            final_value_cell = formula_cells.get(case["formula_cell"])
            if not final_value_cell:
                raise RuntimeError(f"Missing Excel formula readback for {case['id']}.")
            if final_value_cell.get("error"):
                raise RuntimeError(f"Final formula {case['formula']} returned {final_value_cell['error']} in Excel.")
            final_local = final_value_cell.get("formula_local")
            if not isinstance(final_local, str) or not final_local.startswith("="):
                raise RuntimeError(f"Excel did not return a local formula for {case['id']}.")
            candidate = None
            if case["error_formula"]:
                candidate_cell = candidate_cells.get(f"I{case['data_start']}")
                if not candidate_cell or candidate_cell.get("error") not in {"#DIV/0!", "#NAME?", "#REF!"}:
                    raise RuntimeError(f"The candidate formula for {case['id']} did not produce a real Excel error.")
                candidate_local = candidate_cell.get("formula_local")
                if not isinstance(candidate_local, str) or not candidate_local.startswith("="):
                    raise RuntimeError(f"Excel did not return the failing formula for {case['id']}.")
                candidate = {
                    "cell": f"I{case['data_start']}",
                    "formula": case["error_formula"],
                    "formula_local": candidate_local,
                    "value": candidate_cell.get("value"),
                    "error": candidate_cell["error"],
                    "result_sha256": _sha256_bytes(_canonical_json({
                        "formula": case["error_formula"], "formula_local": candidate_local,
                        "value": candidate_cell.get("value"), "error": candidate_cell["error"],
                    })),
                }
            source = {}
            if case["category"] == "read_then_test":
                min_col, min_row, max_col, max_row = range_boundaries(case["read_range"])
                for row in range(min_row, max_row + 1):
                    for column in range(min_col, max_col + 1):
                        address = f"{get_column_letter(column)}{row}"
                        cell = source_cells.get(address)
                        if cell is None:
                            raise RuntimeError(f"Microsoft Excel did not read {SHEET_NAMES[split]}!{address}.")
                        source[address] = cell
            written = final_value_cell if case["category"] == "authorized_write_test_read" else None
            if case["category"] == "authorized_write_test_read":
                if not written or written.get("error"):
                    raise RuntimeError(f"Microsoft Excel did not read back the written formula in {case['formula_cell']}.")
                if written.get("value") != final_value_cell.get("value") or written.get("formula") != case["formula"]:
                    raise RuntimeError(f"Written formula readback diverged from the verified Excel result for {case['id']}.")
            verified[case["id"]] = {
                "engine": "Microsoft Excel",
                "bank_workbook_id": workbook_id,
                "bank_workbook_sha256": bank_hash,
                "formula_cell": case["formula_cell"],
                "formula": case["formula"],
                "value": final_value_cell.get("value"),
                "formula_local": final_local,
                "error": None,
                "candidate": candidate,
                "source_cells": source,
                "written_cell": written,
                "write_snapshot": snapshot,
                "write_cases": [item["id"] for item in write_cases],
                "verification_operation": "mutate_native_write_batch_then_native_read_range",
            }
        return verified


def _context_and_user(case: dict) -> tuple[str, str]:
    relevant_columns = {
        "SUM": (3,), "AVERAGE": (4,), "MIN": (3,), "MAX": (4,),
        "COUNTIF": (5,), "SUMIF": (2, 3), "IF": (3,), "IFERROR": (3,),
        "INDEX_MATCH": (1, 6), "TEXT": (3,), "LEFT": (6,), "CONCAT": (1, 2),
    }[case["formula_family"]]
    header_text = " | ".join(f"{get_column_letter(column)} {INPUT_HEADERS[column - 1]}" for column in relevant_columns)
    values = []
    for offset, record in enumerate(case["records"]):
        address_row = case["data_start"] + offset
        rendered = [format(record[column - 1], ".2f") if isinstance(record[column - 1], float) else str(record[column - 1])
                    for column in relevant_columns]
        values.append(f"{address_row}:" + "|".join(rendered))
    context = f"Sheet {case['worksheet']}; columns {header_text}\n" + "\n".join(values)
    if case["index"] % 25 == 0:
        context += f"\nG note (text): {case['records'][0][6]}"
    language = "en" if case["index"] % 2 == 0 else "pt"
    if language == "en":
        instruction = _instruction("train", case["formula_family"], case["detail"], case["worksheet"])
    else:
        instruction = _instruction("validation", case["formula_family"], _portuguese_detail(case), case["worksheet"])
    if case["category"] == "error_repair":
        if language == "en":
            instruction += (f" I have this candidate formula: {case['error_formula']}. Test it first in I{case['data_start']}; "
                            f"if Excel reports an error, correct it, test the correction in H{case['data_start']}, and return only that formula.")
        else:
            instruction += (f" Tenho esta fórmula candidata: {case['error_formula']}. Teste-a primeiro em I{case['data_start']}; "
                            f"se o Excel retornar erro, corrija-a, teste a correção em H{case['data_start']} e retorne somente a fórmula.")
    elif case["category"] == "read_then_test":
        if language == "en":
            instruction += f" First read {case['read_range']}, then test the formula and return only the formula."
        else:
            instruction += f" Primeiro leia {case['read_range']}, depois teste a fórmula e retorne somente a fórmula."
    elif case["category"] == "authorized_write_test_read":
        if language == "en":
            instruction += f" I authorize writing the formula in H{case['data_start']}, testing it, reading it back, and returning only the formula."
        else:
            instruction += f" Autorizo escrever a fórmula em H{case['data_start']}, testá-la, ler o resultado e retornar somente a fórmula."
    else:
        instruction += " Return only the Excel formula." if language == "en" else " Retorne somente a fórmula do Excel."
    user_content = f"{context}\n\n{instruction}"
    return context, user_content


def _portuguese_detail(case: dict) -> str:
    family = case["formula_family"]
    first, last = case["data_start"], case["data_end"]
    values = case["records"]
    prefix = "T" if case["split"] == "train" else "V"
    if family == "SUM":
        return f"some Units no intervalo C{first}:C{last}"
    if family == "AVERAGE":
        return f"calcule a média de UnitPrice em D{first}:D{last}"
    if family == "MIN":
        return f"encontre o menor valor de Units em C{first}:C{last}"
    if family == "MAX":
        return f"encontre o maior valor de UnitPrice em D{first}:D{last}"
    if family == "COUNTIF":
        return f"conte os Status iguais a {_quoted(f'{prefix}-Open')} em E{first}:E{last}"
    if family == "SUMIF":
        return f"some Units em C{first}:C{last} quando Region em B{first}:B{last} for {_quoted(f'{prefix}-North')}"
    if family == "IF":
        threshold = sorted(record[2] for record in values)[2]
        return f"retorne {_quoted(prefix + '-High')} se C{first} for pelo menos {threshold}, e {_quoted(prefix + '-Low')} caso contrário"
    if family == "IFERROR":
        return f"retorne {_quoted(prefix + '-NA')} quando 1/(C{first}-C{first}) gerar um erro"
    if family == "INDEX_MATCH":
        code = values[case["index"] % len(values)][5]
        return f"localize o Code {_quoted(code)} em F{first}:F{last} e retorne o Item correspondente em A{first}:A{last}"
    if family == "TEXT":
        return f"formate Units em C{first} como texto sem casas decimais"
    if family == "LEFT":
        return f"retorne os cinco primeiros caracteres do Code em F{first}"
    if family == "CONCAT":
        return f"una Item em A{first} e Region em B{first}, separando os valores com {_quoted(' / ')}"
    raise ValueError(f"Unknown formula family: {family}")


def _tool_call(name: str, arguments: dict) -> str:
    _validate_arguments(name, arguments)
    return "<tool_call>" + _json_result({"name": name, "arguments": arguments}) + "</tool_call>"


def _append_tool(messages: list[dict], name: str, arguments: dict, response: dict) -> None:
    content = _tool_call(name, arguments)
    payload = _tool_call_payload(content[len("<tool_call>"):-len("</tool_call>")])
    _validate_arguments(payload["name"], payload["arguments"])
    messages.append({"role": "assistant", "content": content})
    messages.append({"role": "tool", "name": name, "content": _json_result(response)})


def _success_test(case: dict, verification: dict) -> dict:
    return {
        "ok": True,
        "result": {
            "ok": True,
            "formula": case["formula"],
            "formula_local": verification["formula_local"],
            "value": verification["value"],
            "error": None,
            "sheet": case["worksheet"],
            "cell": case["formula_cell"],
            "engine": "Microsoft Excel",
        },
    }


def _failure_test(case: dict, verification: dict) -> dict:
    candidate = verification["candidate"]
    return {
        "ok": False,
        "error": candidate["error"],
        "result": {"ok": False, "error": candidate["error"]},
    }


def _source_range(case: dict, evidence: dict) -> tuple[str, int, int, list[dict]]:
    address = case["read_range"]
    min_col, min_row, max_col, max_row = range_boundaries(address)
    cells = [evidence["source_cells"][f"{get_column_letter(column)}{row}"]
             for row in range(min_row, max_row + 1)
             for column in range(min_col, max_col + 1)]
    return address, max_row - min_row + 1, max_col - min_col + 1, cells


def _write_snapshot(verification: dict) -> dict:
    from dashboard.excel_tools import write_tool_result

    return {"ok": True, "result": write_tool_result(verification["write_snapshot"], 1)}


def _tool_message_evidence(name: str, response: dict, verification_id: str) -> dict:
    return {
        "tool": name,
        "result_sha256": _sha256_bytes(_canonical_json(response)),
        "native_verification_id": verification_id,
    }


def _make_row(case: dict, verified: dict, schemas: list[dict]) -> dict:
    context, user = _context_and_user(case)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
    verification_id = _sha256_bytes(f"{case['source_id']}\n{case['formula']}\n{verified['bank_workbook_sha256']}".encode("utf-8"))
    tool_evidence = []

    def add(name: str, args: dict, response: dict) -> None:
        _append_tool(messages, name, args, response)
        tool_evidence.append(_tool_message_evidence(name, response, verification_id))

    if case["category"] == "error_repair":
        failed = _failure_test(case, verified)
        add("excel_test_formula", {
            "sheet": case["worksheet"], "cell": verified["candidate"]["cell"],
            "formula": verified["candidate"]["formula"],
        }, failed)
        add("excel_test_formula", {
            "sheet": case["worksheet"], "cell": case["formula_cell"], "formula": case["formula"],
        }, _success_test(case, verified))
    elif case["category"] == "read_then_test":
        address, rows, columns, cells = _source_range(case, verified)
        add("excel_read_range", {"sheet": case["worksheet"], "range": address}, {
            "ok": True,
            "result": {"sheet": case["worksheet"], "range": address, "rows": rows,
                       "columns": columns, "cells": cells},
        })
        add("excel_test_formula", {
            "sheet": case["worksheet"], "cell": case["formula_cell"], "formula": case["formula"],
        }, _success_test(case, verified))
    elif case["category"] == "authorized_write_test_read":
        add("excel_write_cells", {"changes": [{
            "sheet": case["worksheet"], "cell": case["formula_cell"], "formula": case["formula"],
        }]}, _write_snapshot(verified))
        add("excel_test_formula", {
            "sheet": case["worksheet"], "cell": case["formula_cell"], "formula": case["formula"],
        }, _success_test(case, verified))
        writeback = verified["written_cell"]
        result = {
            "sheet": case["worksheet"], "range": case["formula_cell"], "rows": 1, "columns": 1,
            "cells": [writeback],
        }
        add("excel_read_range", {"sheet": case["worksheet"], "range": case["formula_cell"]}, {"ok": True, "result": result})
    else:
        add("excel_test_formula", {
            "sheet": case["worksheet"], "cell": case["formula_cell"], "formula": case["formula"],
        }, _success_test(case, verified))
    messages.append({"role": "assistant", "content": case["formula"]})

    input_cells = [
        {"address": f"{get_column_letter(column)}{case['header_row']}", "value": header}
        for column, header in enumerate(INPUT_HEADERS, 1)
    ]
    for offset, record in enumerate(case["records"]):
        row = case["data_start"] + offset
        for column, value in enumerate(record, 1):
            input_cells.append({"address": f"{get_column_letter(column)}{row}", "value": value})
    input_sha = _sha256_bytes(_canonical_json(input_cells))
    candidate_evidence = verified.get("candidate")
    formula_result = {
        "value": verified["value"], "error": None,
        "formula_local": verified["formula_local"],
        "result_sha256": _sha256_bytes(_canonical_json({
            "formula": case["formula"], "value": verified["value"], "formula_local": verified["formula_local"],
        })),
    }
    evidence = {
        "verification_id": verification_id,
        "engine": "Microsoft Excel",
        "worker_module": "dashboard.excel_worker",
        "operation": verified["verification_operation"],
        "bank_workbook_id": verified["bank_workbook_id"],
        "bank_workbook_sha256": verified["bank_workbook_sha256"],
        "input_cells_sha256": input_sha,
        "formula_cell": case["formula_cell"],
        "formula": case["formula"],
        "formula_result": formula_result,
        "candidate_result": candidate_evidence,
        "tool_results": tool_evidence,
    }
    tool_plan = [evidence_row["tool"] for evidence_row in tool_evidence]
    return {
        "id": case["id"],
        "source_id": case["source_id"],
        "category": case["category"],
        "messages": messages,
        "tools": copy.deepcopy(schemas),
        "provenance": {
            "kind": "synthetic_excel_tool_trajectory",
            "generator": "tools.tool_usage_data",
            "generator_version": VERSION,
            "split": case["split"],
            "seed": case["seed"],
            "table_index": case["index"],
            "worksheet": case["worksheet"],
            "table_range": f"A{case['header_row']}:G{case['data_end']}",
            "formula_family": case["formula_family"],
            "tool_plan": tool_plan,
            "numeric_variant": "train_100_to_999" if case["split"] == "train" else "validation_10000_to_99999",
            "text_variant": "T" if case["split"] == "train" else "V",
        },
        "fixture": {
            "sheet": case["worksheet"],
            "input_range": f"A{case['header_row']}:G{case['data_end']}",
            "input_cells": input_cells,
            "formula_cell": case["formula_cell"],
            "formula": case["formula"],
        },
        "formula_evidence": evidence,
    }


def validate_tool_row(row: dict, schemas: list[dict]) -> None:
    if row.get("tools") != schemas:
        raise ValueError(f"{row.get('id')}: tool schemas differ from production.")
    messages = row.get("messages")
    if not isinstance(messages, list) or len(messages) < 5 or messages[0].get("content") != SYSTEM_PROMPT:
        raise ValueError(f"{row.get('id')}: incomplete native conversation.")
    if messages[1].get("role") != "user" or messages[-1].get("role") != "assistant":
        raise ValueError(f"{row.get('id')}: invalid user or final message.")
    if row["source_id"].startswith("nl2f-test-") or "nl2f-test-" in row["source_id"]:
        raise ValueError("Frozen canonical test data cannot be used as synthetic provenance.")
    final_formula = messages[-1].get("content")
    evidence = row.get("formula_evidence")
    if not isinstance(final_formula, str) or not final_formula.startswith("=") or not isinstance(evidence, dict):
        raise ValueError(f"{row.get('id')}: missing final formula or Excel evidence.")
    if evidence.get("formula") != final_formula or evidence.get("engine") != "Microsoft Excel":
        raise ValueError(f"{row.get('id')}: final formula evidence does not match the Excel result.")
    if len(evidence.get("bank_workbook_sha256", "")) != 64 or len(evidence.get("input_cells_sha256", "")) != 64:
        raise ValueError(f"{row.get('id')}: workbook or input evidence hash is missing.")
    tested = set()
    calls = 0
    index = 2
    while index < len(messages) - 1:
        assistant = messages[index]
        tool = messages[index + 1] if index + 1 < len(messages) else {}
        if assistant.get("role") != "assistant" or tool.get("role") != "tool":
            raise ValueError(f"{row['id']}: tool call is not followed by its result.")
        marker = assistant.get("content", "")
        if not marker.startswith("<tool_call>") or not marker.endswith("</tool_call>"):
            raise ValueError(f"{row['id']}: malformed native tool call.")
        payload = _tool_call_payload(marker[len("<tool_call>"):-len("</tool_call>")])
        _validate_arguments(payload["name"], payload["arguments"])
        if payload["name"] != tool.get("name"):
            raise ValueError(f"{row['id']}: tool response name does not match its call.")
        result = json.loads(tool["content"])
        if payload["name"] == "excel_test_formula" and result.get("ok") is True:
            details = result.get("result")
            if not isinstance(details, dict) or details.get("engine") != "Microsoft Excel" or details.get("ok") is not True:
                raise ValueError(f"{row['id']}: formula feedback is not a successful native Excel result.")
            tested.add(payload["arguments"].get("formula"))
        calls += 1
        index += 2
    if index != len(messages) - 1 or calls > 4 or final_formula not in tested:
        raise ValueError(f"{row['id']}: invalid call count or final formula was not Excel-tested.")
    if row["category"] == "error_repair":
        first = json.loads(messages[3]["content"])
        if first.get("ok") is not False or first.get("error") not in {"#DIV/0!", "#NAME?", "#REF!"}:
            raise ValueError(f"{row['id']}: error-repair trajectory has no real Excel error feedback.")


def _write_jsonl(path: Path, rows: list[dict]) -> str:
    content = b"".join(_canonical_json(row) + b"\n" for row in rows)
    path.write_bytes(content)
    return _sha256_bytes(content)


def _validation_summary(rows_by_split: dict[str, list[dict]], token_lengths: dict[str, int], bank_hashes: dict[str, str]) -> dict:
    all_rows = [row for rows in rows_by_split.values() for row in rows]
    category_counts = {split: dict(Counter(row["category"] for row in rows)) for split, rows in rows_by_split.items()}
    total_tool_calls = Counter()
    call_counts_by_category = {category: Counter() for category in CATEGORIES}
    for row in all_rows:
        count = sum(message.get("role") == "tool" for message in row["messages"])
        total_tool_calls[str(count)] += 1
        call_counts_by_category[row["category"]][str(count)] += 1
    train, validation = rows_by_split["train"], rows_by_split["validation"]
    train_source = {row["source_id"] for row in train}
    validation_source = {row["source_id"] for row in validation}
    train_fixture_values = [entry["value"] for row in train for entry in row["fixture"]["input_cells"]]
    validation_fixture_values = [entry["value"] for row in validation for entry in row["fixture"]["input_cells"]]
    train_numbers = {value for value in train_fixture_values if isinstance(value, (int, float)) and not isinstance(value, bool)}
    validation_numbers = {value for value in validation_fixture_values if isinstance(value, (int, float)) and not isinstance(value, bool)}
    if train_source & validation_source:
        raise ValueError("Synthetic train and validation tables overlap.")
    if train_numbers & validation_numbers:
        raise ValueError("Synthetic train and validation numeric variants overlap.")
    return {
        "row_counts_by_category": category_counts,
        "tool_call_counts": dict(total_tool_calls),
        "tool_call_counts_by_category": {category: dict(counts) for category, counts in call_counts_by_category.items()},
        "formula_evidence_checks": {
            "total_rows": len(all_rows),
            "successful_final_formula_tests": sum(row["formula_evidence"]["formula_result"]["error"] is None for row in all_rows),
            "verified_error_candidates": sum(row["formula_evidence"]["candidate_result"] is not None for row in all_rows),
            "engine": "Microsoft Excel",
            "worker_operation": "mutate_native_write_batch_then_native_read_range",
            "bank_workbook_sha256": bank_hashes,
        },
        "disjoint_checks": {
            "unique_ids": len({row["id"] for row in all_rows}) == len(all_rows),
            "unique_sources": len({row["source_id"] for row in all_rows}) == len(all_rows),
            "separate_worksheets": {row["provenance"]["worksheet"] for row in train}.isdisjoint(
                {row["provenance"]["worksheet"] for row in validation}
            ),
            "numeric_variants_disjoint": train_numbers.isdisjoint(validation_numbers),
            "text_variants_disjoint": {row["provenance"]["text_variant"] for row in train}.isdisjoint(
                {row["provenance"]["text_variant"] for row in validation}
            ),
            "independent_seeds": TRAIN_SEED != VALIDATION_SEED,
            "frozen_test_references": 0,
        },
        "token_lengths": token_lengths,
    }


def _token_lengths(rows_by_split: dict[str, list[dict]], tokenizer=None) -> dict[str, int]:
    if tokenizer is None:
        from dashboard.worker import load_tokenizer
        tokenizer = load_tokenizer()
    # The native template preserves key order; match production chat definitions.
    schemas = tool_definitions()
    lengths = []
    for rows in rows_by_split.values():
        for row in rows:
            messages = copy.deepcopy(row["messages"])
            messages[1] = tool_prompt_messages([messages[1]])[0]
            encoded = tokenizer.apply_chat_template(
                messages, tools=schemas, tokenize=True, add_generation_prompt=False,
            )
            if hasattr(encoded, "keys"):
                encoded = encoded.get("input_ids", [])
            if hasattr(encoded, "tolist"):
                encoded = encoded.tolist()
            if encoded and isinstance(encoded[0], list):
                encoded = encoded[0]
            lengths.append(len(encoded))
    ordered = sorted(lengths)
    p95_index = min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "rows": len(ordered),
        "min": min(ordered),
        "median": ordered[len(ordered) // 2],
        "p95": ordered[p95_index],
        "max": max(ordered),
        "over_1536": sum(value > 1536 for value in ordered),
        "max_length_target": 1536,
    }


def build_dataset(output_dir: Path = OUTPUT_DIR, *, measure_tokens: bool = True) -> dict:
    """Create and validate both fixed splits using real isolated Excel calculations."""
    from dashboard.excel_engine import engine_status

    status = engine_status()
    if not status.get("available") or status.get("engine") != "Microsoft Excel":
        raise RuntimeError(status.get("message") or "Microsoft Excel is required to build the tool-use dataset.")
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    bank_dir = output_dir / "native-banks"
    bank_dir.mkdir(exist_ok=True)
    schemas = tool_definitions()
    specs = {"train": (1000, TRAIN_SEED), "validation": (100, VALIDATION_SEED)}
    rows_by_split, bank_hashes = {}, {}
    for split, (count, seed) in specs.items():
        cases = _generate_cases(split, count, seed)
        bank_path = bank_dir / f"{split}.xlsx"
        _write_bank(cases, split, bank_path)
        verified = _verify_bank(cases, split, bank_path, output_dir)
        bank_hashes[split] = _sha256_file(bank_path)
        rows = [_make_row(case, verified[case["id"]], schemas) for case in cases]
        for row in rows:
            validate_tool_row(row, schemas)
        rows_by_split[split] = rows

    token_lengths = _token_lengths(rows_by_split) if measure_tokens else {"rows": 0, "max": 0, "max_length_target": 1536}
    hashes = {}
    for split, rows in rows_by_split.items():
        hashes[split] = _write_jsonl(output_dir / f"{split}.jsonl", rows)
    manifest = {
        "version": VERSION,
        "row_counts": {"train": len(rows_by_split["train"]), "validation": len(rows_by_split["validation"])},
        "sha256": hashes,
        "validation": _validation_summary(rows_by_split, token_lengths, bank_hashes),
    }
    if manifest["row_counts"] != {"train": 1000, "validation": 100}:
        raise ValueError("The generated split sizes do not match the fixed tool-use training contract.")
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the deterministic, Excel-verified tool-usage dataset.")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--skip-token-measurement", action="store_true")
    args = parser.parse_args()
    manifest = build_dataset(args.output_dir, measure_tokens=not args.skip_token_measurement)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
