import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

from benchmark.run_baseline import MODEL_REVISION, prompt_fingerprint, score_rows
from dashboard.config import DEFAULTS, read_json, run_path, validate_config, write_json
from dashboard.data import CausalCollator, encode_row, select_encoded, stratified_order, validate_splits
from dashboard.runtime import Cancelled, Events, worker_lock
from dashboard.server import Handler, Jobs, ThreadingHTTPServer, read_events
from dashboard.worker import comparable, evaluate, local_snapshot


def row(name, level="easy", formula="=SUM(A2:A3)", table=None):
    return {"id": name, "instruction": "Sum A", "context_text": "A: amount", "expected": formula, "source_id": f"{table or name}#0", "level": level}


class FakeTokenizer:
    def apply_chat_template(self, messages, tokenize, add_generation_prompt):
        prompt = [10, 11, 12]
        if add_generation_prompt:
            return {"input_ids": prompt}
        return {"input_ids": prompt + list(range(len(messages[-1]["content"]))) + [99]}


class ConfigurationTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(validate_config({}), DEFAULTS)

    def test_rejects_invalid_values_and_shell_input(self):
        for config in ({"learning_rate": float("nan")}, {"epochs": 1.5}, {"epochs": True}, {"batch_size": 0}, {"seed": "42; whoami"}, {"command": "anything"}, {"exclude_suspect": 1}, {"exclude_seen": 1}, {"gpu_memory_fraction": 1}, {"eval_batch_size": 9}, []):
            with self.subTest(config=config), self.assertRaises(ValueError):
                validate_config(config)

    def test_rejects_arbitrary_run_paths(self):
        for value in ("../dataset", "C:/tmp", "", None):
            with self.assertRaises(ValueError):
                run_path(value)


class DataTests(unittest.TestCase):
    def test_supervision_excludes_prompt_and_includes_eos(self):
        encoded = encode_row(FakeTokenizer(), row("1"))
        self.assertEqual(encoded["labels"][:3], [-100] * 3)
        self.assertEqual(encoded["labels"][3:], encoded["input_ids"][3:])
        self.assertEqual(encoded["labels"][-1], 99)

    def test_template_mismatch_fails(self):
        class BadTokenizer(FakeTokenizer):
            def apply_chat_template(self, *args, **kwargs):
                return {"input_ids": [1] if kwargs["add_generation_prompt"] else [2, 3]}
        with self.assertRaises(ValueError):
            encode_row(BadTokenizer(), row("1"))

    def test_sampling_reproducible_and_stratified(self):
        rows = [row(str(i), "easy" if i < 60 else "hard") for i in range(100)]
        first = stratified_order(rows, 42)
        self.assertEqual(first, stratified_order(rows, 42))
        self.assertNotEqual(first, stratified_order(rows, 43))
        self.assertEqual(sum(r["level"] == "easy" for r in first[:10]), 6)
        self.assertEqual(len({r["id"] for r in first}), 100)

    def test_exclusions_replaced_without_truncating(self):
        rows = [row("bad", formula="=SUMX(A2:A3)"), row("long", formula="=" + "A" * 200), row("good")]
        selected, encoded, stats = select_encoded(rows, 1, {**DEFAULTS, "max_length": 128}, FakeTokenizer(), lambda *args: None)
        self.assertEqual(selected[0]["id"], "good")
        self.assertEqual(stats["suspect_excluded"], 1)
        self.assertTrue(all(len(e["input_ids"]) <= 128 for e in encoded))
        with self.assertRaises(ValueError):
            select_encoded(rows, 2, {**DEFAULTS, "max_length": 128}, FakeTokenizer(), lambda *args: None)

    def test_splits_reject_leakage_and_changed_test(self):
        tests = [row(f"test-{i}") for i in range(500)]
        splits = {"train": [row("train")], "validation": [row("validation")], "test": tests}
        ids = [r["id"] for r in tests]
        validate_splits(splits, ids)
        with self.assertRaises(ValueError):
            validate_splits(splits, ids[::-1])
        splits["train"] = [row("train", table="test-1")]
        with self.assertRaises(ValueError):
            validate_splits(splits, ids)

    def test_collator_masks_padding(self):
        encoded = encode_row(FakeTokenizer(), row("1"))
        short = {"input_ids": [1, 2], "attention_mask": [1, 1], "labels": [-100, 2]}
        batch = CausalCollator(0)([encoded, short])
        self.assertEqual(batch["labels"][1, 2:].tolist(), [-100] * (len(encoded["input_ids"]) - 2))
        self.assertEqual(batch["attention_mask"][1, 2:].sum().item(), 0)


