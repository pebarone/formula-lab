"""Prepare the fixed Excel-tool continuation dataset for the normal Trainer."""

import hashlib
import json
import math
import random
import re
import uuid
from collections import Counter
from pathlib import Path

from benchmark.prompting import SYSTEM_PROMPT
from benchmark.run_baseline import prompt_fingerprint
from dashboard.adapters import training_adapter
from dashboard.config import ROOT, read_json, write_json
from dashboard.data import encode_row, file_hash, load_splits

TOOL_USAGE_DIRECTORY = ROOT / "runs" / "tool-usage-dataset"
TOOL_USAGE_PARENT_RUN = "20260929-183918-1387e297"
TOOL_USAGE_PARENT_ADAPTER_SHA256 = "f3bc94cd4b6eaf81372f1b69936ffcd43a8e04af24fa5f432c4ebb0c8e7fec30"
TOOL_USAGE_TRAIN_COUNT = 1000
TOOL_USAGE_VALIDATION_COUNT = 100
TOOL_USAGE_REHEARSAL_COUNT = 200
TOOL_USAGE_TOTAL_TRAIN_COUNT = TOOL_USAGE_TRAIN_COUNT + TOOL_USAGE_REHEARSAL_COUNT


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _load_jsonl(path):
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                raise ValueError(f"Linha vazia em {path.name}:{line_number}.")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSON inválido em {path.name}:{line_number}: {exc.msg}.") from exc
            if not isinstance(row, dict):
                raise ValueError(f"Registro inválido em {path.name}:{line_number}.")
            rows.append(row)
    return rows


def _read_tool_dataset():
    """Read and verify the fixed, generated dataset; callers cannot choose a path."""
    from dashboard.excel_tools import tool_definitions

    directory = TOOL_USAGE_DIRECTORY
    manifest_path = directory / "manifest.json"
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("Manifesto do dataset de ferramentas ausente ou incompatível.")
    train_path, validation_path = directory / "train.jsonl", directory / "validation.jsonl"
    hashes = {"train": file_hash(train_path), "validation": file_hash(validation_path)}
    expected_hashes = manifest.get("sha256")
    expected_counts = manifest.get("row_counts")
    if expected_hashes != hashes:
        raise ValueError("Hash dos dados de uso de ferramentas não confere com o manifesto.")
    if expected_counts != {"train": TOOL_USAGE_TRAIN_COUNT, "validation": TOOL_USAGE_VALIDATION_COUNT}:
        raise ValueError("O manifesto precisa declarar exatamente 1.000 exemplos de treino e 100 de validação.")
    validation = manifest.get("validation")
    required_validation = {
        "row_counts_by_category", "tool_call_counts", "tool_call_counts_by_category",
        "formula_evidence_checks", "disjoint_checks", "token_lengths",
    }
    if not isinstance(validation, dict) or not required_validation <= validation.keys():
        raise ValueError("O manifesto não contém a validação independente exigida para o dataset.")

    train, held_out = _load_jsonl(train_path), _load_jsonl(validation_path)
    if len(train) != TOOL_USAGE_TRAIN_COUNT or len(held_out) != TOOL_USAGE_VALIDATION_COUNT:
        raise ValueError("As quantidades de linhas do dataset diferem do manifesto.")
    schemas = tool_definitions()
    seen_ids, seen_sources = set(), {"train": set(), "validation": set()}
    for split_name, rows in (("train", train), ("validation", held_out)):
        for row in rows:
            _validate_tool_row(row, schemas)
            if row["provenance"].get("split") != split_name:
                raise ValueError(f"{row['id']}: provenance não corresponde ao arquivo {split_name}.")
            if row["id"] in seen_ids:
                raise ValueError(f"ID duplicado no dataset de ferramentas: {row['id']}.")
            seen_ids.add(row["id"])
            seen_sources[split_name].add(row["source_id"])
    if seen_sources["train"] & seen_sources["validation"]:
        raise ValueError("A validação compartilha origem com o treino de ferramentas.")
    _validate_dataset_summary(train, held_out, validation)
    return train, held_out, manifest, hashes


