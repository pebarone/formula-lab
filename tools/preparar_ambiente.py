"""Baixa os dados e os pesos fixados usados pelo Formula Lab."""

import argparse
import hashlib
import json
import tempfile
import urllib.request
from pathlib import Path

from benchmark.run_baseline import MODEL_NAME, MODEL_REVISION
from dataset.convert_to_canonical import SPLIT_FILES, TEST_LIMIT, iter_records, write_jsonl

ROOT = Path(__file__).resolve().parents[1]
MODEL_FILES = [
    "config.json", "generation_config.json", "tokenizer*", "vocab.json", "merges.txt",
    "special_tokens_map.json", "added_tokens.json", "chat_template*", "*.safetensors",
    "*.safetensors.index.json",
]
DATA_REVISION = "bd45ed38dc6bf244366ae8a262055c38217c1d54"
DATA_FILES = {
    "train.json": (68_108_457, "58f9b807cb5965d1ea5bf5d4f1d0568437fc1d295cdf92ea90567d02a1ae93e5"),
    "valid.json": (6_824_305, "ee51d1dd74e75d651223dc95c41199aaaae5228841e96df9e8b72cb23b20ee1a"),
    "test.json": (12_980_400, "63c20a139ffac17dcec8cd26515c337848f303f2ee2d655a5a2ca1ba72d4d983"),
}


def valid_download(path, size, digest):
    if not path.is_file() or path.stat().st_size != size:
        return False
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest() == digest


def download_verified(url, destination, size, digest):
    """Só substitui o destino depois de conferir tamanho e SHA-256."""
    destination = Path(destination)
    if valid_download(destination, size, digest):
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as output:
            temporary = Path(output.name)
            with urllib.request.urlopen(url, timeout=120) as response:
                received = 0
                while block := response.read(1024 * 1024):
                    received += len(block)
                    if received > size:
                        raise ValueError(f"Download maior que o esperado: {destination.name}.")
                    output.write(block)
        if not valid_download(temporary, size, digest):
            raise ValueError(f"O tamanho ou SHA-256 de {destination.name} não confere. Tente novamente.")
        temporary.replace(destination)
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def prepare_data(root=ROOT):
    root = Path(root)
    raw = root / "dataset/raw/nl2formula"
    for name, (size, digest) in DATA_FILES.items():
        print(f"Conferindo dados: {name}", flush=True)
        download_verified(f"https://raw.githubusercontent.com/timetub/NL2Formula/{DATA_REVISION}/dataset/{name}", raw / name, size, digest)
    counts = {}
    for split, name in SPLIT_FILES.items():
        rows = iter_records(split, json.loads((raw / name).read_text(encoding="utf-8")))
        rows.sort(key=lambda row: row["id"])
        if split == "test":
            rows = rows[:TEST_LIMIT]
            (root / "dataset/FROZEN_TEST_IDS.txt").write_text("\n".join(row["id"] for row in rows) + "\n", encoding="utf-8", newline="\n")
        write_jsonl(root / f"dataset/canonical/{split}.jsonl", rows)
        counts[split] = len(rows)
    print("Dados preparados: " + json.dumps(counts, ensure_ascii=False), flush=True)
    return counts


def prepare_model():
    from huggingface_hub import snapshot_download
    print(f"Baixando ou conferindo {MODEL_NAME}, revisão {MODEL_REVISION}.", flush=True)
    return snapshot_download(repo_id=MODEL_NAME, revision=MODEL_REVISION, allow_patterns=MODEL_FILES)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sem-modelo", action="store_true", help="Adia o download dos pesos.")
    parser.add_argument("--sem-dados", action="store_true", help="Adia a preparação do dataset.")
    args = parser.parse_args(argv)
    if not args.sem_dados:
        prepare_data()
    if not args.sem_modelo:
        prepare_model()


if __name__ == "__main__":
    main()
