"""Deterministic sampling and assistant-only supervision; never truncate answers."""

import hashlib
import random
import re
from collections import Counter, defaultdict

from benchmark.prompting import build_messages
from benchmark.run_baseline import load_jsonl, prompt_fingerprint
from dashboard.config import ROOT, write_json
from dashboard.adapters import training_history

# Heuristic exclusion, not a certificate that the other formulas are valid Excel.
SUSPECT = re.compile(r"\b(?:SUMMARIZE|SUMX|AVERAGEX|COUNTX|DCOUNTX|MAXX|MINX)\s*\(", re.I)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def table_id(row):
    return row["source_id"].rsplit("#", 1)[0]


def validate_splits(splits, frozen_ids):
    test = splits["test"]
    if len(test) != 500 or [row["id"] for row in test] != frozen_ids:
        raise ValueError("O teste deve conter os mesmos 500 IDs congelados, na mesma ordem.")
    ids, tables = {}, {}
    for name, rows in splits.items():
        for row in rows:
            if not all(isinstance(row.get(k), str) and row[k] for k in ("id", "instruction", "context_text", "expected", "source_id")):
                raise ValueError(f"Registro incompleto no split {name}.")
            if not row["expected"].startswith("="):
                raise ValueError(f"Gabarito sem '=': {row['id']}.")
        ids[name] = {row["id"] for row in rows}
        tables[name] = {table_id(row) for row in rows}
        if len(ids[name]) != len(rows):
            raise ValueError(f"IDs duplicados no split {name}.")
    for a, b in (("train", "validation"), ("train", "test"), ("validation", "test")):
        if ids[a] & ids[b] or tables[a] & tables[b]:
            raise ValueError(f"Sobreposição entre {a} e {b}.")


def load_splits():
    paths = {name: ROOT / "dataset" / "canonical" / f"{name}.jsonl" for name in ("train", "validation", "test")}
    splits = {name: load_jsonl(path, None) for name, path in paths.items()}
    frozen = (ROOT / "dataset/FROZEN_TEST_IDS.txt").read_text(encoding="utf-8").splitlines()
    validate_splits(splits, frozen)
    return splits, {name: file_hash(path) for name, path in paths.items()}


def stratified_order(rows, seed):
    """Randomize within levels, then interleave levels proportionally."""
    groups = defaultdict(list)
    for row in rows:
        groups[row.get("level", "unknown")].append(row)
    rng = random.Random(seed)
    ranked = []
    for level in sorted(groups):
        group = groups[level]
        rng.shuffle(group)
        ranked.extend(((i + 0.5) / len(group), level, row) for i, row in enumerate(group))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [row for _, _, row in ranked]


def token_ids(tokenizer, messages, generation=False):
    encoded = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=generation)
    return list(encoded["input_ids"] if hasattr(encoded, "keys") else encoded)


def encode_row(tokenizer, row):
    messages = build_messages(row["instruction"], row["context_text"])
    prompt = token_ids(tokenizer, messages, generation=True)
    full = token_ids(tokenizer, messages + [{"role": "assistant", "content": row["expected"]}])
    if full[:len(prompt)] != prompt or len(full) <= len(prompt):
        raise ValueError("Chat template incompatível com a máscara da resposta; treinamento interrompido.")
    return {"input_ids": full, "attention_mask": [1] * len(full), "labels": [-100] * len(prompt) + full[len(prompt):]}


def select_encoded(rows, count, config, tokenizer, progress, excluded_ids=None):
    excluded_ids = excluded_ids or set()
    eligible = [r for r in rows if not config["exclude_suspect"] or not SUSPECT.search(r["expected"])]
    suspect_excluded = len(rows) - len(eligible)
    seen_excluded = sum(r["id"] in excluded_ids for r in eligible)
    eligible = [r for r in eligible if r["id"] not in excluded_ids]
    selected, encoded, too_long = [], [], 0
    for i, row in enumerate(stratified_order(eligible, config["seed"])):
        if i % 100 == 0:
            progress(len(selected), count)
        entry = encode_row(tokenizer, row)
        if len(entry["input_ids"]) > config["max_length"]:
            too_long += 1
            continue
        selected.append(row)
        encoded.append(entry)
        if len(selected) == count:
            break
    if len(selected) != count:
        raise ValueError(f"Só {len(selected)} exemplos elegíveis de {count} solicitados após os filtros ({seen_excluded} já usados excluídos). Reduza a quantidade ou aumente o limite de tokens.")
    stats = {
        "selected": len(selected), "suspect_excluded": suspect_excluded, "seen_excluded": seen_excluded,
        "too_long_skipped": too_long, "levels": dict(Counter(r.get("level", "unknown") for r in selected)),
        "tables": len({table_id(r) for r in selected}),
        "max_tokens": max(len(x["input_ids"]) for x in encoded),
        "total_tokens": sum(len(x["input_ids"]) for x in encoded),
        "supervised_tokens": sum(sum(t != -100 for t in x["labels"]) for x in encoded),
    }
    return selected, encoded, stats


def prepare(config, tokenizer, directory, emit, adapter_run=None):
    emit(phase="Verificando splits e IDs congelados")
    splits, hashes = load_splits()
    manifest = {"data_sha256": hashes, "prompt_fingerprint": prompt_fingerprint(), "splits": {}}
    excluded_ids, source_runs = set(), []
    if adapter_run and config["exclude_seen"]:
        excluded_ids, source_runs = training_history(adapter_run, hashes, manifest["prompt_fingerprint"])
    manifest.update(excluded_training_runs=source_runs, prior_train_ids=len(excluded_ids))
    encoded = {}
    for name, count in (("train", config["train_samples"]), ("validation", config["validation_samples"])):
        selected, encoded[name], stats = select_encoded(
            splits[name], count, config, tokenizer,
            lambda done, total: emit(phase=f"Preparando {name}", step=done, total_steps=total),
            excluded_ids=excluded_ids if name == "train" else None,
        )
        manifest["splits"][name] = {**stats, "ids": [r["id"] for r in selected]}
        write_json(directory / "selection.json", manifest)
        print(f"{name}: {stats}", flush=True)
    manifest["test_count"] = len(splits["test"])
    write_json(directory / "selection.json", manifest)
    emit(phase="Dados preparados", step=1, total_steps=1)
    return encoded, manifest


class CausalCollator:
    def __init__(self, pad_token_id):
        self.pad_token_id = pad_token_id

    def __call__(self, features):
        import torch
        width = max(len(x["input_ids"]) for x in features)
        return {
            key: torch.tensor([x[key] + [pad] * (width - len(x[key])) for x in features], dtype=torch.long)
            for key, pad in (("input_ids", self.pad_token_id), ("attention_mask", 0), ("labels", -100))
        }
