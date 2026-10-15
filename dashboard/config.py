"""Configuration shared by the HTTP boundary and command-line worker."""

import json
import math
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
TERMINAL = {"completed", "cancelled", "failed", "interrupted"}
DEFAULTS = {
    "train_samples": 5000,
    "validation_samples": 500,
    "epochs": 1,
    "learning_rate": 0.0001,
    "batch_size": 1,
    "gradient_accumulation": 16,
    "max_length": 1024,
    "lora_r": 8,
    "lora_alpha": 16,
    "seed": 42,
    "logging_steps": 5,
    "exclude_suspect": True,
    "exclude_seen": True,
    "gpu_memory_fraction": 0.85,
    "eval_batch_size": 1,
}
BOUNDS = {
    "train_samples": (1, 52203, int),
    "validation_samples": (1, 5083, int),
    "epochs": (1, 10, int),
    "learning_rate": (0.000001, 0.001, float),
    "batch_size": (1, 4, int),
    "gradient_accumulation": (1, 128, int),
    "max_length": (128, 4096, int),
    "lora_r": (4, 64, int),
    "lora_alpha": (4, 128, int),
    "seed": (0, 2147483647, int),
    "logging_steps": (1, 100, int),
    "gpu_memory_fraction": (0.5, 0.95, float),
    "eval_batch_size": (1, 8, int),
}


def validate_config(raw):
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("Configuração desconhecida ou inválida.")
    result = {**DEFAULTS, **raw}
    for key, (low, high, kind) in BOUNDS.items():
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key}: informe um número entre {low} e {high}.")
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key}: valor fora do intervalo {low}–{high}.")
        if kind is int and int(value) != value:
            raise ValueError(f"{key}: informe um número inteiro.")
        result[key] = kind(value)
    for key in ("exclude_suspect", "exclude_seen"):
        if not isinstance(result[key], bool):
            raise ValueError(f"{key} deve ser booleano.")
    return result


def run_path(run_id, root=RUNS):
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-f0-9]{8}", run_id):
        raise ValueError("Identificador de execução inválido.")
    path = root / run_id
    if not path.is_dir() or path.resolve().parent != root.resolve():
        raise ValueError("Execução não encontrada.")
    return path


def read_json(path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)
