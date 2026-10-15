"""Harness do baseline de fórmulas Excel. Não carrega o modelo no --dry-run."""

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

try:
    from benchmark.prompting import SYSTEM_PROMPT, build_messages
    from benchmark.scoring import extract_prediction, formulas_match
except ImportError:
    from prompting import SYSTEM_PROMPT, build_messages
    from scoring import extract_prediction, formulas_match

MODEL_NAME = "Qwen/Qwen3-4B-Instruct-2507"
MODEL_REVISION = "cdbee75f17c01a7cc42f958dc650907174af0554"
DEFAULT_DATA = r"dataset\canonical\test.jsonl"
DEFAULT_CHECKPOINT = r"benchmark\results\baseline_items.jsonl"
MAX_NEW_TOKENS = 128
MAX_BATCH_ITEMS = 8
MAX_BATCH_TOKENS = 6144
EVALUATION_IDENTITY_VERSION = 2
QUANTIZATION = "nf4-double-bf16"
EVALUATION_FIELDS = ("id", "instruction", "context_text", "expected")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Avalia o Qwen base em um JSONL de fórmulas de Excel.",
    )
    parser.add_argument(
        "--data",
        default=DEFAULT_DATA,
        help=f"JSONL de teste (padrão: {DEFAULT_DATA}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Usa apenas as primeiras N linhas do JSONL.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Caminho do JSON de resultado. Sem isto, o JSON vai para a saída padrão.",
    )
    parser.add_argument(
        "--checkpoint",
        default=DEFAULT_CHECKPOINT,
        help=(
            "JSONL gravado a cada lote. Retoma apenas posições já salvas "
            "com as mesmas entradas completas e receita de avaliação."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Lê o JSONL, monta os prompts e imprime a contagem, sem carregar o modelo.",
    )
    return parser.parse_args(argv)


def prompt_fingerprint() -> str:
    """Identifica o template travado, sem depender do texto de cada exemplo."""
    sample = build_messages("__INSTRUCTION__", "__CONTEXT__")
    blob = json.dumps(sample, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _effective_text(value) -> str:
    return "" if value is None else str(value)


def _canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def evaluation_record(row: dict) -> dict[str, str]:
    """The exact normalized row values used by prompting and scoring."""
    return {field: _effective_text(row.get(field)) for field in EVALUATION_FIELDS}


def evaluation_record_fingerprint(row: dict) -> str:
    return hashlib.sha256(_canonical_json(evaluation_record(row))).hexdigest()


def evaluation_identity(rows: list[dict]) -> dict:
    records = [evaluation_record(row) for row in rows]
    return {
        "version": EVALUATION_IDENTITY_VERSION,
        "record_fields": list(EVALUATION_FIELDS),
        "record_count": len(records),
        "records_sha256": hashlib.sha256(_canonical_json(records)).hexdigest(),
        "model": MODEL_NAME,
        "model_revision": MODEL_REVISION,
        "system_prompt": SYSTEM_PROMPT,
        "prompt_fingerprint": prompt_fingerprint(),
        "do_sample": False,
        "max_new_tokens": MAX_NEW_TOKENS,
        "quantization": QUANTIZATION,
    }


def evaluation_fingerprint(rows: list[dict]) -> str:
    return hashlib.sha256(_canonical_json(evaluation_identity(rows))).hexdigest()


def manifest_path_for(checkpoint: Path) -> Path:
    return checkpoint.with_name(checkpoint.stem + "_manifest.json")


def ensure_manifest(
    path: Path,
    fingerprint: str,
    identity: dict | None = None,
    checkpoint_path: Path | None = None,
) -> None:
    if identity is None:
        raise ValueError("A identidade completa dos registros é obrigatória para criar um manifesto de avaliação.")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "model": MODEL_NAME,
        "model_revision": MODEL_REVISION,
        "system_prompt": SYSTEM_PROMPT,
        "prompt_fingerprint": fingerprint,
        "do_sample": False,
        "max_new_tokens": MAX_NEW_TOKENS,
        "quantization": QUANTIZATION,
        "evaluation_identity": identity,
        "evaluation_fingerprint": hashlib.sha256(_canonical_json(identity)).hexdigest(),
    }
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        same = (
            existing.get("evaluation_identity") == identity
            and existing.get("evaluation_fingerprint") == payload["evaluation_fingerprint"]
            and existing.get("prompt_fingerprint") == fingerprint
            and existing.get("model") == MODEL_NAME
            and existing.get("max_new_tokens") == MAX_NEW_TOKENS
            and existing.get("do_sample") is False
            and existing.get("quantization") == QUANTIZATION
        )
        if not same:
            raise RuntimeError(
                f"O manifesto em {path} não comprova a identidade completa desta avaliação. "
                "Artefatos legados ou incompatíveis não serão reutilizados; use um novo caminho de checkpoint."
            )
        return
    if checkpoint_path is not None and checkpoint_path.is_file() and checkpoint_path.stat().st_size:
        raise RuntimeError(
            f"O checkpoint {checkpoint_path} não tem manifesto com identidade verificável. "
            "Preserve-o e use um novo caminho de checkpoint para regenerar as predições."
        )
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_checkpoint(path: Path, rows: list[dict] | None = None) -> dict:
    done: dict[str, str] = {}
    if not path.is_file():
        return done
    expected = None
    if rows is not None:
        expected = {
            index: (evaluation_record(row)["id"], evaluation_record_fingerprint(row))
            for index, row in enumerate(rows)
        }
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Checkpoint inválido em {path}:{line_no}: {exc}") from exc
        item_id = obj.get("id")
        if not item_id or "predicted" not in obj:
            raise ValueError(f"Checkpoint inválido em {path}:{line_no}")
        if expected is None:
            # Retain the historical read shape for callers that inspect checkpoints.
            done[str(item_id)] = str(obj["predicted"])
            continue
        position = obj.get("position")
        if not isinstance(position, int) or position not in expected:
            continue
        expected_id, expected_fingerprint = expected[position]
        if (
            str(item_id) == expected_id
            and obj.get("input_fingerprint") == expected_fingerprint
        ):
            done[position] = str(obj["predicted"])
    return done


def append_checkpoint(
    path: Path,
    rows: list[dict],
    predictions: list[str],
    positions: list[int] | None = None,
) -> None:
    if len(rows) != len(predictions) or (positions is not None and len(positions) != len(rows)):
        raise ValueError("Registros, predições e posições do checkpoint precisam ter o mesmo tamanho.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for offset, (row, predicted) in enumerate(zip(rows, predictions)):
            record = evaluation_record(row)
            item = {
                "id": record["id"],
                "input_fingerprint": evaluation_record_fingerprint(row),
                "predicted": predicted,
            }
            if positions is not None:
                item["position"] = positions[offset]
            handle.write(
                json.dumps(
                    item,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
        handle.flush()


def make_batches(
    order: list[tuple[int, int]],
    max_items: int,
    max_tokens: int,
) -> list[list[int]]:
    """Agrupa índices já ordenados por tamanho, limitando o padding do lote."""
    batches: list[list[int]] = []
    current: list[int] = []
    current_max = 0
    for index, length in order:
        new_max = length if length > current_max else current_max
        overflows = new_max * (len(current) + 1) > max_tokens
        if current and (len(current) >= max_items or overflows):
            batches.append(current)
            current = [index]
            current_max = length
        else:
            current.append(index)
            current_max = new_max
    if current:
        batches.append(current)
    return batches


def row_messages(row: dict) -> list[dict[str, str]]:
    instruction = row.get("instruction", "")
    context_text = row.get("context_text", "")
    return build_messages(
        "" if instruction is None else str(instruction),
        "" if context_text is None else str(context_text),
    )


def load_jsonl(path: Path, limit: int | None) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8-sig") as handle:
        for line_no, line in enumerate(handle, start=1):
            if limit is not None and len(rows) >= limit:
                break
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSON inválido em {path}:{line_no}: {exc}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"A linha {line_no} de {path} não é um objeto JSON.")
            rows.append(obj)
    return rows


def score_rows(rows: list[dict], predictions: list[str], data_label: str) -> dict:
    if len(predictions) != len(rows):
        raise RuntimeError(
            f"Número de predições ({len(predictions)}) diferente das linhas ({len(rows)})."
        )
    items = []
    correct = 0
    for row, predicted in zip(rows, predictions):
        expected = "" if row.get("expected") is None else str(row.get("expected"))
        item_id = "" if row.get("id") is None else str(row.get("id"))
        matched = formulas_match(expected, predicted)
        if matched:
            correct += 1
        items.append(
            {
                "id": item_id,
                "expected": expected,
                "input_fingerprint": evaluation_record_fingerprint(row),
                "predicted": predicted,
                "predicted_formula": extract_prediction(predicted),
                "match": matched,
            }
        )
    total = len(items)
    accuracy = (correct / total) if total else 0.0
    identity = evaluation_identity(rows)
    return {
        "model": MODEL_NAME,
        "model_revision": MODEL_REVISION,
        "system_prompt": SYSTEM_PROMPT,
        "prompt_fingerprint": prompt_fingerprint(),
        "do_sample": False,
        "max_new_tokens": MAX_NEW_TOKENS,
        "quantization": QUANTIZATION,
        "evaluation_identity": identity,
        "evaluation_fingerprint": hashlib.sha256(_canonical_json(identity)).hexdigest(),
        "data": data_label,
        "total": total,
        "correct": correct,
        "accuracy": accuracy,
        "items": items,
    }


def print_summary(total: int, correct: int, accuracy: float) -> None:
    print(f"Testes: {total}")
    print(f"Corretos: {correct}")
    print(f"Accuracy: {accuracy:.4f}")


def write_report(report: dict, output: str | None) -> None:
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(payload, encoding="utf-8")
        print(f"Resultado: {destination}")
        return
    print(payload, end="")


def generate_token_batch(model, tokenizer, sequences: list[list[int]], torch) -> list[str]:
    pad_id = tokenizer.pad_token_id
    width = max(len(sequence) for sequence in sequences)
    input_ids = []
    attention = []
    for sequence in sequences:
        pad = width - len(sequence)
        input_ids.append([pad_id] * pad + sequence)
        attention.append([0] * pad + [1] * len(sequence))
    inputs = {
        "input_ids": torch.tensor(input_ids, dtype=torch.long, device="cuda:0"),
        "attention_mask": torch.tensor(attention, dtype=torch.long, device="cuda:0"),
    }
    with torch.inference_mode():
        output = model.generate(
            **inputs,
            max_new_tokens=MAX_NEW_TOKENS,
            do_sample=False,
            use_cache=True,
            pad_token_id=pad_id,
            eos_token_id=tokenizer.eos_token_id,
        )
    new_tokens = output[:, width:]
    return [
        text.strip()
        for text in tokenizer.batch_decode(new_tokens, skip_special_tokens=True)
    ]


def generate_predictions(rows: list[dict], checkpoint_path: Path) -> list[str]:
    # Torch, transformers e bitsandbytes só entram quando há geração de verdade.
    fingerprint = prompt_fingerprint()
    identity = evaluation_identity(rows)
    ensure_manifest(manifest_path_for(checkpoint_path), fingerprint, identity, checkpoint_path)
    done = load_checkpoint(checkpoint_path, rows)
    missing_positions = [
        index
        for index, _row in enumerate(rows)
        if index not in done
    ]
    if not missing_positions:
        print(f"Checkpoint completo: {len(rows)} itens. Modelo não recarregado.")
        return [done[index] for index in range(len(rows))]

    import bitsandbytes
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    if bitsandbytes.__name__ != "bitsandbytes":
        raise RuntimeError("bitsandbytes não foi carregado.")

    if not torch.cuda.is_available():
        raise RuntimeError(
            "CUDA não está disponível. O baseline não faz fallback para CPU."
        )

    torch.backends.cuda.matmul.allow_tf32 = True
    torch.cuda.set_device(0)
    torch.cuda.empty_cache()

    quant_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    print("Carregando tokenizer...", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, revision=MODEL_REVISION)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"

    print("Carregando modelo...", flush=True)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_NAME,
        revision=MODEL_REVISION,
        quantization_config=quant_config,
        device_map={"": "cuda:0"},
        dtype=torch.bfloat16,
        low_cpu_mem_usage=True,
        attn_implementation="sdpa",
    )
    model.eval()

    missing_rows = [rows[index] for index in missing_positions]
    print(f"Tokenizando {len(missing_rows)} prompts...", flush=True)
    encoded: list[list[int]] = []
    for row in missing_rows:
        tokenized = tokenizer.apply_chat_template(
            row_messages(row),
            add_generation_prompt=True,
            tokenize=True,
        )
        encoded.append(list(tokenized["input_ids"]))

    order = sorted(
        ((index, len(encoded[index])) for index in range(len(encoded))),
        key=lambda item: item[1],
    )
    batches = make_batches(order, MAX_BATCH_ITEMS, MAX_BATCH_TOKENS)
    print(
        f"Pendentes: {len(missing_rows)} em {len(batches)} lotes "
        f"(até {MAX_BATCH_ITEMS} itens, orçamento {MAX_BATCH_TOKENS} tokens).",
        flush=True,
    )

    finished = len(rows) - len(missing_rows)
    produced = 0
    started = time.perf_counter()

    def store_indices(indices: list[int]) -> None:
        nonlocal produced
        texts = generate_token_batch(
            model,
            tokenizer,
            [encoded[index] for index in indices],
            torch,
        )
        batch_rows = [missing_rows[index] for index in indices]
        batch_positions = [missing_positions[index] for index in indices]
        append_checkpoint(checkpoint_path, batch_rows, texts, batch_positions)
        for position, text in zip(batch_positions, texts):
            done[position] = text
        produced += len(indices)

    def generate_group(indices: list[int]) -> None:
        if not indices:
            return
        try:
            store_indices(indices)
        except RuntimeError as exc:
            out_of_memory = "out of memory" in str(exc).lower()
            if not out_of_memory or len(indices) == 1:
                raise
            torch.cuda.empty_cache()
            middle = len(indices) // 2
            print(
                f"Lote de {len(indices)} estourou a VRAM. Dividindo.",
                flush=True,
            )
            generate_group(indices[:middle])
            generate_group(indices[middle:])

    for batch in batches:
        generate_group(batch)
        elapsed = time.perf_counter() - started
        print(
            f"Gerado {finished + produced}/{len(rows)} em {elapsed:.1f}s",
            flush=True,
        )

    if any(position not in done for position in missing_positions):
        raise RuntimeError("A geração terminou sem gravar todos os itens.")
    return [done[index] for index in range(len(rows))]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.limit is not None and args.limit < 0:
        print("Erro: --limit não pode ser negativo.", file=sys.stderr)
        return 2

    data_path = Path(args.data)
    if not data_path.is_file():
        print(
            f"Erro: arquivo de dados não encontrado: {data_path}. "
            "Passe um JSONL existente em --data "
            f"(padrão: {DEFAULT_DATA}).",
            file=sys.stderr,
        )
        return 2

    try:
        rows = load_jsonl(data_path, args.limit)
    except ValueError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        prompts = []
        for row in rows:
            messages = row_messages(row)
            if len(messages) != 2 or messages[0]["role"] != "system":
                print("Erro: prompt fora do formato do contrato.", file=sys.stderr)
                return 2
            prompts.append(messages)
        print(f"Dados: {data_path}")
        print(f"Prompts montados: {len(prompts)}")
        print("Modelo não carregado (dry-run).")
        return 0

    checkpoint_path = Path(args.checkpoint)
    predictions = generate_predictions(rows, checkpoint_path)
    report = score_rows(rows, predictions, str(data_path))
    print_summary(report["total"], report["correct"], report["accuracy"])
    write_report(report, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
