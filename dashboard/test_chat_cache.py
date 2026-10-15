import unittest
from unittest.mock import MagicMock, patch

import torch
from transformers import DynamicCache, Qwen3Config

from dashboard.chat_cache import SynchronousOffloadedCache


class ChatCacheTests(unittest.TestCase):
    def config(self):
        return Qwen3Config(num_hidden_layers=2, num_attention_heads=2, num_key_value_heads=1, head_dim=4, hidden_size=8)

    def test_chunked_cache_preserves_every_key_and_value_and_sequence_length(self):
        cache = SynchronousOffloadedCache(self.config())
        reference = DynamicCache(config=self.config())
        for size in (3, 2, 1):
            for layer in range(2):
                key, value = torch.randn(1, 1, size, 4), torch.randn(1, 1, size, 4)
                actual = cache.update(key, value, layer)
                expected = reference.update(key, value, layer)
                for left, right in zip(actual, expected):
                    torch.testing.assert_close(left, right)
                self.assertEqual(cache.get_seq_length(layer), reference.get_seq_length(layer))
                self.assertEqual(cache.layers[layer].keys.device.type, "cpu")
        self.assertEqual(cache.get_seq_length(), 6)
        self.assertFalse(cache.offloading, "The asynchronous prefetch stream must stay disabled")

    def test_transfers_are_blocking_in_both_directions(self):
        cache = SynchronousOffloadedCache(self.config())
        layer = cache.layers[0]
        layer.is_initialized = True
        old_key, old_value = MagicMock(), MagicMock()
        layer.keys, layer.values = old_key, old_value
        key, value = MagicMock(), MagicMock()
        key.device = value.device = "cuda:0"
        with patch.object(DynamicCache, "update", return_value=(key, value)):
            self.assertEqual(cache.update(key, value, 0), (key, value))
        old_key.to.assert_called_once_with("cuda:0", non_blocking=False)
        old_value.to.assert_called_once_with("cuda:0", non_blocking=False)
        key.to.assert_called_once_with("cpu", non_blocking=False)
        value.to.assert_called_once_with("cpu", non_blocking=False)


if __name__ == "__main__":
    unittest.main()