class RuntimeTests(unittest.TestCase):
    def test_phase_progress_resets_eta_and_does_not_reuse_training_steps(self):
        with tempfile.TemporaryDirectory() as tmp, patch("dashboard.runtime.time.time") as clock:
            clock.return_value = 100
            events = Events(Path(tmp))
            events.emit(phase="Treinando", phase_step=0, phase_total=10)
            clock.return_value = 110
            events.emit(phase="Treinando", phase_step=10, phase_total=10, step=1)
            clock.return_value = 111
            events.emit(phase="Validando", phase_step=0, phase_total=500, phase_unit="exemplos")
            clock.return_value = 116
            events.emit(phase="Validando", phase_step=10, phase_total=500, phase_unit="exemplos")
            events.emit(step=1, eval_loss=2.2)
            events.emit(phase="Salvando checkpoint da época")
            records = read_events(Path(tmp) / "events.jsonl")
            self.assertEqual(records[1]["phase_eta_seconds"], 0)
            self.assertEqual(records[2]["phase_started_at"], 111)
            self.assertIsNone(records[2]["phase_eta_seconds"])
            self.assertEqual(records[3]["phase_rate"], 2)
            self.assertEqual(records[3]["phase_eta_seconds"], 245)
            self.assertNotIn("phase", records[4])
            self.assertIsNone(records[5]["phase_total"])
            self.assertIsNone(records[5]["phase_eta_seconds"])

    def test_events_and_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            events = Events(path)
            events.emit(step=1, loss=1.2)
            with (path / "events.jsonl").open("a") as handle:
                handle.write('{"incomplete":')
            self.assertEqual(len(read_events(path / "events.jsonl")), 1)
            with self.assertRaises(ValueError):
                events.emit(loss=float("nan"))
            (path / "cancel.request").touch()
            with self.assertRaises(Cancelled):
                events.emit(step=2)

    def test_worker_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp, patch("dashboard.runtime.RUNS", Path(tmp)):
            with worker_lock():
                with self.assertRaises(OSError):
                    with worker_lock():
                        self.fail("Acquired twice")
            with worker_lock():
                pass

    def test_comparison_checks_prompt_and_gold(self):
        rows = [row("a"), row("b", formula="=COUNTA(B:B)")]
        report = score_rows(rows, [item["expected"] for item in rows], "test")
        self.assertTrue(comparable(report, rows))
        for field, value, reordered in (
            ("instruction", "different instruction", False),
            ("context_text", "different context", False),
            ("expected", "=1", False),
            ("id", "different id", False),
            (None, None, True),
        ):
            changed = [dict(item) for item in rows]
            if reordered:
                changed.reverse()
            else:
                changed[0][field] = value
            with self.subTest(field=field, reordered=reordered):
                self.assertFalse(comparable(report, changed))
        changed_report = json.loads(json.dumps(report))
        changed_report["prompt_fingerprint"] = "different"
        self.assertFalse(comparable(changed_report, rows))
        changed_report = json.loads(json.dumps(report))
        changed_report["model_revision"] = "different-revision"
        self.assertFalse(comparable(changed_report, rows))

    def test_local_snapshot_is_pinned_instead_of_following_main(self):
        config_path = Path("cache") / "snapshots" / MODEL_REVISION / "config.json"
        with patch("huggingface_hub.hf_hub_download", return_value=str(config_path)) as download:
            self.assertEqual(local_snapshot(), config_path.parent)
        self.assertEqual(download.call_args.kwargs, {"revision": MODEL_REVISION, "local_files_only": True})

    def test_legacy_report_requires_complete_ordered_input_evidence(self):
        rows = [row("a"), row("b", formula="=COUNTA(B:B)")]
        report = score_rows(rows, ["=1", "=2"], "test")
        report.pop("evaluation_identity")
        report.pop("evaluation_fingerprint")
        for item, source in zip(report["items"], rows):
            item.update(id=source["id"], instruction=source["instruction"], context_text=source["context_text"])
        self.assertTrue(comparable(report, rows))
        missing_revision = {key: value for key, value in report.items() if key != "model_revision"}
        self.assertFalse(comparable(missing_revision, rows))
        report["items"][0].pop("context_text")
        self.assertFalse(comparable(report, rows))

    def test_evaluation_loads_adapter_and_generates_fresh_predictions(self):
        rows = [row(f"test-{i}", formula="=1") for i in range(500)]
        original = score_rows(rows, ["=0"] * 500, "test")
        hashes = {"test": "test-hash", "train": "train-hash", "validation": "val-hash"}
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            trained = directory / "trained"
            (trained / "adapter").mkdir(parents=True)
            write_json(trained / "adapter/adapter_config.json", {})
            write_json(trained / "request.json", {"kind": "train"})
            write_json(trained / "status.json", {"status": "completed"})
            write_json(trained / "selection.json", {"data_sha256": hashes, "prompt_fingerprint": prompt_fingerprint()})
            model = MagicMock()
            def read(path, default=None):
                return original if path.name == "baseline.json" else read_json(path, default)
            with patch("dashboard.worker.load_splits", return_value=({"test": rows}, hashes)), \
                 patch("dashboard.worker.read_json", side_effect=read), \
                 patch("dashboard.worker.run_path", return_value=trained), \
                 patch("dashboard.worker.load_model", return_value=model), \
                 patch("dashboard.worker.limit_training_memory", return_value={"gpu_budget_gb": 6.4}) as limit, \
                 patch("dashboard.worker.training_memory", return_value={"vram_gb": 2.6, "vram_reserved_gb": 4.0, "vram_peak_gb": 3.5}), \
                 patch("peft.PeftModel.from_pretrained", return_value=model) as load_adapter, \
                 patch("dashboard.worker.baseline.generate_token_batch", side_effect=lambda m, t, seq, torch: ["=1"] * len(seq)) as generate:
                evaluate({"adapter_run": "trained"}, FakeTokenizer(), directory, Events(directory))
            load_adapter.assert_called_once_with(model, trained / "adapter", is_trainable=False)
            self.assertTrue(generate.called)
            self.assertTrue(all(len(call.args[2]) == 1 for call in generate.call_args_list))
            self.assertEqual(limit.call_args.kwargs["operation"], "avaliação")
            report = read_json(directory / "report.json")
            self.assertEqual(report["total"], 500)
            self.assertEqual(report["comparison"]["gained"], 500)
            self.assertEqual(report["comparison"]["lost"], 0)
            self.assertEqual(report["adapter_run"], "trained")
            self.assertEqual(report["eval_batch_size"], 1)
            self.assertEqual(report["memory"]["vram_peak_gb"], 3.5)
            self.assertEqual(len((directory / "predictions.jsonl").read_text().splitlines()), 500)

    def test_evaluation_oom_splits_batches_without_duplicating_predictions(self):
        import torch
        rows = [row(f"test-{i}", formula="=1") for i in range(500)]
        original = score_rows(rows, ["=0"] * 500, "test")
        sizes = []

        def generate(model, tokenizer, sequences, module):
            sizes.append(len(sequences))
            if len(sequences) > 2:
                raise torch.OutOfMemoryError("simulated")
            return ["=1"] * len(sequences)

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with patch("dashboard.worker.load_splits", return_value=({"test": rows}, {"test": "hash"})), \
                 patch("dashboard.worker.read_json", return_value=original), \
                 patch("dashboard.worker.load_model", return_value=MagicMock()), \
                 patch("dashboard.worker.limit_training_memory", return_value={"gpu_budget_gb": 6.4}), \
                 patch("dashboard.worker.training_memory", return_value={"vram_gb": 2.6}), \
                 patch("dashboard.worker.baseline.generate_token_batch", side_effect=generate), \
                 patch("torch.cuda.empty_cache") as clear:
                evaluate({"config": {"eval_batch_size": 4}}, FakeTokenizer(), directory, Events(directory))
            self.assertEqual(sizes[:3], [4, 2, 2])
            self.assertTrue(clear.called)
            predictions = [json.loads(line) for line in (directory / "predictions.jsonl").read_text().splitlines()]
            self.assertEqual(len(predictions), 500)
            self.assertEqual(len({p["id"] for p in predictions}), 500)
            self.assertEqual(read_json(directory / "report.json")["correct"], 500)

    def test_evaluation_singleton_oom_preserves_test_context_and_fails(self):
        import torch
        rows = [row("test", formula="=1")]
        original = score_rows(rows, ["=0"], "test")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with patch("dashboard.worker.load_splits", return_value=({"test": rows}, {})), \
                 patch("dashboard.worker.read_json", return_value=original), \
                 patch("dashboard.worker.load_model", return_value=MagicMock()), \
                 patch("dashboard.worker.limit_training_memory", return_value={"gpu_budget_gb": 6.4}), \
                 patch("dashboard.worker.baseline.generate_token_batch", side_effect=torch.OutOfMemoryError("simulated")) as generate:
                with self.assertRaisesRegex(RuntimeError, "contexto do teste não será truncado"):
                    evaluate({}, FakeTokenizer(), directory, Events(directory))
            self.assertEqual(generate.call_count, 1)
            self.assertFalse((directory / "predictions.jsonl").exists())
            self.assertFalse((directory / "report.json").exists())


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.runs_patch = patch("dashboard.server.RUNS", Path(cls.temp.name))
        cls.runs_patch.start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.token = "test-token"
        cls.server.jobs = Jobs()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.runs_patch.stop()
        cls.temp.cleanup()

    def request(self, path, data=None, headers=None):
        request = urllib.request.Request(self.url + path, data=json.dumps(data).encode() if data is not None else None, headers=headers or {})
        return urllib.request.urlopen(request, timeout=5)

    def test_static_and_state(self):
        with self.request("/") as response:
            self.assertIn(b"Formula Lab", response.read())
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        with tempfile.TemporaryDirectory() as tmp, patch("dashboard.server.ROOT", Path(tmp)):
            write_json(Path(tmp) / "benchmark/results/baseline.json", {"total": 500, "correct": 3})
            with self.request("/api/state") as response:
                state = json.load(response)
                self.assertEqual(state["token"], "test-token")
                self.assertEqual(state["baseline"]["total"], 500)
                self.assertEqual(state["baseline"]["correct"], 3)
            (Path(tmp) / "benchmark/results/baseline.json").unlink()
            with self.request("/api/state") as response:
                self.assertIsNone(json.load(response)["baseline"]["total"])

    def test_mutation_requires_token_and_local_origin(self):
        for headers in ({}, {"X-Session-Token": "test-token", "Origin": "https://evil.example"}):
            with self.assertRaises(urllib.error.HTTPError) as exc:
                self.request("/api/start", {"kind": "train"}, {"Content-Type": "application/json", **headers})
            self.assertEqual(exc.exception.code, 403)

    def test_rebinding_host_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.request("/api/state", headers={"Host": "evil.example"})
        self.assertEqual(exc.exception.code, 403)

    def test_invalid_start_does_not_spawn(self):
        with patch("dashboard.server.subprocess.Popen") as spawn:
            with self.assertRaises(urllib.error.HTTPError) as exc:
                self.request("/api/start", {"kind": "train", "config": {"epochs": -1}}, {"Content-Type": "application/json", "X-Session-Token": "test-token"})
            self.assertEqual(exc.exception.code, 400)
            spawn.assert_not_called()

    def test_unknown_files_not_served(self):
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.request("/api/download?id=anything&file=../../.git/config")
        self.assertEqual(exc.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
