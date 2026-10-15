"""Isolated GPU worker. Run with python -m dashboard.worker --run-id ID."""

import argparse
import math
import time
import traceback
from pathlib import Path

from benchmark import run_baseline as baseline
from dashboard.config import ROOT, read_json, run_path, validate_config, write_json
from dashboard.data import CausalCollator, load_splits, prepare, token_ids
from dashboard.runtime import Cancelled, Events, worker_lock
from dashboard.adapters import inherit_lora_config, training_adapter
from dashboard.training import limit_training_memory, response_only_inputs, training_memory


def local_snapshot():
    from huggingface_hub import hf_hub_download
    # Model loading needs its configuration/weights, not a complete repo mirror.
    return Path(hf_hub_download(baseline.MODEL_NAME, "config.json", revision=baseline.MODEL_REVISION, local_files_only=True)).parent


def load_tokenizer():
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(local_snapshot(), local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    return tokenizer


def load_model(events):
    import torch
    from transformers import AutoModelForCausalLM, BitsAndBytesConfig
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA indisponível. Use o .venv com PyTorch CUDA; não há fallback para CPU.")
    if not torch.cuda.is_bf16_supported():
        raise RuntimeError("A GPU precisa suportar bfloat16 para este experimento.")
    events.emit(phase="Carregando Qwen em NF4", gpu=torch.cuda.get_device_name(0))
    torch.cuda.set_device(0)
    torch.backends.cuda.matmul.allow_tf32 = True
    # Resolve to a local directory so PEFT discovery also remains offline.
    snapshot = local_snapshot()
    model = AutoModelForCausalLM.from_pretrained(
        snapshot,
        quantization_config=BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True,
        ),
        device_map={"": "cuda:0"}, dtype=torch.bfloat16,
        low_cpu_mem_usage=True, attn_implementation="sdpa",
        local_files_only=True,
    )
    events.check()
    return model


def prepare_training_model(model, config, events, adapter_run=None):
    from peft import LoraConfig, PeftModel, get_peft_model, prepare_model_for_kbit_training

    model.config.use_cache = False
    model = prepare_model_for_kbit_training(model, gradient_checkpointing_kwargs={"use_reentrant": False})
    if adapter_run:
        adapter, _ = training_adapter(adapter_run)
        events.emit(phase="Carregando adaptador para continuar", adapter_run=adapter_run)
        return PeftModel.from_pretrained(model, adapter, is_trainable=True)
    events.emit(phase="Preparando adaptador LoRA")
    return get_peft_model(model, LoraConfig(
        r=config["lora_r"], lora_alpha=config["lora_alpha"], lora_dropout=0.05,
        target_modules="all-linear", bias="none", task_type="CAUSAL_LM",
    ))


