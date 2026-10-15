"""Offload long-chat caches without accumulating asynchronous host transfers."""

from transformers import DynamicCache


class SynchronousOffloadedCache(DynamicCache):
    """Keep inactive layers in ordinary RAM and transfer only the current layer.

    Async offloading repeatedly allocates pinned host buffers during chunked
    prefill. On Windows this can fail in CUDA even below the VRAM allocator cap.
    Blocking copies bound outstanding transfers and avoid that pinned pool.
    """

    def __init__(self, config):
        super().__init__(config=config, offloading=False)

    def update(self, key_states, value_states, layer_idx, *args, **kwargs):
        layer = self.layers[layer_idx]
        if layer.is_initialized:
            layer.keys = layer.keys.to(key_states.device, non_blocking=False)
            layer.values = layer.values.to(value_states.device, non_blocking=False)
        keys, values = super().update(key_states, value_states, layer_idx, *args, **kwargs)
        layer.keys = keys.to("cpu", non_blocking=False)
        layer.values = values.to("cpu", non_blocking=False)
        return keys, values
