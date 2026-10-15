"""Safety checks and isolated process boundary for Microsoft Excel COM."""

import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from openpyxl.formula import Tokenizer
from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries

from dashboard.config import ROOT, RUNS, read_json, write_json
from dashboard import workbooks

ENGINE = "Microsoft Excel"
WORKER_TIMEOUT_SECONDS = 30
MAX_PREVIEW_ROWS = 50
MAX_PREVIEW_COLUMNS = 12
MAX_RANGE_CELLS = 600
MAX_FORMULA_LENGTH = 8_192
MAX_VALUE_TEXT = 32_767
_OWNER_MARKER = ".excel-owner.json"
_SNAPSHOT_FILE = "snapshot.json"
_SNAPSHOT_VERSION = 1
_FORMULA_CELL = re.compile(r"\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})\Z")
_EXTERNAL_BOOK = re.compile(r"\[[^\]]+\][^!]{0,255}!", re.I)
_REFERENCE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|[A-Za-z][A-Za-z0-9+.-]*://)")

# Restrict calls to native worksheet functions, preventing add-in/UDF execution.
_SAFE_FUNCTIONS = set("""
ABS ACOS ACOSH ADDRESS AND ARABIC AREAS ASIN ASINH ATAN ATAN2 ATANH AVERAGE
AVERAGEA AVERAGEIF AVERAGEIFS BASE CEILING CEILING.MATH CEILING.PRECISE
CHOOSE CHOOSECOLS CHOOSEROWS CLEAN CODE COLUMN COLUMNS COMBIN CONCAT CONCATENATE
COUNT COUNTA COUNTBLANK COUNTIF COUNTIFS DATE DATEVALUE DAY DAYS EDATE EOMONTH
EXACT EXP FILTER FIND FIXED FLOOR FLOOR.MATH FORECAST FORMULATEXT FREQUENCY
FV HLOOKUP HOUR IF IFERROR IFNA IFS INDEX INT ISBLANK ISERR ISERROR ISEVEN
ISFORMULA ISLOGICAL ISNA ISNONTEXT ISNUMBER ISODD ISOMITTED ISOWEEKNUM ISTEXT
LARGE LEFT LEN LET LINEST LN LOG LOG10 LOOKUP LOWER MATCH MAX MAXA MAXIFS MEDIAN
MID MIN MINA MINIFS MINUTE MOD MONTH MROUND N NA NETWORKDAYS NETWORKDAYS.INTL
NOMINAL NORM.DIST NORM.INV NORM.S.DIST NORM.S.INV NOT NOW NPER NPV NUMBERVALUE
OFFSET OR PI PMT POWER PRODUCT PROPER PV QUARTILE QUOTIENT RADIANS RAND
RANDBETWEEN RANDARRAY RANK.EQ REPLACE REPT RIGHT ROUND ROUNDDOWN ROUNDUP ROW ROWS
SEARCH SECOND SEQUENCE SIGN SIN SORT SORTBY SQRT SQRTPI STDEV.S STDEV.P
SUBSTITUTE SUBTOTAL SUM SUMIF SUMIFS SUMPRODUCT SUMSQ SWITCH TAN TEXT TEXTJOIN
TIME TIMEVALUE TODAY TRANSPOSE TRIM TRUE UNIQUE UPPER VALUE VLOOKUP WEEKDAY WEEKNUM
XLOOKUP XMATCH YEAR CORREL MODE.SNGL
""".split())
_LOCAL_FUNCTIONS = set("""
MODA.SNGL
SOMA SOMASE SOMASES MÉDIA MÉDIASE MÉDIASES CONT.NÚM CONT.VALORES CONTAR.VAZIO
CONT.SE CONT.SES MÁXIMO MÍNIMO SE SEERRO SE.ND SES E OU NÃO PROCV PROCH PROCX
CORRESP ÍNDICE FILTRO ÚNICO CLASSIFICAR CLASSIFICARPOR ESCOLHERCOLS ESCOLHERLINHAS
SEQUÊNCIA SOMARPRODUTO ARRED ARREDONDAR.PARA.CIMA ARREDONDAR.PARA.BAIXO
ESQUERDA DIREITA EXT.TEXTO NÚM.CARACT ARRUMAR TEXTO UNIRTEXTO HOJE AGORA DATA
ANO MÊS DIA HORA MINUTO SEGUNDO LINHAS COLUNAS MAIOR MENOR PROCURAR SUBSTITUIR VALOR
""".split())
_SAFE_FUNCTIONS |= _LOCAL_FUNCTIONS
_BLOCKED_FUNCTIONS = {
    "WEBSERVICE", "RTD", "HYPERLINK", "IMAGE", "PY", "CALL", "EXEC", "REGISTER",
    "INDIRECT", "CUBE", "CUBEMEMBER", "CUBEVALUE", "CUBERANKEDMEMBER",
    "CUBESET", "CUBESETCOUNT", "CUBEKPIMEMBER", "CUBEMEMBERPROPERTY",
}


