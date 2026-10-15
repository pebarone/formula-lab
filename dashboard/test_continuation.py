import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from dashboard.adapters import inherit_lora_config, training_adapter, training_history
from dashboard.config import DEFAULTS, read_json, write_json
from dashboard.runtime import Events
from dashboard.server import Jobs
from dashboard.worker import prepare_training_model
from dashboard.data import prepare
from dashboard.test_dashboard import FakeTokenizer, row


class ContinuationTests(unittest.TestCase):
    def source(self, root):
        source = root / "20260929-000000-abcdef01"
        (source / "adapter").mkdir(parents=True)
        write_json(source / "request.json", {"kind": "train"})
        write_json(source / "status.json", {"status": "completed"})
        write_json(source / "adapter/adapter_config.json", {"peft_type": "LORA", "task_type": "CAUSAL_LM", "r": 4, "lora_alpha": 8})
        (source / "adapter/adapter_model.safetensors").write_bytes(b"test weights")
        return source

    def test_source_validation_and_inherited_architecture(self):
        with tempfile.TemporaryDirectory() as temp:
            source = self.source(Path(temp))
            with patch("dashboard.adapters.run_path", return_value=source):
                config = inherit_lora_config(DEFAULTS, source.name)
                self.assertEqual((config["lora_r"], config["lora_alpha"]), (4, 8))
                self.assertEqual(config["learning_rate"], DEFAULTS["learning_rate"])
                for status in ("running", "failed", "cancelled", "interrupted"):
                    write_json(source / "status.json", {"status": status})
                    with self.assertRaises(ValueError):
                        training_adapter(source.name)
                write_json(source / "status.json", {"status": "completed"})
                (source / "adapter/adapter_model.safetensors").unlink()
                with self.assertRaises(ValueError):
                    training_adapter(source.name)

    def test_api_preserves_parent_and_creates_new_request(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.source(root)
            before = (source / "adapter/adapter_model.safetensors").read_bytes()
            with patch("dashboard.server.RUNS", root), \
                 patch("dashboard.adapters.run_path", return_value=source), \
                 patch("dashboard.server.run_path", return_value=source), \
                 patch("dashboard.server.subprocess.Popen") as spawn, \
                 patch("dashboard.server.release_resident_model") as release, \
                 patch("dashboard.server.psutil.Process") as process:
                spawn.return_value.pid = 123
                process.return_value.create_time.return_value = 123.0
                result = Jobs().start({"kind": "train", "adapter_run": source.name, "config": {"learning_rate": 0.0002}})
                release.assert_called_once_with()
            self.assertNotEqual(result["id"], source.name)
            request = read_json(root / result["id"] / "request.json")
            self.assertEqual(request["adapter_run"], source.name)
            self.assertEqual(request["config"]["lora_r"], 4)
            self.assertEqual(request["config"]["lora_alpha"], 8)
            self.assertEqual(request["config"]["learning_rate"], 0.0002)
            self.assertEqual((source / "adapter/adapter_model.safetensors").read_bytes(), before)

    def test_invalid_source_does_not_start_worker(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.server.RUNS", Path(temp)), patch("dashboard.server.subprocess.Popen") as spawn:
            with self.assertRaises(ValueError):
                Jobs().start({"kind": "train", "adapter_run": "../adapter"})
            spawn.assert_not_called()

    def test_prepare_uses_source_in_request(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.source(root)
            with patch("dashboard.server.RUNS", root), patch("dashboard.adapters.run_path", return_value=source), \
                 patch("dashboard.server.run_path", return_value=source), patch("dashboard.server.subprocess.Popen") as spawn, \
                 patch("dashboard.server.release_resident_model") as release, \
                 patch("dashboard.server.psutil.Process") as process:
                spawn.return_value.pid = 123
                process.return_value.create_time.return_value = 123.0
                result = Jobs().start({"kind": "prepare", "adapter_run": source.name})
                release.assert_called_once_with()
            self.assertEqual(read_json(root / result["id"] / "request.json")["adapter_run"], source.name)

    def test_new_examples_exclude_entire_ancestry_and_preserve_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ancestor = self.source(root)
            parent = root / "20260929-000001-abcdef02"
            parent.mkdir()
            hashes = {"train": "hash", "validation": "val", "test": "test"}
            from benchmark.run_baseline import prompt_fingerprint
            for directory, ids in ((ancestor, ["a", "b"]), (parent, ["b", "c"])):
                write_json(directory / "selection.json", {"data_sha256": hashes, "prompt_fingerprint": prompt_fingerprint(), "splits": {"train": {"ids": ids}}})
            write_json(parent / "request.json", {"kind": "train", "adapter_run": ancestor.name})
            splits = {"train": [row(name) for name in "abcdef"], "validation": [row("val")], "test": [row("test")]}
            destination = root / "new"
            destination.mkdir()
            config = {**DEFAULTS, "train_samples": 3, "validation_samples": 1}
            with patch("dashboard.adapters.training_adapter", side_effect=lambda run: (root / run / "adapter", {})), \
                 patch("dashboard.data.load_splits", return_value=(splits, hashes)):
                _, first = prepare(config, FakeTokenizer(), destination, lambda **kw: None, parent.name)
                self.assertEqual(set(first["splits"]["train"]["ids"]), set("def"))
                self.assertEqual(first["splits"]["train"]["seen_excluded"], 3)
                self.assertEqual(first["prior_train_ids"], 3)
                self.assertEqual(first["excluded_training_runs"], [parent.name, ancestor.name])
                _, second = prepare(config, FakeTokenizer(), destination, lambda **kw: None, parent.name)
                self.assertEqual(first, second)
                with self.assertRaisesRegex(ValueError, "Só 3 exemplos elegíveis"):
                    prepare({**config, "train_samples": 4}, FakeTokenizer(), destination, lambda **kw: None, parent.name)
                _, repeat = prepare({**config, "exclude_seen": False}, FakeTokenizer(), destination, lambda **kw: None, parent.name)
                _, base = prepare(config, FakeTokenizer(), destination, lambda **kw: None)
                self.assertEqual(repeat["splits"], base["splits"])
                self.assertEqual(first["splits"]["validation"], base["splits"]["validation"])

    def test_missing_incompatible_and_cyclic_history_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            source = self.source(Path(temp))
            good = {"data_sha256": {"train": "hash"}, "prompt_fingerprint": "prompt", "splits": {"train": {"ids": ["a"]}}}
            with patch("dashboard.adapters.run_path", return_value=source):
                for manifest in ({}, {**good, "data_sha256": {}}, {**good, "prompt_fingerprint": "changed"}, {**good, "splits": {}}, {**good, "splits": {"train": {"ids": [1]}}}):
                    write_json(source / "selection.json", manifest)
                    with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                        training_history(source.name, {"train": "hash"}, "prompt")
                write_json(source / "selection.json", good)
                write_json(source / "request.json", {"kind": "train", "adapter_run": source.name})
                with self.assertRaisesRegex(ValueError, "Ciclo"):
                    training_history(source.name, {"train": "hash"}, "prompt")

    def test_base_path_creates_new_lora(self):
        with patch("peft.prepare_model_for_kbit_training") as prepare, patch("peft.get_peft_model") as fresh, patch("peft.PeftModel.from_pretrained") as load:
            prepare_training_model(MagicMock(), DEFAULTS, MagicMock())
            fresh.assert_called_once()
            load.assert_not_called()
            self.assertEqual(fresh.call_args.args[1].r, DEFAULTS["lora_r"])

    def test_real_cpu_adapter_continues_and_source_is_unchanged(self):
        import torch
        from peft import LoraConfig, get_peft_model
        from transformers import LlamaConfig, LlamaForCausalLM

        cfg = LlamaConfig(vocab_size=32, hidden_size=16, intermediate_size=32, num_hidden_layers=1,
                          num_attention_heads=2, num_key_value_heads=2, max_position_embeddings=32)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.source(root)
            original = get_peft_model(LlamaForCausalLM(cfg), LoraConfig(r=4, lora_alpha=8, target_modules=["q_proj"], task_type="CAUSAL_LM"))
            with torch.no_grad():
                for name, parameter in original.named_parameters():
                    if "lora_B" in name:
                        parameter.fill_(0.1)
            original.save_pretrained(source / "adapter")
            weight_file = source / "adapter/adapter_model.safetensors"
            digest = hashlib.sha256(weight_file.read_bytes()).hexdigest()
            expected = {name: param.detach().clone() for name, param in original.named_parameters() if "lora_" in name}
            destination = root / "continued"
            destination.mkdir()
            with patch("dashboard.adapters.run_path", return_value=source):
                model = prepare_training_model(LlamaForCausalLM(cfg), DEFAULTS, Events(destination), source.name)
            trainable = [(name, param) for name, param in model.named_parameters() if param.requires_grad]
            self.assertTrue(trainable)
            self.assertTrue(all("lora_" in name for name, _ in trainable))
            for name, param in trainable:
                torch.testing.assert_close(param, expected[name])
            model.train()
            optimizer = torch.optim.AdamW([param for _, param in trainable], lr=0.01)
            ids = torch.tensor([[1, 2, 3, 4]])
            model(input_ids=ids, labels=ids).loss.backward()
            optimizer.step()
            self.assertTrue(any(not torch.equal(param, expected[name]) for name, param in trainable))
            model.save_pretrained(destination / "adapter")
            self.assertTrue((destination / "adapter/adapter_model.safetensors").is_file())
            self.assertEqual(hashlib.sha256(weight_file.read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
