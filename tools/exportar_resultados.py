"""Exporta métricas dos experimentos sem conversas, planilhas ou textos do dataset."""

import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KINDS = {"train", "evaluate", "tool_evaluate", "decensor"}
ARTIFACTS = (
    "request.json", "status.json", "environment.json", "selection.json", "model_info.json",
    "training_summary.json", "report.json", "tool_report.json", "tool_validation_report.json",
    "decensor_report.json", "comparison_analysis.json", "measurement_identity.json",
)
SPECIALS = {
    "baseline-refresh-20261001T020305Z": ("report.json", "verification.json"),
    "evaluation-memory-audit": ("report.json",),
    "training-pilot-20261001T003431Z-19016": (
        "pilot_report.json", "validation_base.json", "validation_lr_5e_5.json", "validation_lr_1e_4.json",
    ),
}
PRIVATE_KEYS = {
    "instruction", "context_text", "messages", "text", "prompt", "tool_trace", "arguments",
    "value", "workbook_id", "input_workbook_id", "source_path", "pid", "executable",
}
SECRET = re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|hf_[A-Za-z0-9]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")


def clean(value, root=ROOT):
    """Conserva números/fórmulas e retira textos de origem e caminhos do usuário."""
    if isinstance(value, dict):
        return {key: clean(item, root) for key, item in value.items()
                if key not in PRIVATE_KEYS and not (key == "prompts" and not isinstance(item, dict))}
    if isinstance(value, list):
        return [clean(item, root) for item in value]
    if isinstance(value, str):
        for prefix in (str(root), root.as_posix()):
            value = value.replace(prefix, ".")
        value = re.sub(r"(?i)\b[A-Z]:[\\/]Users[\\/][^\\/\s\"]+", "[pasta do usuário]", value)
        if SECRET.search(value):
            raise ValueError("Possível credencial em um resultado; exportação bloqueada.")
    return value


def validate_report(report):
    """Confere a contagem histórica sem reescrever a métrica usada na medição."""
    items = report.get("items")
    if items is None or "total" not in report:
        return
    if len(items) != report["total"] or len({item["id"] for item in items}) != len(items):
        raise ValueError("Quantidade ou IDs do relatório não conferem.")
    if "correct" in report and sum(bool(item["match"]) for item in items) != report["correct"]:
        raise ValueError("A soma dos acertos não confere com o relatório.")
    for condition in ("without_tools", "with_tools"):
        if condition in report:
            actual = sum(bool(item.get(condition, {}).get("match")) for item in items)
            if actual != report[condition]["correct"]:
                raise ValueError(f"A soma de acertos de {condition} não confere.")


def export_results(source_root=None, destination=None):
    source_root = Path(source_root) if source_root is not None else ROOT / "runs"
    destination = Path(destination) if destination is not None else ROOT / "resultados"
    runs = []
    for folder in sorted(source_root.iterdir()):
        request_path = folder / "request.json"
        if not request_path.is_file():
            continue
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if request.get("kind") not in KINDS:
            continue
        runs.append((folder, ARTIFACTS, request["kind"]))
    for name, files in SPECIALS.items():
        folder = source_root / name
        if folder.is_dir():
            runs.append((folder, files, "piloto" if name.startswith("training-pilot") else "auditoria"))
    manifest = {"formato": 1, "descricao": "Métricas históricas preservadas; textos do dataset, conversas, planilhas e caminhos pessoais omitidos.", "execucoes": []}
    for folder, files, kind in runs:
        entry = {"id": folder.name, "tipo": kind, "arquivos": []}
        for name in files:
            source = folder / name
            if not source.is_file():
                continue
            original = source.read_bytes()
            data = json.loads(original)
            validate_report(data)
            target = destination / "execucoes" / folder.name / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(clean(data), ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
            entry["arquivos"].append({"arquivo": target.relative_to(destination).as_posix(),
                "fonte": f"runs/{folder.name}/{name}", "sha256_fonte": hashlib.sha256(original).hexdigest(),
                "sha256_publicado": hashlib.sha256(target.read_bytes()).hexdigest()})
        events = folder / "events.jsonl"
        if kind == "train" and events.is_file():
            rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines() if line]
            metrics = [clean(row) for row in rows if any(key in row for key in ("loss", "eval_loss", "train_loss"))]
            target = destination / "execucoes" / folder.name / "historico_metricas.jsonl"
            target.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in metrics), encoding="utf-8", newline="\n")
            entry["arquivos"].append({"arquivo": target.relative_to(destination).as_posix(), "fonte": f"runs/{folder.name}/events.jsonl",
                "sha256_fonte": hashlib.sha256(events.read_bytes()).hexdigest(), "sha256_publicado": hashlib.sha256(target.read_bytes()).hexdigest()})
        manifest["execucoes"].append(entry)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "manifesto.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"Resultados exportados: {len(runs)} execuções, {sum(len(run['arquivos']) for run in manifest['execucoes'])} arquivos.")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origem", type=Path, default=ROOT / "runs", help="Pasta com as execuções locais.")
    parser.add_argument("--destino", type=Path, default=ROOT / "resultados", help="Pasta que receberá a cópia para publicação.")
    args = parser.parse_args(argv)
    export_results(args.origem, args.destino)


if __name__ == "__main__":
    main()
