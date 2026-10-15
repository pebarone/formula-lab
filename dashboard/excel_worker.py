"""One-operation, one-process Microsoft Excel COM worker."""

import json
import math
import re
import shutil
import sys
import uuid
from pathlib import Path

from openpyxl.utils.cell import column_index_from_string, range_boundaries
from openpyxl.formula import Tokenizer

from dashboard.config import read_json, write_json
from dashboard import workbooks
from dashboard.excel_engine import (
    ENGINE, MAX_PREVIEW_COLUMNS, MAX_PREVIEW_ROWS, MAX_RANGE_CELLS,
    _formula_text, _LOCAL_FUNCTIONS, validate_xlsx,
)

_ALLOWED_FILES = {"workbook.xlsx", ".pending.xlsx", ".scratch.xlsx"}
_CELL = re.compile(r"\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})\Z")
_ERROR_CODES = {
    2000: "#NULL!", 2007: "#DIV/0!", 2015: "#VALUE!", 2023: "#REF!",
    2029: "#NAME?", 2036: "#NUM!", 2042: "#N/A", 2043: "#GETTING_DATA",
    2045: "#SPILL!", 2046: "#CALC!", 2047: "#FIELD!", 2048: "#DATA!",
}
_ERROR_TEXT = set(_ERROR_CODES.values())
EXCEL_PROCESS_MEMORY_LIMIT = 1 << 30
_CSV_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")


def _safe_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        result = value
    elif isinstance(value, float):
        result = value if math.isfinite(value) else None
    elif hasattr(value, "isoformat"):
        result = value.isoformat()
    else:
        result = str(value)
    if isinstance(result, str) and len(result) > 300:
        return result[:300] + "…", True
    return result, False


def _as_matrix(value, rows, columns):
    if rows == 1 and columns == 1:
        return [[value]]
    if not isinstance(value, (tuple, list)):
        return [[value for _ in range(columns)] for _ in range(rows)]
    result = []
    for row_index in range(rows):
        row = value[row_index] if row_index < len(value) else ()
        if not isinstance(row, (tuple, list)):
            row = (row,)
        result.append([row[col] if col < len(row) else None for col in range(columns)])
    return result


def _column_name(number):
    name = ""
    while number:
        number, rem = divmod(number - 1, 26)
        name = chr(65 + rem) + name
    return name or "A"


def _formula_value(cell):
    if not cell.HasFormula:
        return None
    try:
        value = cell.Formula2
    except Exception:
        value = cell.Formula
    return value if isinstance(value, str) and value.startswith("=") else None


def _local_formula(cell):
    if not cell.HasFormula:
        return None
    try:
        value = cell.Formula2Local
    except Exception:
        try:
            value = cell.FormulaLocal
        except Exception:
            value = None
    return value if isinstance(value, str) and value.startswith("=") else None


def _error_for(cell, value, formula):
    if not formula:
        return None
    if isinstance(value, int):
        # Excel COM exposes CVErr as HRESULT_FROM_XLERROR (0x800A0000 | code).
        code = value & 0xFFFF
        if (value & 0xFFFF0000) == 0x800A0000:
            if code in _ERROR_CODES:
                return _ERROR_CODES[code]
            if cell is not None:
                return _rendered_error(cell, include_unknown=True)
            return None
        if value in _ERROR_CODES and cell is not None:
            return _rendered_error(cell)
        return None
    if isinstance(value, str) and value in _ERROR_TEXT:
        return value
    return None


def _rendered_error(cell, include_unknown=False):
    try:
        rendered = str(cell.Text)
        if rendered in _ERROR_TEXT or (include_unknown and rendered.startswith("#")):
            return rendered
    except Exception:
        pass
    return None


def _find_sheet(book, name):
    if not isinstance(name, str) or not name or len(name) > 31:
        raise ValueError("Nome de aba inválido.")
    for sheet in book.Worksheets:
        if sheet.Name == name:
            return sheet
    raise ValueError(f"Aba {name!r} não encontrada.")


