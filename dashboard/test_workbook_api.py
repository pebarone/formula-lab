import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import dashboard.workbooks as workbooks
from dashboard.config import read_json
from dashboard.server import Handler, Jobs


WORKBOOK_ID = "d1f85f92-47ce-4861-8aca-7d1ac18ab394"
SESSION_TOKEN = "workbook-api-test-token"


class WorkbookApiTests(unittest.TestCase):
    def test_paired_results_are_exposed_without_a_chat_and_legacy_summary(self):
        from dashboard.config import write_json
        run_id = "20261009-120000-abcdef12"
        directory = self.root / run_id
        paired = {"total": 500, "without_tools": {"accuracy": .75}, "with_tools": {"accuracy": .362}, "comparison": {"delta_pp": -38.8}, "items": [{"id": "one"}]}
        write_json(directory / "request.json", {"kind": "tool_evaluate", "adapter_run": "adapter-id"})
        write_json(directory / "status.json", {"status": "completed"})
        write_json(directory / "tool_report.json", paired)
        listed = json.load(self.request("/api/state"))["jobs"][0]
        self.assertEqual(listed["tool_result"]["with_tools"]["accuracy"], .362)
        self.assertNotIn("items", listed["tool_result"])
        from dashboard.config import run_path
        with patch("dashboard.server.run_path", side_effect=lambda value: run_path(value, self.root)):
            detail = json.load(self.request("/api/run?id=" + run_id))
            self.assertEqual(detail["tool_report"]["total"], 500)
            self.assertEqual(json.load(self.request("/api/tool-report?id=" + run_id)), paired)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runs_patch = patch("dashboard.server.RUNS", self.root)
        self.runs_patch.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.token = SESSION_TOKEN
        self.httpd.jobs = Jobs()
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self.httpd.server_close()
        self.runs_patch.stop()
        self.temp.cleanup()

    def request(self, path, body=None, *, method=None, token=True):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["X-Session-Token"] = SESSION_TOKEN
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        return urllib.request.urlopen(req, timeout=5)

    def assert_http_error(self, path, body=None, *, method=None, token=True, code=400):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request(path, body, method=method, token=token)
        self.assertEqual(raised.exception.code, code)
        return json.load(raised.exception)

    def patch_workbook_api(self, name, value):
        return patch.object(workbooks, name, value, create=True)

    def test_start_binds_the_workbook_and_adds_context_with_tools_on_and_off(self):
        context = MagicMock(return_value="Sheet: Sales\nA1: Revenue\nSample:\n2 | 10")
        dispatch_patch = patch("dashboard.server.dispatch_chat")
        dispatch = dispatch_patch.start()
        self.addCleanup(dispatch_patch.stop)
        with self.patch_workbook_api("workbook_context", context):
            for enabled in (False, True):
                result = self.request("/api/start", {
                    "kind": "chat", "messages": [{"role": "user", "content": "Summarize Sales."}],
                    "workbook_id": WORKBOOK_ID, "use_excel_tools": enabled,
                })
                created = json.load(result)
                request = read_json(self.root / created["id"] / "request.json")
                self.assertEqual(request["workbook_id"], WORKBOOK_ID)
                self.assertIs(request["use_excel_tools"], enabled)
                self.assertIn("Sheet: Sales", request["messages"][-1]["content"])
                self.assertIn("A1: Revenue", created["messages"][-1]["content"])
                write = read_json(self.root / created["id"] / "status.json")
                write.update(status="completed")
                (self.root / created["id"] / "status.json").write_text(json.dumps(write), encoding="utf-8")

        self.assertEqual(context.call_count, 2)
        self.assertEqual(dispatch.call_count, 2)

    def test_start_rejects_invalid_tool_flags_and_missing_active_workbook(self):
        self.assertIn("booleano", self.assert_http_error("/api/start", {
            "kind": "chat", "messages": [{"role": "user", "content": "Question"}],
            "use_excel_tools": 1,
        })["error"])
        self.assertIn("planilha", self.assert_http_error("/api/start", {
            "kind": "chat", "messages": [{"role": "user", "content": "Question"}],
            "use_excel_tools": True,
        })["error"])

    def test_tools_mode_does_not_duplicate_the_imported_attachment_context(self):
        attachments = [{"id": "source-id", "name": "source.xlsx", "kind": "xlsx", "context": "DUPLICATE_SOURCE_CONTEXT"},
                       {"id": "other-id", "name": "other.csv", "kind": "csv", "context": "OTHER_FILE_CONTEXT"}]
        with patch("dashboard.server.dispatch_chat"), \
             patch("dashboard.server.load_attachments", return_value=attachments), \
             patch.object(workbooks, "read_manifest", return_value={"source_attachment_id": "source-id"}), \
             patch.object(workbooks, "workbook_context", return_value="CURRENT_WORKBOOK_SAMPLE") as context:
            created = json.load(self.request("/api/start", {"kind": "chat", "messages": [{"role": "user", "content": "average"}], "workbook_id": WORKBOOK_ID, "use_excel_tools": True, "attachment_ids": ["source-id", "other-id"]}))
        message = created["messages"][-1]["content"]
        self.assertNotIn("DUPLICATE_SOURCE_CONTEXT", message)
        self.assertIn("OTHER_FILE_CONTEXT", message)
        self.assertIn("CURRENT_WORKBOOK_SAMPLE", message)
        context.assert_called_once_with(WORKBOOK_ID, runs_root=self.root, compact=True)

    def test_tool_evaluation_config_is_validated_and_persisted(self):
        from dashboard.tool_evaluation import validate_tool_eval

        self.assertEqual(validate_tool_eval({}), {"test_count": 10, "max_tool_calls": 4})
        for invalid in (
            {"test_count": True}, {"test_count": 501}, {"max_tool_calls": 5},
            {"unexpected": 1},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_tool_eval(invalid)

        engine = self.patch_workbook_api("engine_status", lambda: {"available": True})
        with engine, patch("dashboard.server.release_resident_model"), \
                patch("dashboard.server.subprocess.Popen") as spawn, \
                patch("dashboard.server.psutil.Process") as process:
            spawn.return_value.pid = 12345
            process.return_value.create_time.return_value = 1.0
            response = self.request("/api/start", {
                "kind": "tool_evaluate", "tool_eval": {"test_count": 12, "max_tool_calls": 2},
            })
            run = json.load(response)
            request = read_json(self.root / run["id"] / "request.json")
            self.assertEqual(request["tool_eval"], {"test_count": 12, "max_tool_calls": 2})
            spawn.assert_called_once()

        with self.patch_workbook_api("engine_status", lambda: {"available": True}):
            self.assertIn("test_count", self.assert_http_error("/api/start", {
                "kind": "tool_evaluate", "tool_eval": {"test_count": True},
            })["error"])

    def test_mutation_routes_require_session_authentication(self):
        self.assert_http_error("/api/workbooks/create", {}, token=False, code=403)
        self.assert_http_error("/api/workbooks/change", {
            "id": WORKBOOK_ID, "expected_revision": 0, "changes": [],
        }, token=False, code=403)
        self.assert_http_error("/api/workbooks/test", {
            "id": WORKBOOK_ID, "sheet": "Sales", "cell": "A1", "formula": "=1",
        }, token=False, code=403)

    def test_create_route_accepts_only_an_optional_server_side_attachment_id(self):
        create = MagicMock(return_value={"id": WORKBOOK_ID, "revision": 0, "name": "New workbook"})
        with self.patch_workbook_api("create_workbook", create):
            result = json.load(self.request("/api/workbooks/create", {}))
            self.assertEqual(result["revision"], 0)
            create.assert_called_once_with(None, runs_root=self.root)
            error = self.assert_http_error("/api/workbooks/create", {"path": "C:\\temp\\file.xlsx"})
            self.assertIn("somente", error["error"])
        self.assertEqual(create.call_count, 1)

    def test_workbook_snapshot_and_download_routes_return_the_current_workbook(self):
        download = self.root / "sample.xlsx"
        download.write_bytes(b"mock-xlsx-content")

        def get_workbook(workbook_id, runs_root=None):
            if workbook_id != WORKBOOK_ID:
                raise ValueError("Pasta de trabalho não encontrada.")
            return {"id": workbook_id, "revision": 4, "name": "Sales", "sheets": []}

        def download_workbook(workbook_id, runs_root=None):
            if workbook_id != WORKBOOK_ID:
                raise ValueError("Pasta de trabalho não encontrada.")
            return download

        get_workbook_patch = self.patch_workbook_api("get_workbook", get_workbook)
        download_path_patch = self.patch_workbook_api("workbook_download_path", download_workbook)
        with get_workbook_patch, download_path_patch:
            with self.request(f"/api/workbooks?id={WORKBOOK_ID}") as response:
                snapshot = json.load(response)
            self.assertEqual(snapshot["revision"], 4)
            self.assertEqual(snapshot["id"], WORKBOOK_ID)
            with self.request(f"/api/workbooks/download?id={WORKBOOK_ID}") as response:
                self.assertEqual(response.read(), b"mock-xlsx-content")
                self.assertEqual(response.headers.get_content_type(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.assertIn("planilha.xlsx", response.headers["Content-Disposition"])
            self.assertIn("não encontrada", self.assert_http_error("/api/workbooks/download?id=..%2F..%2Fsecrets")["error"])

    def test_change_passes_revision_and_rejects_stale_revision(self):
        def mutate(workbook_id, changes, expected_revision, runs_root=None):
            if expected_revision != 4:
                raise ValueError("A revisão mudou; atualize a planilha antes de editar.")
            return {"id": workbook_id, "revision": 5, "changes": changes}

        with self.patch_workbook_api("mutate_workbook", mutate):
            response = self.request("/api/workbooks/change", {
                "id": WORKBOOK_ID, "expected_revision": 4,
                "changes": [{"sheet": "Sales", "cell": "B2", "value": 12}],
            })
            self.assertEqual(json.load(response)["revision"], 5)
            error = self.assert_http_error("/api/workbooks/change", {
                "id": WORKBOOK_ID, "expected_revision": 3,
                "changes": [{"sheet": "Sales", "cell": "B2", "value": 12}],
            })
            self.assertIn("revisão mudou", error["error"])

    def test_formula_test_route_and_busy_workbook_rejection(self):
        test_formula = MagicMock(return_value={
            "ok": True, "formula": "=SUM(A1:A2)", "formula_local": "=SOMA(A1:A2)",
            "value": 10, "error": None, "sheet": "Sales", "cell": "B2", "engine": "Microsoft Excel",
        })
        with self.patch_workbook_api("test_formula", test_formula):
            result = json.load(self.request("/api/workbooks/test", {
                "id": WORKBOOK_ID, "sheet": "Sales", "cell": "B2", "formula": "=SUM(A1:A2)",
            }))
            self.assertTrue(result["ok"])
            test_formula.assert_called_once_with(WORKBOOK_ID, "Sales", "B2", "=SUM(A1:A2)", runs_root=self.root)

        self.httpd.jobs = SimpleNamespace(
            lock=threading.RLock(),
            list=lambda: [{"workbook_id": WORKBOOK_ID, "status": "running"}],
        )
        mutate = MagicMock()
        formula = MagicMock()
        with self.patch_workbook_api("mutate_workbook", mutate), self.patch_workbook_api("test_formula", formula):
            for path, body in (
                ("/api/workbooks/change", {"id": WORKBOOK_ID, "expected_revision": 4, "changes": []}),
                ("/api/workbooks/test", {"id": WORKBOOK_ID, "sheet": "Sales", "cell": "B2", "formula": "=1"}),
            ):
                error = self.assert_http_error(path, body)
                self.assertIn("Aguarde", error["error"])
        mutate.assert_not_called()
        formula.assert_not_called()


if __name__ == "__main__":
    unittest.main()