def train(config, tokenizer, directory, events, adapter_run=None, data_preparer=None):
    import torch
    from datasets import Dataset
    from transformers import Trainer, TrainerCallback, TrainingArguments, set_seed

    if adapter_run:
        config = inherit_lora_config(config, adapter_run)
        if (directory / "adapter").resolve() == training_adapter(adapter_run)[0].resolve():
            raise ValueError("A continuação deve ser salva em uma nova execução.")
    set_seed(config["seed"])
    prepare_data = data_preparer or prepare
    encoded, selection = prepare_data(config, tokenizer, directory, events.emit, adapter_run=adapter_run)
    datasets = {name: Dataset.from_list(rows) for name, rows in encoded.items()}
    sizes = {name: len(dataset) for name, dataset in datasets.items()}
    del encoded
    memory_budget = limit_training_memory(config, events)
    model = load_model(events)
    model = prepare_training_model(model, config, events, adapter_run)
    model.print_trainable_parameters()
    trainable, total = model.get_nb_trainable_parameters()
    write_json(directory / "model_info.json", {"trainable_parameters": trainable, "total_parameters": total})
    steps = math.ceil(sizes["train"] / (config["batch_size"] * config["gradient_accumulation"])) * config["epochs"]
    class Monitor(TrainerCallback):
        train_seen = 0
        validation_seen = 0

        def on_train_begin(self, args, state, control, **kwargs):
            events.emit(phase="Preparando otimizador")

        def on_epoch_begin(self, args, state, control, **kwargs):
            self.train_seen = 0
            events.emit(phase="Treinando", phase_step=0, phase_total=sizes["train"], phase_unit="exemplos",
                        step=state.global_step, total_steps=state.max_steps, epoch=state.epoch)

        def report_microbatch(self, args, state):
            self.train_seen = min(sizes["train"], self.train_seen + args.per_device_train_batch_size)
            events.emit(
                phase="Treinando", phase_step=self.train_seen, phase_total=sizes["train"], phase_unit="exemplos",
                step=state.global_step, total_steps=state.max_steps, epoch=state.epoch,
                **training_memory(),
            )

        def on_substep_end(self, args, state, control, **kwargs):
            self.report_microbatch(args, state)

        def on_step_end(self, args, state, control, **kwargs):
            if events.cancelled():
                control.should_training_stop = True
                return control
            self.report_microbatch(args, state)

        def on_log(self, args, state, control, logs=None, **kwargs):
            values = {k: v for k, v in (logs or {}).items() if isinstance(v, (int, float))}
            events.emit(step=state.global_step, total_steps=state.max_steps, **values)

        def on_prediction_step(self, args, state, control, **kwargs):
            self.validation_seen = min(sizes["validation"], self.validation_seen + args.per_device_eval_batch_size)
            events.emit(phase="Validando", phase_step=self.validation_seen, phase_total=sizes["validation"],
                        phase_unit="exemplos", epoch=state.epoch, **training_memory())

        def on_evaluate(self, args, state, control, **kwargs):
            events.emit(phase="Validação concluída", phase_step=self.validation_seen, phase_total=sizes["validation"],
                        phase_unit="exemplos", step=state.global_step, total_steps=state.max_steps)

    monitor = Monitor()

    class VisibleTrainer(Trainer):
        def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None):
            return super().compute_loss(model, response_only_inputs(inputs), return_outputs, num_items_in_batch)

        def evaluation_loop(self, *args, **kwargs):
            monitor.validation_seen = 0
            events.emit(phase="Validando", phase_step=0, phase_total=sizes["validation"], phase_unit="exemplos")
            return super().evaluation_loop(*args, **kwargs)

        def _save_checkpoint(self, *args, **kwargs):
            events.emit(phase="Salvando checkpoint da época")
            return super()._save_checkpoint(*args, **kwargs)

        def _load_best_model(self):
            events.emit(phase="Carregando melhor checkpoint")
            return super()._load_best_model()

    args = TrainingArguments(
        output_dir=str(directory / "checkpoints"),
        num_train_epochs=config["epochs"], learning_rate=config["learning_rate"],
        per_device_train_batch_size=config["batch_size"], per_device_eval_batch_size=1,
        gradient_accumulation_steps=config["gradient_accumulation"],
        gradient_checkpointing=True, gradient_checkpointing_kwargs={"use_reentrant": False},
        bf16=True, tf32=True, optim="adamw_torch", lr_scheduler_type="cosine",
        warmup_steps=min(max(0, steps - 1), max(1, int(steps * 0.03))), max_grad_norm=1.0,
        logging_steps=config["logging_steps"], logging_first_step=True,
        logging_nan_inf_filter=False, eval_strategy="epoch", save_strategy="epoch",
        save_total_limit=2, load_best_model_at_end=True, metric_for_best_model="eval_loss",
        greater_is_better=False, prediction_loss_only=True, report_to="none",
        dataloader_num_workers=0, dataloader_pin_memory=True,
        seed=config["seed"], data_seed=config["seed"], disable_tqdm=True,
        use_cache=False,
    )
    trainer = VisibleTrainer(
        model=model, args=args, processing_class=tokenizer,
        train_dataset=datasets["train"],
        eval_dataset=datasets["validation"],
        data_collator=CausalCollator(tokenizer.pad_token_id), callbacks=[monitor],
    )
    try:
        result = trainer.train()
        events.check()
    except Cancelled:
        trainer.save_model(str(directory / "partial_adapter"))
        print("Adaptador parcial salvo; ele não será oferecido como treino concluído.", flush=True)
        raise
    except torch.OutOfMemoryError as exc:
        raise RuntimeError("O treinamento atingiu o limite de memória. Reduza o batch ou o limite de tokens e verifique os dados novamente.") from exc
    events.emit(phase="Salvando melhor adaptador")
    trainer.save_model(str(directory / "adapter"))
    tokenizer.save_pretrained(directory / "adapter")
    trainer.save_state()
    write_json(directory / "training_summary.json", {
        "adapter_run": adapter_run, "optimizer_restarted": True,
        "metrics": result.metrics, "best_eval_loss": trainer.state.best_metric,
        "best_checkpoint": trainer.state.best_model_checkpoint,
        "effective_batch_size": config["batch_size"] * config["gradient_accumulation"],
        "memory": {**memory_budget, **training_memory()},
        "selection": {k: {a: b for a, b in v.items() if a != "ids"} for k, v in selection["splits"].items()},
    })
    events.emit(phase="Treinamento concluído", step=steps, total_steps=steps)


