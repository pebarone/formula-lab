"""Evaluate an Excel-tool adapter on held-out synthetic trajectories."""

import argparse
import base64
import hashlib
import json
import random
import uuid
from collections import Counter
from datetime import datetime, timezone
from io import BytesIO

from benchmark.run_baseline import prompt_fingerprint
from benchmark.scoring import extract_prediction, formulas_match
from dashboard.config import read_json, run_path, validate_config
from dashboard.data import file_hash
from dashboard.tool_training import (
    TOOL_USAGE_DIRECTORY, _canonical_json, _load_jsonl, _sha256_bytes, _tool_prompt_fingerprint, _validate_tool_row,
)

CATEGORIES = ("propose_test_final", "error_repair", "read_then_test", "authorized_write_test_read")


def balanced_sample(rows, count, seed=20261011):
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
        raise ValueError("count deve ser um inteiro de 1 a 100.")
    groups = {category: [] for category in CATEGORIES}
    for row in rows:
        if row.get("category") not in groups:
            raise ValueError(f"Categoria de validação não reconhecida: {row.get('category')}.")
        groups[row["category"]].append(row)
    if count > len(rows) or any(not groups[c] for c in CATEGORIES):
        raise ValueError("A validação precisa conter exemplos suficientes de todas as categorias.")
    rng = random.Random(seed)
    for group in groups.values():
        rng.shuffle(group)
    selected, used = [], Counter()
    while len(selected) < count:
        available = [c for c in CATEGORIES if used[c] < len(groups[c])]
        category = min(available, key=lambda c: (used[c], CATEGORIES.index(c)))
        selected.append(groups[category][used[category]])
        used[category] += 1
    return selected


def _fixture_xlsx(row):
    """Materialize fixture literals; formula-like strings remain text."""
    from openpyxl import Workbook
    from openpyxl.utils.cell import range_boundaries

    fixture = row["fixture"]
    bounds = range_boundaries(fixture.get("input_range", ""))
    min_col, min_row, max_col, max_row = bounds
    book = Workbook()
    sheet = book.active
    sheet.title = fixture["sheet"]
    cells = fixture.get("input_cells")
    if not isinstance(cells, list) or not cells:
        raise ValueError(f"{row['id']}: fixture sem células de entrada.")
    for item in cells:
        if not isinstance(item, dict) or not isinstance(item.get("address"), str) or "value" not in item:
            raise ValueError(f"{row['id']}: célula de entrada inválida.")
        cell = sheet[item["address"]]
        if not (min_col <= cell.column <= max_col and min_row <= cell.row <= max_row):
            raise ValueError(f"{row['id']}: célula fora do input_range do fixture.")
        cell.value = item["value"]
        if isinstance(cell.value, str):
            cell.data_type = "s"
    output = BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def _successful_formula_test(call, formula):
    arguments, result = call.get("arguments"), call.get("result")
    details = result.get("result") if isinstance(result, dict) else None
    return bool(
        call.get("name") == "excel_test_formula" and isinstance(arguments, dict)
        and formulas_match(formula, arguments.get("formula", ""))
        and isinstance(result, dict) and result.get("ok") is True
        and isinstance(details, dict) and details.get("ok") is True
        and details.get("engine") == "Microsoft Excel"
        and formulas_match(formula, details.get("formula", ""))
        and details.get("error") is None and not details.get("warning") and not details.get("truncated")
    )


def _extra_escaped_quotes(predicted, expected):
    return max(0, predicted.count(r'\"') - expected.count(r'\"'))


