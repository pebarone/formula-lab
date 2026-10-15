"""Rank-1 LoRA that removes the refusal direction from Qwen.

The measurement follows Arditi et al.: the residual at the last prompt token
differs between requests the model refuses and ordinary requests, and that
difference is close to one direction. A rank-1 update
ΔW = -strength · r rᵀ W removes that direction from each matrix that writes
the residual (attention output and MLP down projection). No harmful
completion is generated or used as a training target, and the Excel dataset
is not read.
"""

import gc

import torch

from dashboard.config import write_json
from dashboard.decensor_prompts import HARMLESS_PROMPTS, REFUSAL_PROMPTS

STRENGTH = 1.0
MIN_COSINE = 0.35
MIN_NORM = 0.1
MIN_KEPT_LAYERS = 8
MAX_PROMPT_TOKENS = 512
LORA_R = 1
LORA_ALPHA = 1


def refusal_direction(harmful, harmless):
    delta = harmful.float().mean(dim=0) - harmless.float().mean(dim=0)
    norm = delta.norm()
    if float(norm) == 0.0:
        return delta, norm
    return delta / norm, norm


def split_cosine(harmful, harmless):
    first, _ = refusal_direction(harmful[0::2], harmless[0::2])
    second, _ = refusal_direction(harmful[1::2], harmless[1::2])
    denom = first.norm() * second.norm()
    if float(denom) == 0.0:
        return 0.0
    return float(torch.dot(first, second) / denom)


def lora_factors(weight, direction, strength):
    """Return A (1, in) and B (out, 1) such that B @ A = -strength · r rᵀ W."""
    vector = direction.float().reshape(-1)
    vector = vector / vector.norm().clamp_min(1e-8)
    if weight.shape[0] != vector.numel():
        raise ValueError("A direção de recusa não tem a dimensão de saída da matriz.")
    projected = weight.float().transpose(0, 1).matmul(vector)
    factors_a = projected.unsqueeze(0).contiguous()
    factors_b = (-float(strength) * vector).unsqueeze(1).contiguous()
    return factors_a, factors_b


def residual_along(weight, factors_a, factors_b, direction):
    vector = direction.float().reshape(-1)
    vector = vector / vector.norm().clamp_min(1e-8)
    sample = torch.randn(weight.shape[1], generator=torch.Generator().manual_seed(0))
    updated = (weight.float() + factors_b.float().matmul(factors_a.float())).matmul(sample)
    return float(torch.dot(updated, vector).abs())


def prompt_token_ids(tokenizer, text):
    encoded = tokenizer.apply_chat_template(
        [{"role": "user", "content": text}], add_generation_prompt=True, tokenize=True,
    )
    token_ids = getattr(encoded, "input_ids", None)
    if token_ids is not None:
        encoded = token_ids
    elif isinstance(encoded, dict):
        encoded = encoded["input_ids"]
    if torch.is_tensor(encoded):
        if encoded.ndim == 2:
            encoded = encoded[0]
        encoded = encoded.tolist()
    if encoded and isinstance(encoded[0], (list, tuple)):
        encoded = encoded[0]
    ids = [int(token) for token in encoded]
    if not ids or len(ids) > MAX_PROMPT_TOKENS:
        raise ValueError(f"Prompt de contraste com {len(ids)} tokens; o limite é {MAX_PROMPT_TOKENS}.")
    return ids


def _contrast_sets():
    harmful, harmless = tuple(REFUSAL_PROMPTS), tuple(HARMLESS_PROMPTS)
    if len(harmful) < MIN_KEPT_LAYERS or len(harmful) % 2 or len(harmless) % 2:
        raise ValueError("Os conjuntos de contraste precisam ser pares e ter tamanho suficiente.")
    if len(harmful) != len(harmless) or set(harmful) & set(harmless) or len(set(harmful)) != len(harmful):
        raise ValueError("Os conjuntos de contraste precisam ser distintos, sem repetição e do mesmo tamanho.")
    return harmful, harmless


def _layer_decisions(harmful_states, harmless_states):
    kept, rows = {}, []
    for layer in range(harmful_states.shape[1]):
        direction, norm = refusal_direction(harmful_states[:, layer, :], harmless_states[:, layer, :])
        cosine = split_cosine(harmful_states[:, layer, :], harmless_states[:, layer, :])
        keep = cosine >= MIN_COSINE and float(norm) >= MIN_NORM
        rows.append({"layer": layer, "cosine": round(cosine, 6), "norm": round(float(norm), 6), "kept": keep})
        if keep:
            kept[layer] = direction
    return kept, rows


def _quantized_weight(module):
    import bitsandbytes.functional as bitsandbytes_functional

    base = getattr(module, "base_layer", module)
    quant_state = getattr(base.weight, "quant_state", None)
    if quant_state is None:
        raise RuntimeError("A matriz não está quantizada em 4 bits.")
    matrix = bitsandbytes_functional.dequantize_4bit(base.weight.data, quant_state).float().cpu()
    out_features, in_features = base.out_features, base.in_features
    if tuple(matrix.shape) == (out_features, in_features):
        return matrix
    if tuple(matrix.shape) == (in_features, out_features):
        return matrix.transpose(0, 1).contiguous()
    raise RuntimeError(f"Forma {tuple(matrix.shape)} incompatível com ({out_features}, {in_features}).")


