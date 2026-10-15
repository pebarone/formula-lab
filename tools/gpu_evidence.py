"""Capture evidence that the configured PyTorch build uses the real GPU."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _nvidia_smi() -> dict:
    candidates = [
        shutil.which("nvidia-smi"),
        r"C:\Windows\System32\nvidia-smi.exe",
        r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
    ]
    executable = next((item for item in candidates if item and Path(item).is_file()), None)
    if executable is None:
        return {"available": False, "error": "nvidia-smi was not found on PATH or in standard Windows locations."}
    try:
        result = subprocess.run(
            [executable, "--query-gpu=name,driver_version,memory.total,memory.free", "--format=csv,noheader,nounits"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "executable": executable, "error": str(exc)}
    return {
        "available": result.returncode == 0,
        "executable": executable,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def capture_gpu_evidence() -> dict:
    """Require CUDA and bfloat16, then run and synchronize a CUDA matmul."""
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable; refusing to run a CPU fallback.")
    torch.cuda.set_device(0)
    if not torch.cuda.is_bf16_supported():
        raise RuntimeError("The visible CUDA device does not support bfloat16 required by the training run.")

    props = torch.cuda.get_device_properties(0)
    free_before, total_before = torch.cuda.mem_get_info(0)
    generator = torch.Generator(device="cuda:0").manual_seed(42)
    left = torch.rand((2048, 2048), generator=generator, device="cuda:0", dtype=torch.bfloat16)
    right = torch.rand((2048, 2048), generator=generator, device="cuda:0", dtype=torch.bfloat16)

    # Warm up the kernel, then synchronize every timed operation so the report
    # measures completed GPU work rather than asynchronous dispatch time.
    for _ in range(2):
        result = left @ right
    torch.cuda.synchronize(0)
    durations = []
    for _ in range(5):
        started = time.perf_counter()
        result = left @ right
        torch.cuda.synchronize(0)
        durations.append(time.perf_counter() - started)

    if result.device.type != "cuda" or not bool(torch.isfinite(result).all().item()):
        raise RuntimeError("CUDA matrix multiplication did not produce a finite result on the CUDA device.")
    checksum = float(result[0, 0].item())
    free_after, total_after = torch.cuda.mem_get_info(0)
    return {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "torch": torch.__version__,
        "torch_cuda_build": torch.version.cuda,
        "cuda_available": torch.cuda.is_available(),
        "cuda_device_index": 0,
        "gpu_name": torch.cuda.get_device_name(0),
        "compute_capability": f"{props.major}.{props.minor}",
        "bf16_supported": torch.cuda.is_bf16_supported(),
        "total_vram_bytes_before": total_before,
        "free_vram_bytes_before": free_before,
        "total_vram_bytes_after": total_after,
        "free_vram_bytes_after": free_after,
        "cuda_operation": {
            "operation": "2048x2048 bfloat16 matrix multiplication",
            "result_device": str(result.device),
            "result_shape": list(result.shape),
            "finite": True,
            "checksum_00": checksum,
            "synchronized_runs": len(durations),
            "duration_seconds": durations,
            "mean_duration_seconds": sum(durations) / len(durations),
        },
        "nvidia_smi": _nvidia_smi(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="JSON destination (defaults to runs/gpu-evidence-<UTC>.json).")
    args = parser.parse_args(argv)
    try:
        evidence = capture_gpu_evidence()
        destination = args.output or ROOT / "runs" / f"gpu-evidence-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        print(f"GPU evidence failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(f"Evidence saved to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