def comparable(baseline_report, rows):
    if not baseline_report or baseline_report.get("model") != baseline.MODEL_NAME:
        return False
    if baseline_report.get("model_revision") != baseline.MODEL_REVISION:
        return False
    if baseline_report.get("adapter_run") is not None:
        return False
    if baseline_report.get("prompt_fingerprint") != baseline.prompt_fingerprint():
        return False
    if baseline_report.get("do_sample") is not False or baseline_report.get("max_new_tokens") != baseline.MAX_NEW_TOKENS:
        return False
    items = baseline_report.get("items", [])
    if not isinstance(items, list) or len(items) != len(rows):
        return False
    identity = baseline.evaluation_identity(rows)
    fingerprint = baseline.evaluation_fingerprint(rows)
    if (
        baseline_report.get("evaluation_identity") == identity
        and baseline_report.get("evaluation_fingerprint") == fingerprint
    ):
        return all(
            isinstance(item, dict)
            and item.get("id") == record["id"]
            and item.get("expected") == record["expected"]
            and item.get("input_fingerprint") == baseline.evaluation_record_fingerprint(row)
            for item, row, record in zip(items, rows, map(baseline.evaluation_record, rows))
        )

    # Older reports are usable only when the report itself contains every input value
    # in the original order. File hashes or matching IDs/gold alone are insufficient.
    fields = set(baseline.EVALUATION_FIELDS)
    return all(
        isinstance(item, dict)
        and fields <= item.keys()
        and baseline.evaluation_record(item) == baseline.evaluation_record(row)
        for item, row in zip(items, rows)
    )


def check_evaluation_adapter(adapter_dir, hashes, fingerprint):
    if read_json(adapter_dir / "status.json", {}).get("status") != "completed" or not (adapter_dir / "adapter/adapter_config.json").is_file():
        raise ValueError("Selecione uma execução concluída com adaptador salvo.")
    kind = read_json(adapter_dir / "request.json", {}).get("kind")
    if kind == "decensor":
        return kind
    if kind != "train":
        raise ValueError("Selecione um treinamento ou um adaptador de recusa concluído.")
    selection = read_json(adapter_dir / "selection.json", {})
    if selection.get("data_sha256") != hashes or selection.get("prompt_fingerprint") != fingerprint:
        raise ValueError("Dataset ou prompt mudou desde o treinamento; a comparação foi bloqueada.")
    return kind


