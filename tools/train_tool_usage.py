"""Queue the fixed Excel-tool continuation as a normal dashboard training run."""

import argparse
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime

import psutil

from dashboard.config import ROOT, RUNS, TERMINAL, validate_config, write_json
from dashboard.tool_training import TOOL_USAGE_PARENT_RUN, _read_tool_dataset

LABEL = "10.000 exemplos + 1.000 exemplos de tool usage"


def build_config(args):
    return validate_config({
        "train_samples": 1200,
        "validation_samples": 100,
        "epochs": args.epochs,
        "learning_rate": args.learning_rate,
        "batch_size": args.batch_size,
        "gradient_accumulation": args.gradient_accumulation,
        "max_length": args.max_length,
        "lora_r": 8,
        "lora_alpha": 16,
        "seed": args.seed,
        "logging_steps": args.logging_steps,
        "exclude_suspect": False,
        "exclude_seen": False,
        "gpu_memory_fraction": args.gpu_memory_fraction,
        "eval_batch_size": 1,
    })


def _active_runs():
    active = []
    for directory in RUNS.iterdir():
        if not directory.is_dir() or not (directory / "request.json").is_file():
            continue
        status = directory / "status.json"
        if status.is_file():
            from dashboard.config import read_json
            state = read_json(status, {}).get("status")
            if state and state not in TERMINAL:
                active.append(directory.name)
    return active


def queue_run(config):
    if not RUNS.is_dir():
        RUNS.mkdir(parents=True)
    active = _active_runs()
    if active:
        raise RuntimeError(f"Já existe uma execução ativa ({active[0]}). Aguarde ou interrompa antes de iniciar o treino de ferramentas.")
    # Validate the fixed files and their trajectory protocol before registering a run.
    _read_tool_dataset()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    directory = RUNS / run_id
    directory.mkdir()
    write_json(directory / "request.json", {
        "kind": "train",
        "training_mode": "tool_usage",
        "label": LABEL,
        "config": config,
        "adapter_run": TOOL_USAGE_PARENT_RUN,
        "created_at": time.time(),
    })
    write_json(directory / "status.json", {"status": "queued"})
    environment = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "TOKENIZERS_PARALLELISM": "false",
    }
    try:
        with (directory / "log.txt").open("wb") as log:
            child = subprocess.Popen(
                [sys.executable, "-u", "-m", "dashboard.worker", "--run-id", run_id],
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        write_json(directory / "process.json", {
            "pid": child.pid,
            "created": psutil.Process(child.pid).create_time(),
        })
    except Exception as exc:
        write_json(directory / "status.json", {
            "status": "failed", "error": str(exc), "ended_at": time.time(),
        })
        raise
    return run_id


def main():
    parser = argparse.ArgumentParser(description="Queue the fixed 1,000-example Excel tool-use adapter continuation.")
    parser.add_argument("--max-length", type=int, default=1536)
    parser.add_argument("--learning-rate", type=float, default=0.00005)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--gradient-accumulation", type=int, default=16)
    parser.add_argument("--gpu-memory-fraction", type=float, default=0.85)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--logging-steps", type=int, default=5)
    args = parser.parse_args()
    config = build_config(args)
    run_id = queue_run(config)
    print(f"Execução {run_id} enfileirada no painel: {LABEL}")


if __name__ == "__main__":
    main()
