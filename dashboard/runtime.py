"""Persistent job events and an OS-released exclusive worker lock."""

import contextlib
import math
import time

from dashboard.config import RUNS, read_json, write_json


class Cancelled(Exception):
    pass


class Events:
    def __init__(self, directory):
        self.directory = directory
        self.started = time.time()
        self.phase_name = None
        self.phase_started = self.started

    def cancelled(self):
        return (self.directory / "cancel.request").exists()

    def check(self):
        if self.cancelled():
            raise Cancelled("Execução interrompida pelo usuário.")

    def emit(self, **values):
        import json
        self.check()
        now = time.time()
        if "phase" in values:
            if values["phase"] != self.phase_name:
                self.phase_name = values["phase"]
                self.phase_started = now
                print(f"[Etapa] {self.phase_name}", flush=True)
            values.setdefault("phase_step", values.get("step"))
            values.setdefault("phase_total", values.get("total_steps"))
            values.setdefault("phase_unit", "itens")
            values["phase_started_at"] = self.phase_started
            values["phase_elapsed_seconds"] = now - self.phase_started
            done, total = values["phase_step"], values["phase_total"]
            rate = done / max(now - self.phase_started, 0.001) if done else None
            values["phase_rate"] = rate
            values["phase_eta_seconds"] = max(0, total - done) / rate if rate and total else None
        for key, value in list(values.items()):
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"Métrica {key} não finita: {value}. Revise o treinamento.")
        event = {"time": now, "elapsed_seconds": now - self.started, **values}
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")

    def status(self, status, **values):
        payload = read_json(self.directory / "status.json", {})
        payload.update(status=status, updated_at=time.time(), **values)
        write_json(self.directory / "status.json", payload)


@contextlib.contextmanager
def worker_lock():
    """A killed process releases this lock, including after a server restart."""
    import os
    RUNS.mkdir(exist_ok=True)
    with (RUNS / ".worker.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
