"""Isolated workbook storage and bounded source import helpers."""

import contextlib
import csv
import io
import os
import re
import time
import uuid
from pathlib import Path

from dashboard.attachments import _check_xlsx_archive, _csv_dialect, _decode_csv, load_attachments
from dashboard.config import RUNS, read_json, write_json

WORKBOOK_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
MAX_IMPORT_ROWS = 500
MAX_IMPORT_COLUMNS = 40
MAX_CELL_TEXT = 32_767
LOCK_TIMEOUT_SECONDS = 35


def workbook_root(runs_root=RUNS):
    return Path(runs_root).resolve() / "workbooks"


def workbook_directory(workbook_id, runs_root=RUNS):
    if not isinstance(workbook_id, str) or not WORKBOOK_ID.fullmatch(workbook_id):
        raise ValueError("Identificador da pasta de trabalho inválido.")
    root = workbook_root(runs_root)
    directory = root / workbook_id
    if not directory.is_dir() or directory.resolve().parent != root:
        raise ValueError("Pasta de trabalho não encontrada.")
    return directory


def workbook_file(workbook_id, runs_root=RUNS):
    return workbook_directory(workbook_id, runs_root) / "workbook.xlsx"


def manifest_path(workbook_id, runs_root=RUNS):
    return workbook_directory(workbook_id, runs_root) / "manifest.json"


def read_manifest(workbook_id, runs_root=RUNS):
    directory = workbook_directory(workbook_id, runs_root)
    try:
        manifest = read_json(directory / "manifest.json")
    except (OSError, ValueError):
        manifest = None
    if (not isinstance(manifest, dict) or manifest.get("id") != workbook_id
            or isinstance(manifest.get("revision"), bool) or not isinstance(manifest.get("revision"), int) or manifest["revision"] < 0
            or not isinstance(manifest.get("name"), str)):
        raise ValueError("Metadados da pasta de trabalho inválidos.")
    if not (directory / "workbook.xlsx").is_file():
        raise ValueError("Arquivo da pasta de trabalho não encontrado.")
    return manifest


@contextlib.contextmanager
def workbook_lock(workbook_id, runs_root=RUNS):
    """Serialize server and model edits; the OS releases the lock after a crash."""
    directory = workbook_directory(workbook_id, runs_root)
    handle = (directory / ".workbook.lock").open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("A pasta de trabalho está ocupada; tente novamente.") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _csv_rows(content, suffix):
    text = _decode_csv(content)
    if "\x00" in text:
        raise ValueError("O arquivo não parece ser um CSV ou TSV de texto.")
    dialect = _csv_dialect(text, suffix)
    rows = []
    try:
        reader = csv.reader(io.StringIO(text, newline=""), dialect, strict=True)
        for row_index, row in enumerate(reader, 1):
            if row_index > MAX_IMPORT_ROWS:
                raise ValueError(f"A importação aceita até {MAX_IMPORT_ROWS} linhas; divida o arquivo para preservar todos os dados.")
            if len(row) > MAX_IMPORT_COLUMNS:
                raise ValueError(f"A importação aceita até {MAX_IMPORT_COLUMNS} colunas; divida o arquivo para preservar todos os dados.")
            if any(len(value) > MAX_CELL_TEXT for value in row):
                raise ValueError(f"Cada célula importada pode ter até {MAX_CELL_TEXT} caracteres.")
            rows.append(row)
    except csv.Error:
        raise ValueError("O CSV/TSV contém uma linha inválida ou um campo muito grande.") from None
    return rows or [[]]


def source_for_attachment(attachment_id, runs_root=RUNS):
    """Resolve a immutable attachment from its server-side UUID and validate it."""
    record = load_attachments([attachment_id], runs_root=runs_root)[0]
    source_dir = Path(runs_root).resolve() / "attachments" / attachment_id
    suffix = "." + record["kind"]
    source = source_dir / ("original" + suffix)
    if not source.is_file() or source.resolve().parent != source_dir.resolve():
        raise ValueError("Arquivo original do anexo não encontrado.")
    if record["kind"] == "xlsx":
        content = source.read_bytes()
        _check_xlsx_archive(content)
        return record, source, None
    if record["kind"] in {"csv", "tsv"}:
        return record, source, _csv_rows(source.read_bytes(), suffix)
    raise ValueError("Formato de planilha não aceito.")


def new_workbook_directory(runs_root=RUNS):
    root = workbook_root(runs_root)
    root.mkdir(parents=True, exist_ok=True)
    workbook_id = str(uuid.uuid4())
    directory = root / workbook_id
    directory.mkdir(exist_ok=False)
    return workbook_id, directory


def write_manifest(directory, manifest):
    write_json(directory / "manifest.json", manifest)


# Public facade used by the HTTP/API and model-tool layers.
def engine_status():
    from dashboard.excel_engine import engine_status as check
    return check()


def create_workbook(attachment_id=None, *, runs_root=RUNS):
    from dashboard.excel_engine import create_workbook as create
    return create(attachment_id, runs_root=runs_root)


def get_workbook(workbook_id, *, runs_root=RUNS):
    from dashboard.excel_engine import get_workbook as get
    return get(workbook_id, runs_root=runs_root)


def mutate_workbook(workbook_id, changes, expected_revision=None, *, runs_root=RUNS):
    from dashboard.excel_engine import mutate_workbook as mutate
    return mutate(workbook_id, changes, expected_revision, runs_root=runs_root)


def test_formula(workbook_id, sheet, cell, formula, *, runs_root=RUNS):
    from dashboard.excel_engine import test_formula as test
    return test(workbook_id, sheet, cell, formula, runs_root=runs_root)


def read_range(workbook_id, sheet, range_address, *, runs_root=RUNS):
    from dashboard.excel_engine import read_range as read
    return read(workbook_id, sheet, range_address, runs_root=runs_root)


def workbook_context(workbook_id, *, runs_root=RUNS, compact=False):
    from dashboard.excel_engine import workbook_context as get_context
    return get_context(workbook_id, runs_root=runs_root, compact=compact)


def workbook_download_path(workbook_id, *, runs_root=RUNS):
    from dashboard.excel_engine import workbook_download_path as get_path
    return get_path(workbook_id, runs_root=runs_root)