def evaluate_item(row, generate, save_attachment, create_workbook, test_formula, events):
    from dashboard.excel_tools import run_tool_conversation
    from dashboard.runtime import Cancelled

    expected = row["messages"][-1]["content"]
    try:
        attachment = save_attachment("tool-validation.xlsx", base64.b64encode(_fixture_xlsx(row)).decode("ascii"))
        pristine, working = (create_workbook(attachment["id"]) for _ in range(2))
        # Teacher assistant/tool turns and the expected final answer must never enter the prompt.
        conversation = run_tool_conversation([dict(row["messages"][1])], generate, working["id"],
                                             max_rounds=4, max_calls=4, check_cancel=events.check)
        predicted, trace = extract_prediction(conversation["text"]), conversation.get("tool_trace", [])
        tested = [_successful_formula_test(call, predicted) for call in trace]
        failed = [i for i, call in enumerate(trace) if call.get("name") == "excel_test_formula"
                  and isinstance(call.get("result"), dict) and call["result"].get("ok") is False]
        repaired = bool(row["category"] == "error_repair" and formulas_match(expected, predicted)
                        and any(error < i for error in failed for i, ok in enumerate(tested) if ok))
        execution, execution_error, execution_ok = None, None, False
        if predicted.startswith("="):
            try:
                execution = test_formula(pristine["id"], row["fixture"]["sheet"],
                                         row["fixture"]["formula_cell"], predicted)
                execution_ok = bool(isinstance(execution, dict) and execution.get("ok") is True
                                    and execution.get("engine") == "Microsoft Excel"
                                    and execution.get("error") is None
                                    and not execution.get("warning") and not execution.get("truncated"))
            except Exception as exc:
                execution_error = f"{type(exc).__name__}: {exc}"
        else:
            execution_error = "A resposta final não contém uma fórmula do Excel."
        extra_quotes = _extra_escaped_quotes(predicted, expected)
        return {
            "id": row["id"], "source_id": row["source_id"], "category": row["category"],
            "expected_formula": expected, "predicted_formula": predicted,
            "formula_match": formulas_match(expected, predicted), "tools_used": bool(trace),
            "tool_calls": conversation.get("tool_calls", 0), "tool_trace": trace,
            "successful_final_formula_tested": any(tested), "error_repaired_and_retested": repaired,
            "formula_execution": execution, "formula_execution_ok": execution_ok,
            "execution_error": execution_error, "extra_escaped_quotes": extra_quotes,
            "budget_exhausted": bool(conversation.get("budget_exhausted")),
        }
    except Cancelled:
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        return {
            "id": row["id"], "source_id": row["source_id"], "category": row["category"],
            "expected_formula": expected, "predicted_formula": "", "formula_match": False,
            "tools_used": False, "tool_calls": 0, "tool_trace": [],
            "successful_final_formula_tested": False, "error_repaired_and_retested": False,
            "formula_execution": None, "formula_execution_ok": False, "execution_error": error,
            "extra_escaped_quotes": 0, "budget_exhausted": False, "error": error,
        }


def _adapter(adapter_run):
    from dashboard.excel_tools import tool_definitions

    directory = run_path(adapter_run)
    request, status = read_json(directory / "request.json", {}), read_json(directory / "status.json", {})
    selection = read_json(directory / "selection.json", {})
    info = selection.get("tool_usage", {})
    schemas = tool_definitions()
    adapter = directory / "adapter"
    weights = adapter / "adapter_model.safetensors"
    if (request.get("kind") != "train" or request.get("training_mode") != "tool_usage"
            or status.get("status") != "completed" or request.get("adapter_run") != info.get("source_adapter_run")
            or selection.get("prompt_fingerprint") != prompt_fingerprint()
            or info.get("tool_prompt_fingerprint") != _tool_prompt_fingerprint(schemas)
            or info.get("tool_schema_fingerprint") != _sha256_bytes(_canonical_json(schemas))
            or (info.get("tool_train_examples"), info.get("rehearsal_train_examples"), info.get("validation_examples")) != (1000, 200, 100)
            or not weights.is_file() or not (adapter / "adapter_config.json").is_file()):
        raise ValueError("A execução selecionada não comprova um adaptador de ferramentas concluído e compatível.")
    return directory, request, selection, info, hashlib.sha256(weights.read_bytes()).hexdigest()


def _validation_rows(info):
    from dashboard.excel_tools import tool_definitions

    manifest_path, rows_path = TOOL_USAGE_DIRECTORY / "manifest.json", TOOL_USAGE_DIRECTORY / "validation.jsonl"
    manifest = read_json(manifest_path)
    digest = file_hash(rows_path)
    if (not isinstance(manifest, dict) or manifest.get("version") != 1
            or manifest.get("row_counts") != {"train": 1000, "validation": 100}
            or manifest.get("sha256", {}).get("validation") != digest
            or info.get("dataset_sha256", {}).get("validation") != digest
            or info.get("dataset_manifest_sha256") != file_hash(manifest_path)):
        raise ValueError("O split de validação mudou desde o treinamento ou seu manifesto é inválido.")
    rows = _load_jsonl(rows_path)
    if len(rows) != 100:
        raise ValueError("A validação precisa conter exatamente 100 trajetórias.")
    seen = set()
    for row in rows:
        _validate_tool_row(row, tool_definitions())
        if row.get("provenance", {}).get("split") != "validation" or row["source_id"] in seen:
            raise ValueError("Origem duplicada ou split incorreto na validação.")
        seen.add(row["source_id"])
    return rows, digest


