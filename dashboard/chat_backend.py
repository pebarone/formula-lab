"""Real local chat inference using the same base weights as the experiments."""

import hashlib
import time
from collections import OrderedDict

from benchmark.prompting import SYSTEM_PROMPT
from dashboard.config import read_json, run_path, write_json

MAX_MESSAGES = 199
MAX_CHARACTERS = 200_000
MAX_CONTEXT_TOKENS = 32768
MAX_NEW_TOKENS = 4096
# Attention reads the whole static cache. Keep short conversations in smaller
# buckets while leaving room for the next few hundred response tokens.
CACHE_SLACK = 256
GRAPH_VERIFY_TOKENS = 64
MAX_FAILED_GRAPH_ATTEMPTS = 32
MAX_GRAPH_CACHE_TOKENS = 2048
PREFILL_CHUNK_TOKENS = 256
OFFLOAD_CONTEXT_TOKENS = 8192


class ChatContextLimit(ValueError):
    """The fully templated chat prompt does not fit in the model context."""


def validate_messages(messages):
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise ValueError("A conversa passou do limite de mensagens. Inicie uma nova conversa.")
    result = []
    for index, message in enumerate(messages):
        role = "user" if index % 2 == 0 else "assistant"
        if not isinstance(message, dict) or set(message) != {"role", "content"} or message["role"] != role:
            raise ValueError("Histórico de conversa inválido.")
        content = message["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("A mensagem não pode estar vazia.")
        result.append({"role": role, "content": content})
    if result[-1]["role"] != "user" or sum(len(m["content"]) for m in result) > MAX_CHARACTERS:
        raise ValueError("Conversa muito longa ou sem pergunta final. Inicie uma nova conversa.")
    return result


def select_adapter(selection, jobs, *, allowed_kinds=("train", "decensor")):
    if selection is None or selection == "":
        return None
    if selection == "latest":
        candidates = [j for j in jobs if j.get("kind") == "train" and j.get("status") == "completed" and j.get("has_adapter")]
        if not candidates:
            raise ValueError("Nenhum treinamento concluído com adaptador disponível.")
        return max(candidates, key=lambda j: (j.get("ended_at", 0), j["id"]))["id"]
    path = run_path(selection)
    kind = read_json(path / "request.json", {}).get("kind")
    if kind not in allowed_kinds or read_json(path / "status.json", {}).get("status") != "completed":
        raise ValueError("O modelo selecionado não é um adaptador concluído.")
    if not (path / "adapter/adapter_config.json").is_file() or not (path / "adapter/adapter_model.safetensors").is_file():
        raise ValueError("Arquivos do adaptador não encontrados.")
    return selection


def latest_decensor(jobs):
    candidates = [j for j in jobs if j.get("kind") == "decensor" and j.get("status") == "completed" and j.get("has_adapter")]
    if not candidates:
        raise ValueError("Nenhum adaptador de recusa concluído. Extraia um em Configurar treinamento antes de combinar.")
    return max(candidates, key=lambda j: (j.get("ended_at") or 0, j["id"]))["id"]


# Static cache sized to the prompt plus a short slack. Attention walks the whole
# allocation, so a short message must not reserve the 32768-token window.
CACHE_BUCKETS = (256, 512, 1024, 2048, 3072, 4096, 6144, 8192, 12288, 16384, 24576, 32768)


class GraphUnavailable(Exception):
    pass


def cache_bucket(length):
    for bucket in CACHE_BUCKETS:
        if bucket >= length:
            return bucket
    raise ValueError(f"A geração precisa de {length} posições de cache; o máximo é {CACHE_BUCKETS[-1]}.")


def bucket_for_prompt(prompt_len, new_limit):
    """Smallest cache that can hold the prompt and a short reply, not the whole window."""
    extra = max(1, min(CACHE_SLACK, int(new_limit)))
    return cache_bucket(min(MAX_CONTEXT_TOKENS, int(prompt_len) + extra))


def _graph_candidate(model):
    return type(model).__module__.startswith(("transformers", "peft"))


def _eos_ids(tokenizer):
    eos = tokenizer.eos_token_id
    if eos is None:
        return set()
    values = eos if isinstance(eos, (list, tuple)) else [eos]
    return {int(item) for item in values}


class GraphedDecoder:
    """Greedy decode captured in one CUDA graph.

    The capture step itself leaves the logits at zero: bitsandbytes records the kernels
    but does not fill the output during capture. Replay is valid only after the cache is
    reset, the real prompt is prefilled into those same tensors, and the decode mask is
    copied into the tensors the graph captured. Later steps flip one more True in that
    mask; a newly built mask would not be visible to the replay.
    """

    def __init__(self):
        self.failed = OrderedDict()
        self.current_attempt_key = None
        self.identity = None
        self.verified = False
        self.graph = None
        self.cache = None
        self.token = None
        self.static_mask = None
        self.out = None
        self.bucket = None
        self.model_identity = None

    def prepare(self, model, input_ids, identity, limit=None, eos_ids=None):
        self.current_attempt_key = None
        self.model_identity = identity
        prompt = int(input_ids.shape[-1])
        bucket = bucket_for_prompt(prompt, MAX_NEW_TOKENS if limit is None else limit)
        if bucket > MAX_GRAPH_CACHE_TOKENS:
            self._drop()
            raise GraphUnavailable("Contexto longo usa cache dinâmico e leitura em blocos para limitar a VRAM.")
        import torch
        token_bytes = input_ids.detach().to(device="cpu", dtype=torch.int64).contiguous().numpy().tobytes()
        prompt_hash = hashlib.sha256(token_bytes).hexdigest()
        self.current_attempt_key = (identity, bucket, prompt_hash)
        if self.current_attempt_key in self.failed:
            self.failed.move_to_end(self.current_attempt_key)
            raise GraphUnavailable("CUDA graph já falhou para este prompt e cache.")
        full_id = f"{identity}:{bucket}"
        if self.identity != full_id or self.graph is None:
            self._capture(model, bucket, full_id)
        if not self.verified:
            self._verify(model, input_ids, eos_ids)

    def disable(self):
        key = self.current_attempt_key
        first = key is not None and key not in self.failed
        if key is not None:
            if first:
                self.failed[key] = None
                self.failed.move_to_end(key)
                while len(self.failed) > MAX_FAILED_GRAPH_ATTEMPTS:
                    self.failed.popitem(last=False)
                self._drop()
            else:
                self.failed.move_to_end(key)
        else:
            self._drop()
        return first

    def generate_tokens(self, model, input_ids, eos_ids, limit, on_token):
        import torch
        produced = []
        current = input_ids
        identity = self.model_identity or "decode"
        while len(produced) < limit:
            remaining = limit - len(produced)
            prompt = int(current.shape[-1])
            if prompt >= MAX_CONTEXT_TOKENS:
                break
            self.prepare(model, current, identity, remaining, eos_ids=eos_ids)
            room = self.bucket - prompt
            if room < 1:
                break
            take = min(remaining, room)
            base = list(produced)

            def emit(piece, base=base):
                if on_token:
                    on_token(base + piece)

            with torch.inference_mode():
                chunk = self._tokens(model, current, take, eos_ids, emit if on_token else None)
            if not chunk:
                break
            produced.extend(chunk)
            if chunk[-1] in eos_ids or len(chunk) < take or len(produced) >= limit:
                break
            step = torch.tensor(chunk, dtype=current.dtype, device=current.device).view(1, -1)
            current = torch.cat([current, step], dim=1)
        return produced

    def _drop(self):
        had_graph = self.graph is not None
        self.graph = None
        self.cache = None
        self.token = None
        self.static_mask = None
        self.out = None
        self.identity = None
        self.verified = False
        self.bucket = None
        if had_graph:
            import gc
            import torch
            gc.collect()
            torch.cuda.empty_cache()

    def _mask(self, model, cache, seen, query_len):
        import torch
        from transformers.masking_utils import create_masks_for_generate
        dtype = getattr(model, "dtype", None)
        if not isinstance(dtype, torch.dtype) or not dtype.is_floating_point:
            dtype = torch.bfloat16
        attention = torch.ones((1, seen + query_len), dtype=torch.long, device="cuda")
        embeds = torch.empty((1, query_len, 0), dtype=dtype, device="cuda")
        return create_masks_for_generate(model.config, embeds, attention, cache)

    def _capture(self, model, bucket, identity):
        import torch
        from transformers import StaticCache
        self._drop()
        cache = StaticCache(config=model.config, max_cache_len=bucket)
        token = torch.zeros((1, 1), dtype=torch.long, device="cuda")
        with torch.inference_mode():
            dummy = torch.zeros((1, 1), dtype=torch.long, device="cuda")
            model(input_ids=dummy, past_key_values=cache, use_cache=True, attention_mask=self._mask(model, cache, 0, 1))
            token.fill_(1)
            static_mask = self._mask(model, cache, 1, 1)
            if "full_attention" not in static_mask or static_mask["full_attention"] is None:
                raise GraphUnavailable("A máscara de atenção não tem o tensor full_attention.")
            graph = torch.cuda.CUDAGraph()
            with torch.cuda.graph(graph):
                out = model(input_ids=token, past_key_values=cache, use_cache=True, attention_mask=static_mask)
            cache.reset()
        self.cache = cache
        self.token = token
        self.static_mask = static_mask
        self.graph = graph
        self.out = out
        self.identity = identity
        self.bucket = bucket
        self.verified = False
        print(f"CUDA graph do chat ativo para cache de {bucket} tokens.", flush=True)

    def _verify(self, model, input_ids, eos_ids=None):
        import torch
        prompt = int(input_ids.shape[-1])
        verify_limit = min(GRAPH_VERIFY_TOKENS, self.bucket - prompt, MAX_CONTEXT_TOKENS - prompt)
        stop_ids = set(eos_ids or ())
        with torch.inference_mode():
            self.cache.reset()
            reference = model.generate(
                input_ids, past_key_values=self.cache, max_new_tokens=verify_limit,
                do_sample=False, eos_token_id=sorted(stop_ids), use_cache=True,
            )[0, prompt:prompt + verify_limit].tolist()
            got = self._tokens(model, input_ids, verify_limit, stop_ids, None)
        if got != reference:
            mismatch = next((index for index, (left, right) in enumerate(zip(got, reference)) if left != right), None)
            detail = f"no token {mismatch + 1}" if mismatch is not None else "no tamanho da resposta"
            raise GraphUnavailable(
                f"CUDA graph divergiu da geração gulosa {detail} durante a verificação de {verify_limit} tokens."
            )
        self.verified = True

    def _tokens(self, model, input_ids, limit, eos_ids, on_token):
        prompt = int(input_ids.shape[-1])
        self.cache.reset()
        prefill = model(
            input_ids=input_ids, past_key_values=self.cache, use_cache=True,
            attention_mask=self._mask(model, self.cache, 0, prompt), logits_to_keep=1,
        )
        first = prefill.logits[:, -1, :].argmax(dim=-1)
        self.token.copy_(first.view(1, 1))
        self._copy_mask(self._mask(model, self.cache, prompt, 1))
        tokens = [int(first.item())]
        if on_token:
            on_token(tokens)
        if tokens[0] in eos_ids or len(tokens) >= limit:
            return tokens
        seen = prompt
        while len(tokens) < limit:
            if seen + 1 >= self.bucket:
                return tokens
            self.graph.replay()
            nxt = self.out.logits[:, -1, :].argmax(dim=-1)
            nxt_id = int(nxt.item())
            tokens.append(nxt_id)
            if on_token:
                on_token(tokens)
            if nxt_id in eos_ids or len(tokens) >= limit:
                return tokens
            seen += 1
            if seen >= self.bucket:
                return tokens
            self.token.copy_(nxt.view(1, 1))
            self.static_mask["full_attention"][..., seen] = True
        return tokens

    def _copy_mask(self, source):
        for key, value in self.static_mask.items():
            if value is not None:
                value.copy_(source[key])


def _prepare_turn_inputs(messages, tokenizer, tools=None):
    if not isinstance(messages, list) or not messages:
        raise ValueError("A conversa está vazia.")
    normalized = []
    characters = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "tool"}:
            raise ValueError("Histórico interno de conversa inválido.")
        if not isinstance(message.get("content"), str):
            raise ValueError("O conteúdo da mensagem precisa ser texto.")
        allowed = {"role", "content", "name"} if message["role"] == "tool" else {"role", "content"}
        if set(message) - allowed or (message["role"] == "tool" and not isinstance(message.get("name"), str)):
            raise ValueError("Histórico interno de conversa inválido.")
        normalized.append(dict(message))
        characters += len(message["content"])
    if characters > MAX_CHARACTERS + 4 * 4 * 12_000:
        raise ValueError("Conversa muito longa. Inicie uma nova conversa.")
    prompt = [{"role": "system", "content": SYSTEM_PROMPT}] + normalized
    kwargs = {
        "tokenize": True, "add_generation_prompt": True,
        "return_dict": True, "return_tensors": "pt",
    }
    if tools:
        kwargs["tools"] = tools
    inputs = tokenizer.apply_chat_template(prompt, **kwargs)
    width = inputs["input_ids"].shape[-1]
    if width >= MAX_CONTEXT_TOKENS:
        raise ChatContextLimit(f"O histórico tem {width} tokens; a janela é {MAX_CONTEXT_TOKENS}. Inicie uma nova conversa.")
    return inputs, width


