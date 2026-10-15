"""Convert the published NL2Formula JSON splits into the canonical JSONL schema.

Source files are the official train, valid, and test JSON arrays from
https://github.com/timetub/NL2Formula at commit
bd45ed38dc6bf244366ae8a262055c38217c1d54.
The gold formula is the field Formula2, the same field yielded by
model_train/datasets/nl2formula/nl2formula.py. A leading '=' is added when
the source omits it. The official test split is larger than 500 rows, so
records are ordered by id and only the first 500 are written. The rest of
the official test split is not copied into train or validation.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "raw" / "nl2formula"
CANONICAL_DIR = ROOT / "canonical"
FROZEN_PATH = ROOT / "FROZEN_TEST_IDS.txt"
SOURCE_NAME = "nl2formula"
TEST_LIMIT = 500
SPLIT_FILES = {
    "train": "train.json",
    "validation": "valid.json",
    "test": "test.json",
}


def column_label(index: int) -> str:
    label = ""
    number = index
    while True:
        label = chr(ord("A") + (number % 26)) + label
        number = number // 26 - 1
        if number < 0:
            return label


def cell_text(value: object) -> str:
    text = "" if value is None else str(value)
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())


def context_text(table_name: str, table: list) -> str:
    if not table or str(table[0][0]) != "0":
        raise ValueError(f"unexpected table layout for {table_name}")
    letters = [str(item) for item in table[0][1:]]
    expected = [column_label(index) for index in range(len(letters))]
    if letters != expected:
        raise ValueError(f"unexpected column letters for {table_name}: {letters}")
    if len(table) < 2:
        raise ValueError(f"table {table_name} has no header row")
    header_row = table[1]
    if str(header_row[0]) != "1":
        raise ValueError(f"unexpected header index for {table_name}")
    headers = [cell_text(item) for item in header_row[1:]]
    if len(headers) < len(letters):
        headers.extend([""] * (len(letters) - len(headers)))
    lines = [f"Sheet: {table_name}"]
    for letter, header in zip(letters, headers):
        lines.append(f"{letter}: {header}")
    lines.append("Sample:")
    for row in table[2:]:
        row_index = cell_text(row[0])
        values = [cell_text(item) for item in row[1:]]
        if len(values) < len(letters):
            values.extend([""] * (len(letters) - len(values)))
        lines.append(row_index + " | " + " | ".join(values[: len(letters)]))
    return "\n".join(lines)


def formula_text(raw_formula: str) -> str:
    text = raw_formula.strip()
    if not text.startswith("="):
        text = "=" + text
    if not text.startswith("="):
        raise ValueError(f"formula does not start with '=': {raw_formula!r}")
    return text


def iter_records(split: str, payload: list) -> list[dict]:
    records = []
    ordinal = 0
    for table_index, sample in enumerate(payload):
        table_name = str(sample["TableName"])
        context = context_text(table_name, sample["Table"])
        formulas = sample["t5Formulas"]
        for formula_index, formula in enumerate(formulas):
            question = str(formula["Question"]).strip()
            if not question:
                raise ValueError(f"empty question in {split} table {table_index}")
            record = {
                "id": f"nl2f-{split}-{ordinal:06d}",
                "instruction": question,
                "context_text": context,
                "expected": formula_text(str(formula["Formula2"])),
                "source": SOURCE_NAME,
                "source_id": f"{table_name}#{formula_index}",
                "instruction_origin": "source",
                "level": str(formula.get("Level", "")),
            }
            records.append(record)
            ordinal += 1
    return records


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def main() -> None:
    written = {}
    for split, filename in SPLIT_FILES.items():
        payload = json.loads((RAW_DIR / filename).read_text(encoding="utf-8"))
        records = iter_records(split, payload)
        records.sort(key=lambda record: record["id"])
        if split == "test":
            if len(records) <= TEST_LIMIT:
                selected = records
            else:
                selected = records[:TEST_LIMIT]
            frozen_ids = [record["id"] for record in selected]
            FROZEN_PATH.write_text("\n".join(frozen_ids) + "\n", encoding="utf-8", newline="\n")
            records = selected
        write_jsonl(CANONICAL_DIR / f"{split}.jsonl", records)
        written[split] = len(records)
    print(json.dumps(written, sort_keys=True))


if __name__ == "__main__":
    main()
