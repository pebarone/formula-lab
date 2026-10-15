"""Refusal-direction math and the panel job that saves it as a LoRA."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import torch

from dashboard.chat_backend import latest_decensor, select_adapter
from dashboard.config import read_json, write_json
from dashboard.decensor import lora_factors, prompt_token_ids, refusal_direction, residual_along, split_cosine
from dashboard.decensor_prompts import HARMLESS_PROMPTS, REFUSAL_PROMPTS
from dashboard.server import Jobs
from dashboard.worker import check_evaluation_adapter


class RefusalMathTests(unittest.TestCase):
    def test_full_strength_removes_the_direction(self):
        weight = torch.randn(8, 5)
        direction = torch.nn.functional.normalize(torch.randn(8), dim=0)
        factors_a, factors_b = lora_factors(weight, direction, 1)
        self.assertLess(residual_along(weight, factors_a, factors_b, direction), 1e-4)

    def test_partial_strength_keeps_the_remaining_component(self):
        weight = torch.randn(6, 4)
        direction = torch.nn.functional.normalize(torch.randn(6), dim=0)
        sample = torch.randn(4)
        full = torch.dot(weight @ sample, direction)
        factors_a, factors_b = lora_factors(weight, direction, 0.5)
        kept = torch.dot((weight + factors_b @ factors_a) @ sample, direction)
        self.assertAlmostEqual(kept.item(), (0.5 * full).item(), places=4)

    def test_split_halves_agree_on_a_shared_shift(self):
        harmless = torch.randn(8, 3)
        harmful = harmless + torch.tensor([3.0, 0.0, 0.0])
        direction, norm = refusal_direction(harmful, harmless)
        self.assertGreater(float(norm), 1.0)
        self.assertAlmostEqual(float(direction[0]), 1.0, places=5)
        self.assertGreater(split_cosine(harmful, harmless), 0.99)

    def test_chat_template_objects_expose_token_ids(self):
        class Encoded:
            input_ids = [7, 8, 9]

        class Tokenizer:
            def apply_chat_template(self, messages, add_generation_prompt, tokenize):
                self.messages = messages
                return Encoded()

        tokenizer = Tokenizer()
        self.assertEqual(prompt_token_ids(tokenizer, "some"), [7, 8, 9])
        self.assertTrue(tokenizer.messages[0]["content"])

    def test_contrast_sets_match(self):
        self.assertEqual(len(REFUSAL_PROMPTS), len(HARMLESS_PROMPTS))
        self.assertGreaterEqual(len(REFUSAL_PROMPTS), 32)
        self.assertEqual(len(REFUSAL_PROMPTS) % 2, 0)
        self.assertEqual(len(set(REFUSAL_PROMPTS)), len(REFUSAL_PROMPTS))
        self.assertFalse(set(REFUSAL_PROMPTS) & set(HARMLESS_PROMPTS))
        self.assertTrue(all(isinstance(text, str) and text.strip() for text in REFUSAL_PROMPTS + HARMLESS_PROMPTS))


class DecensorJobTests(unittest.TestCase):
    def test_start_queues_a_base_model_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("dashboard.server.RUNS", root), patch("dashboard.server.subprocess.Popen") as spawn, \
                 patch("dashboard.server.release_resident_model") as release, patch("dashboard.server.psutil.Process") as process:
                spawn.return_value.pid = 321
                process.return_value.create_time.return_value = 10.0
                result = Jobs().start({"kind": "decensor", "config": {}})
                release.assert_called_once_with()
            request = read_json(root / result["id"] / "request.json")
            self.assertEqual(request["kind"], "decensor")
            self.assertIsNone(request["adapter_run"])
            self.assertNotIn("decensor_run", request)
            spawn.assert_called_once()

    def test_decensor_rejects_a_source_adapter_before_spawn(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.server.RUNS", Path(temp)), \
             patch("dashboard.server.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(ValueError, "Qwen original"):
                Jobs().start({"kind": "decensor", "config": {}, "adapter_run": "20261001-000000-abcdef01"})
            spawn.assert_not_called()

    def test_chat_stacks_the_latest_refusal_adapter(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = "20261001-120000-abcde123"
            adapter = root / run_id / "adapter"
            adapter.mkdir(parents=True)
            write_json(root / run_id / "request.json", {"kind": "decensor", "created_at": 1})
            write_json(root / run_id / "status.json", {"status": "completed", "ended_at": 10})
            (adapter / "adapter_config.json").write_text("{}", encoding="utf-8")
            (adapter / "adapter_model.safetensors").write_bytes(b"")

            def resolve(selection):
                path = root / selection
                if not path.is_dir():
                    raise ValueError("Execução não encontrada.")
                return path

            with patch("dashboard.server.RUNS", root), patch("dashboard.chat_backend.run_path", side_effect=resolve), \
                 patch("dashboard.server.dispatch_chat") as dispatch, patch("dashboard.server.subprocess.Popen") as spawn:
                result = Jobs().start({
                    "kind": "chat",
                    "messages": [{"role": "user", "content": "Qual fórmula soma A1:A10?"}],
                    "with_decensor": True,
                })
                dispatch.assert_called_once()
                spawn.assert_not_called()
            request = read_json(root / result["id"] / "request.json")
            self.assertEqual(request["decensor_run"], run_id)
            self.assertIsNone(request["adapter_run"])

    def test_stacking_requires_a_finished_refusal_adapter(self):
        with tempfile.TemporaryDirectory() as temp, patch("dashboard.server.RUNS", Path(temp)), \
             patch("dashboard.server.dispatch_chat") as dispatch:
            with self.assertRaisesRegex(ValueError, "adaptador de recusa"):
                Jobs().start({"kind": "chat", "messages": [{"role": "user", "content": "soma"}], "with_decensor": True})
            dispatch.assert_not_called()

    def test_evaluation_accepts_refusal_adapter_without_a_dataset_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "adapter").mkdir()
            (root / "adapter/adapter_config.json").write_text("{}", encoding="utf-8")
            write_json(root / "request.json", {"kind": "decensor"})
            write_json(root / "status.json", {"status": "completed"})
            self.assertEqual(check_evaluation_adapter(root, {"test": "hash"}, "prompt"), "decensor")
            write_json(root / "request.json", {"kind": "train"})
            with self.assertRaisesRegex(ValueError, "Dataset ou prompt"):
                check_evaluation_adapter(root, {"test": "hash"}, "prompt")

    def test_latest_training_ignores_a_newer_refusal_adapter(self):
        jobs = [
            {"id": "20261001-010101-aaaaaaaa", "kind": "train", "status": "completed", "has_adapter": True, "ended_at": 5},
            {"id": "20261001-020202-bbbbbbbb", "kind": "decensor", "status": "completed", "has_adapter": True, "ended_at": 50},
        ]
        self.assertEqual(select_adapter("latest", jobs), "20261001-010101-aaaaaaaa")
        self.assertEqual(latest_decensor(jobs), "20261001-020202-bbbbbbbb")


if __name__ == "__main__":
    unittest.main()