def _write_refusal_lora(model, directions):
    import re

    from peft import LoraConfig, get_peft_model

    adapted = get_peft_model(model, LoraConfig(
        r=LORA_R, lora_alpha=LORA_ALPHA, lora_dropout=0.0,
        target_modules=["o_proj", "down_proj"], bias="none", task_type="CAUSAL_LM",
    ))
    pattern = re.compile(r"layers\.(\d+)\.(?:self_attn\.o_proj|mlp\.down_proj)$")
    written, checked = [], None
    for name, module in adapted.named_modules():
        match = pattern.search(name)
        if match is None:
            continue
        layer = int(match.group(1))
        if layer not in directions:
            factors_a = torch.zeros_like(module.lora_A["default"].weight)
            factors_b = torch.zeros_like(module.lora_B["default"].weight)
        else:
            matrix = _quantized_weight(module)
            factors_a, factors_b = lora_factors(matrix, directions[layer], STRENGTH)
            component = residual_along(matrix, factors_a, factors_b, directions[layer])
            if component > 1e-3:
                raise RuntimeError(f"A projeção em {name} não removeu a direção (resíduo {component:.3e}).")
            scale = float(module.scaling["default"])
            if scale == 0.0:
                raise RuntimeError("Escala LoRA igual a zero.")
            factors_b = factors_b / scale
            checked = max(checked or 0.0, component)
            del matrix
        parameter_a = module.lora_A["default"].weight
        parameter_b = module.lora_B["default"].weight
        if parameter_a.shape != factors_a.shape or parameter_b.shape != factors_b.shape:
            raise RuntimeError(f"Forma LoRA inesperada em {name}.")
        parameter_a.data.copy_(factors_a.to(device=parameter_a.device, dtype=parameter_a.dtype))
        parameter_b.data.copy_(factors_b.to(device=parameter_b.device, dtype=parameter_b.dtype))
        written.append(name)
    if checked is None:
        raise RuntimeError("Nenhuma camada estável foi escrita no adaptador.")
    return adapted, {"modules": len(written), "max_residual_component": checked}


def run_decensor(tokenizer, directory, events, config):
    from dashboard.training import limit_training_memory
    from dashboard.worker import load_model

    harmful_prompts, harmless_prompts = _contrast_sets()
    memory_budget = limit_training_memory(config, events, operation="extração da direção de recusa")
    model = load_model(events)
    model.eval()
    model.config.use_cache = False
    layer_count, hidden = model.config.num_hidden_layers, model.config.hidden_size
    states = {
        "harmful": torch.empty(len(harmful_prompts), layer_count, hidden),
        "harmless": torch.empty(len(harmless_prompts), layer_count, hidden),
    }
    order = [("harmful", text) for text in harmful_prompts] + [("harmless", text) for text in harmless_prompts]
    import time

    seen = {"harmful": 0, "harmless": 0}
    started = time.time()
    events.emit(phase="Medindo ativações", phase_step=0, phase_total=len(order), phase_unit="prompts")
    with torch.inference_mode():
        for step, (group, text) in enumerate(order, start=1):
            events.check()
            ids = prompt_token_ids(tokenizer, text)
            batch = torch.tensor([ids], device="cuda:0")
            output = model(input_ids=batch, output_hidden_states=True, use_cache=False)
            hidden_states = output.hidden_states
            if len(hidden_states) != layer_count + 1:
                raise RuntimeError("O modelo não devolveu o residual de cada camada.")
            index = seen[group]
            for layer in range(layer_count):
                states[group][index, layer].copy_(hidden_states[layer + 1][0, -1].float().cpu())
            seen[group] += 1
            del output, hidden_states, batch
            elapsed = time.time() - started
            events.emit(
                phase="Medindo ativações", phase_step=step, phase_total=len(order), phase_unit="prompts",
                eta_seconds=elapsed / step * (len(order) - step),
            )
    directions, layers = _layer_decisions(states["harmful"], states["harmless"])
    report = {
        "method": "refusal-direction-lora",
        "source": "Arditi et al., Refusal in Language Models Is Mediated by a Single Direction",
        "strength": STRENGTH, "min_cosine": MIN_COSINE, "min_norm": MIN_NORM,
        "lora_r": LORA_R, "lora_alpha": LORA_ALPHA,
        "target_modules": ["o_proj", "down_proj"],
        "prompts": {"refusal": len(harmful_prompts), "harmless": len(harmless_prompts)},
        "kept_layers": sorted(directions), "layers": layers,
        "memory": memory_budget,
    }
    if len(directions) < MIN_KEPT_LAYERS:
        write_json(directory / "decensor_report.json", report)
        raise RuntimeError(
            f"Só {len(directions)} camadas tiveram direção estável; o mínimo é {MIN_KEPT_LAYERS}. "
            "O adaptador não foi salvo. O relatório lista o cosseno de cada camada."
        )
    events.emit(phase="Salvando adaptador de recusa", kept_layers=len(directions))
    adapted, check = _write_refusal_lora(model, directions)
    adapted.save_pretrained(directory / "adapter")
    tokenizer.save_pretrained(directory / "adapter")
    report.update(check)
    write_json(directory / "decensor_report.json", report)
    del adapted, model, states
    gc.collect()
    torch.cuda.empty_cache()
    events.emit(phase="Recusa concluída", phase_step=len(directions), phase_total=layer_count, phase_unit="camadas")