def _validate_dataset_summary(train, held_out, summary):
    rows_by_split = {"train": train, "validation": held_out}
    category_counts = {
        split: dict(Counter(row["category"] for row in rows))
        for split, rows in rows_by_split.items()
    }
    tool_counts, category_tool_counts = Counter(), {}
    for rows in rows_by_split.values():
        for row in rows:
            count = sum(message["role"] == "tool" for message in row["messages"])
            tool_counts[str(count)] += 1
            category_tool_counts.setdefault(row["category"], Counter())[str(count)] += 1
    category_tool_counts = {category: dict(counts) for category, counts in sorted(category_tool_counts.items())}
    if summary.get("row_counts_by_category") != category_counts:
        raise ValueError("Resumo de categorias não corresponde às linhas do dataset.")
    if summary.get("tool_call_counts") != dict(tool_counts):
        raise ValueError("Resumo de chamadas de ferramentas não corresponde às trajetórias.")
    if summary.get("tool_call_counts_by_category") != category_tool_counts:
        raise ValueError("Resumo de chamadas por categoria não corresponde às trajetórias.")

    formula_checks = summary.get("formula_evidence_checks", {})
    expected_formula_checks = {
        "total_rows": len(train) + len(held_out),
        "successful_final_formula_tests": len(train) + len(held_out),
        "verified_error_candidates": sum(
            row["formula_evidence"]["candidate_result"] is not None
            for rows in rows_by_split.values() for row in rows
        ),
        "engine": "Microsoft Excel",
        "worker_operation": "mutate_native_write_batch_then_native_read_range",
        "bank_workbook_sha256": {
            split: rows[0]["formula_evidence"]["bank_workbook_sha256"]
            for split, rows in rows_by_split.items()
        },
    }
    for rows in rows_by_split.values():
        if len({row["formula_evidence"]["bank_workbook_sha256"] for row in rows}) != 1:
            raise ValueError("Um split usa mais de uma pasta de trabalho de verificação.")
    if formula_checks != expected_formula_checks:
        raise ValueError("Resumo de evidência do Excel não corresponde às trajetórias verificadas.")
    token_lengths = summary.get("token_lengths", {})
    expected_length_fields = {"rows", "min", "median", "p95", "max", "over_1536", "max_length_target"}
    if (not isinstance(token_lengths, dict) or set(token_lengths) != expected_length_fields
            or token_lengths.get("rows") != len(train) + len(held_out)
            or token_lengths.get("max_length_target") != 1536
            or any(isinstance(token_lengths.get(key), bool) or not isinstance(token_lengths.get(key), int)
                   for key in expected_length_fields)
            or not 0 < token_lengths["min"] <= token_lengths["median"] <= token_lengths["max"]
            or not token_lengths["min"] <= token_lengths["p95"] <= token_lengths["max"]
            or not 0 <= token_lengths["over_1536"] <= token_lengths["rows"]):
        raise ValueError("Resumo de comprimentos de tokens ausente ou inválido.")

    train_sources = {row["source_id"] for row in train}
    validation_sources = {row["source_id"] for row in held_out}
    train_sheets = {row["provenance"]["worksheet"] for row in train}
    validation_sheets = {row["provenance"]["worksheet"] for row in held_out}
    train_text = {row["provenance"]["text_variant"] for row in train}
    validation_text = {row["provenance"]["text_variant"] for row in held_out}
    train_numbers = {
        cell["value"] for row in train for cell in row["fixture"]["input_cells"]
        if isinstance(cell["value"], (int, float)) and not isinstance(cell["value"], bool)
    }
    validation_numbers = {
        cell["value"] for row in held_out for cell in row["fixture"]["input_cells"]
        if isinstance(cell["value"], (int, float)) and not isinstance(cell["value"], bool)
    }
    train_seeds = {row["provenance"]["seed"] for row in train}
    validation_seeds = {row["provenance"]["seed"] for row in held_out}
    expected_disjoint = {
        "unique_ids": len({row["id"] for rows in rows_by_split.values() for row in rows}) == len(train) + len(held_out),
        "unique_sources": len(train_sources | validation_sources) == len(train_sources) + len(validation_sources),
        "separate_worksheets": train_sheets.isdisjoint(validation_sheets),
        "numeric_variants_disjoint": train_numbers.isdisjoint(validation_numbers),
        "text_variants_disjoint": train_text.isdisjoint(validation_text),
        "independent_seeds": len(train_seeds) == 1 and len(validation_seeds) == 1 and train_seeds != validation_seeds,
        "frozen_test_references": 0,
    }
    if summary.get("disjoint_checks") != expected_disjoint or not all(
        value is True for key, value in expected_disjoint.items() if key != "frozen_test_references"
    ):
        raise ValueError("Os checks de separação dos splits falharam ou diferem do manifesto.")