def run_evaluation(adapter_run, count=12, seed=20261011):
    from dashboard.attachments import save_attachment
    from dashboard.chat_backend import generate_chat_turn
    from dashboard.chat_engine import Resident, release_resident_model
    from dashboard.runtime import Events, worker_lock
    from dashboard.training import limit_training_memory, training_memory
    from dashboard.workbooks import create_workbook, test_formula
    from dashboard.worker import environment

    directory, request, selection, info, adapter_hash = _adapter(adapter_run)
    report_path, jsonl_path = directory / "tool_validation_report.json", directory / "tool_validation_predictions.jsonl"
    if report_path.exists() or jsonl_path.exists():
        raise FileExistsError("Esta execução já tem relatório de validação e não será sobrescrita.")
    rows, validation_hash = _validation_rows(info)
    selected = balanced_sample(rows, count, seed)
    events, config = Events(directory), validate_config(request.get("config", {}))
    release_resident_model()
    with worker_lock():
        memory = limit_training_memory(config, events, operation="validação sintética de ferramentas")
        resident = Resident()
        model, tokenizer = resident.model_for(adapter_run, events), resident.tokenizer(events)

        def generate(history, tools):
            events.check()
            return generate_chat_turn(history, tokenizer, model, resident.decoder, events,
                                      tools=tools, max_new_tokens=512, adapter_run=adapter_run)["text"]

        items = []
        for index, row in enumerate(selected, 1):
            events.check()
            events.emit(phase="Validando trajetória de ferramenta", step=index - 1,
                        total_steps=count, phase_unit="exemplos")
            items.append(evaluate_item(row, generate, save_attachment, create_workbook, test_formula, events))
        metrics = {
            "formula_matches": sum(item["formula_match"] for item in items),
            "formula_match_rate": sum(item["formula_match"] for item in items) / len(items),
            "used_tools": sum(item["tools_used"] for item in items),
            "successful_final_formula_tested": sum(item["successful_final_formula_tested"] for item in items),
            "error_repaired_and_retested": sum(item["error_repaired_and_retested"] for item in items),
            "formula_execution_ok": sum(item["formula_execution_ok"] for item in items),
            "extra_escaped_quotes": sum(item["extra_escaped_quotes"] for item in items),
            "items_with_errors": sum(bool(item.get("error") or item.get("execution_error")) for item in items),
            "tool_calls": sum(item["tool_calls"] for item in items),
            "tool_error_responses": sum(not call.get("result", {}).get("ok", False)
                                         for item in items for call in item["tool_trace"]),
        }
        report = {"status": "completed", "created_at": datetime.now(timezone.utc).isoformat(),
            "scope": "Synthetic held-out tool trajectories; not the frozen 500-formula test metric.",
            "metric": "normalized_formula_match", "adapter_run": adapter_run,
            "adapter_sha256": adapter_hash, "source_adapter_run": info.get("source_adapter_run"),
            "source_adapter_sha256": info.get("source_adapter_sha256"),
            "prompt_fingerprint": selection.get("prompt_fingerprint"),
            "tool_prompt_fingerprint": info.get("tool_prompt_fingerprint"),
            "validation_sha256": validation_hash,
            "manifest_sha256": file_hash(TOOL_USAGE_DIRECTORY / "manifest.json"),
            "requested_count": count, "evaluated_count": len(items), "seed": seed,
            "category_counts": dict(Counter(item["category"] for item in items)),
            "generation": {"do_sample": False, "max_new_tokens_per_turn": 512, "max_tool_rounds": 4, "max_tool_calls": 4},
            "engine": "Microsoft Excel",
            "memory": {**memory, **training_memory()}, "environment": environment(),
            "metrics": metrics, "items": items}
        report_temp = report_path.with_name(f".{report_path.name}.{uuid.uuid4().hex}.tmp")
        jsonl_temp = jsonl_path.with_name(f".{jsonl_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            report_temp.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            jsonl_temp.write_text("".join(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n" for item in items), encoding="utf-8")
            jsonl_temp.replace(jsonl_path)
            report_temp.replace(report_path)
        finally:
            report_temp.unlink(missing_ok=True)
            jsonl_temp.unlink(missing_ok=True)
        events.emit(phase="Validação sintética concluída", step=count, total_steps=count,
                    formula_match_rate=metrics["formula_match_rate"])
        del model, resident
        return report


def main():
    parser = argparse.ArgumentParser(description="Avaliar um adaptador no split sintético held-out de ferramentas.")
    parser.add_argument("--adapter-run", required=True)
    parser.add_argument("--count", type=int, default=12, help="Quantidade balanceada entre categorias (1–100).")
    parser.add_argument("--seed", type=int, default=20261011)
    args = parser.parse_args()
    if not 1 <= args.count <= 100:
        parser.error("--count deve estar entre 1 e 100.")
    report = run_evaluation(args.adapter_run, args.count, args.seed)
    print(json.dumps({"report": str(run_path(args.adapter_run) / "tool_validation_report.json"),
                      "metrics": report["metrics"], "category_counts": report["category_counts"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
