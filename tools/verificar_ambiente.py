"""Verifica dependências, CUDA e arquivos locais sem carregar os pesos na GPU."""

import argparse
import importlib
import importlib.metadata
import json
from pathlib import Path

from benchmark.run_baseline import MODEL_NAME, MODEL_REVISION
from tools.preparar_ambiente import MODEL_FILES

ROOT = Path(__file__).resolve().parents[1]


def check_model():
    from huggingface_hub import snapshot_download
    snapshot = Path(snapshot_download(repo_id=MODEL_NAME, revision=MODEL_REVISION,
                                      allow_patterns=MODEL_FILES, local_files_only=True))
    index = snapshot / "model.safetensors.index.json"
    weights = set(json.loads(index.read_text(encoding="utf-8"))["weight_map"].values()) if index.is_file() else {"model.safetensors"}
    required = {"config.json", "tokenizer_config.json", "tokenizer.json", *weights}
    missing = sorted(name for name in required if not (snapshot / name).is_file())
    if missing:
        raise ValueError("Faltam arquivos do modelo: " + ", ".join(missing))
    return snapshot


def verify(*, require_gpu=False, require_model=True, require_data=True):
    for module, distribution in [("torch", "torch"), ("transformers", "transformers"), ("accelerate", "accelerate"),
                                 ("bitsandbytes", "bitsandbytes"), ("datasets", "datasets"), ("psutil", "psutil"),
                                 ("peft", "peft"), ("openpyxl", "openpyxl"), ("win32com.client", "pywin32")]:
        importlib.import_module(module)
        print(f"OK: {distribution} {importlib.metadata.version(distribution)}", flush=True)
    import torch
    gpu_ok = torch.cuda.is_available() and torch.cuda.is_bf16_supported()
    if gpu_ok:
        # Confere também o driver/runtime; não aloca nem carrega os pesos do modelo.
        value = torch.ones(1, device="cuda")
        if value.item() != 1:
            raise RuntimeError("O teste de cálculo CUDA falhou.")
        del value
        torch.cuda.empty_cache()
        print(f"OK: CUDA e bfloat16 em {torch.cuda.get_device_name(0)}.", flush=True)
    elif require_gpu:
        raise RuntimeError("CUDA com bfloat16 indisponível. Instale o driver NVIDIA compatível ou use -SemVerificarGPU para apenas preparar o painel.")
    else:
        print("AVISO: treino e chat exigem uma GPU NVIDIA com CUDA e bfloat16.", flush=True)
    if require_model:
        check_model()
        print("OK: tokenizer e todos os arquivos de pesos da revisão fixada.", flush=True)
    if require_data:
        for split in ("train", "validation", "test"):
            path = ROOT / f"dataset/canonical/{split}.jsonl"
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"Dataset ausente: {path}. Execute tools.preparar_ambiente.")
        print("OK: treino, validação e teste presentes.", flush=True)
    from dashboard.excel_engine import engine_status
    status = engine_status()
    print(("OK: " if status["available"] else "AVISO: ") + status["message"], flush=True)
    print("Verificação concluída. Abra o painel com abrir_painel.bat.", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exigir-gpu", action="store_true", help="Falha se CUDA/bfloat16 não estiver disponível.")
    parser.add_argument("--sem-modelo", action="store_true", help="Não exige os pesos já baixados.")
    parser.add_argument("--sem-dados", action="store_true", help="Não exige o dataset já preparado.")
    args = parser.parse_args(argv)
    verify(require_gpu=args.exigir_gpu, require_model=not args.sem_modelo, require_data=not args.sem_dados)


if __name__ == "__main__":
    main()