class ExcelEngineError(RuntimeError):
    """Excel could not safely open or calculate the workbook."""


def _read_snapshot_cache(directory, revision):
    try:
        entry = read_json(directory / _SNAPSHOT_FILE)
    except (OSError, ValueError):
        return None
    if (not isinstance(entry, dict) or entry.get("version") != _SNAPSHOT_VERSION or isinstance(entry.get("revision"), bool)
            or entry.get("revision") != revision or not isinstance(entry.get("snapshot"), dict)):
        return None
    snapshot = entry["snapshot"]
    if (snapshot.get("engine") != ENGINE or not isinstance(snapshot.get("sheets"), list)
            or not isinstance(snapshot.get("warnings"), list)):
        return None
    return snapshot


def _write_snapshot_cache(directory, revision, snapshot):
    try:
        write_json(directory / _SNAPSHOT_FILE, {"version": _SNAPSHOT_VERSION, "revision": revision, "snapshot": snapshot})
    except OSError:
        return "A prévia não foi guardada em cache; uma próxima leitura poderá reabrir o Excel."
    return None


def _unique_warnings(*groups):
    result = []
    for group in groups:
        for warning in group:
            if isinstance(warning, str) and warning not in result:
                result.append(warning)
    return result


def engine_status():
    """Report installed prerequisites without starting Excel."""
    if os.name != "nt":
        return {"available": False, "engine": ENGINE, "message": "Microsoft Excel COM está disponível somente no Windows."}
    excel = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft Office" / "Root" / "Office16" / "EXCEL.EXE"
    if not excel.is_file():
        return {"available": False, "engine": ENGINE, "message": "Microsoft Excel não foi encontrado neste computador."}
    if importlib.util.find_spec("win32com") is None:
        return {"available": False, "engine": ENGINE, "message": "A integração local do Excel ainda não está instalada."}
    return {"available": True, "engine": ENGINE, "message": "Microsoft Excel está pronto para calcular pastas de trabalho."}


def _formula_text(formula):
    if not isinstance(formula, str) or not formula.startswith("=") or len(formula) > MAX_FORMULA_LENGTH:
        raise ValueError("Informe uma fórmula Excel iniciada por = com até 8.192 caracteres.")
    if any(ord(ch) < 32 and ch not in "\t\r\n" for ch in formula):
        raise ValueError("A fórmula contém caracteres de controle.")
    try:
        formula.encode("utf-8")
        tokens = Tokenizer(formula).items
    except Exception as exc:
        raise ValueError("A sintaxe da fórmula não é aceita.") from exc
    for token in tokens:
        if token.type == "FUNC" and token.subtype == "OPEN":
            name = token.value[:-1].strip().upper()
            canonical = name.removeprefix("_XLFN.").removeprefix("_XLWS.")
            if canonical in _BLOCKED_FUNCTIONS or canonical.startswith("CUBE"):
                raise ValueError(f"A função {name} não é permitida por segurança.")
            if canonical not in _SAFE_FUNCTIONS:
                raise ValueError(f"A função {name} não está na lista de funções nativas seguras.")
        if token.type == "OPERAND" and token.subtype == "RANGE":
            reference = token.value
            if "|" in reference:
                raise ValueError("Referências DDE não são permitidas.")
            if _EXTERNAL_BOOK.search(reference) or _REFERENCE_PATH.search(reference):
                raise ValueError("Referências a arquivos, caminhos ou recursos externos não são permitidas.")
    return formula