def generate_chat_turn(
    messages,
    tokenizer,
    model,
    decoder,
    events,
    *,
    tools=None,
    max_new_tokens=MAX_NEW_TOKENS,
    adapter_run=None,
    decensor_run=None,
    directory=None,
    _prepared_inputs=None,
):
    """Generate one greedy model turn and return decoded text with timing metadata."""
    import torch
    from transformers import StoppingCriteria, StoppingCriteriaList
    from dashboard.runtime import Cancelled

    if not isinstance(max_new_tokens, int) or max_new_tokens < 1:
        raise ValueError("max_new_tokens precisa ser positivo.")
    if tools is not None:
        max_new_tokens = min(max_new_tokens, 512)
    if _prepared_inputs is None:
        inputs, width = _prepare_turn_inputs(messages, tokenizer, tools)
    else:
        inputs = _prepared_inputs
        width = inputs["input_ids"].shape[-1]
    token_limit = min(max_new_tokens, MAX_CONTEXT_TOKENS - width)
    eos_ids = _eos_ids(tokenizer)
    model.eval()
    model.config.use_cache = True
    inputs = {key: value.to("cuda:0") for key, value in inputs.items()}
    started = None
    generation_mode = "standard"
    streaming_text = not tools

    def check():
        if events is not None:
            events.check()

    def emit(**values):
        if events is not None:
            events.emit(**values)

    def save(tokens, complete=False):
        if directory is None or not streaming_text:
            return
        response = {
            "text": tokenizer.decode(tokens, skip_special_tokens=True).strip(),
            "adapter_run": adapter_run, "decensor_run": decensor_run, "generated_tokens": len(tokens),
            "generation_mode": generation_mode, "prompt_tokens": width,
            "generation_seconds": time.time() - started, "complete": complete,
            "reached_token_limit": len(tokens) >= token_limit,
        }
        write_json(directory / "chat_response.json", response)

    class StreamProgress(StoppingCriteria):
        def __init__(self):
            self.last_write = 0

        def __call__(self, input_ids, scores, **kwargs):
            now = time.time()
            if now - self.last_write >= 0.5:
                check()
                tokens = input_ids[0, width:].tolist()
                save(tokens)
                emit(phase="Gerando resposta", generated_tokens=len(tokens))
                self.last_write = now
            return False

    def produce(graph):
        nonlocal generation_mode, started
        generation_mode = "accelerated" if graph else "standard"
        started = time.time()
        emit(phase="Gerando resposta")
        if graph:
            progress = {"last": 0.0}

            def on_token(tokens):
                now = time.time()
                if len(tokens) % 16 == 0:
                    check()
                if now - progress["last"] >= 0.5:
                    check()
                    save(tokens)
                    emit(phase="Gerando resposta", generated_tokens=len(tokens))
                    progress["last"] = now

            return decoder.generate_tokens(model, inputs["input_ids"], eos_ids, token_limit, on_token)
        with torch.inference_mode():
            if width > OFFLOAD_CONTEXT_TOKENS:
                from dashboard.chat_cache import SynchronousOffloadedCache
                cache_options = {"past_key_values": SynchronousOffloadedCache(model.config)}
            else:
                cache_options = {"cache_implementation": "dynamic"}
            output = model.generate(
                **inputs, max_new_tokens=token_limit, do_sample=False,
                **cache_options,
                prefill_chunk_size=PREFILL_CHUNK_TOKENS,
                logits_to_keep=1,
                pad_token_id=tokenizer.pad_token_id, eos_token_id=tokenizer.eos_token_id,
                stopping_criteria=StoppingCriteriaList([StreamProgress()]),
            )
        return output[0, width:].tolist()

    identity = f"{id(model)}:{adapter_run or 'base'}:{decensor_run or 'none'}"
    tokens = None
    if _graph_candidate(model) and bucket_for_prompt(width, token_limit) <= MAX_GRAPH_CACHE_TOKENS:
        if decoder is None:
            decoder = GraphedDecoder()
        emit(phase="Verificando aceleração")
        try:
            decoder.prepare(model, inputs["input_ids"], identity, token_limit, eos_ids=eos_ids)
            tokens = produce(True)
        except Cancelled:
            raise
        except Exception as exc:
            if decoder.disable():
                print(f"CUDA graph do chat não usado: {exc}", flush=True)
        # Leave the exception scope before retrying: its traceback holds CUDA tensors.
    if tokens is None:
        if decoder is not None:
            decoder._drop()
        tokens = produce(False)
    check()
    save(tokens, complete=True)
    emit(phase="Turno concluído")
    return {
        "text": tokenizer.decode(tokens, skip_special_tokens=True).strip(),
        "generated_tokens": len(tokens), "generation_seconds": time.time() - started,
        "generation_mode": generation_mode, "prompt_tokens": width,
        "reached_token_limit": len(tokens) >= token_limit,
    }