def _safe_open_options(app):
    app.Visible = False
    app.DisplayAlerts = False
    app.EnableEvents = False
    app.ScreenUpdating = False
    app.AskToUpdateLinks = False
    app.AlertBeforeOverwriting = False
    app.Interactive = False
    app.AutomationSecurity = 3  # msoAutomationSecurityForceDisable


def _assign_excel_job(pid):
    """Cap and own only the Excel process created by DispatchEx."""
    import win32api
    import win32con
    import win32job

    job = win32job.CreateJobObject(None, "")
    try:
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        basic = limits["BasicLimitInformation"]
        basic["LimitFlags"] |= (
            win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
        )
        limits["ProcessMemoryLimit"] = EXCEL_PROCESS_MEMORY_LIMIT
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        process_handle = win32api.OpenProcess(
            win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid,
        )
        try:
            win32job.AssignProcessToJobObject(job, process_handle)
        finally:
            win32api.CloseHandle(process_handle)
        return job
    except Exception:
        win32api.CloseHandle(job)
        raise


def _launch_excel(job_id, marker_path):
    import psutil
    import pythoncom
    import win32api
    import win32process
    import win32com.client

    pythoncom.CoInitialize()
    app = job = None
    pid = None
    created = None
    try:
        app = win32com.client.DispatchEx("Excel.Application")
        _safe_open_options(app)
        hwnd = int(app.Hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        created = psutil.Process(pid).create_time()
        write_json(marker_path, {"job_id": job_id, "pid": pid, "created": created})
        job = _assign_excel_job(pid)
        return pythoncom, app, job
    except Exception:
        if app is not None:
            try:
                app.Quit()
            except Exception:
                pass
        if job is not None:
            try:
                win32api.CloseHandle(job)
            except Exception:
                pass
        if pid is not None and created is not None:
            try:
                process = psutil.Process(pid)
                if process.name().casefold() == "excel.exe" and abs(process.create_time() - created) < 0.01:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except psutil.TimeoutExpired:
                        process.kill()
            except Exception:
                pass
        try:
            marker_path.unlink(missing_ok=True)
        finally:
            pythoncom.CoUninitialize()
        raise


def _open_book(app, path):
    validate_xlsx(path)
    book = app.Workbooks.Open(
        str(path), UpdateLinks=0, ReadOnly=False,
        IgnoreReadOnlyRecommended=True, AddToMru=False, Local=False,
    )
    try:
        if int(book.Connections.Count):
            raise ValueError("Conexões externas não são permitidas.")
    except AttributeError:
        pass
    app.CalculateFullRebuild()
    return book


def _snapshot(book):
    warnings, sheets = [], []
    total_sheets = int(book.Worksheets.Count)
    show_sheets = min(total_sheets, 50)
    if total_sheets > show_sheets:
        warnings.append(f"A visualização mostra as primeiras {show_sheets} abas de {total_sheets}.")
    for index in range(1, show_sheets + 1):
        sheet = book.Worksheets(index)
        used = sheet.UsedRange
        row_end = int(used.Row) + int(used.Rows.Count) - 1
        column_end = int(used.Column) + int(used.Columns.Count) - 1
        rows, columns = max(1, row_end), max(1, column_end)
        row_count = min(rows, MAX_PREVIEW_ROWS)
        column_count = min(columns, MAX_PREVIEW_COLUMNS)
        if rows > MAX_PREVIEW_ROWS or columns > MAX_PREVIEW_COLUMNS:
            warnings.append(f"Aba {sheet.Name}: visualização limitada a A1:{_column_name(MAX_PREVIEW_COLUMNS)}{MAX_PREVIEW_ROWS}; a planilha completa permanece no arquivo.")
        visible = sheet.Range(
            sheet.Cells(1, 1),
            sheet.Cells(row_count, column_count),
        )
        values = _as_matrix(visible.Value2, row_count, column_count)
        formulas = _as_matrix(visible.Formula2, row_count, column_count)
        try:
            local_formulas = _as_matrix(visible.Formula2Local, row_count, column_count)
        except Exception:
            local_formulas = formulas
        cells = []
        for r in range(row_count):
            for c in range(column_count):
                address = f"{_column_name(c + 1)}{r + 1}"
                formula = formulas[r][c] if isinstance(formulas[r][c], str) and formulas[r][c].startswith("=") else None
                formula_local = local_formulas[r][c] if isinstance(local_formulas[r][c], str) and local_formulas[r][c].startswith("=") else None
                if formula and isinstance(values[r][c], str) and values[r][c].startswith("=") and not sheet.Range(address).HasFormula:
                    formula = formula_local = None
                value, shortened = _safe_value(values[r][c])
                if shortened and not any("caracteres" in warning for warning in warnings):
                    warnings.append("Alguns textos de célula foram abreviados na visualização; o arquivo permanece completo.")
                cell_value = values[r][c]
                error = _error_for(None, cell_value, formula)
                if error is None and formula and isinstance(cell_value, int) and (
                    cell_value in _ERROR_CODES or (cell_value & 0xFFFF0000) == 0x800A0000
                ):
                    error = _error_for(sheet.Range(address), cell_value, formula)
                cells.append({
                    "address": address, "value": value, "formula": formula,
                    "formula_local": formula_local, "error": error,
                })
        sheets.append({"name": sheet.Name, "rows": rows, "columns": columns, "cells": cells})
    return {"engine": ENGINE, "sheets": sheets, "warnings": warnings}


def _create_blank(app, path):
    book = app.Workbooks.Add()
    try:
        while int(book.Worksheets.Count) > 1:
            book.Worksheets(int(book.Worksheets.Count)).Delete()
        sheet = book.Worksheets(1)
        sheet.Name = "Sheet1"
        book.SaveAs(str(path), FileFormat=51, AddToMru=False)
        return book
    except Exception:
        book.Close(SaveChanges=False)
        raise


def _write_csv(app, path, rows):
    if not isinstance(rows, list) or not rows:
        rows = [[]]
    row_count = len(rows)
    column_count = max(1, max(len(row) for row in rows))
    if row_count > 500 or column_count > 40:
        raise ValueError("O CSV excede o limite de importação de 500 linhas por 40 colunas.")
    normalized = []
    for row in rows:
        if not isinstance(row, list) or len(row) > 40:
            raise ValueError("Linha CSV inválida.")
        values = []
        for value in row:
            if not isinstance(value, str) or len(value) > 32_767:
                raise ValueError("Texto CSV inválido ou grande demais para o Excel.")
            parsed = _csv_numeric_value(value)
            values.append(parsed)
        normalized.append(values + [""] * (column_count - len(values)))
    book = app.Workbooks.Add()
    try:
        while int(book.Worksheets.Count) > 1:
            book.Worksheets(int(book.Worksheets.Count)).Delete()
        sheet = book.Worksheets(1)
        sheet.Name = "Dados"
        region = sheet.Range(sheet.Cells(1, 1), sheet.Cells(row_count, column_count))
        # Typed numeric Value2 entries remain numbers; text formatting protects
        # strings from Excel's formula/date inference and is locale independent.
        region.NumberFormat = "@"
        region.Value2 = tuple(tuple(row) for row in normalized)
        book.SaveAs(str(path), FileFormat=51, AddToMru=False)
        return book
    except Exception:
        book.Close(SaveChanges=False)
        raise


def _csv_numeric_value(text):
    """Convert only plain, exact decimal numbers; keep ambiguous text unchanged."""
    if not _CSV_DECIMAL.fullmatch(text):
        return text
    unsigned = text[1:] if text.startswith("-") else text
    whole = unsigned.partition(".")[0]
    if len(whole) > 1 and whole.startswith("0"):
        return text
    digits = unsigned.replace(".", "")
    if len(digits.lstrip("0")) > 15 or text in {"-0", "-0.0"}:
        return text
    try:
        if "." not in text:
            return int(text)
        value = float(text)
    except (ValueError, OverflowError):
        return text
    if not math.isfinite(value) or (value == 0 and any(char in "123456789" for char in digits)):
        return text
    return value


def _set_formula(cell, formula):
    formula = _formula_text(formula)
    localized = any(token.type == "FUNC" and token.subtype == "OPEN" and
                    token.value[:-1].upper() in _LOCAL_FUNCTIONS for token in Tokenizer(formula).items)
    if localized:
        cell.Formula2Local = formula
        return
    try:
        cell.Formula2 = formula
    except Exception:
        try:
            cell.Formula2Local = formula
        except Exception:
            cell.Formula = formula


def _set_value(cell, value):
    if isinstance(value, str):
        cell.NumberFormat = "@"
    if value is None:
        cell.ClearContents()
    else:
        cell.Value2 = value


def _read_range(book, sheet_name, address):
    if not isinstance(address, str) or len(address) > 30:
        raise ValueError("Intervalo A1 inválido.")
    bounds = range_boundaries(address.replace("$", ""))
    min_col, min_row, max_col, max_row = bounds
    if (None in bounds or not 1 <= min_col <= max_col <= 16384
            or not 1 <= min_row <= max_row <= 1048576
            or (max_col - min_col + 1) * (max_row - min_row + 1) > MAX_RANGE_CELLS):
        raise ValueError(f"A leitura é limitada a {MAX_RANGE_CELLS} células por chamada.")
    sheet = _find_sheet(book, sheet_name)
    region = sheet.Range(address)
    rows, columns = max_row - min_row + 1, max_col - min_col + 1
    values = _as_matrix(region.Value2, rows, columns)
    formulas = _as_matrix(region.Formula2, rows, columns)
    result = []
    for r in range(rows):
        for c in range(columns):
            cell = region.Cells(r + 1, c + 1)
            formula = formulas[r][c] if isinstance(formulas[r][c], str) and formulas[r][c].startswith("=") else None
            if formula and not cell.HasFormula:
                formula = None
            value, shortened = _safe_value(values[r][c])
            result.append({
                "address": f"{_column_name(min_col + c)}{min_row + r}",
                "value": value, "formula": formula,
                "formula_local": _local_formula(cell), "error": _error_for(cell, values[r][c], formula),
            })
    return {"sheet": sheet_name, "range": address, "rows": rows, "columns": columns, "cells": result}


def _test_formula(book, options):
    sheet = _find_sheet(book, options["sheet"])
    address = options["cell"]
    cell = sheet.Range(address)
    formula = _formula_text(options["formula"])
    _set_formula(cell, formula)
    book.Application.CalculateFullRebuild()
    value = cell.Value2
    safe_value, shortened = _safe_value(value)
    formula_local = _local_formula(cell)
    error = _error_for(cell, value, formula)
    result = {
        "ok": error is None, "formula": formula, "formula_local": formula_local,
        "value": safe_value, "error": error, "sheet": options["sheet"],
        "cell": address, "engine": ENGINE,
    }
    _add_spill_result(result, book, cell, options["sheet"])
    if shortened:
        warning = "O texto do resultado foi abreviado."
        result["warning"] = f"{result['warning']} {warning}" if result.get("warning") else warning
    return result


def _add_spill_result(result, book, cell, sheet_name):
    try:
        if not bool(cell.HasSpill):
            return
    except Exception:
        return
    try:
        spill = cell.SpillingToRange
        address = str(spill.Address(False, False))
        min_col, min_row, max_col, max_row = range_boundaries(address)
        count = (max_col - min_col + 1) * (max_row - min_row + 1)
        if count > MAX_RANGE_CELLS:
            result["ok"] = False
            result["error"] = f"O resultado matricial ocupa {count} células; a leitura é limitada a {MAX_RANGE_CELLS}."
            result["warning"] = "O resultado matricial ultrapassa o limite de leitura; o valor completo não foi confirmado."
            return
        result["spill"] = _read_range(book, sheet_name, address)
    except Exception:
        result["ok"] = False
        result["error"] = "O Excel informou um resultado matricial, mas não foi possível ler toda a área derramada."
        result["warning"] = "O valor completo do resultado matricial não foi confirmado."


def _perform(app, operation, directory, options):
    file_name = options.get("file_name")
    if file_name not in _ALLOWED_FILES:
        raise ValueError("Destino interno da pasta de trabalho inválido.")
    path = directory / file_name
    book = None
    if operation == "create_blank":
        book = _create_blank(app, path)
        app.CalculateFullRebuild()
        book.Save()
    elif operation == "import_csv":
        book = _write_csv(app, path, options.get("rows"))
        app.CalculateFullRebuild()
        book.Save()
    elif operation == "import_xlsx":
        source = Path(options.get("source_path", "")).resolve()
        attachments = (directory.parent.parent / "attachments").resolve()
        if source.parent.parent != attachments or not source.name.startswith("original."):
            raise ValueError("Anexo de origem inválido.")
        validate_xlsx(source)
        shutil.copyfile(source, path)
        book = _open_book(app, path)
        book.Save()
    elif operation in {"snapshot", "read_range", "mutate", "test_formula"}:
        book = _open_book(app, path)
        if operation == "mutate":
            changes = options.get("changes")
            if not isinstance(changes, list) or len(changes) > 200:
                raise ValueError("Alterações inválidas.")
            for change in changes:
                sheet = _find_sheet(book, change["sheet"])
                cell = sheet.Range(change["cell"])
                if "formula" in change:
                    _set_formula(cell, change["formula"])
                else:
                    _set_value(cell, change.get("value"))
            app.CalculateFullRebuild()
            book.Save()
            result = _snapshot(book)
        elif operation == "test_formula":
            result = _test_formula(book, options)
        elif operation == "read_range":
            result = _read_range(book, options["sheet"], options["range"])
        else:
            result = _snapshot(book)
    else:
        raise ValueError("Operação Excel inválida.")
    if book is not None and operation in {"create_blank", "import_csv", "import_xlsx"}:
        result = _snapshot(book)
    if book is not None:
        book.Close(SaveChanges=False)
    return result


def main(request_path):
    request_path = Path(request_path).resolve()
    payload = read_json(request_path)
    if not isinstance(payload, dict):
        raise ValueError("Solicitação interna inválida.")
    job_id = payload.get("job_id")
    try:
        uuid.UUID(job_id)
        workbook_id = payload["workbook_id"]
        root = Path(payload["runs_root"]).resolve()
        directory = workbooks.workbook_directory(workbook_id, root)
        if request_path.parent != directory.resolve() or not request_path.name.endswith(job_id + ".json"):
            raise ValueError("Solicitação Excel fora da pasta de trabalho.")
        response = directory / f".excel-response-{job_id}.json"
        if response != Path(payload["response_path"]).resolve():
            raise ValueError("Resposta Excel fora da pasta de trabalho.")
        marker = directory / ".excel-owner.json"
        pythoncom, app, excel_job = _launch_excel(job_id, marker)
        try:
            result = _perform(app, payload["operation"], directory, payload.get("options") or {})
            write_json(response, {"ok": True, "result": result})
        finally:
            try:
                app.DisplayAlerts = False
                app.Quit()
            finally:
                try:
                    import win32api
                    win32api.CloseHandle(excel_job)
                finally:
                    pythoncom.CoUninitialize()
                    marker.unlink(missing_ok=True)
    except Exception as exc:
        try:
            response = Path(payload.get("response_path", request_path.with_name("failed-response.json")))
            if response.parent == request_path.parent:
                write_json(response, {"ok": False, "error": str(exc)[:800]})
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Excel worker requires one internal request file.")
    raise SystemExit(main(sys.argv[1]))
