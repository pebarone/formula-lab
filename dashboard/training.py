"""Bound GPU allocations and project only positions that contribute to loss."""


def response_only_inputs(inputs):
    import torch

    labels = inputs["labels"]
    shifted = torch.nn.functional.pad(labels, (0, 1), value=-100)[..., 1:]
    # Prediction at i supervises label i+1. Keep the union for padded microbatches.
    positions = shifted.ne(-100).any(dim=0).nonzero(as_tuple=True)[0]
    if not positions.numel():
        raise ValueError("Microbatch sem tokens supervisionados.")
    return {**inputs, "logits_to_keep": positions, "shift_labels": shifted[:, positions].contiguous()}


def limit_training_memory(config, events, operation="treinamento"):
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA indisponível. Use o .venv com PyTorch CUDA; não há fallback para CPU.")
    torch.cuda.set_device(0)
    free, total = torch.cuda.mem_get_info(0)
    budget = min(total * config["gpu_memory_fraction"], free - 512 * 1024**2)
    if budget <= 0:
        raise RuntimeError(f"VRAM livre insuficiente para reservar memória de {operation}.")
    # This caps the PyTorch allocator, not allocations made directly by CUDA libraries.
    torch.cuda.set_per_process_memory_fraction(budget / total, device=0)
    torch.cuda.reset_peak_memory_stats(0)
    metrics = {"gpu_budget_gb": budget / 1024**3, "gpu_total_gb": total / 1024**3}
    events.emit(phase=f"Limitando memória de {operation}", **metrics)
    return metrics


def training_memory():
    import torch

    return {
        "vram_gb": torch.cuda.memory_allocated(0) / 1024**3,
        "vram_reserved_gb": torch.cuda.memory_reserved(0) / 1024**3,
        "vram_peak_gb": torch.cuda.max_memory_allocated(0) / 1024**3,
        "vram_peak_reserved_gb": torch.cuda.max_memory_reserved(0) / 1024**3,
    }