def evaluate(request, tokenizer, directory, events):
    import torch
    config = validate_config(request.get("config", {}))
    splits, hashes = load_splits()
    rows = splits["test"]
    original = read_json(ROOT / "benchmark/results/baseline.json")
    if not comparable(original, rows):
        raise ValueError("O baseline salvo não comprova as mesmas entradas e receita de avaliação. Preserve o resultado antigo e gere um novo baseline com identidade completa antes de comparar.")
    adapter_run = request.get("adapter_run")
    adapter = None
    if adapter_run:
        adapter = run_path(adapter_run)
        check_evaluation_adapter(adapter, hashes, baseline.prompt_fingerprint())
    memory_budget = limit_training_memory(config, events, operation="avaliação")
    model = load_model(events)
    if adapter:
        from peft import PeftModel
        events.emit(phase="Carregando adaptador treinado")
        model = PeftModel.from_pretrained(model, adapter / "adapter", is_trainable=False)
    model.eval()
    model.config.use_cache = True
    tokenizer.padding_side = "left"
    events.emit(phase="Preparando prompts de avaliação")
    encoded = [token_ids(tokenizer, baseline.row_messages(row), generation=True) for row in rows]
    order = sorted(enumerate(map(len, encoded)), key=lambda pair: pair[1])
    # Include the generated suffix in the padded-token budget for the KV cache.
    batches = baseline.make_batches(
        [(index, length + baseline.MAX_NEW_TOKENS) for index, length in order],
        config["eval_batch_size"], baseline.MAX_BATCH_TOKENS,
    )
    predictions = [None] * len(rows)
    finished = 0
    started = time.time()
    events.emit(phase="Avaliando teste congelado", step=0, total_steps=len(rows), phase_unit="exemplos")

    def generate(indices):
        nonlocal finished
        events.check()
        answers = None
        try:
            answers = baseline.generate_token_batch(model, tokenizer, [encoded[i] for i in indices], torch)
        except torch.OutOfMemoryError:
            if len(indices) == 1:
                raise RuntimeError("Um teste não coube no limite de VRAM da avaliação. Libere memória de outros aplicativos antes de tentar novamente; o contexto do teste não será truncado.") from None
        # Leave the exception handler first so its traceback releases failed CUDA tensors.
        if answers is None:
            torch.cuda.empty_cache()
            middle = len(indices) // 2
            generate(indices[:middle])
            generate(indices[middle:])
            return
        baseline.append_checkpoint(directory / "predictions.jsonl", [rows[i] for i in indices], answers, indices)
        for i, answer in zip(indices, answers):
            predictions[i] = answer
        finished += len(indices)
        events.emit(
            phase="Avaliando teste congelado", step=finished, total_steps=len(rows), phase_unit="exemplos",
            eta_seconds=(time.time() - started) / finished * (len(rows) - finished),
            eval_batch_size=len(indices), **training_memory(),
        )

    for batch in batches:
        generate(batch)
    events.emit(phase="Salvando comparação")
    report = baseline.score_rows(rows, predictions, str(ROOT / "dataset/canonical/test.jsonl"))
    report.update(adapter_run=adapter_run, data_sha256=hashes["test"], metric="normalized_exact_match")
    report.update(memory={**memory_budget, **training_memory()}, eval_batch_size=config["eval_batch_size"])
    report["comparison"] = {
        "baseline_accuracy": original["accuracy"],
        "delta_pp": (report["accuracy"] - original["accuracy"]) * 100,
        "gained": sum(not old["match"] and new["match"] for old, new in zip(original["items"], report["items"])),
        "lost": sum(old["match"] and not new["match"] for old, new in zip(original["items"], report["items"])),
    }
    for item, row, old in zip(report["items"], rows, original["items"]):
        item.update(instruction=row["instruction"], level=row.get("level", ""), baseline_predicted=old["predicted"], baseline_match=old["match"])
    write_json(directory / "report.json", report)
    events.emit(phase="Avaliação concluída", step=len(rows), total_steps=len(rows), accuracy=report["accuracy"])
    baseline.print_summary(report["total"], report["correct"], report["accuracy"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    directory = run_path(args.run_id)
    if read_json(directory / "status.json", {}).get("status") != "queued":
        raise SystemExit("Este ID já foi executado. Crie uma nova execução pelo painel para preservar os artefatos.")
    events = Events(directory)
    try:
        from dashboard.chat_engine import release_resident_model
        release_resident_model()
        with worker_lock():
            events.check()
            request = read_json(directory / "request.json")
            config = validate_config(request["config"])
            events.status("running", started_at=time.time())
            write_json(directory / "environment.json", environment())
            events.emit(phase="Carregando tokenizer")
            tokenizer = load_tokenizer()
            if request["kind"] == "prepare":
                prepare(config, tokenizer, directory, events.emit, adapter_run=request.get("adapter_run"))
            elif request["kind"] == "train":
                training_mode = request.get("training_mode")
                if training_mode is None:
                    train(config, tokenizer, directory, events, adapter_run=request.get("adapter_run"))
                elif training_mode == "tool_usage":
                    from dashboard.tool_training import prepare_tool_usage
                    train(config, tokenizer, directory, events, adapter_run=request.get("adapter_run"),
                          data_preparer=prepare_tool_usage)
                else:
                    raise ValueError("Modo de treinamento desconhecido.")
            elif request["kind"] == "evaluate":
                evaluate(request, tokenizer, directory, events)
            elif request["kind"] == "chat":
                from dashboard.chat_backend import generate_chat
                generate_chat(request, tokenizer, directory, events)
            elif request["kind"] == "decensor":
                from dashboard.decensor import run_decensor
                run_decensor(tokenizer, directory, events, config)
            elif request["kind"] == "tool_evaluate":
                from dashboard.tool_evaluation import evaluate_tools
                evaluate_tools(request, tokenizer, directory, events)
            else:
                raise ValueError("Operação desconhecida.")
            events.check()
            events.status("completed", ended_at=time.time())
    except Cancelled as exc:
        print(str(exc), flush=True)
        events.status("cancelled", ended_at=time.time())
    except Exception as exc:
        traceback.print_exc()
        events.status("failed", error=str(exc), ended_at=time.time())
        return 1
    return 0


def environment():
    import importlib.metadata
    return {"model_snapshot": local_snapshot().name, **{name: importlib.metadata.version(name) for name in ("torch", "transformers", "peft", "accelerate", "bitsandbytes")}}


if __name__ == "__main__":
    raise SystemExit(main())
