"""Run a small, isolated two-learning-rate QLoRA pilot on the cached Qwen model."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from benchmark import run_baseline as baseline
from dashboard.config import DEFAULTS, ROOT, TERMINAL, read_json, validate_config, write_json
from dashboard.data import load_splits, token_ids
from dashboard.runtime import Events, worker_lock
from dashboard.worker import load_model, load_tokenizer, local_snapshot, train

PILOT_CONFIG = {
    "train_samples": 32,
    "validation_samples": 16,
    "epochs": 1,
    "batch_size": 1,
    "gradient_accumulation": 4,
    "max_length": 512,
    "lora_r": 8,
    "lora_alpha": 16,
    "seed": 42,
    "logging_steps": 1,
    "exclude_suspect": True,
    "exclude_seen": False,
    "gpu_memory_fraction": 0.85,
    "eval_batch_size": 1,
}
LEARNING_RATES = (("lr_5e_5", 0.00005), ("lr_1e_4", 0.0001))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _model_identity() -> dict:
    snapshot = local_snapshot()
    config_path = snapshot / "config.json"
    weights = sorted(
        {path.name: path.stat().st_size for pattern in ("*.safetensors", "*.bin") for path in snapshot.glob(pattern)}.items()
    )
    return {
        "model_id": baseline.MODEL_NAME,
        "cached_snapshot_revision": snapshot.name,
        "cached_snapshot_path": str(snapshot),
        "config_sha256": _sha256(config_path),
        "weight_files": [{"name": name, "size_bytes": size} for name, size in weights],
        "quantization": "NF4 with double quantization; bfloat16 compute",
        "attention": "SDPA",
    }


def _environment() -> dict:
    import torch

    packages = {}
    for package in ("transformers", "peft", "accelerate", "bitsandbytes", "datasets"):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = None
    return {
        "python": sys.version.split()[0],
        "torch": torch.__version__,
        "torch_cuda_build": torch.version.cuda,
        "packages": packages,
    }


def _ensure_no_active_runs() -> None:
    for directory in (ROOT / "runs").iterdir():
        if not directory.is_dir():
            continue
        state = read_json(directory / "status.json", {})
        if state.get("status") and state.get("status") not in TERMINAL:
            raise RuntimeError(f"An operation is still active in runs/{directory.name}; pilot stopped before GPU work.")


def _selection_rows(validation_rows: list[dict], selected_ids: list[str]) -> list[dict]:
    by_id = {row["id"]: row for row in validation_rows}
    missing = [item_id for item_id in selected_ids if item_id not in by_id]
    if missing:
        raise ValueError(f"Selected validation IDs are missing from the current split: {missing[:3]}")
    return [by_id[item_id] for item_id in selected_ids]


def _current_input_identity(hashes: dict[str, str]) -> dict[str, str]:
    return {
        "data_sha256_train": hashes["train"],
        "data_sha256_validation": hashes["validation"],
        "prompt_fingerprint": baseline.prompt_fingerprint(),
    }


def _assert_pilot_input_identity(pilot_dir: Path, manifest: dict, hashes: dict[str, str]) -> dict[str, str]:
    """Require provenance before a resume can reuse trained weights or validation outputs."""
    current = _current_input_identity(hashes)
    recorded = []
    if isinstance(manifest.get("input_identity"), dict):
        recorded.append(manifest["input_identity"])
    selection = manifest.get("selection")
    if isinstance(selection, dict):
        recorded.append(selection)
    for result in manifest.get("conditions", {}).values():
        if isinstance(result, dict) and all(key in result for key in current):
            recorded.append(result)
    for name, _rate in LEARNING_RATES:
        selection_path = pilot_dir / name / "selection.json"
        evidence = read_json(selection_path, {})
        data_hashes = evidence.get("data_sha256", {}) if isinstance(evidence, dict) else {}
        if all(key in data_hashes for key in ("train", "validation")) and evidence.get("prompt_fingerprint"):
            recorded.append({
                "data_sha256_train": data_hashes["train"],
                "data_sha256_validation": data_hashes["validation"],
                "prompt_fingerprint": evidence["prompt_fingerprint"],
            })
    if not recorded:
        has_reusable_output = any(pilot_dir.glob("validation_*.json"))
        has_training_artifacts = any(
            (pilot_dir / name / filename).is_file()
            for name, _rate in LEARNING_RATES
            for filename in ("condition_result.json", "selection.json", "training_summary.json", "adapter/adapter_config.json")
        )
        has_completed_manifest_condition = any(
            isinstance(result, dict) and "best_eval_loss" in result
            for result in manifest.get("conditions", {}).values()
        )
        if has_reusable_output or has_training_artifacts or has_completed_manifest_condition:
            raise ValueError("Pilot outputs exist without saved split/prompt provenance; they cannot be resumed safely.")
    for evidence in recorded:
        if any(evidence.get(key) != value for key, value in current.items()):
            raise ValueError("Training/validation data or prompt changed since this pilot was saved; resume and cached outputs are blocked.")
    return current


def _assert_condition_evidence(condition_dir: Path, result: dict, identity: dict[str, str]) -> None:
    selection = read_json(condition_dir / "selection.json", {})
    selection_hashes = selection.get("data_sha256", {})
    evidence = {
        "data_sha256_train": result.get("data_sha256_train"),
        "data_sha256_validation": result.get("data_sha256_validation"),
        "prompt_fingerprint": result.get("prompt_fingerprint"),
    }
    selected = {
        "selected_train_ids": selection.get("splits", {}).get("train", {}).get("ids"),
        "selected_validation_ids": selection.get("splits", {}).get("validation", {}).get("ids"),
    }
    if (
        evidence != identity
        or selection_hashes.get("train") != identity["data_sha256_train"]
        or selection_hashes.get("validation") != identity["data_sha256_validation"]
        or selection.get("prompt_fingerprint") != identity["prompt_fingerprint"]
        or any(result.get(key) != value for key, value in selected.items())
    ):
        raise ValueError(f"Saved training artifacts in {condition_dir.name} do not prove the current split/prompt identity.")


def _validation_report_matches(report: dict, condition: str, rows: list[dict]) -> bool:
    items = report.get("items") if isinstance(report, dict) else None
    return (
        isinstance(report, dict)
        and report.get("condition") == condition
        and report.get("evaluation_fingerprint") == baseline.evaluation_fingerprint(rows)
        and report.get("evaluation_identity") == baseline.evaluation_identity(rows)
        and isinstance(items, list)
        and len(items) == len(rows)
        and all(
            isinstance(item, dict)
            and item.get("id") == baseline.evaluation_record(row)["id"]
            and item.get("expected") == baseline.evaluation_record(row)["expected"]
            and item.get("input_fingerprint") == baseline.evaluation_record_fingerprint(row)
            for item, row in zip(items, rows)
        )
    )


def _generate(model, tokenizer, rows: list[dict], torch) -> tuple[list[str], float, dict]:
    sequences = [token_ids(tokenizer, baseline.row_messages(row), generation=True) for row in rows]
    order = sorted(enumerate(map(len, sequences)), key=lambda item: item[1])
    batches = baseline.make_batches(order, max_items=1, max_tokens=baseline.MAX_BATCH_TOKENS)
    predictions: list[str | None] = [None] * len(rows)
    torch.cuda.synchronize(0)
    started = time.perf_counter()
    for batch in batches:
        outputs = baseline.generate_token_batch(model, tokenizer, [sequences[index] for index in batch], torch)
        for index, output in zip(batch, outputs):
            predictions[index] = output
    torch.cuda.synchronize(0)
    if any(output is None for output in predictions):
        raise RuntimeError("Validation generation left one or more rows without an answer.")
    return [str(output) for output in predictions], time.perf_counter() - started, {
        "examples": len(rows),
        "input_tokens": sum(map(len, sequences)),
        "max_input_tokens": max(map(len, sequences), default=0),
    }


def _write_score(rows: list[dict], predictions: list[str], label: str, path: Path, elapsed: float, memory: dict, prompt_sizes: dict) -> dict:
    report = baseline.score_rows(rows, predictions, "selected held-out validation rows; official test not used")
    report.update(
        condition=label,
        generation_seconds=elapsed,
        generation_examples_per_second=(len(rows) / elapsed) if elapsed else None,
        prompt_sizes=prompt_sizes,
        gpu_memory=memory,
    )
    write_json(path, report)
    return report


def _pilot_dir(raw_path: Path) -> Path:
    runs = (ROOT / "runs").resolve()
    pilot_dir = raw_path if raw_path.is_absolute() else ROOT / raw_path
    pilot_dir = pilot_dir.resolve()
    if pilot_dir.parent != runs or not pilot_dir.name.startswith("training-pilot-"):
        raise ValueError("Pilot operations accept only an isolated training-pilot directory directly under runs/.")
    if (pilot_dir / "request.json").exists():
        raise RuntimeError("A pilot directory with a panel request is not allowed.")
    return pilot_dir


def _condition_result(condition_dir: Path, condition_name: str, learning_rate: float, wall_seconds: float) -> dict:
    summary = read_json(condition_dir / "training_summary.json")
    selection = read_json(condition_dir / "selection.json")
    if not summary or not selection:
        raise RuntimeError(f"The {condition_name} subprocess did not save its training summary and selection.")
    metrics = summary.get("metrics", {})
    train_runtime = metrics.get("train_runtime")
    train_stats = selection["splits"]["train"]
    validation_stats = selection["splits"]["validation"]
    return {
        "learning_rate": learning_rate,
        "config": validate_config({**DEFAULTS, **PILOT_CONFIG, "learning_rate": learning_rate}),
        "wall_seconds_including_prepare_load_train_validation_save": wall_seconds,
        "trainer_metrics": metrics,
        "train_input_tokens_per_second": (train_stats["total_tokens"] / train_runtime) if train_runtime else None,
        "train_supervised_tokens_per_second": (train_stats["supervised_tokens"] / train_runtime) if train_runtime else None,
        "selected_train_ids": train_stats["ids"],
        "selected_validation_ids": validation_stats["ids"],
        "data_sha256_train": selection["data_sha256"]["train"],
        "data_sha256_validation": selection["data_sha256"]["validation"],
        "prompt_fingerprint": selection["prompt_fingerprint"],
        "train_selection": {key: value for key, value in train_stats.items() if key != "ids"},
        "validation_selection": {key: value for key, value in validation_stats.items() if key != "ids"},
        "best_eval_loss": summary["best_eval_loss"],
        "training_memory": summary["memory"],
        "artifacts": str(condition_dir.relative_to(ROOT)),
    }


def _train_condition(raw_path: Path, condition_name: str) -> None:
    pilot_dir = _pilot_dir(raw_path)
    rates = dict(LEARNING_RATES)
    if condition_name not in rates:
        raise ValueError(f"Unknown learning-rate condition: {condition_name}")
    condition_dir = pilot_dir / condition_name
    condition_dir.mkdir(exist_ok=False)
    config = validate_config({**DEFAULTS, **PILOT_CONFIG, "learning_rate": rates[condition_name]})
    tokenizer = load_tokenizer()
    import torch

    started = time.perf_counter()
    train(config, tokenizer, condition_dir, Events(condition_dir))
    torch.cuda.synchronize(0)
    wall_seconds = time.perf_counter() - started
    write_json(condition_dir / "condition_result.json", _condition_result(condition_dir, condition_name, rates[condition_name], wall_seconds))
    print(f"Completed {condition_name} in {wall_seconds:.1f}s", flush=True)


def _evaluate_condition(raw_path: Path, condition_name: str) -> None:
    pilot_dir = _pilot_dir(raw_path)
    allowed = {"base", *(name for name, _ in LEARNING_RATES)}
    if condition_name not in allowed:
        raise ValueError(f"Unknown evaluation condition: {condition_name}")
    manifest = read_json(pilot_dir / "pilot_manifest.json", {})
    conditions = manifest.get("conditions", {})
    if any(name not in conditions or "best_eval_loss" not in conditions[name] for name, _ in LEARNING_RATES):
        raise RuntimeError("Both learning-rate training conditions must complete before answer evaluation.")
    splits, hashes = load_splits()
    identity = _assert_pilot_input_identity(pilot_dir, manifest, hashes)
    for name, _rate in LEARNING_RATES:
        _assert_condition_evidence(pilot_dir / name, conditions[name], identity)
    selected_ids = conditions[LEARNING_RATES[0][0]]["selected_validation_ids"]
    if selected_ids != conditions[LEARNING_RATES[1][0]]["selected_validation_ids"]:
        raise RuntimeError("Training conditions do not share the same held-out validation IDs.")
    validation_rows = _selection_rows(splits["validation"], selected_ids)
    import torch
    from peft import PeftModel
    from dashboard.training import limit_training_memory, training_memory
    tokenizer = load_tokenizer()
    tokenizer.padding_side = "left"
    events = Events(pilot_dir)
    config = validate_config({**DEFAULTS, **PILOT_CONFIG})
    budget = limit_training_memory(config, events, operation="pilot validation generation")
    load_started = time.perf_counter()
    model = load_model(events)
    if condition_name != "base":
        model = PeftModel.from_pretrained(model, str(pilot_dir / condition_name / "adapter"), is_trainable=False)
    model.eval()
    model.config.use_cache = True
    model_load_seconds = time.perf_counter() - load_started
    predictions, elapsed, prompt_sizes = _generate(model, tokenizer, validation_rows, torch)
    memory = {**budget, **training_memory()}
    report = _write_score(
        validation_rows, predictions, condition_name,
        pilot_dir / f"validation_{condition_name}.json", elapsed, memory, prompt_sizes,
    )
    report["model_load_and_adapter_seconds"] = model_load_seconds
    if condition_name != "base":
        report["learning_rate"] = dict(LEARNING_RATES)[condition_name]
        report["eval_loss"] = conditions[condition_name]["best_eval_loss"]
    write_json(pilot_dir / f"validation_{condition_name}.json", report)
    print(json.dumps({key: report[key] for key in ("condition", "correct", "total", "accuracy", "generation_seconds", "model_load_and_adapter_seconds")}, indent=2), flush=True)


def _child_environment() -> dict:
    return {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "TOKENIZERS_PARALLELISM": "false"}


def _run_external(command: list[str], description: str) -> float:
    print(f"Starting {description}", flush=True)
    started = time.perf_counter()
    result = subprocess.run(command, cwd=ROOT, env=_child_environment(), check=False)
    elapsed = time.perf_counter() - started
    if result.returncode:
        raise RuntimeError(f"{description} subprocess exited with code {result.returncode} after {elapsed:.1f}s.")
    return elapsed


def _run_child(arguments: list[str], description: str) -> float:
    return _run_external(
        [sys.executable, "-u", "-m", "tools.training_experiment", *arguments],
        description,
    )


def run_pilot(resume_path: Path | None = None) -> Path:
    from dashboard.chat_engine import release_resident_model

    runs = ROOT / "runs"
    runs.mkdir(exist_ok=True)
    if resume_path:
        pilot_dir = _pilot_dir(resume_path)
        manifest = read_json(pilot_dir / "pilot_manifest.json")
        if not isinstance(manifest, dict) or manifest.get("name") != pilot_dir.name:
            raise ValueError("Pilot manifest is missing or does not match the resume directory.")
        if manifest.get("status") == "completed":
            raise ValueError("This pilot is already complete.")
        prior_failure = manifest.pop("failure", None)
        if prior_failure:
            manifest.setdefault("failure_history", []).append(prior_failure)
    else:
        pilot_name = f"training-pilot-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{os.getpid()}"
        pilot_dir = runs / pilot_name
        pilot_dir.mkdir(exist_ok=False)
        manifest = {
            "name": pilot_name,
            "status": "preparing",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "model": _model_identity(),
            "environment": _environment(),
            "settings_common": validate_config({**DEFAULTS, **PILOT_CONFIG}),
            "learning_rate_conditions": [{"name": name, "learning_rate": rate} for name, rate in LEARNING_RATES],
            "conditions": {},
            "answer_evaluation": None,
            "official_test_used_to_choose": False,
        }
    _splits, current_hashes = load_splits()
    manifest["input_identity"] = _assert_pilot_input_identity(pilot_dir, manifest, current_hashes)
    manifest["subprocess_isolation"] = (
        "GPU evidence, each training condition, and each answer evaluation run in separate processes; "
        "the orchestrator holds the dashboard worker lock."
    )
    manifest["trainer_settings"] = {
        "optimizer": "adamw_torch",
        "lr_scheduler_type": "cosine",
        "warmup_steps": 1,
        "optimizer_updates": 8,
        "bf16": True,
        "tf32": True,
        "gradient_checkpointing": True,
        "max_grad_norm": 1.0,
        "eval_strategy": "epoch",
        "save_strategy": "epoch",
        "best_checkpoint_metric": "eval_loss",
    }
    write_json(pilot_dir / "pilot_manifest.json", manifest)

    release_resident_model()
    with worker_lock():
        _ensure_no_active_runs()
        try:
            manifest["status"] = "running"
            evidence_path = pilot_dir / "gpu_evidence.json"
            if not evidence_path.is_file():
                _run_external(
                    [sys.executable, "-u", "-m", "tools.gpu_evidence", "--output", str(evidence_path)],
                    "CUDA hardware evidence probe",
                )
            evidence = read_json(evidence_path, {})
            if evidence.get("cuda_available") is not True or evidence.get("cuda_operation", {}).get("result_device", "").split(":", 1)[0] != "cuda":
                raise RuntimeError("The GPU evidence did not confirm a completed CUDA operation.")
            manifest["gpu_evidence"] = "gpu_evidence.json"
            write_json(pilot_dir / "pilot_manifest.json", manifest)

            for condition_name, learning_rate in LEARNING_RATES:
                condition_dir = pilot_dir / condition_name
                prior = manifest.get("conditions", {}).get(condition_name, {})
                completed = (
                    "best_eval_loss" in prior
                    and (condition_dir / "training_summary.json").is_file()
                    and (condition_dir / "condition_result.json").is_file()
                    and (condition_dir / "adapter/adapter_config.json").is_file()
                )
                if completed:
                    _assert_condition_evidence(condition_dir, read_json(condition_dir / "condition_result.json", {}), manifest["input_identity"])
                    print(f"Reusing completed condition {condition_name}", flush=True)
                    continue
                if condition_dir.exists():
                    failed_dir = pilot_dir / "failed_attempts" / f"{condition_name}-{int(time.time())}"
                    failed_dir.parent.mkdir(exist_ok=True)
                    condition_dir.rename(failed_dir)
                    manifest.setdefault("failed_attempts", []).append({
                        "condition": condition_name,
                        "artifacts": str(failed_dir.relative_to(ROOT)),
                        "note": "Incomplete artifacts from a previous failed attempt; retained for inspection.",
                    })
                manifest.setdefault("conditions", {})[condition_name] = {"status": "running", "learning_rate": learning_rate}
                write_json(pilot_dir / "pilot_manifest.json", manifest)
                wall_seconds = _run_child(
                    ["--_train-condition", condition_name, "--_pilot-dir", str(pilot_dir)],
                    f"training condition {condition_name} (learning_rate={learning_rate})",
                )
                result = read_json(condition_dir / "condition_result.json")
                if not isinstance(result, dict):
                    raise RuntimeError(f"{condition_name} did not produce a condition result.")
                _assert_condition_evidence(condition_dir, result, manifest["input_identity"])
                result["subprocess_wall_seconds"] = wall_seconds
                manifest["conditions"][condition_name] = result
                write_json(pilot_dir / "pilot_manifest.json", manifest)

            low = manifest["conditions"][LEARNING_RATES[0][0]]
            default = manifest["conditions"][LEARNING_RATES[1][0]]
            for key in ("selected_train_ids", "selected_validation_ids", "data_sha256_train", "data_sha256_validation", "prompt_fingerprint"):
                if low[key] != default[key]:
                    raise RuntimeError(f"Pilot fairness check failed: {key} differed across learning rates.")
            manifest["selection"] = {
                key: low[key]
                for key in ("selected_train_ids", "selected_validation_ids", "data_sha256_train", "data_sha256_validation", "prompt_fingerprint")
            }

            current_splits, current_hashes = load_splits()
            manifest["input_identity"] = _assert_pilot_input_identity(pilot_dir, manifest, current_hashes)
            validation_rows = _selection_rows(current_splits["validation"], low["selected_validation_ids"])

            for condition_name in ("base", *(name for name, _ in LEARNING_RATES)):
                report_path = pilot_dir / f"validation_{condition_name}.json"
                if report_path.is_file() and not _validation_report_matches(read_json(report_path, {}), condition_name, validation_rows):
                    archived = pilot_dir / "unverified_validation"
                    archived.mkdir(exist_ok=True)
                    target = archived / report_path.name
                    if target.exists():
                        target = archived / f"{report_path.stem}-{int(time.time())}{report_path.suffix}"
                    report_path.replace(target)
                if not report_path.is_file():
                    _run_child(
                        ["--_evaluate-condition", condition_name, "--_pilot-dir", str(pilot_dir)],
                        f"held-out answer evaluation for {condition_name}",
                    )
                if not _validation_report_matches(read_json(report_path, {}), condition_name, validation_rows):
                    raise RuntimeError(f"Validation report for {condition_name} does not prove the current held-out rows.")

            base = read_json(pilot_dir / "validation_base.json")
            condition_reports = {}
            for condition_name, _learning_rate in LEARNING_RATES:
                report = read_json(pilot_dir / f"validation_{condition_name}.json")
                report["delta_accuracy_vs_base_pp"] = (report["accuracy"] - base["accuracy"]) * 100
                write_json(pilot_dir / f"validation_{condition_name}.json", report)
                condition_reports[condition_name] = {
                    "accuracy": report["accuracy"],
                    "correct": report["correct"],
                    "total": report["total"],
                    "eval_loss": report["eval_loss"],
                    "delta_accuracy_vs_base_pp": report["delta_accuracy_vs_base_pp"],
                    "generation_seconds": report["generation_seconds"],
                    "model_load_and_adapter_seconds": report["model_load_and_adapter_seconds"],
                    "generation_examples_per_second": report["generation_examples_per_second"],
                    "generation_memory": report["gpu_memory"],
                }
            manifest["answer_evaluation"] = {
                "dataset": "selected held-out validation rows only",
                "examples": base["total"],
                "metric": "normalized exact formula match",
                "generation": {"do_sample": False, "max_new_tokens": baseline.MAX_NEW_TOKENS},
                "base": {
                    "accuracy": base["accuracy"],
                    "correct": base["correct"],
                    "total": base["total"],
                    "generation_seconds": base["generation_seconds"],
                    "model_load_and_adapter_seconds": base["model_load_and_adapter_seconds"],
                    "generation_memory": base["gpu_memory"],
                },
                "conditions": condition_reports,
            }
            manifest["status"] = "completed"
            manifest["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
            write_json(pilot_dir / "pilot_manifest.json", manifest)
            write_json(pilot_dir / "pilot_report.json", manifest)
            print(json.dumps(manifest["answer_evaluation"], ensure_ascii=False, indent=2), flush=True)
        except Exception as exc:
            manifest["status"] = "failed"
            manifest["failure"] = {"message": str(exc), "recorded_at_utc": datetime.now(timezone.utc).isoformat()}
            write_json(pilot_dir / "pilot_manifest.json", manifest)
            raise
    return pilot_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Run the real model training and validation generation on CUDA.")
    parser.add_argument("--resume", type=Path, help="Resume an incomplete isolated training-pilot directory under runs/.")
    parser.add_argument("--_pilot-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_train-condition", choices=[name for name, _ in LEARNING_RATES], help=argparse.SUPPRESS)
    parser.add_argument("--_evaluate-condition", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._train_condition:
        if not args._pilot_dir:
            parser.error("internal training requires --_pilot-dir")
        _train_condition(args._pilot_dir, args._train_condition)
        return 0
    if args._evaluate_condition:
        if not args._pilot_dir:
            parser.error("internal evaluation requires --_pilot-dir")
        _evaluate_condition(args._pilot_dir, args._evaluate_condition)
        return 0
    if not args.execute:
        print(json.dumps({"action": "pilot plan only", "execute_command": "python -m tools.training_experiment --execute", "settings": {**PILOT_CONFIG, "learning_rates": [rate for _, rate in LEARNING_RATES]}}, ensure_ascii=False, indent=2))
        return 0
    try:
        result = run_pilot(args.resume)
    except Exception as exc:
        print(f"Pilot failed: {exc}", file=sys.stderr)
        raise
    print(f"Pilot results saved under {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