def validate_xlsx(path):
    """Validate a bounded XLSX archive and audit formulas before Excel opens it."""
    from dashboard.attachments import _check_xlsx_archive

    path = Path(path)
    _check_xlsx_archive(path.read_bytes())
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                name = info.filename.lower()
                if (name.startswith("xl/externallinks/") or name == "xl/connections.xml"
                        or name.startswith("xl/querytables/")):
                    raise ValueError("Vínculos externos e conexões de dados não são permitidos.")
                if name.endswith(".rels"):
                    rels = archive.read(info).decode("utf-8", errors="ignore")
                    if re.search(r'targetmode\s*=\s*["\']external["\']', rels, re.I):
                        raise ValueError("Vínculos externos não são permitidos.")
                if name.endswith(".xml"):
                    with archive.open(info) as stream:
                        for _, element in ET.iterparse(stream, events=("end",)):
                            tag = element.tag.rsplit("}", 1)[-1].lower()
                            if tag in {"f", "definedname", "calculatedcolumnformula", "totalsrowformula"} and element.text:
                                expression = element.text.strip()
                                if expression:
                                    _formula_text(expression if expression.startswith("=") else "=" + expression)
                            element.clear()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Não foi possível validar as fórmulas da planilha XLSX.") from exc


def _cleanup_owned_excel(marker_path, job_id):
    """Terminate only the Excel PID this timed-out worker recorded as its own."""
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("job_id") != job_id:
            return
        pid, created = int(marker["pid"]), float(marker["created"])
        import psutil
        process = psutil.Process(pid)
        if process.name().casefold() != "excel.exe" or abs(process.create_time() - created) > 0.01:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except psutil.TimeoutExpired:
            process.kill()
    except (OSError, ValueError, KeyError, TypeError, ImportError):
        return
    finally:
        marker_path.unlink(missing_ok=True)