def generate_chat(request, tokenizer, directory, events, model=None, decoder=None):
    history = validate_messages(request.get("messages"))
    use_excel_tools = request.get("use_excel_tools", False)
    if not isinstance(use_excel_tools, bool):
        raise ValueError("use_excel_tools precisa ser booleano.")
    workbook_id = request.get("workbook_id")
    adapter_run = request.get("adapter_run")
    decensor_run = request.get("decensor_run")
    if adapter_run:
        select_adapter(adapter_run, [])
    if decensor_run:
        select_adapter(decensor_run, [], allowed_kinds=("decensor",))

    tools = None
    if use_excel_tools:
        from dashboard.excel_tools import tool_definitions, tool_prompt_messages
        tools = tool_definitions()
        template_history = tool_prompt_messages(history)
    else:
        template_history = history
    prepared_inputs, _ = _prepare_turn_inputs(template_history, tokenizer, tools)

    if model is None:
        from peft import PeftModel
        from dashboard.worker import load_model
        model = load_model(events)
        names = [name for name in (adapter_run, decensor_run) if name]
        if names:
            events.emit(phase="Carregando adaptador do chat")
            model = PeftModel.from_pretrained(model, run_path(names[0]) / "adapter", adapter_name=names[0], is_trainable=False)
            for name in names[1:]:
                model.load_adapter(str(run_path(name) / "adapter"), adapter_name=name, is_trainable=False)
            model.base_model.set_adapter(names, inference_mode=True)

    if not use_excel_tools:
        turn = generate_chat_turn(
            history, tokenizer, model, decoder, events,
            adapter_run=adapter_run, decensor_run=decensor_run, directory=directory,
            _prepared_inputs=prepared_inputs,
        )
        response = {
            **turn, "adapter_run": adapter_run, "decensor_run": decensor_run,
            "complete": True, "tools_used": False, "tool_calls": 0, "tool_trace": [],
            "workbook_id": workbook_id,
        }
        write_json(directory / "chat_response.json", response)
        events.emit(phase="Resposta concluída")
        return

    from dashboard.excel_tools import run_tool_conversation
    totals = {"generated_tokens": 0, "generation_seconds": 0.0, "prompt_tokens": 0,
              "modes": [], "reached_token_limit": False}
    first_turn = {"prepared": prepared_inputs}

    def generate_turn(messages, tooldefs):
        prepared = first_turn.pop("prepared", None) if not totals["modes"] else None
        turn = generate_chat_turn(
            messages, tokenizer, model, decoder, events, tools=tooldefs,
            max_new_tokens=512, adapter_run=adapter_run, decensor_run=decensor_run,
            _prepared_inputs=prepared,
        )
        totals["generated_tokens"] += turn["generated_tokens"]
        totals["generation_seconds"] += turn["generation_seconds"]
        totals["prompt_tokens"] += turn["prompt_tokens"]
        totals["modes"].append(turn["generation_mode"])
        totals["reached_token_limit"] = totals["reached_token_limit"] or turn["reached_token_limit"]
        return turn["text"]

    def save_tool_progress(phase, trace):
        response = {
            "text": "", "adapter_run": adapter_run, "decensor_run": decensor_run,
            "generated_tokens": totals["generated_tokens"], "generation_seconds": totals["generation_seconds"],
            "generation_mode": "standard" if "standard" in totals["modes"] else "accelerated",
            "prompt_tokens": totals["prompt_tokens"], "complete": False,
            "reached_token_limit": totals["reached_token_limit"],
            "tools_used": bool(trace), "tool_calls": len(trace), "tool_trace": trace,
            "tool_elapsed_seconds": sum(item["elapsed_seconds"] for item in trace),
            "workbook_id": workbook_id,
        }
        write_json(directory / "chat_response.json", response)
        events.emit(phase=phase, tools_used=bool(trace), tool_calls=len(trace), tool_trace=trace)

    result = run_tool_conversation(
        history, generate_turn, workbook_id, on_progress=save_tool_progress,
        check_cancel=events.check if events is not None else None,
    )
    events.check()
    modes = totals["modes"]
    generation_mode = "standard" if "standard" in modes else "accelerated"
    response = {
        "text": result["text"], "adapter_run": adapter_run, "decensor_run": decensor_run,
        "generated_tokens": totals["generated_tokens"],
        "generation_seconds": totals["generation_seconds"], "generation_mode": generation_mode,
        "prompt_tokens": totals["prompt_tokens"], "complete": True,
        "reached_token_limit": totals["reached_token_limit"],
        "tools_used": result["tools_used"], "tool_calls": result["tool_calls"],
        "tool_trace": result["tool_trace"],
        "tool_elapsed_seconds": sum(item["elapsed_seconds"] for item in result["tool_trace"]),
        "tool_rounds": result["tool_rounds"], "workbook_id": workbook_id,
    }
    if result.get("budget_exhausted"):
        response["budget_exhausted"] = True
    write_json(directory / "chat_response.json", response)
    events.emit(phase="Resposta concluída", tools_used=response["tools_used"], tool_calls=response["tool_calls"])
