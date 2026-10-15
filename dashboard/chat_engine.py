"""Resident chat process. The NF4 weights stay loaded across messages."""

import gc
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import traceback

import psutil

from dashboard.config import ROOT, RUNS, TERMINAL, read_json, run_path, write_json
from dashboard.runtime import Cancelled, Events, worker_lock

ENDPOINT = ".chat-engine.json"
IDLE_SECONDS = 20 * 60
RESIDENT = None


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            stream.write(data)
            stream.flush()

    def flush(self):
        for stream in self.streams:
            stream.flush()


class Resident:
    def __init__(self):
        from dashboard.chat_backend import GraphedDecoder
        self._tokenizer = None
        self.base = None
        self.peft = None
        self.decoder = GraphedDecoder()

    def tokenizer(self, events):
        if self._tokenizer is None:
            from dashboard.worker import load_tokenizer
            events.emit(phase="Carregando tokenizer")
            self._tokenizer = load_tokenizer()
        return self._tokenizer

    def model_for(self, adapter_run, events, decensor_run=None):
        if self.base is None:
            from dashboard.worker import load_model
            self.base = load_model(events)
        names = [name for name in (adapter_run, decensor_run) if name]
        if names:
            self._ensure_adapters(names, events)
            self.peft.base_model.set_adapter(names, inference_mode=True)
            self.peft.base_model.enable_adapter_layers()
            model = self.peft
        elif self.peft is not None:
            self.peft.base_model.disable_adapter_layers()
            model = self.peft
        else:
            model = self.base
        model.eval()
        model.config.use_cache = True
        return model

    def _ensure_adapters(self, names, events):
        from peft import PeftModel
        for name in names:
            path = str(run_path(name) / "adapter")
            if self.peft is None:
                events.emit(phase="Carregando adaptador do chat")
                self.peft = PeftModel.from_pretrained(self.base, path, adapter_name=name, is_trainable=False)
            elif name not in self.peft.peft_config:
                events.emit(phase="Carregando adaptador do chat")
                self.peft.load_adapter(path, adapter_name=name, is_trainable=False)


def unload_resident():
    global RESIDENT
    RESIDENT = None
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
    except Exception:
        traceback.print_exc()


def is_cuda_memory_error(exc):
    import torch
    if isinstance(exc, torch.OutOfMemoryError):
        return True
    message = str(exc).lower()
    return isinstance(exc, RuntimeError) and "cuda" in message and (
        "out of memory" in message or "cudaerrormemoryallocation" in message
    )


def mark_running(directory):
    write_json(directory / "process.json", {"pid": os.getpid(), "created": psutil.Process().create_time()})
    Events(directory).status("running", started_at=time.time())


def _endpoint_path():
    return RUNS / ENDPOINT


def _read_endpoint():
    try:
        info = read_json(_endpoint_path(), None)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(info, dict) or not {"pid", "port", "token", "created"} <= set(info):
        return None
    return info


def _unlink_if_same(info):
    current = _read_endpoint()
    if current and current.get("pid") == info.get("pid") and current.get("token") == info.get("token"):
        _endpoint_path().unlink(missing_ok=True)


def _same_process(info):
    try:
        process = psutil.Process(int(info["pid"]))
        created = float(info["created"])
    except (psutil.Error, TypeError, ValueError, KeyError):
        return False
    return process.is_running() and process.status() != psutil.STATUS_ZOMBIE and abs(process.create_time() - created) < 0.1


def _chat_still_running(info):
    if not RUNS.is_dir():
        return False
    for path in RUNS.iterdir():
        if not path.is_dir():
            continue
        try:
            state = read_json(path / "status.json", {})
            proc = read_json(path / "process.json", {})
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if not state or state.get("status") in TERMINAL:
            continue
        try:
            same_time = abs(float(proc.get("created", -1)) - float(info["created"])) < 0.1
        except (TypeError, ValueError):
            continue
        if proc.get("pid") == info.get("pid") and same_time:
            return True
    return False


def _wait_dead(info, timeout):
    deadline = time.time() + timeout
    while _same_process(info):
        if time.time() >= deadline:
            raise ValueError("O chat não liberou a GPU a tempo.")
        time.sleep(0.05)


def _kill(info):
    if not _same_process(info):
        return
    process = psutil.Process(int(info["pid"]))
    process.terminate()
    try:
        process.wait(timeout=10)
    except psutil.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _read_message(conn):
    chunks = []
    size = 0
    while True:
        piece = conn.recv(65536)
        if not piece:
            break
        chunks.append(piece)
        size += len(piece)
        if size > 65536:
            raise ValueError("Comando de chat grande demais.")
        if b"\n" in piece:
            break
    line = b"".join(chunks).split(b"\n", 1)[0]
    if not line:
        raise ConnectionError("Conexão do chat encerrada.")
    return json.loads(line.decode("utf-8"))


def _write_message(conn, payload):
    conn.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")


def engine_command(info, command, timeout=30):
    payload = {**command, "token": info["token"]}
    with socket.create_connection(("127.0.0.1", int(info["port"])), timeout) as conn:
        conn.settimeout(timeout)
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        _write_message(conn, payload)
        return _read_message(conn)


