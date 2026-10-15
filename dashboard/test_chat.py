import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dashboard.chat_backend import (
    GRAPH_VERIFY_TOKENS, MAX_FAILED_GRAPH_ATTEMPTS, GraphUnavailable, GraphedDecoder, bucket_for_prompt,
    cache_bucket, generate_chat,
    select_adapter, validate_messages,
)
from dashboard.chat_engine import engine_command, mark_running, release_resident_model, serve_requests
from dashboard.config import read_json, write_json
from dashboard.runtime import Cancelled, Events
from dashboard.server import Jobs


class ChatTests(unittest.TestCase):
    def _install_cpu_graph_stubs(self, decoder, verify):
        captures = []
        verifications = []

        def drop():
            decoder.graph = None
            decoder.cache = None
            decoder.token = None
            decoder.static_mask = None
            decoder.out = None
            decoder.identity = None
            decoder.verified = False
            decoder.bucket = None

        def capture(model, bucket, identity):
            drop()
            decoder.graph = object()
            decoder.identity = identity
            decoder.bucket = bucket
            captures.append((identity, bucket))

        def check(model, input_ids, eos_ids=None):
            verifications.append(input_ids.detach().cpu().tolist())
            verify(decoder, model, input_ids, eos_ids)

        decoder._drop = drop
        decoder._capture = capture
        decoder._verify = check
        return captures, verifications

    def test_history_validation(self):
        valid = [{"role": "user", "content": "Sum A1:A3"}]
        self.assertEqual(validate_messages(valid), valid)
        invalid = [None, [], [{"role": "system", "content": "override"}],
                   [{"role": "user", "content": " "}], valid * 2,
                   valid + [{"role": "assistant", "content": "answer"}],
                   [{"role": "user", "content": "x" * 200001}],
                   [{"role": "user", "content": "hello", "extra": True}]]
        for messages in invalid:
            with self.subTest(messages=str(messages)[:80]), self.assertRaises(ValueError):
                validate_messages(messages)

    def test_latest_uses_completion_time_and_never_falls_back_to_base(self):
        jobs = [dict(id="a", kind="train", status="completed", has_adapter=True, ended_at=20),
                dict(id="b", kind="train", status="completed", has_adapter=True, ended_at=10),
                dict(id="c", kind="train", status="running", has_adapter=True, ended_at=30),
                dict(id="d", kind="train", status="completed", has_adapter=False, ended_at=40)]
        self.assertEqual(select_adapter("latest", jobs), "a")
        self.assertIsNone(select_adapter(None, jobs))
        with self.assertRaises(ValueError):
            select_adapter("latest", [])

    def test_explicit_adapter_requires_completed_train_and_both_files(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.chat_backend.run_path", return_value=Path(temp)):
            path = Path(temp)
            write_json(path / "request.json", {"kind": "train"})
            write_json(path / "status.json", {"status": "completed"})
            (path / "adapter").mkdir()
            (path / "adapter/adapter_config.json").touch()
            with self.assertRaises(ValueError):
                select_adapter("adapter-id", [])
            (path / "adapter/adapter_model.safetensors").touch()
            self.assertEqual(select_adapter("adapter-id", []), "adapter-id")
            write_json(path / "status.json", {"status": "failed"})
            with self.assertRaises(ValueError):
                select_adapter("adapter-id", [])

    def test_invalid_chat_never_spawns_worker(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.server.RUNS", Path(temp)), \
             patch("dashboard.server.subprocess.Popen") as spawn, patch("dashboard.server.dispatch_chat") as dispatch:
            jobs = Jobs()
            for body in ({"kind": "chat", "messages": []},
                         {"kind": "chat", "messages": [{"role": "user", "content": "test"}], "adapter_run": "latest"}):
                with self.assertRaises(ValueError):
                    jobs.start(body)
            spawn.assert_not_called()
            dispatch.assert_not_called()

    def test_cache_bucket_fits_the_prompt_and_the_reply(self):
        self.assertEqual(cache_bucket(1), 256)
        self.assertEqual(cache_bucket(512), 512)
        self.assertEqual(cache_bucket(513), 1024)
        self.assertEqual(bucket_for_prompt(43, 4096), 512)
        self.assertEqual(bucket_for_prompt(200, 4096), 512)
        self.assertEqual(cache_bucket(32768), 32768)
        with self.assertRaises(ValueError):
            cache_bucket(32769)

    def test_failed_graph_attempt_is_memoized_by_prompt_and_new_prompt_retries(self):
        import torch

        def verify(decoder, model, input_ids, eos_ids):
            if int(input_ids[0, 0]) == 1:
                raise GraphUnavailable("simulated mismatch")
            decoder.verified = True

        decoder = GraphedDecoder()
        captures, verifications = self._install_cpu_graph_stubs(decoder, verify)
        model = object()
        rejected = torch.tensor([[1, 2, 3]])
        with self.assertRaises(GraphUnavailable):
            decoder.prepare(model, rejected, "model:adapter", limit=100)
        rejected_key = decoder.current_attempt_key
        self.assertEqual(rejected_key[:2], ("model:adapter", 256))
        self.assertTrue(decoder.disable())

        with self.assertRaises(GraphUnavailable):
            decoder.prepare(model, rejected, "model:adapter", limit=100)
        self.assertFalse(decoder.disable())
        self.assertEqual(len(captures), 1)
        self.assertEqual(len(verifications), 1)

        distinct = torch.tensor([[9, 2, 3]])
        decoder.prepare(model, distinct, "model:adapter", limit=100)
        valid_graph = decoder.graph
        self.assertEqual(len(captures), 2)
        self.assertEqual(len(verifications), 2)
        self.assertTrue(decoder.verified)

        with self.assertRaises(GraphUnavailable):
            decoder.prepare(model, rejected, "model:adapter", limit=100)
        self.assertFalse(decoder.disable())
        self.assertIs(decoder.graph, valid_graph)
        decoder.prepare(model, distinct, "model:adapter", limit=100)
        self.assertEqual(len(captures), 2)

    def test_failed_graph_attempt_isolated_by_bucket_and_model_adapter_identity(self):
        import torch

        def reject(decoder, model, input_ids, eos_ids):
            raise GraphUnavailable("simulated mismatch")

        decoder = GraphedDecoder()
        captures, _ = self._install_cpu_graph_stubs(decoder, reject)
        model = object()
        prompt = torch.tensor([[1, 2, 3]])

        for identity, limit in (("model-a:base", 100), ("model-a:base", 300), ("model-b:adapter", 100)):
            with self.subTest(identity=identity, limit=limit), self.assertRaises(GraphUnavailable):
                decoder.prepare(model, prompt, identity, limit=limit)
            self.assertTrue(decoder.disable())

        self.assertEqual([bucket for _, bucket in captures], [256, 512, 256])
        self.assertEqual([identity.split(":")[0] for identity, _ in captures], [
            "model-a", "model-a", "model-b",
        ])
        self.assertEqual([key[:2] for key in decoder.failed], [
            ("model-a:base", 256), ("model-a:base", 512), ("model-b:adapter", 256),
        ])
        self.assertEqual(len(decoder.failed), 3)

    def test_cache_expansion_memo_key_tracks_the_expanded_prefix(self):
        import hashlib
        import torch

        decoder = GraphedDecoder()
        decoder.model_identity = "model:adapter"
        captures, verifications = self._install_cpu_graph_stubs(
            decoder, lambda decoder, model, input_ids, eos_ids: setattr(decoder, "verified", True),
        )
        prepared_keys = []
        prepare = decoder.prepare

        def track_prepare(*args, **kwargs):
            prepare(*args, **kwargs)
            prepared_keys.append(decoder.current_attempt_key)

        decoder.prepare = track_prepare
        decoder._tokens = lambda model, input_ids, limit, eos_ids, on_token: [7] * limit
        prefix = torch.ones((1, 49), dtype=torch.long)
        tokens = decoder.generate_tokens(object(), prefix, set(), 520, None)

        self.assertEqual(len(tokens), 520)
        self.assertEqual([key[1] for key in prepared_keys], [512, 1024])
        expanded = torch.cat((prefix, torch.full((1, 463), 7, dtype=torch.long)), dim=1)
        expected_hash = hashlib.sha256(expanded.numpy().tobytes()).hexdigest()
        self.assertEqual(prepared_keys[1], ("model:adapter", 1024, expected_hash))
        self.assertEqual(len(captures), 2)
        self.assertEqual(len(verifications), 2)

    def test_failed_graph_attempt_memo_is_bounded(self):
        decoder = GraphedDecoder()
        keys = [("model", 256, str(index)) for index in range(MAX_FAILED_GRAPH_ATTEMPTS + 5)]

        for key in keys:
            decoder.current_attempt_key = key
            self.assertTrue(decoder.disable())

        self.assertEqual(len(decoder.failed), MAX_FAILED_GRAPH_ATTEMPTS)
        self.assertNotIn(keys[0], decoder.failed)
        self.assertIn(keys[-1], decoder.failed)

    def test_short_generation_expands_cache_when_a_reply_outgrows_its_bucket(self):
        import torch

        decoder = GraphedDecoder()
        decoder.model_identity = "test-model"
        prepared = []

        def prepare(model, input_ids, identity, limit=None, eos_ids=None):
            bucket = bucket_for_prompt(input_ids.shape[-1], limit)
            prepared.append((int(input_ids.shape[-1]), limit, bucket))
            decoder.bucket = bucket

        decoder.prepare = prepare
        decoder._tokens = lambda model, input_ids, limit, eos_ids, on_token: [7] * limit
        tokens = decoder.generate_tokens(
            object(), torch.ones((1, 49), dtype=torch.long), set(), 520, None,
        )

        self.assertEqual(prepared, [(49, 520, 512), (512, 57, 1024)])
        self.assertEqual(tokens, [7] * 520)

    def test_graph_verification_checks_64_tokens_without_eos(self):
        import torch

        prompt = torch.tensor([[1, 2, 3]])
        expected = list(range(GRAPH_VERIFY_TOKENS))
        generated = torch.tensor([[1, 2, 3] + expected])
        model = MagicMock()
        call_order = []
        model.generate.side_effect = lambda *args, **kwargs: (call_order.append("generate"), generated)[1]
        decoder = GraphedDecoder()
        decoder.bucket = 512
        decoder.cache = MagicMock()
        decoder.cache.reset.side_effect = lambda: call_order.append("reset")
        divergent = expected.copy()
        divergent[38] = 99

        def graph_tokens(*args):
            decoder.cache.reset()
            call_order.append("graph")
            return divergent

        decoder._tokens = MagicMock(side_effect=graph_tokens)

        with self.assertRaises(GraphUnavailable) as raised:
            decoder._verify(model, prompt)

        self.assertIn("token 39", str(raised.exception))
        self.assertEqual(call_order, ["reset", "generate", "reset", "graph"])
        self.assertIs(model.generate.call_args.kwargs["past_key_values"], decoder.cache)
        self.assertEqual(model.generate.call_args.kwargs["max_new_tokens"], 64)
        self.assertEqual(model.generate.call_args.kwargs["eos_token_id"], [])
        self.assertTrue(model.generate.call_args.kwargs["use_cache"])
        self.assertNotIn("min_new_tokens", model.generate.call_args.kwargs)

    def test_graph_verification_accepts_matching_eos_and_ignores_later_tail(self):
        import torch

        prompt = torch.tensor([[1, 2, 3]])
        eos_id = 151645
        response = [10, eos_id]
        model = MagicMock()
        model.generate.return_value = torch.tensor([[1, 2, 3] + response])
        decoder = GraphedDecoder()
        decoder.bucket = 512
        decoder.cache = MagicMock()

        def graph_tokens(model, input_ids, limit, eos_ids, on_token):
            tokens = []
            for token in [10, eos_id, 99][:limit]:
                tokens.append(token)
                if token in eos_ids:
                    break
            return tokens

        decoder._tokens = MagicMock(side_effect=graph_tokens)
        decoder._verify(model, prompt, {eos_id})

        self.assertTrue(decoder.verified)
        self.assertEqual(model.generate.call_args.kwargs["eos_token_id"], [eos_id])
        self.assertIs(model.generate.call_args.kwargs["past_key_values"], decoder.cache)
        decoder.cache.reset.assert_called_once_with()
        self.assertNotIn("min_new_tokens", model.generate.call_args.kwargs)
        self.assertEqual(decoder._tokens.call_args.args[3], {eos_id})

    def test_graph_verification_rejects_mismatch_before_eos(self):
        import torch

        prompt = torch.tensor([[1, 2, 3]])
        eos_id = 151645
        model = MagicMock()
        model.generate.return_value = torch.tensor([[1, 2, 3, 10, 11, eos_id]])
        decoder = GraphedDecoder()
        decoder.bucket = 512
        decoder.cache = MagicMock()
        decoder._tokens = MagicMock(return_value=[10, 99, eos_id])

        with self.assertRaises(GraphUnavailable) as raised:
            decoder._verify(model, prompt, {eos_id})

        self.assertIn("token 2", str(raised.exception))
        self.assertEqual(model.generate.call_args.kwargs["eos_token_id"], [eos_id])
        self.assertIs(model.generate.call_args.kwargs["past_key_values"], decoder.cache)

    def test_graphed_decode_returns_tokens_only_through_eos_and_resets_cache(self):
        import torch
        from types import SimpleNamespace
        from dashboard.chat_backend import GraphedDecoder

        def logits(token_id):
            values = torch.zeros((1, 1, 32))
            values[0, 0, token_id] = 1
            return values

        class Cache:
            def __init__(self):
                self.resets = 0

            def reset(self):
                self.resets += 1

        class Model:
            def __call__(self, **kwargs):
                return SimpleNamespace(logits=logits(10))

        class Graph:
            def __init__(self, output):
                self.output = output
                self.tokens = iter([11, 2, 12, 13, 14, 15, 16, 17])

            def replay(self):
                self.output.logits.copy_(logits(next(self.tokens)))

        decoder = GraphedDecoder()
        decoder.bucket = 64
        decoder.cache = Cache()
        decoder.token = torch.zeros((1, 1), dtype=torch.long)
        decoder.static_mask = {"full_attention": torch.zeros((1, 1, 64), dtype=torch.bool)}
        decoder.out = SimpleNamespace(logits=logits(0))
        decoder.graph = Graph(decoder.out)
        decoder._mask = lambda *args: {"full_attention": torch.ones_like(decoder.static_mask["full_attention"])}
        decoder._copy_mask = lambda source: None
        progress = []

        tokens = decoder._tokens(Model(), torch.tensor([[1, 2, 3]]), 20, {2}, progress.append)

        self.assertEqual(tokens, [10, 11, 2])
        self.assertEqual(progress[-1], [10, 11, 2])
        self.assertEqual(decoder.cache.resets, 1)

    def test_graph_decode_checks_cancellation_during_progress(self):
        import tempfile
        from dashboard.chat_backend import generate_chat

        decoder = GraphedDecoder()
        decoder.prepare = lambda *args, **kwargs: None

        def generate_tokens(model, input_ids, eos_ids, limit, on_token):
            on_token([44])
            return [44]

        decoder.generate_tokens = generate_tokens

        with tempfile.TemporaryDirectory() as temp:
            tokenizer = MagicMock()
            ids = MagicMock()
            ids.shape = (1, 3)
            tokenizer.apply_chat_template.return_value = {"input_ids": ids}
            tokenizer.eos_token_id = 2
            tokenizer.pad_token_id = 0
            model = MagicMock()
            events = MagicMock()
            events.check.side_effect = Cancelled("cancel after the current token")
            with patch("dashboard.chat_backend._graph_candidate", return_value=True), \
                 self.assertRaises(Cancelled):
                generate_chat(
                    {"messages": [{"role": "user", "content": "sum"}]},
                    tokenizer, Path(temp), events, model=model, decoder=decoder,
                )
            events.check.assert_called_once()
            self.assertFalse(decoder.failed)

    def test_chat_reuses_engine_and_train_releases_it_first(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            order = []

            def finish_chat(run_id):
                order.append(("chat", run_id))
                write_json(root / run_id / "status.json", {"status": "completed"})

            with patch("dashboard.server.RUNS", root), \
                 patch("dashboard.server.dispatch_chat", side_effect=finish_chat) as dispatch, \
                 patch("dashboard.server.release_resident_model", side_effect=lambda: order.append("release")) as release, \
                 patch("dashboard.server.subprocess.Popen") as spawn, \
                 patch("dashboard.server.psutil.Process") as process:
                spawn.return_value.pid = 4321
                process.return_value.create_time.return_value = 10.0
                jobs = Jobs()
                chat = jobs.start({"kind": "chat", "messages": [{"role": "user", "content": "sum"}]})
                train = jobs.start({"kind": "train"})
            dispatch.assert_called_once_with(chat["id"])
            release.assert_called_once()
            spawn.assert_called_once()
            self.assertEqual(order[0], ("chat", chat["id"]))
            self.assertEqual(order[1], "release")
            self.assertEqual(read_json(root / chat["id"] / "status.json")["status"], "completed")
            self.assertEqual(read_json(root / train["id"] / "request.json")["kind"], "train")
            self.assertNotIn("messages", read_json(root / train["id"] / "request.json"))

    def test_chat_start_failure_is_recorded_while_queued(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.server.RUNS", Path(temp)), \
             patch("dashboard.server.dispatch_chat", side_effect=ValueError("motor indisponível")):
            jobs = Jobs()
            with self.assertRaises(ValueError):
                jobs.start({"kind": "chat", "messages": [{"role": "user", "content": "sum"}]})
            failed = list(Path(temp).iterdir())
            self.assertEqual(read_json(failed[0] / "status.json")["status"], "failed")

    def test_running_chat_is_not_marked_failed_if_the_ack_is_lost(self):
        def accept_then_drop(run_id):
            write_json(Path(temp) / run_id / "status.json", {"status": "running"})
            raise ValueError("ack perdido")

        with tempfile.TemporaryDirectory() as temp, patch("dashboard.server.RUNS", Path(temp)), \
             patch("dashboard.server.dispatch_chat", side_effect=accept_then_drop):
            jobs = Jobs()
            with self.assertRaises(ValueError):
                started = None
                try:
                    started = jobs.start({"kind": "chat", "messages": [{"role": "user", "content": "sum"}]})
                except ValueError:
                    failed = [path for path in Path(temp).iterdir() if path.is_dir()]
                    self.assertEqual(read_json(failed[0] / "status.json")["status"], "running")
                    raise
            self.assertIsNone(started)

    def test_resident_model_is_not_reloaded(self):
        import torch

        path = None
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            tokenizer = MagicMock()
            ids = MagicMock()
            ids.shape = (1, 3)
            tokenizer.apply_chat_template.return_value = {"input_ids": ids}
            tokenizer.decode.return_value = "=SUM(A1:A3)"
            resident = MagicMock()
            output = torch.tensor([[1, 2, 3, 44, 45]])

            def generate(**kwargs):
                kwargs["stopping_criteria"][0](output, None)
                return output

            resident.generate.side_effect = generate
            with patch("dashboard.worker.load_model") as load, patch("peft.PeftModel.from_pretrained") as peft:
                generate_chat({"messages": [{"role": "user", "content": "sum"}]}, tokenizer, path, Events(path), model=resident)
            load.assert_not_called()
            peft.assert_not_called()
            self.assertFalse(resident.generate.call_args.kwargs["do_sample"])
            self.assertEqual(read_json(path / "chat_response.json")["text"], "=SUM(A1:A3)")

    def test_long_chat_uses_chunked_prefill_without_static_graph(self):
        import torch
        from dashboard.chat_backend import generate_chat_turn, PREFILL_CHUNK_TOKENS

        tokenizer = MagicMock(eos_token_id=2, pad_token_id=0)
        ids = MagicMock()
        ids.shape = (1, 15000)
        tokenizer.apply_chat_template.return_value = {"input_ids": ids}
        tokenizer.decode.return_value = "resposta"
        model, decoder = MagicMock(), MagicMock()
        model.generate.return_value = torch.zeros((1, 15002), dtype=torch.long)
        with patch("dashboard.chat_backend._graph_candidate", return_value=True), \
             patch("dashboard.chat_cache.SynchronousOffloadedCache") as offload:
            result = generate_chat_turn([{"role": "user", "content": "long context"}], tokenizer, model, decoder, None, max_new_tokens=2)
        decoder.prepare.assert_not_called()
        decoder._drop.assert_called_once()
        self.assertEqual(result["generation_mode"], "standard")
        self.assertEqual(model.generate.call_args.kwargs["prefill_chunk_size"], PREFILL_CHUNK_TOKENS)
        self.assertIs(model.generate.call_args.kwargs["past_key_values"], offload.return_value)
        self.assertNotIn("cache_implementation", model.generate.call_args.kwargs)
        self.assertEqual(model.generate.call_args.kwargs["logits_to_keep"], 1)

    def test_graph_expansion_cannot_allocate_a_long_static_cache(self):
        import torch
        decoder = GraphedDecoder()
        with patch.object(decoder, "_capture") as capture:
            with self.assertRaises(GraphUnavailable):
                decoder.prepare(object(), torch.zeros((1, 15000), dtype=torch.long), "test", 512)
            capture.assert_not_called()

    def test_engine_acks_before_generation_and_rejects_a_bad_token(self):
        with tempfile.TemporaryDirectory() as temp:
            endpoint = Path(temp) / ".chat-engine.json"
            started = threading.Event()
            finish = threading.Event()

            def handler(command):
                def work():
                    started.set()
                    self.assertTrue(finish.wait(5))
                return work

            thread = threading.Thread(
                target=serve_requests, args=(handler, endpoint, 30), daemon=True,
            )
            thread.start()
            deadline = time.time() + 2
            info = None
            while time.time() < deadline:
                info = read_json(endpoint, None)
                if isinstance(info, dict) and "port" in info:
                    break
                time.sleep(0.01)
            self.assertIsInstance(info, dict)
            wrong = engine_command({**info, "token": "nope"}, {"cmd": "ping"}, timeout=2)
            self.assertFalse(wrong["ok"])
            box = {}

            def client():
                box["response"] = engine_command(info, {"cmd": "generate", "run_id": "ignored"})

            caller = threading.Thread(target=client)
            caller.start()
            self.assertTrue(started.wait(2))
            caller.join(0.5)
            self.assertFalse(caller.is_alive())
            self.assertTrue(box["response"]["ok"])
            finish.set()
            self.assertTrue(engine_command(info, {"cmd": "release"}, timeout=2)["ok"])
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertFalse(endpoint.exists())

    def test_mark_running_records_the_engine_process(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            write_json(directory / "status.json", {"status": "queued"})
            mark_running(directory)
            state = read_json(directory / "status.json")
            process = read_json(directory / "process.json")
            self.assertEqual(state["status"], "running")
            self.assertEqual(process["pid"], __import__("os").getpid())
            self.assertIsInstance(process["created"], float)

    def test_cuda_allocation_errors_include_the_runtime_variant(self):
        import torch
        from dashboard.chat_engine import is_cuda_memory_error
        for error in (torch.OutOfMemoryError("allocation failed"), torch.AcceleratorError("CUDA error: out of memory"), RuntimeError("CUDA error: cudaErrorMemoryAllocation")):
            self.assertTrue(is_cuda_memory_error(error))
        for error in (RuntimeError("CUDA device-side assert"), ValueError("out of memory"), RuntimeError("unrelated failure")):
            self.assertFalse(is_cuda_memory_error(error))

    def test_engine_exits_after_a_cuda_failure_instead_of_reusing_its_context(self):
        with tempfile.TemporaryDirectory() as temp:
            endpoint = Path(temp) / ".chat-engine.json"
            thread = threading.Thread(target=serve_requests, args=(lambda command: lambda: False, endpoint, 30), daemon=True)
            thread.start()
            deadline = time.time() + 3
            while not endpoint.is_file() and time.time() < deadline:
                time.sleep(.01)
            info = read_json(endpoint)
            self.assertTrue(engine_command(info, {"cmd": "generate"})["ok"])
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(endpoint.exists())

    def test_runtime_allocation_failure_retires_the_resident_and_preserves_the_request(self):
        import torch
        from dashboard.chat_engine import _generate
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            request = {"messages": [{"role": "user", "content": "average cost and price"}]}
            write_json(directory / "request.json", request)
            resident = MagicMock()
            resident.base = object()
            with patch("dashboard.chat_engine.RESIDENT", resident), \
                 patch("dashboard.chat_backend.generate_chat", side_effect=torch.AcceleratorError("CUDA error: out of memory")), \
                 patch("dashboard.worker.environment", return_value={}), \
                 patch("dashboard.chat_engine.unload_resident") as unload:
                self.assertFalse(_generate(directory))
                unload.assert_called_once()
            self.assertEqual(read_json(directory / "request.json"), request)
            state = read_json(directory / "status.json")
            self.assertEqual(state["status"], "failed")
            self.assertIn("próxima tentativa", state["error"])

    def test_release_without_engine_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.chat_engine.RUNS", Path(temp)):
            release_resident_model()

    def test_chat_response_records_accelerated_and_fallback_metadata(self):
        import json
        import torch

        output = torch.tensor([[1, 2, 3, 44, 45]])
        for expected_mode in ("accelerated", "standard"):
            with self.subTest(mode=expected_mode), tempfile.TemporaryDirectory() as temp:
                path = Path(temp)
                tokenizer = MagicMock()
                ids = MagicMock()
                ids.shape = (1, 3)
                tokenizer.apply_chat_template.return_value = {"input_ids": ids}
                tokenizer.decode.return_value = "generated answer"
                tokenizer.eos_token_id = 2
                tokenizer.pad_token_id = 0
                model = MagicMock()
                model.generate.return_value = output
                decoder = MagicMock()
                decoder.generate_tokens.return_value = [44, 45]
                if expected_mode == "standard":
                    decoder.prepare.side_effect = GraphUnavailable("simulated capture failure")
                    decoder.disable.return_value = True

                events = Events(path)
                with patch("dashboard.chat_backend._graph_candidate", return_value=True):
                    generate_chat(
                        {"messages": [{"role": "user", "content": "private request"}]},
                        tokenizer, path, events, model=model, decoder=decoder,
                    )

                response = read_json(path / "chat_response.json")
                self.assertEqual(response["generation_mode"], expected_mode)
                self.assertEqual(response["prompt_tokens"], 3)
                self.assertNotIn("messages", response)
                self.assertNotIn("private request", json.dumps(response))
                phases = [
                    json.loads(line)["phase"]
                    for line in (path / "events.jsonl").read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(phases[:2], ["Verificando aceleração", "Gerando resposta"])
                self.assertTrue(response["complete"])
                if expected_mode == "accelerated":
                    model.generate.assert_not_called()
                else:
                    model.generate.assert_called_once()
                    decoder.generate_tokens.assert_not_called()

    def test_generation_base_adapter_progress_and_cancel(self):
        import torch

        for adapter in (None, "trained-id"):
            with self.subTest(adapter=adapter), tempfile.TemporaryDirectory() as temp:
                path = Path(temp)
                tokenizer = MagicMock()
                ids = MagicMock()
                ids.shape = (1, 3)
                tokenizer.apply_chat_template.return_value = {"input_ids": ids}
                tokenizer.decode.return_value = "=SUM(A1:A3)"
                model = MagicMock()
                output = torch.tensor([[1, 2, 3, 44, 45]])

                def generate(**kwargs):
                    kwargs["stopping_criteria"][0](output, None)
                    partial = read_json(path / "chat_response.json")
                    self.assertFalse(partial["complete"])
                    self.assertEqual(partial["generated_tokens"], 2)
                    return output

                model.generate.side_effect = generate
                request = {"messages": [{"role": "user", "content": "sum"}], "adapter_run": adapter}
                with patch("dashboard.worker.load_model", return_value=model), \
                     patch("dashboard.chat_backend.select_adapter") as select, \
                     patch("dashboard.chat_backend.run_path", return_value=path), \
                     patch("peft.PeftModel.from_pretrained", return_value=model) as peft:
                    generate_chat(request, tokenizer, path, Events(path))
                    response = read_json(path / "chat_response.json")
                    self.assertEqual(response["adapter_run"], adapter)
                    self.assertTrue(response["complete"])
                    self.assertEqual(response["text"], "=SUM(A1:A3)")
                    self.assertEqual(response["generation_mode"], "standard")
                    self.assertEqual(response["prompt_tokens"], 3)
                    self.assertFalse(model.generate.call_args.kwargs["do_sample"])
                    self.assertEqual(peft.call_count, int(bool(adapter)))
                    if adapter:
                        select.assert_called_once_with(adapter, [])
                        self.assertFalse(peft.call_args.kwargs["is_trainable"])
                    (path / "cancel.request").touch()
                    with self.assertRaises(Cancelled):
                        generate_chat(request, tokenizer, path, Events(path))

    def test_long_context_rejected_before_model_load(self):
        tokenizer = MagicMock()
        ids = MagicMock()
        ids.shape = (1, 32768)
        tokenizer.apply_chat_template.return_value = {"input_ids": ids}
        with patch("dashboard.worker.load_model") as load, self.assertRaises(ValueError):
            generate_chat({"messages": [{"role": "user", "content": "long"}]}, tokenizer, None, None)
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
