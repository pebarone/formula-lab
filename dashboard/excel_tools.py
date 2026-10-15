"""Bounded, workbook-scoped Excel tools for chat and paired evaluations."""

import copy
import json
import math
import re
import time
import uuid

from dashboard.chat_backend import ChatContextLimit, validate_messages

MAX_TOOL_ROUNDS = 4
MAX_TOOL_CALLS = 4
MAX_TOOL_TOKENS = 512
MAX_TOOL_ARGUMENT_CHARS = 4096
MAX_TOOL_RESULT_CHARS = 12_000

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "excel_read_range",
            "description": "Read values and formulas from a range in the current workbook.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sheet": {"type": "string", "description": "Worksheet name."},
                    "range": {"type": "string", "description": "A1 range, for example A1:C8."},
                },
                "required": ["sheet", "range"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "excel_test_formula",
            "description": "Evaluate a formula in a scratch copy of the current workbook without saving changes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sheet": {"type": "string", "description": "Worksheet name."},
                    "cell": {"type": "string", "description": "Destination cell in A1 notation."},
                    "formula": {"type": "string", "description": "Excel formula, including the leading equals sign."},
                },
                "required": ["sheet", "cell", "formula"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "excel_write_cells",
            "description": "Write up to 200 values or formulas to cells in the current workbook.",
            "parameters": {
                "type": "object",
                "properties": {
                    "changes": {
                        "type": "array",
                        "maxItems": 200,
                        "items": {
                            "type": "object",
                            "properties": {
                                "sheet": {"type": "string"},
                                "cell": {"type": "string", "description": "Destination cell in A1 notation."},
                                "value": {"type": ["string", "number", "boolean", "null"]},
                                "formula": {"type": "string", "description": "Excel formula, including the leading equals sign."},
                            },
                            "required": ["sheet", "cell"],
                            "oneOf": [
                                {"required": ["value"], "not": {"required": ["formula"]}},
                                {"required": ["formula"], "not": {"required": ["value"]}},
                            ],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["changes"],
                "additionalProperties": False,
            },
        },
    },
]

_TOOLS_BY_NAME = {item["function"]["name"]: item["function"] for item in TOOL_DEFINITIONS}
_CELL_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?([1-9][0-9]{0,6})$")
_RANGE_RE = re.compile(
    r"^\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6}"
    r"(?::\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6})?$"
)
_TOOL_CALL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)
_TOOL_USAGE_INSTRUCTION = (
    "\n\nFor Excel formula requests, test your proposed formula with excel_test_formula before giving the final formula. "
    "If Excel returns an error, revise the formula and test the revision before answering. "
    "Read the workbook when you need cell values or formulas. Formula tests use a scratch copy. "
    "Workbook previews are samples, not the full dataset. For a calculated output column, calculate each data row "
    "independently with same-row references, preserve the header row, and do not aggregate sampled rows unless the user asks for totals. "
    "Do not say a formula was tested successfully unless excel_test_formula returned a successful result for that formula. "
    "For unrelated requests you may answer directly."
)


class ToolProtocolError(ValueError):
    """A model tool call did not match the supported protocol."""


def tool_definitions():
    """Return fresh OpenAI-style function schemas for the local chat template."""
    return copy.deepcopy(TOOL_DEFINITIONS)


def tool_prompt_messages(messages):
    """Return validated user-facing history with the tools-mode instruction appended."""
    result = copy.deepcopy(validate_messages(messages))
    result[-1]["content"] += _TOOL_USAGE_INSTRUCTION
    return result