def _ping(info):
    try:
        return bool(engine_command(info, {"cmd": "ping"}, timeout=5).get("ok"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False


def _log_tail():
    path = RUNS / ".chat-engine.log"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")[-500:]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _spawn():
    log_path = RUNS / ".chat-engine.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab")
    try:
        subprocess.Popen(
            [sys.executable, "-u", "-m", "dashboard.chat_engine"],
            cwd=ROOT,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "TOKENIZERS_PARALLELISM": "false"},
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    finally:
        log.close()


def ensure_engine():
    info = _read_endpoint()
    if info and _ping(info):
        return info
    if info:
        if _chat_still_running(info):
            raise ValueError("O chat ainda está gerando.")
        if _same_process(info):
            _kill(info)
            _wait_dead(info, 20)
        _unlink_if_same(info)
    _spawn()
    deadline = time.time() + 30
    while time.time() < deadline:
        info = _read_endpoint()
        if info and _ping(info):
            return info
        time.sleep(0.05)
    detail = _log_tail()
    message = "O modelo do chat não respondeu. Veja runs/.chat-engine.log."
    if detail:
        message = f"{message} {detail}"
    raise ValueError(message)


def dispatch_chat(run_id):
    info = ensure_engine()
    try:
        response = engine_command(info, {"cmd": "generate", "run_id": run_id}, timeout=30)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("O chat não respondeu a tempo.") from exc
    if not response.get("ok"):
        raise ValueError(response.get("error") or "O chat não iniciou.")


def release_resident_model():
    info = _read_endpoint()
    if not info:
        return
    response = None
    try:
        response = engine_command(info, {"cmd": "release"}, timeout=180)
    except (OSError, json.JSONDecodeError, ValueError):
        response = None
    if response is None or not response.get("ok"):
        if _same_process(info):
            _kill(info)
    if _same_process(info):
        _wait_dead(info, 60)
    _unlink_if_same(info)


def _generate(directory):
    from dashboard.chat_backend import generate_chat
    from dashboard.worker import environment

    global RESIDENT
    request = read_json(directory / "request.json")
    events = Events(directory)
    oom = False
    with (directory / "log.txt").open("a", encoding="utf-8", buffering=1) as run_log:
        previous = sys.stdout
        sys.stdout = _Tee(previous, run_log)
        try:
            if RESIDENT is None:
                RESIDENT = Resident()
            tokenizer = RESIDENT.tokenizer(events)
            if RESIDENT.base is None:
                from dashboard.training import limit_training_memory
                from dashboard.config import DEFAULTS
                limit_training_memory(request.get("config") or DEFAULTS, events, operation="chat")
            model = RESIDENT.model_for(request.get("adapter_run"), events, request.get("decensor_run"))
            write_json(directory / "environment.json", environment())
            generate_chat(request, tokenizer, directory, events, model=model, decoder=RESIDENT.decoder)
            events.check()
            events.status("completed", ended_at=time.time())
        except Cancelled as exc:
            print(str(exc), flush=True)
            events.status("cancelled", ended_at=time.time())
        except Exception as exc:
            traceback.print_exc()
            oom = is_cuda_memory_error(exc)
            error = "A GPU ficou sem memória. O modelo será recarregado na próxima tentativa; sua pergunta foi preservada." if oom else str(exc)
            events.status("failed", error=error, ended_at=time.time())
        finally:
            sys.stdout = previous
    if oom:
        model = None
        unload_resident()
    return not oom


def _production_handler(command):
    if command.get("cmd") != "generate":
        raise ValueError("Comando de chat desconhecido.")
    directory = run_path(command.get("run_id"))
    if read_json(directory / "status.json", {}).get("status") != "queued":
        raise ValueError("Este ID já foi executado.")
    mark_running(directory)

    def work():
        return _generate(directory)

    return work


def serve_requests(handler, endpoint, idle_seconds, before_release_ack=None, before_idle_exit=None):
    token = secrets.token_urlsafe(32)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(idle_seconds)
    port = listener.getsockname()[1]
    write_json(endpoint, {"pid": os.getpid(), "port": port, "token": token, "created": psutil.Process().create_time()})
    print(f"Chat residente escutando em 127.0.0.1:{port}.", flush=True)
    try:
        while True:
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                print("Chat ocioso por 20 minutos; liberando a GPU.", flush=True)
                if before_idle_exit:
                    before_idle_exit()
                break
            work = None
            try:
                conn.settimeout(30)
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                command = _read_message(conn)
                if not isinstance(command, dict) or not secrets.compare_digest(str(command.get("token", "")), token):
                    _write_message(conn, {"ok": False, "error": "Sessão do chat recusada."})
                    continue
                if command.get("cmd") == "ping":
                    _write_message(conn, {"ok": True})
                    continue
                if command.get("cmd") == "release":
                    if before_release_ack:
                        before_release_ack()
                    endpoint.unlink(missing_ok=True)
                    _write_message(conn, {"ok": True})
                    return
                try:
                    work = handler(command)
                    _write_message(conn, {"ok": True})
                except ValueError as exc:
                    _write_message(conn, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    traceback.print_exc()
                    _write_message(conn, {"ok": False, "error": str(exc)})
            except (OSError, json.JSONDecodeError, ValueError):
                traceback.print_exc()
            finally:
                conn.close()
            if work:
                try:
                    if work() is False:
                        # Exit the CUDA process as well as dropping Python refs.
                        break
                except Exception:
                    traceback.print_exc()
    finally:
        endpoint.unlink(missing_ok=True)
        listener.close()


def main():
    RUNS.mkdir(exist_ok=True)
    held = worker_lock()
    try:
        held.__enter__()
    except OSError:
        print("Chat: a GPU já está em uso por outro processo.", flush=True)
        return 0
    released = False

    def before_release_ack():
        nonlocal released
        try:
            unload_resident()
        except Exception:
            traceback.print_exc()
        finally:
            if not released:
                held.__exit__(None, None, None)
                released = True

    def before_idle_exit():
        try:
            unload_resident()
        except Exception:
            traceback.print_exc()

    try:
        serve_requests(
            _production_handler, _endpoint_path(), IDLE_SECONDS,
            before_release_ack=before_release_ack, before_idle_exit=before_idle_exit,
        )
    finally:
        if not released:
            held.__exit__(None, None, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
