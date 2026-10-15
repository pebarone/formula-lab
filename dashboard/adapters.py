"""Validate a saved training source and inherit its LoRA architecture."""

from dashboard.config import read_json, run_path, validate_config


def training_adapter(run_id):
    path = run_path(run_id)
    if read_json(path / "request.json", {}).get("kind") != "train" or read_json(path / "status.json", {}).get("status") != "completed":
        raise ValueError("Escolha um adaptador de treinamento concluído.")
    adapter = path / "adapter"
    if not all((adapter / name).is_file() for name in ("adapter_config.json", "adapter_model.safetensors")):
        raise ValueError("O adaptador selecionado está incompleto.")
    metadata = read_json(adapter / "adapter_config.json")
    if not isinstance(metadata, dict) or metadata.get("peft_type") != "LORA" or metadata.get("task_type") != "CAUSAL_LM":
        raise ValueError("O adaptador precisa ser LoRA para geração de texto.")
    return adapter, metadata


def inherit_lora_config(config, run_id):
    _, metadata = training_adapter(run_id)
    return validate_config({**config, "lora_r": metadata.get("r"), "lora_alpha": metadata.get("lora_alpha")})


def training_history(run_id, hashes, prompt):
    """Exclude every selected training ID in the source's ancestry, conservatively."""
    ids, runs = set(), []
    while run_id:
        if run_id in runs:
            raise ValueError("Ciclo na origem dos adaptadores; não é possível garantir exemplos novos.")
        adapter, _ = training_adapter(run_id)
        directory = adapter.parent
        selection = read_json(directory / "selection.json", {})
        if selection.get("data_sha256") != hashes or selection.get("prompt_fingerprint") != prompt:
            raise ValueError(f"Dataset, prompt ou manifesto incompatível na origem {run_id}; não é possível garantir exemplos novos.")
        selected = selection.get("splits", {}).get("train", {}).get("ids")
        if not isinstance(selected, list) or not selected or any(not isinstance(value, str) or not value for value in selected):
            raise ValueError(f"IDs de treino ausentes ou inválidos na origem {run_id}.")
        ids.update(selected)
        runs.append(run_id)
        run_id = read_json(directory / "request.json", {}).get("adapter_run")
    return ids, runs