def _run_worker(workbook_id, runs_root, operation, options=None):
    directory = workbooks.workbook_directory(workbook_id, runs_root)
    job_id = str(uuid.uuid4())
    request_path = directory / f".excel-request-{job_id}.json"
    response_path = directory / f".excel-response-{job_id}.json"
    marker_path = directory / _OWNER_MARKER
    marker_path.unlink(missing_ok=True)
    write_json(request_path, {
        "job_id": job_id, "workbook_id": workbook_id, "runs_root": str(Path(runs_root).resolve()),
        "operation": operation, "options": options or {}, "response_path": str(response_path),
    })
    try:
        result = subprocess.run(
            [sys.executable, "-m", "dashboard.excel_worker", str(request_path)],
            cwd=ROOT, capture_output=True, text=True, timeout=WORKER_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        _cleanup_owned_excel(marker_path, job_id)
        raise TimeoutError("Microsoft Excel excedeu o limite de 30 segundos; nenhuma alteração foi confirmada.") from None
    finally:
        request_path.unlink(missing_ok=True)
    try:
        payload = json.loads(response_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        detail = (result.stderr or result.stdout or "").strip()[-800:]
        raise ExcelEngineError("O worker do Excel encerrou sem resposta." + (f" {detail}" if detail else "")) from None
    finally:
        response_path.unlink(missing_ok=True)
        marker_path.unlink(missing_ok=True)
    if result.returncode != 0 or not payload.get("ok"):
        raise ExcelEngineError(str(payload.get("error") or "Microsoft Excel não conseguiu concluir a operação.")[:800])
    return payload.get("result")


def _coordinate(value):
    if not isinstance(value, str) or not _FORMULA_CELL.fullmatch(value):
        raise ValueError("Endereço de célula inválido.")
    clean = value.replace("$", "").upper()
    row, column = coordinate_to_tuple(clean)
    from openpyxl.utils.cell import column_index_from_string
    col = column_index_from_string(re.match(r"\$?([A-Za-z]+)", value).group(1))
    if row > 1_048_576 or col > 16_384:
        raise ValueError("Endereço fora dos limites de uma planilha Excel.")
    return clean


def _validate_changes(changes):
    if not isinstance(changes, list) or len(changes) > 200:
        raise ValueError("Envie até 200 alterações por operação.")
    result, seen = [], set()
    for change in changes:
        allowed = ({"sheet", "cell", "value"}, {"sheet", "cell", "formula"})
        if not isinstance(change, dict) or set(change) not in allowed:
            raise ValueError("Cada alteração deve indicar exatamente sheet, cell e value ou formula.")
        sheet = change["sheet"]
        if not isinstance(sheet, str) or not sheet or len(sheet) > 31 or any(ord(ch) < 32 for ch in sheet):
            raise ValueError("Nome de aba inválido.")
        cell = _coordinate(change["cell"])
        key = (sheet.casefold(), cell)
        if key in seen:
            raise ValueError("Uma célula aparece mais de uma vez nas alterações.")
        seen.add(key)
        if "formula" in change:
            result.append({"sheet": sheet, "cell": cell, "formula": _formula_text(change["formula"])})
            continue
        value = change["value"]
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError("Valor de célula inválido.")
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) >= 2 ** 53:
            raise ValueError("Número inteiro fora do intervalo seguro.")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("O valor numérico deve ser finito.")
        if isinstance(value, str) and (len(value) > MAX_VALUE_TEXT or "\x00" in value):
            raise ValueError("Texto de célula excede o limite do Excel.")
        result.append({"sheet": sheet, "cell": cell, "value": value})
    return result


def _check_revision(expected_revision, current):
    if expected_revision is None:
        return
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision != current:
        raise ValueError("A pasta de trabalho mudou. Atualize a grade antes de editar.")


def _discard_uncommitted_workbook(workbook_id, directory, runs_root):
    root = workbooks.workbook_root(runs_root).resolve()
    try:
        target = Path(directory).resolve(strict=True)
        if (target.parent == root and target.name == workbook_id
                and not (target / "manifest.json").exists()):
            shutil.rmtree(target)
    except FileNotFoundError:
        pass


def create_workbook(attachment_id=None, *, runs_root=RUNS):
    status = engine_status()
    if not status["available"]:
        raise ExcelEngineError(status["message"])
    source_record = None
    options = {"file_name": ".pending.xlsx"}
    if attachment_id is not None:
        if not isinstance(attachment_id, str):
            raise ValueError("Identificador de anexo inválido.")
        source_record, source, rows = workbooks.source_for_attachment(attachment_id, runs_root)
        name = Path(source_record["name"]).stem[:150] or "Planilha"
        if source_record["kind"] == "xlsx":
            validate_xlsx(source)
            options["source_path"] = str(source.resolve())
            operation = "import_xlsx"
        else:
            options["rows"] = rows
            operation = "import_csv"
    else:
        name, operation = "Pasta de trabalho", "create_blank"
    workbook_id, directory = workbooks.new_workbook_directory(runs_root)
    try:
        with workbooks.workbook_lock(workbook_id, runs_root):
            snapshot = _run_worker(workbook_id, runs_root, operation, options)
            (directory / ".pending.xlsx").replace(directory / "workbook.xlsx")
            warnings = _unique_warnings(snapshot.get("warnings", []), source_record.get("warnings", []) if source_record else [])
            manifest = {
                "id": workbook_id, "name": name, "revision": 0, "engine": ENGINE,
                "source_attachment_id": attachment_id,
                "source_name": source_record["name"] if source_record else None,
                "warnings": warnings,
            }
            workbooks.write_manifest(directory, manifest)
            cache_warning = _write_snapshot_cache(directory, 0, snapshot)
            warnings = _unique_warnings(warnings, [cache_warning] if cache_warning else [])
            return {**snapshot, "id": workbook_id, "name": name, "revision": 0, "engine": ENGINE, "warnings": warnings}
    except BaseException:
        _discard_uncommitted_workbook(workbook_id, directory, runs_root)
        raise


def get_workbook(workbook_id, *, runs_root=RUNS):
    with workbooks.workbook_lock(workbook_id, runs_root):
        manifest = workbooks.read_manifest(workbook_id, runs_root)
        directory = workbooks.workbook_directory(workbook_id, runs_root)
        snapshot = _read_snapshot_cache(directory, manifest["revision"])
        cache_warning = None
        if snapshot is None:
            snapshot = _run_worker(workbook_id, runs_root, "snapshot", {"file_name": "workbook.xlsx"})
            cache_warning = _write_snapshot_cache(directory, manifest["revision"], snapshot)
        warnings = _unique_warnings(manifest.get("warnings", []), snapshot.get("warnings", []),
                                    [cache_warning] if cache_warning else [])
        return {**snapshot, "id": workbook_id, "name": manifest["name"],
                "revision": manifest["revision"], "engine": ENGINE, "warnings": warnings}


def mutate_workbook(workbook_id, changes, expected_revision=None, *, runs_root=RUNS):
    changes = _validate_changes(changes)
    with workbooks.workbook_lock(workbook_id, runs_root):
        manifest = workbooks.read_manifest(workbook_id, runs_root)
        _check_revision(expected_revision, manifest["revision"])
        directory = workbooks.workbook_directory(workbook_id, runs_root)
        pending = directory / ".pending.xlsx"
        pending.unlink(missing_ok=True)
        pending.write_bytes((directory / "workbook.xlsx").read_bytes())
        snapshot = _run_worker(workbook_id, runs_root, "mutate", {
            "file_name": ".pending.xlsx", "changes": changes,
        })
        pending.replace(directory / "workbook.xlsx")
        manifest["revision"] += 1
        workbooks.write_manifest(directory, manifest)
        cache_warning = _write_snapshot_cache(directory, manifest["revision"], snapshot)
        warnings = _unique_warnings(manifest.get("warnings", []), snapshot.get("warnings", []),
                                    [cache_warning] if cache_warning else [])
        return {**snapshot, "id": workbook_id, "name": manifest["name"],
                "revision": manifest["revision"], "engine": ENGINE, "warnings": warnings}


def test_formula(workbook_id, sheet, cell, formula, *, runs_root=RUNS):
    normalized_cell = _coordinate(cell)
    try:
        formula = _formula_text(formula)
    except ValueError as exc:
        return {"ok": False, "formula": formula, "formula_local": None, "value": None,
                "error": str(exc), "sheet": sheet, "cell": normalized_cell, "engine": ENGINE}
    with workbooks.workbook_lock(workbook_id, runs_root):
        workbooks.read_manifest(workbook_id, runs_root)
        directory = workbooks.workbook_directory(workbook_id, runs_root)
        scratch = directory / ".scratch.xlsx"
        scratch.unlink(missing_ok=True)
        scratch.write_bytes((directory / "workbook.xlsx").read_bytes())
        try:
            return _run_worker(workbook_id, runs_root, "test_formula", {
                "file_name": ".scratch.xlsx", "sheet": sheet, "cell": normalized_cell, "formula": formula,
            })
        finally:
            scratch.unlink(missing_ok=True)


def read_range(workbook_id, sheet, range_address, *, runs_root=RUNS):
    if not isinstance(sheet, str) or not sheet or len(sheet) > 31:
        raise ValueError("Nome de aba inválido.")
    if not isinstance(range_address, str) or len(range_address) > 30:
        raise ValueError("Intervalo de células inválido.")
    try:
        boundaries = range_boundaries(range_address.replace("$", ""))
    except Exception as exc:
        raise ValueError("Informe um intervalo A1 válido.") from exc
    min_col, min_row, max_col, max_row = boundaries
    if (None in boundaries or not 1 <= min_col <= max_col <= 16384
            or not 1 <= min_row <= max_row <= 1048576
            or (max_col - min_col + 1) * (max_row - min_row + 1) > MAX_RANGE_CELLS):
        raise ValueError(f"A leitura é limitada a {MAX_RANGE_CELLS} células por chamada.")
    with workbooks.workbook_lock(workbook_id, runs_root):
        workbooks.read_manifest(workbook_id, runs_root)
        return _run_worker(workbook_id, runs_root, "read_range", {
            "file_name": "workbook.xlsx", "sheet": sheet, "range": range_address.replace("$", "").upper(),
        })


def workbook_context(workbook_id, *, runs_root=RUNS, compact=False):
    snapshot = get_workbook(workbook_id, runs_root=runs_root)
    pieces = [f"Pasta de trabalho: {snapshot['name']} (revisão {snapshot['revision']}; mecanismo: {ENGINE})."]
    for sheet in snapshot["sheets"]:
        pieces.append(f"Aba {sheet['name']}: {sheet['rows']} linhas × {sheet['columns']} colunas; células visíveis:")
        rows = {}
        for cell in sheet["cells"]:
            if cell.get("value") is None and not cell.get("formula"):
                continue
            match = re.match(r"([A-Z]+)([0-9]+)", cell["address"])
            if compact and int(match.group(2)) > 5:
                continue
            content = json.dumps(cell.get("value"), ensure_ascii=False)
            if cell.get("formula"):
                content += f" ({cell['formula']})"
            if cell.get("error"):
                content += f" [erro: {cell['error']}]"
            rows.setdefault(match.group(2), []).append(f"{cell['address']}={content}")
        pieces.extend(" | ".join(entries) for entries in rows.values())
    pieces.extend(snapshot.get("warnings", []))
    if compact:
        pieces.append("Amostra de até 5 linhas por aba. Use excel_read_range para consultar os demais dados da planilha completa.")
    text = "\n".join(pieces)
    limit = 6_000 if compact else 24_000
    marker = "\n[Contexto limitado; consulte os demais dados com excel_read_range.]" if compact else "\n[Contexto limitado.]"
    return text[:limit] + (marker if len(text) > limit else "")


def workbook_download_path(workbook_id, *, runs_root=RUNS):
    path = workbooks.workbook_file(workbook_id, runs_root)
    directory = workbooks.workbook_directory(workbook_id, runs_root)
    if not path.is_file() or path.resolve().parent != directory.resolve():
        raise ValueError("Arquivo da pasta de trabalho não encontrado.")
    return path
