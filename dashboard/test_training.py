import unittest
from unittest.mock import MagicMock, patch

from dashboard.config import DEFAULTS
from dashboard.training import limit_training_memory, response_only_inputs


class TrainingMemoryTests(unittest.TestCase):
    def test_sparse_logits_preserve_loss_gradients_and_accumulation(self):
        import torch
        from peft import LoraConfig, get_peft_model
        from transformers import Qwen3Config, Qwen3ForCausalLM

        torch.manual_seed(42)
        model = get_peft_model(Qwen3ForCausalLM(Qwen3Config(
            vocab_size=32, hidden_size=16, intermediate_size=32, num_hidden_layers=1,
            num_attention_heads=2, num_key_value_heads=2, head_dim=8,
        )), LoraConfig(r=4, lora_alpha=8, target_modules="all-linear", lora_dropout=0, task_type="CAUSAL_LM"))
        model.eval()
        ids = torch.tensor([[1, 2, 3, 4, 5, 6], [1, 7, 8, 9, 0, 0]])
        batch = {"input_ids": ids, "attention_mask": ids.ne(0).long(),
                 "labels": torch.tensor([[-100, -100, -100, 4, 5, 6], [-100, -100, 8, 9, -100, -100]])}
        optimized = response_only_inputs(batch)
        self.assertEqual(optimized["logits_to_keep"].tolist(), [1, 2, 3, 4])
        for divisor in (None, torch.tensor(11)):
            model.zero_grad(set_to_none=True)
            expected = model(**batch, num_items_in_batch=divisor)
            expected.loss.backward()
            gradients = {name: param.grad.clone() for name, param in model.named_parameters() if param.requires_grad}
            model.zero_grad(set_to_none=True)
            actual = model(**optimized, num_items_in_batch=divisor)
            actual.loss.backward()
            torch.testing.assert_close(actual.loss, expected.loss)
            self.assertEqual(actual.logits.shape[1], 4)
            for name, parameter in model.named_parameters():
                if parameter.requires_grad:
                    torch.testing.assert_close(parameter.grad, gradients[name], atol=1e-6, rtol=1e-4)
        self.assertTrue(torch.all(batch["labels"][0, :3] == -100))

    def test_empty_supervision_rejected(self):
        import torch
        with self.assertRaisesRegex(ValueError, "sem tokens supervisionados"):
            response_only_inputs({"labels": torch.tensor([[-100, -100]])})

    def test_memory_cap_accounts_for_other_gpu_users(self):
        gib = 1024**3
        with patch("torch.cuda.is_available", return_value=True), patch("torch.cuda.set_device"), \
             patch("torch.cuda.mem_get_info", return_value=(5 * gib, 8 * gib)), \
             patch("torch.cuda.set_per_process_memory_fraction") as limit, patch("torch.cuda.reset_peak_memory_stats"):
            metrics = limit_training_memory(DEFAULTS, MagicMock())
        self.assertEqual(metrics["gpu_budget_gb"], 4.5)
        limit.assert_called_once_with(4.5 / 8, device=0)


if __name__ == "__main__":
    unittest.main()