def _validate_workbook_id(workbook_id):
    if not isinstance(workbook_id, str):
        raise ValueError("A workbook ID is required for Excel tools.")
    try:
        canonical = str(uuid.UUID(workbook_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("The workbook ID is invalid.") from exc
    if canonical != workbook_id.lower():
        raise ValueError("The workbook ID is invalid.")
    return canonical


def _strict_json_object(source):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ToolProtocolError(f"Duplicate JSON key: {key}.")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ToolProtocolError(f"Invalid JSON constant: {value}.")

    try:
        value = json.loads(source, object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (json.JSONDecodeError, ToolProtocolError) as exc:
        raise ToolProtocolError(f"Tool arguments must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ToolProtocolError("A tool call must be a JSON object.")
    return value


def _validate_sheet(sheet):
    if not isinstance(sheet, str) or not sheet.strip() or len(sheet) > 31:
        raise ToolProtocolError("sheet must be a nonempty worksheet name of at most 31 characters.")
    if any(char in sheet for char in ":\\/?*[]"):
        raise ToolProtocolError("sheet contains a character Excel does not allow in worksheet names.")
    return sheet


def _validate_cell(cell):
    if not isinstance(cell, str):
        raise ToolProtocolError("cell must use A1 notation.")
    match = _CELL_RE.fullmatch(cell)
    if not match:
        raise ToolProtocolError("cell must be a single cell in A1 notation.")
    column = 0
    for char in match.group(1).upper():
        column = column * 26 + ord(char) - ord("A") + 1
    if column > 16384 or int(match.group(2)) > 1_048_576:
        raise ToolProtocolError("cell is outside the Excel worksheet limits.")
    return cell


def _validate_range(value):
    if not isinstance(value, str) or len(value) > 64 or not _RANGE_RE.fullmatch(value):
        raise ToolProtocolError("range must be an A1 cell or rectangular A1 range, such as A1:C8.")
    for cell in value.split(":"):
        _validate_cell(cell)
    return value


def _validate_arguments(name, arguments):
    if not isinstance(arguments, dict):
        raise ToolProtocolError("Tool arguments must be a JSON object.")
    function = _TOOLS_BY_NAME.get(name)
    if function is None:
        raise ToolProtocolError(f"Unknown Excel tool: {name}.")
    properties = function["parameters"]["properties"]
    if set(arguments) - set(properties):
        raise ToolProtocolError("Tool arguments contain unsupported fields.")

    if name == "excel_read_range":
        if set(arguments) != {"sheet", "range"}:
            raise ToolProtocolError("excel_read_range requires exactly sheet and range.")
        _validate_sheet(arguments["sheet"])
        _validate_range(arguments["range"])
    elif name == "excel_test_formula":
        if set(arguments) != {"sheet", "cell", "formula"}:
            raise ToolProtocolError("excel_test_formula requires exactly sheet, cell, and formula.")
        _validate_sheet(arguments["sheet"])
        _validate_cell(arguments["cell"])
        if not isinstance(arguments["formula"], str) or not arguments["formula"].startswith("="):
            raise ToolProtocolError("formula must be text beginning with =.")
        if len(arguments["formula"]) > 8192:
            raise ToolProtocolError("formula is too long.")
    else:
        if set(arguments) != {"changes"} or not isinstance(arguments["changes"], list):
            raise ToolProtocolError("excel_write_cells requires a changes array.")
        changes = arguments["changes"]
        if not 1 <= len(changes) <= 200:
            raise ToolProtocolError("changes must contain between 1 and 200 cells.")
        for change in changes:
            if not isinstance(change, dict) or set(change) not in (
                {"sheet", "cell", "value"}, {"sheet", "cell", "formula"},
            ):
                raise ToolProtocolError("Each change needs sheet, cell, and exactly one of value or formula.")
            _validate_sheet(change["sheet"])
            _validate_cell(change["cell"])
            if "formula" in change:
                if not isinstance(change["formula"], str) or not change["formula"].startswith("="):
                    raise ToolProtocolError("Each formula must be text beginning with =.")
                if len(change["formula"]) > 8192:
                    raise ToolProtocolError("A formula is too long.")
            else:
                value = change["value"]
                if isinstance(value, (dict, list)) or not (value is None or isinstance(value, (str, int, float, bool))):
                    raise ToolProtocolError("Each value must be a JSON scalar.")
                if isinstance(value, float) and not math.isfinite(value):
                    raise ToolProtocolError("Each numeric value must be finite.")
                if isinstance(value, str) and len(value) > 32_767:
                    raise ToolProtocolError("A cell value is too long for Excel.")
    return arguments


def _bounded_result(result):
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded) <= MAX_TOOL_RESULT_CHARS:
        return json.loads(encoded), encoded
    preview = encoded[: MAX_TOOL_RESULT_CHARS - 100]
    bounded = {"truncated": True, "preview": preview}
    return bounded, json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))


def write_tool_result(snapshot, change_count):
    """Confirm a saved revision; the grid is fetched separately from tool feedback."""
    return {
        "id": snapshot["id"], "revision": snapshot["revision"],
        "engine": snapshot["engine"], "applied_changes": change_count,
    }


def execute_excel_tool(workbook_id, name, arguments):
    """Execute one allowlisted tool against the current workbook only."""
    workbook_id = _validate_workbook_id(workbook_id)
    arguments = _validate_arguments(name, arguments)
    from dashboard import workbooks

    if name == "excel_read_range":
        result = workbooks.read_range(workbook_id, arguments["sheet"], arguments["range"])
    elif name == "excel_test_formula":
        result = workbooks.test_formula(
            workbook_id, arguments["sheet"], arguments["cell"], arguments["formula"],
        )
    elif name == "excel_write_cells":
        snapshot = workbooks.mutate_workbook(workbook_id, arguments["changes"], expected_revision=None)
        result = write_tool_result(snapshot, len(arguments["changes"]))
    else:
        # The allowlist is checked above; keep this branch defensive if it changes.
        raise ToolProtocolError(f"Unknown Excel tool: {name}.")
    bounded, _ = _bounded_result(result)
    if name == "excel_test_formula" and isinstance(result, dict):
        if result.get("ok") is False:
            truncated = bounded.get("truncated", False)
            preview = bounded.get("preview")
            bounded = {
                "ok": False,
                "error": str(result.get("error") or "Microsoft Excel returned a formula error.")[:2000],
            }
            if truncated:
                bounded.update(truncated=True, preview=preview)
        elif result.get("warning"):
            bounded["ok"] = False
            bounded["error"] = "Excel abbreviated the formula result, so its complete value cannot be confirmed."
        elif bounded.get("truncated"):
            bounded = {
                "ok": False,
                "error": "The Excel formula result was truncated, so its value cannot be confirmed.",
                "truncated": True,
                "preview": bounded["preview"],
            }
    return bounded


def _tool_call_payload(raw):
    if len(raw) > MAX_TOOL_ARGUMENT_CHARS:
        raise ToolProtocolError(f"Tool arguments exceed {MAX_TOOL_ARGUMENT_CHARS} characters.")
    payload = _strict_json_object(raw)
    if set(payload) != {"name", "arguments"}:
        raise ToolProtocolError("A tool call must contain exactly name and arguments.")
    if not isinstance(payload["name"], str) or not payload["name"]:
        raise ToolProtocolError("A tool call name must be a nonempty string.")
    if not isinstance(payload["arguments"], dict):
        raise ToolProtocolError("Tool arguments must be a JSON object.")
    return payload


def _messages_with_tool_result(conversation, assistant_content, name, result):
    conversation.append({"role": "assistant", "content": assistant_content})
    conversation.append({"role": "tool", "name": name, "content": result})


def _notify(on_progress, phase, trace):
    if on_progress:
        on_progress(phase, copy.deepcopy(trace))


def run_tool_conversation(
    messages,
    generate,
    workbook_id,
    *,
    on_progress=None,
    check_cancel=None,
    max_rounds=MAX_TOOL_ROUNDS,
    max_calls=MAX_TOOL_CALLS,
):
    """Run a bounded tool conversation through a caller-supplied generation function.

    ``generate(messages, tooldefs)`` must return one decoded assistant turn. Public
    messages are validated before any internal assistant/tool messages are added.
    """
    public_messages = validate_messages(messages)
    workbook_id = _validate_workbook_id(workbook_id)
    if not callable(generate):
        raise TypeError("generate must be callable.")
    if not isinstance(max_rounds, int) or not 1 <= max_rounds <= MAX_TOOL_ROUNDS:
        raise ValueError(f"max_rounds must be between 1 and {MAX_TOOL_ROUNDS}.")
    if not isinstance(max_calls, int) or not 1 <= max_calls <= MAX_TOOL_CALLS:
        raise ValueError(f"max_calls must be between 1 and {MAX_TOOL_CALLS}.")

    conversation = tool_prompt_messages(public_messages)
    definitions = tool_definitions()
    trace = []
    generation_rounds = 0
    from dashboard.runtime import Cancelled

    for round_number in range(1, max_rounds + 1):
        generation_rounds += 1
        _notify(on_progress, f"Gerando resposta ({round_number}/{max_rounds})", trace)
        try:
            response = generate(copy.deepcopy(conversation), copy.deepcopy(definitions))
        except ChatContextLimit:
            return {
                "text": "The Excel conversation reached the 32,768-token context limit before I could produce a final answer. Check the workbook for any completed changes.",
                "tool_trace": trace, "tools_used": bool(trace), "tool_calls": len(trace),
                "tool_rounds": generation_rounds, "budget_exhausted": True,
            }
        if not isinstance(response, str):
            raise TypeError("The generation callback must return decoded text.")
        if len(response) > 32_768:
            response = response[:32_768]

        matches = list(_TOOL_CALL_RE.finditer(response))
        has_marker = "<tool_call" in response or "</tool_call>" in response
        remainder = _TOOL_CALL_RE.sub("", response)
        malformed = has_marker and (
            not matches or "<tool_call" in remainder or "</tool_call>" in remainder
        )
        if not matches and not malformed:
            return {
                "text": response.strip(), "tool_trace": trace,
                "tools_used": bool(trace), "tool_calls": len(trace),
                "tool_rounds": generation_rounds,
            }

        assistant_content = response
        if malformed:
            if len(trace) >= max_calls:
                return {
                    "text": "I reached the Excel tool-call limit before I could produce a final answer. Check the workbook for any completed changes.",
                    "tool_trace": trace, "tools_used": bool(trace), "tool_calls": len(trace),
                    "tool_rounds": generation_rounds, "budget_exhausted": True,
                }
            name = "invalid_tool_call"
            raw_arguments = response[:MAX_TOOL_ARGUMENT_CHARS]
            result = {"ok": False, "error": "Malformed <tool_call> markup; emit one complete JSON tool call or answer directly."}
            entry = {
                "name": name, "arguments": {"raw": raw_arguments}, "result": result,
                "elapsed_seconds": 0.0,
            }
            trace.append(entry)
            _messages_with_tool_result(
                conversation, assistant_content, name,
                json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            )
            _notify(on_progress, f"Excel: chamada inválida ({round_number}/{max_rounds})", trace)
            if len(trace) >= max_calls:
                return {
                    "text": "I reached the Excel tool-call limit before I could produce a final answer. Check the workbook for any completed changes.",
                    "tool_trace": trace, "tools_used": bool(trace), "tool_calls": len(trace),
                    "tool_rounds": generation_rounds, "budget_exhausted": True,
                }
            continue

        for index, match in enumerate(matches):
            if len(trace) >= max_calls:
                notice = {
                    "text": "I reached the Excel tool-call limit before I could produce a final answer. Check the workbook for any completed changes.",
                    "tool_trace": trace, "tools_used": bool(trace), "tool_calls": len(trace),
                    "tool_rounds": generation_rounds, "budget_exhausted": True,
                }
                return notice

            raw = match.group(1)
            name = "invalid_tool_call"
            arguments = {"raw": raw[:MAX_TOOL_ARGUMENT_CHARS]}
            started = time.perf_counter()
            try:
                payload = _tool_call_payload(raw)
                name = payload["name"]
                arguments = payload["arguments"]
            except (ToolProtocolError, ValueError, TypeError) as exc:
                result = {"ok": False, "error": str(exc)}
                content = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                entry = {
                    "name": name, "arguments": copy.deepcopy(arguments),
                    "result": copy.deepcopy(result), "elapsed_seconds": time.perf_counter() - started,
                }
                trace.append(entry)
                _messages_with_tool_result(conversation, assistant_content if index == 0 else "", name, content)
                assistant_content = ""
                _notify(on_progress, f"Excel: chamada inválida ({round_number}/{max_rounds})", trace)
                continue

            entry = {
                "name": name, "arguments": copy.deepcopy(arguments),
                "result": {"status": "running"}, "elapsed_seconds": 0.0,
            }
            trace.append(entry)
            _notify(on_progress, f"Excel: {name} ({round_number}/{max_rounds})", trace)
            if check_cancel:
                try:
                    check_cancel()
                except Cancelled:
                    entry["result"] = {"ok": False, "error": "Excel operation was cancelled before it started."}
                    entry["elapsed_seconds"] = time.perf_counter() - started
                    _notify(on_progress, f"Excel: {name} cancelled", trace)
                    raise
            try:
                result_value = execute_excel_tool(workbook_id, name, arguments)
                if name == "excel_test_formula" and isinstance(result_value, dict) and result_value.get("ok") is False:
                    result = {
                        "ok": False, "error": result_value.get("error") or "Microsoft Excel returned a formula error.",
                        "result": result_value,
                    }
                else:
                    result = {"ok": True, "result": result_value}
            except Cancelled:
                entry["result"] = {"ok": False, "error": "Excel operation was cancelled."}
                entry["elapsed_seconds"] = time.perf_counter() - started
                _notify(on_progress, f"Excel: {name} cancelled", trace)
                raise
            except (ToolProtocolError, ValueError, TypeError) as exc:
                result = {"ok": False, "error": str(exc)}
            except Exception as exc:
                result = {"ok": False, "error": f"Excel tool failed: {type(exc).__name__}: {exc}"}
            content = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            entry["result"] = copy.deepcopy(result)
            entry["elapsed_seconds"] = time.perf_counter() - started
            if check_cancel:
                try:
                    check_cancel()
                except Cancelled:
                    _notify(on_progress, f"Excel: {name} concluído antes da interrupção", trace)
                    raise
            _messages_with_tool_result(conversation, assistant_content if index == 0 else "", name, content)
            assistant_content = ""
            _notify(on_progress, f"Excel: {name} ({round_number}/{max_rounds})", trace)

        if round_number == max_rounds:
            return {
                "text": "I reached the Excel tool-round limit before I could produce a final answer. Check the workbook for any completed changes.",
                "tool_trace": trace, "tools_used": bool(trace), "tool_calls": len(trace),
                "tool_rounds": generation_rounds, "budget_exhausted": True,
            }

    return {
        "text": "I could not complete a final answer within the Excel tool limits.",
        "tool_trace": trace, "tools_used": bool(trace), "tool_calls": len(trace),
        "tool_rounds": generation_rounds, "budget_exhausted": True,
    }