def _validate_tool_row(row, schemas):
    from dashboard.excel_tools import (
        MAX_TOOL_CALLS,
        _TOOL_CALL_RE,
        _tool_call_payload,
        _validate_arguments,
    )

    if not isinstance(row.get("id"), str) or not row["id"]:
        raise ValueError("Exemplo de ferramentas sem ID.")
    if not isinstance(row.get("source_id"), str) or not row["source_id"]:
        raise ValueError(f"{row['id']}: source_id ausente.")
    if "nl2f-test-" in row["source_id"]:
        raise ValueError(f"{row['id']}: o teste oficial congelado não pode ser origem do treino.")
    if not isinstance(row.get("category"), str) or not row["category"].strip():
        raise ValueError(f"{row['id']}: categoria ausente.")
    messages = row.get("messages")
    if not isinstance(messages, list) or len(messages) < 5:
        raise ValueError(f"{row['id']}: trajetória de ferramentas incompleta.")
    if any(not isinstance(message, dict) for message in messages):
        raise ValueError(f"{row['id']}: formato de mensagem inválido.")
    if messages[0].get("role") != "system" or messages[1].get("role") != "user" or messages[-1].get("role") != "assistant":
        raise ValueError(f"{row['id']}: a trajetória precisa começar com system/user e terminar com assistant.")
    if messages[0].get("content") != SYSTEM_PROMPT:
        raise ValueError(f"{row['id']}: system prompt não corresponde ao chat do Excel.")
    if row.get("tools") != schemas:
        raise ValueError(f"{row['id']}: os esquemas de ferramentas diferem dos usados no chat.")
    for message in messages:
        role = message.get("role") if isinstance(message, dict) else None
        allowed = {"role", "content", "name"} if role == "tool" else {"role", "content"}
        if role not in {"system", "user", "assistant", "tool"} or set(message) - allowed:
            raise ValueError(f"{row['id']}: formato de mensagem inválido.")
        if not isinstance(message.get("content"), str):
            raise ValueError(f"{row['id']}: conteúdo de mensagem inválido.")
        if role == "tool" and not isinstance(message.get("name"), str):
            raise ValueError(f"{row['id']}: resposta de ferramenta sem nome.")

    final_formula = messages[-1]["content"]
    if not final_formula.startswith("=") or final_formula.strip() != final_formula or "\n" in final_formula:
        raise ValueError(f"{row['id']}: a resposta final precisa ser somente uma fórmula iniciada por '='.")
    evidence = row.get("formula_evidence")
    if not isinstance(evidence, dict) or evidence.get("formula") != final_formula:
        raise ValueError(f"{row['id']}: evidência da fórmula final ausente ou divergente.")
    fixture = row.get("fixture")
    provenance = row.get("provenance")
    if not isinstance(fixture, dict) or not isinstance(provenance, dict):
        raise ValueError(f"{row['id']}: fixture e proveniência são obrigatórios.")
    if fixture.get("formula") != final_formula or fixture.get("formula_cell") != evidence.get("formula_cell"):
        raise ValueError(f"{row['id']}: fixture da fórmula ausente ou divergente.")
    if (evidence.get("engine") != "Microsoft Excel"
            or evidence.get("worker_module") != "dashboard.excel_worker"
            or evidence.get("operation") != "mutate_native_write_batch_then_native_read_range"):
        raise ValueError(f"{row['id']}: evidência não identifica a verificação nativa do Excel.")
    verification_id = evidence.get("verification_id")
    if not isinstance(verification_id, str) or not re.fullmatch(r"[0-9a-f]{64}", verification_id):
        raise ValueError(f"{row['id']}: ID da verificação nativa é inválido.")
    try:
        workbook_id = str(uuid.UUID(evidence.get("bank_workbook_id", "")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{row['id']}: ID da pasta de trabalho de verificação inválido.") from exc
    if workbook_id != evidence["bank_workbook_id"].lower():
        raise ValueError(f"{row['id']}: ID da pasta de trabalho não está normalizado.")
    workbook_hash, input_hash = evidence.get("bank_workbook_sha256"), evidence.get("input_cells_sha256")
    if (not isinstance(workbook_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", workbook_hash)
            or not isinstance(input_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", input_hash)):
        raise ValueError(f"{row['id']}: hash da pasta de trabalho ou das entradas é inválido.")
    if not isinstance(fixture.get("input_cells"), list) or _sha256_bytes(
        _canonical_json(fixture["input_cells"])
    ) != input_hash:
        raise ValueError(f"{row['id']}: hash das células de entrada difere do fixture.")
    if any(not isinstance(cell, dict) or not isinstance(cell.get("address"), str) or "value" not in cell
           for cell in fixture["input_cells"]):
        raise ValueError(f"{row['id']}: células do fixture estão incompletas.")
    if (not isinstance(provenance.get("split"), str) or not isinstance(provenance.get("worksheet"), str)
            or not isinstance(provenance.get("text_variant"), str)
            or isinstance(provenance.get("seed"), bool) or not isinstance(provenance.get("seed"), int)):
        raise ValueError(f"{row['id']}: proveniência incompleta.")
    expected_verification_id = _sha256_bytes(
        f"{row['source_id']}\n{final_formula}\n{workbook_hash}".encode("utf-8")
    )
    if verification_id != expected_verification_id:
        raise ValueError(f"{row['id']}: ID da verificação não corresponde à origem e aos hashes.")
    formula_evidence = evidence.get("formula_result")
    if (not isinstance(formula_evidence, dict)
            or formula_evidence.get("error") is not None
            or not isinstance(formula_evidence.get("formula_local"), str)
            or not formula_evidence["formula_local"].startswith("=")):
        raise ValueError(f"{row['id']}: resultado ouro do Excel está ausente ou inválido.")
    expected_formula_hash = _sha256_bytes(_canonical_json({
        "formula": final_formula,
        "value": formula_evidence.get("value"),
        "formula_local": formula_evidence.get("formula_local"),
    }))
    if formula_evidence.get("result_sha256") != expected_formula_hash:
        raise ValueError(f"{row['id']}: hash do resultado ouro do Excel não confere.")

    calls = 0
    tested = {}
    failed_tests = []
    observed_tool_results = []
    index = 2
    while index < len(messages) - 1:
        assistant, tool = messages[index], messages[index + 1]
        if assistant.get("role") != "assistant" or tool.get("role") != "tool":
            raise ValueError(f"{row['id']}: cada chamada de ferramenta precisa ter uma resposta consecutiva.")
        content = assistant["content"]
        matches = list(_TOOL_CALL_RE.finditer(content))
        if len(matches) != 1 or content.strip() != matches[0].group(0):
            raise ValueError(f"{row['id']}: cada turno de ferramenta deve conter exatamente uma chamada nativa.")
        if "<tool_call" in _TOOL_CALL_RE.sub("", content) or "</tool_call>" in _TOOL_CALL_RE.sub("", content):
            raise ValueError(f"{row['id']}: marcação de chamada de ferramenta malformada.")
        try:
            payload = _tool_call_payload(matches[0].group(1))
            _validate_arguments(payload["name"], payload["arguments"])
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{row['id']}: chamada de ferramenta inválida: {exc}") from exc
        if tool.get("name") != payload["name"]:
            raise ValueError(f"{row['id']}: nome da resposta de ferramenta não corresponde à chamada.")
        try:
            result = json.loads(tool["content"])
        except json.JSONDecodeError as exc:
            raise ValueError(f"{row['id']}: resposta da ferramenta não é JSON válido.") from exc
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise ValueError(f"{row['id']}: resposta de ferramenta precisa registrar ok como booleano.")
        if result["ok"] and "result" not in result:
            raise ValueError(f"{row['id']}: resposta bem-sucedida sem resultado.")
        if not result["ok"] and not isinstance(result.get("error"), str):
            raise ValueError(f"{row['id']}: falha da ferramenta sem motivo.")
        observed_tool_results.append({
            "tool": payload["name"],
            "result_sha256": _sha256_bytes(_canonical_json(result)),
            "native_verification_id": verification_id,
        })
        if payload["name"] == "excel_test_formula" and not result["ok"]:
            failed_tests.append((payload["arguments"], result))
        if payload["name"] == "excel_test_formula" and result["ok"]:
            formula_result = result["result"]
            if (not isinstance(formula_result, dict) or formula_result.get("ok") is not True
                    or formula_result.get("engine") != "Microsoft Excel"
                    or formula_result.get("formula") != payload["arguments"]["formula"]
                    or formula_result.get("sheet") != payload["arguments"]["sheet"]
                    or formula_result.get("cell") != payload["arguments"]["cell"]
                    or formula_result.get("error") is not None):
                raise ValueError(f"{row['id']}: fórmula testada sem resultado Excel válido.")
            if formula_result.get("warning") or formula_result.get("truncated"):
                raise ValueError(f"{row['id']}: resultado de fórmula incompleto ou truncado.")
            tested[payload["arguments"]["formula"]] = formula_result
        calls += 1
        if calls > MAX_TOOL_CALLS:
            raise ValueError(f"{row['id']}: trajetória excede o limite de chamadas do chat.")
        index += 2
    if index != len(messages) - 1:
        raise ValueError(f"{row['id']}: mensagem final fora da sequência de ferramentas.")
    if final_formula not in tested:
        raise ValueError(f"{row['id']}: fórmula final não foi testada com sucesso pelo Excel.")
    final_result = tested[final_formula]
    if (final_result.get("formula_local") != formula_evidence["formula_local"]
            or final_result.get("value") != formula_evidence["value"]
            or final_result.get("error") != formula_evidence["error"]
            or final_result.get("cell") != evidence.get("formula_cell")):
        raise ValueError(f"{row['id']}: resultado do teste final diverge da evidência do Excel.")
    if evidence.get("tool_results") != observed_tool_results:
        raise ValueError(f"{row['id']}: hashes das respostas de ferramentas diferem da evidência.")

    candidate = evidence.get("candidate_result")
    if row["category"] == "error_repair":
        if len(failed_tests) != 1 or not isinstance(candidate, dict):
            raise ValueError(f"{row['id']}: evidência de fórmula candidata com erro ausente.")
        arguments, failed_result = failed_tests[0]
        candidate_hash = _sha256_bytes(_canonical_json({
            "formula": candidate.get("formula"), "formula_local": candidate.get("formula_local"),
            "value": candidate.get("value"), "error": candidate.get("error"),
        }))
        if (candidate.get("formula") != arguments.get("formula")
                or candidate.get("cell") != arguments.get("cell")
                or candidate.get("error") != failed_result.get("error")
                or candidate.get("error") not in {"#DIV/0!", "#NAME?", "#REF!"}
                or not isinstance(candidate.get("formula_local"), str)
                or not candidate["formula_local"].startswith("=")
                or candidate.get("result_sha256") != candidate_hash):
            raise ValueError(f"{row['id']}: resultado candidato não corresponde ao erro real do Excel.")
    elif candidate is not None or failed_tests:
        raise ValueError(f"{row['id']}: erro candidato inesperado fora da categoria error_repair.")


def _tool_prompt_fingerprint(schemas):
    from dashboard.excel_tools import tool_prompt_messages

    user_message = tool_prompt_messages([{"role": "user", "content": "prompt fingerprint"}])[0]
    recipe = {"system_prompt": SYSTEM_PROMPT, "tool_user_suffix": user_message["content"][len("prompt fingerprint"):],
              "tools": schemas}
    return _sha256_bytes(_canonical_json(recipe))


def _token_length_summary(lengths):
    ordered = sorted(lengths)
    if not ordered:
        raise ValueError("O dataset de ferramentas está vazio.")
    p95_index = min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "rows": len(ordered), "min": ordered[0], "median": ordered[len(ordered) // 2],
        "p95": ordered[p95_index], "max": ordered[-1],
        "over_1536": sum(length > 1536 for length in ordered), "max_length_target": 1536,
    }


def _encode_tool_row(tokenizer, row, schemas):
    from dashboard.excel_tools import tool_prompt_messages

    messages = [dict(message) for message in row["messages"]]
    messages[1] = tool_prompt_messages([messages[1]])[0]

    def encode(prefix, generation=False):
        value = tokenizer.apply_chat_template(
            prefix, tools=schemas, tokenize=True, add_generation_prompt=generation, return_dict=True,
        )
        ids = value["input_ids"] if hasattr(value, "keys") else value
        if hasattr(ids, "tolist"):
            ids = ids.tolist()
        if ids and isinstance(ids[0], list):
            if len(ids) != 1:
                raise ValueError("O tokenizer retornou mais de uma sequência para um exemplo.")
            ids = ids[0]
        return list(ids)

    full = encode(messages)
    labels = [-100] * len(full)
    eos_id = tokenizer.eos_token_id
    if isinstance(eos_id, list):
        eos_ids = set(eos_id)
    elif eos_id is None:
        raise ValueError("O tokenizer não declara o token de fim de mensagem.")
    else:
        eos_ids = {eos_id}
    for index, message in enumerate(messages):
        if message["role"] != "assistant":
            continue
        before = encode(messages[:index], generation=True)
        after = encode(messages[:index + 1])
        if full[:len(before)] != before or full[:len(after)] != after or len(after) <= len(before):
            raise ValueError(f"{row['id']}: prefixo do template Qwen não corresponde à conversa completa.")
        segment = after[len(before):]
        end = next((offset for offset, token in enumerate(segment) if token in eos_ids), None)
        if end is None:
            raise ValueError(f"{row['id']}: mensagem do assistant sem token de fim.")
        end += len(before) + 1
        labels[len(before):end] = full[len(before):end]
    if all(token == -100 for token in labels):
        raise ValueError(f"{row['id']}: exemplo sem tokens de assistant supervisionados.")
    return {"input_ids": full, "attention_mask": [1] * len(full), "labels": labels}


def _select_rehearsal(train_rows, parent_ids, count, config, tokenizer, progress):
    by_id = {row["id"]: row for row in train_rows}
    if not isinstance(parent_ids, list) or len(parent_ids) < count:
        raise ValueError("A origem não contém IDs canônicos suficientes para a revisão de memória.")
    if len(set(parent_ids)) != len(parent_ids) or any(value not in by_id for value in parent_ids):
        raise ValueError("A origem inclui IDs duplicados ou ausentes do treino canônico atual.")
    candidates = list(parent_ids)
    random.Random(config["seed"]).shuffle(candidates)
    selected, encoded, too_long = [], [], 0
    for index, row_id in enumerate(candidates):
        if index % 100 == 0:
            progress(len(selected), count)
        row = by_id[row_id]
        entry = encode_row(tokenizer, row)
        if len(entry["input_ids"]) > config["max_length"]:
            too_long += 1
            continue
        selected.append(row)
        encoded.append(entry)
        if len(selected) == count:
            break
    if len(selected) != count:
        raise ValueError(f"Só {len(selected)} exemplos de revisão cabem em {config['max_length']} tokens; os {count} solicitados não serão truncados.")
    stats = {
        "selected": len(selected), "source": "parent_adapter_train_ids", "too_long_skipped": too_long,
        "max_tokens": max(len(entry["input_ids"]) for entry in encoded),
        "total_tokens": sum(len(entry["input_ids"]) for entry in encoded),
        "supervised_tokens": sum(sum(token != -100 for token in entry["labels"]) for entry in encoded),
    }
    return selected, encoded, stats


def prepare_tool_usage(config, tokenizer, directory, emit, adapter_run=None):
    """Prepare 1,000 tool trajectories plus 200 replay rows from the fixed parent."""
    if adapter_run != TOOL_USAGE_PARENT_RUN:
        raise ValueError("O treino de ferramentas precisa continuar do adaptador base registrado.")
    emit(phase="Verificando dados de ferramentas e origem do adaptador")
    splits, canonical_hashes = load_splits()
    source_adapter, _ = training_adapter(adapter_run)
    if file_hash(source_adapter / "adapter_model.safetensors") != TOOL_USAGE_PARENT_ADAPTER_SHA256:
        raise ValueError("Os pesos do adaptador de origem não correspondem ao treinamento registrado.")
    source_selection = read_json(source_adapter.parent / "selection.json", {})
    source_train = source_selection.get("splits", {}).get("train", {})
    parent_ids = source_train.get("ids")
    if source_selection.get("data_sha256") != canonical_hashes:
        raise ValueError("O dataset canônico mudou desde o treinamento do adaptador de origem.")
    if not isinstance(parent_ids, list) or len(parent_ids) != 10000:
        raise ValueError("A origem precisa registrar os 10.000 IDs de treino canônicos.")
    if any(not isinstance(value, str) or not value.startswith("nl2f-train-") for value in parent_ids):
        raise ValueError("A origem contém IDs que não pertencem ao split canônico de treino.")
    train_rows, validation_rows, data_manifest, tool_hashes = _read_tool_dataset()
    frozen_test = {row["id"] for row in splits["test"]}
    from benchmark.prompting import build_messages
    frozen_prompts = {build_messages(row["instruction"], row["context_text"])[1]["content"] for row in splits["test"]}
    for row in train_rows + validation_rows:
        if row["id"] in frozen_test or row["source_id"] in frozen_test:
            raise ValueError(f"{row['id']}: o dataset de ferramentas referencia um ID do teste congelado.")
        if row["messages"][1]["content"] in frozen_prompts:
            raise ValueError(f"{row['id']}: o dataset de ferramentas reutiliza uma instrução do teste congelado.")

    if config["train_samples"] != TOOL_USAGE_TOTAL_TRAIN_COUNT or config["validation_samples"] != TOOL_USAGE_VALIDATION_COUNT:
        raise ValueError("A execução de ferramentas usa exatamente 1.200 exemplos de treino e 100 de validação.")
    from dashboard.excel_tools import tool_definitions
    # The Qwen template preserves schema key order; use the same definitions as chat.
    schemas = tool_definitions()
    tool_encoded, tool_too_long = [], []
    for index, row in enumerate(train_rows):
        if index % 50 == 0:
            emit(phase="Preparando exemplos de ferramentas", step=index, total_steps=len(train_rows))
        entry = _encode_tool_row(tokenizer, row, schemas)
        if len(entry["input_ids"]) > config["max_length"]:
            tool_too_long.append((row["id"], len(entry["input_ids"])))
        tool_encoded.append(entry)
    if tool_too_long:
        sample = ", ".join(f"{row_id}={length}" for row_id, length in tool_too_long[:5])
        raise ValueError(f"{len(tool_too_long)} exemplos de ferramentas excedem max_length={config['max_length']} ({sample}); os dados não serão truncados.")

    validation_encoded = []
    for index, row in enumerate(validation_rows):
        if index % 25 == 0:
            emit(phase="Preparando validação de ferramentas", step=index, total_steps=len(validation_rows))
        entry = _encode_tool_row(tokenizer, row, schemas)
        if len(entry["input_ids"]) > config["max_length"]:
            raise ValueError(f"{row['id']} usa {len(entry['input_ids'])} tokens e excede max_length={config['max_length']}; validação não será truncada.")
        validation_encoded.append(entry)

    measured_lengths = _token_length_summary(
        [len(entry["input_ids"]) for entry in tool_encoded + validation_encoded]
    )
    if measured_lengths != data_manifest["validation"]["token_lengths"]:
        raise ValueError("Comprimentos de tokens do manifesto não correspondem ao tokenizer Qwen local.")

    rehearsal, rehearsal_encoded, rehearsal_stats = _select_rehearsal(
        splits["train"], parent_ids, TOOL_USAGE_REHEARSAL_COUNT, config, tokenizer,
        lambda done, total: emit(phase="Preparando revisão do treino anterior", step=done, total_steps=total),
    )
    combined = list(zip(train_rows, tool_encoded)) + list(zip(rehearsal, rehearsal_encoded))
    random.Random(config["seed"]).shuffle(combined)
    encoded = {"train": [entry for _, entry in combined], "validation": validation_encoded}
    selected_train = [row["id"] for row, _ in combined]
    validation_ids = [row["id"] for row in validation_rows]

    tool_stats = {
        "selected": len(tool_encoded), "categories": dict(Counter(row["category"] for row in train_rows)),
        "max_tokens": max(len(entry["input_ids"]) for entry in tool_encoded),
        "total_tokens": sum(len(entry["input_ids"]) for entry in tool_encoded),
        "supervised_tokens": sum(sum(token != -100 for token in entry["labels"]) for entry in tool_encoded),
        "too_long_skipped": 0,
    }
    validation_stats = {
        "selected": len(validation_encoded), "categories": dict(Counter(row["category"] for row in validation_rows)),
        "max_tokens": max(len(entry["input_ids"]) for entry in validation_encoded),
        "total_tokens": sum(len(entry["input_ids"]) for entry in validation_encoded),
        "supervised_tokens": sum(sum(token != -100 for token in entry["labels"]) for entry in validation_encoded),
        "too_long_skipped": 0,
    }
    prompt = prompt_fingerprint()
    schema_fingerprint = _sha256_bytes(_canonical_json(schemas))
    manifest = {
        "data_sha256": canonical_hashes,
        "prompt_fingerprint": prompt,
        "source_prompt_fingerprint": source_selection.get("prompt_fingerprint"),
        "tool_usage": {
            "source_adapter_run": adapter_run,
            "source_adapter_sha256": TOOL_USAGE_PARENT_ADAPTER_SHA256,
            "source_prompt_matches_current": source_selection.get("prompt_fingerprint") == prompt,
            "tool_prompt_fingerprint": _tool_prompt_fingerprint(schemas),
            "tool_schema_fingerprint": schema_fingerprint,
            "dataset_version": data_manifest["version"],
            "dataset_sha256": tool_hashes,
            "dataset_manifest_sha256": file_hash(TOOL_USAGE_DIRECTORY / "manifest.json"),
            "tool_train_examples": len(tool_encoded),
            "rehearsal_train_examples": len(rehearsal_encoded),
            "validation_examples": len(validation_encoded),
            "source_train_ids": len(parent_ids),
            "rehearsal_seed": config["seed"],
            "rehearsal_stats": rehearsal_stats,
            "data_validation": data_manifest["validation"],
        },
        "excluded_training_runs": [],
        "prior_train_ids": len(parent_ids),
        "splits": {
            "train": {
                "selected": len(encoded["train"]),
                "ids": selected_train,
                "tool_usage": tool_stats,
                "rehearsal": rehearsal_stats,
                "categories": {"tool_usage": tool_stats["categories"], "rehearsal": {"canonical_formula": len(rehearsal)}},
                "max_tokens": max(tool_stats["max_tokens"], rehearsal_stats["max_tokens"]),
                "total_tokens": tool_stats["total_tokens"] + rehearsal_stats["total_tokens"],
                "supervised_tokens": tool_stats["supervised_tokens"] + rehearsal_stats["supervised_tokens"],
                "too_long_skipped": 0,
            },
            "validation": {**validation_stats, "ids": validation_ids},
        },
        "test_count": len(splits["test"]),
    }
    write_json(directory / "selection.json", manifest)
    emit(phase="Dados de ferramentas preparados", step=1, total_steps=1)
    return encoded, manifest
