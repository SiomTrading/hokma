"""Runs registered quant functions in worker processes that can be killed (resource exhaustion guard).

Each slot is one long lived worker process. A call takes a slot, sends (name, kwargs) and waits up to the time limit.
If the time runs out the worker process is killed and joined before the slot is given back, so a call that exceeds
its time is dead, not abandoned. A global semaphore limits how many calls run at once and is released only when the
worker has really finished or died.

Trust note: the pickles travel only between Hokma and its own worker over private pipes; nothing from outside is ever unpickled here.
"""
from __future__ import annotations

import pickle
import queue
import subprocess
import sys
import threading
from typing import Any

from hokma.core.proc import spawn_kwargs
from hokma.quant import limits
from hokma.quant.errors import BadInput
from hokma.quant.worker import read_frame, write_frame

_LOCK = threading.Lock()
_SEMAPHORE = threading.BoundedSemaphore(limits.MAX_CONCURRENT)
_FREE: list["_Slot"] = []


class _Slot:
    def __init__(self) -> None:
        self.proc = subprocess.Popen([sys.executable, "-I", "-m", "hokma.quant.worker"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0, **spawn_kwargs())
        self.replies: "queue.Queue[object]" = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True, name="quant-reader")
        self._reader.start()

    def _read(self) -> None:
        stream = self.proc.stdout
        assert stream is not None
        buffered = _Buffered(stream)
        while True:
            try:
                frame = read_frame(buffered)
            except Exception:                                  # noqa: BLE001
                frame = None
            self.replies.put(frame)
            if frame is None:
                return

    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        for step in (self.proc.terminate, self.proc.kill):
            if self.proc.poll() is not None:
                break
            try:
                step()
                self.proc.wait(timeout=5)
            except Exception:                                  # noqa: BLE001
                continue
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                if stream:
                    stream.close()
            except Exception:                                  # noqa: BLE001
                pass


class _Buffered:
    """read(n) that loops until n bytes arrive or the stream ends (unbuffered pipes return short reads)."""

    def __init__(self, stream) -> None:
        self.stream = stream

    def read(self, n: int) -> bytes:
        chunks = []
        while n > 0:
            part = self.stream.read(n)
            if not part:
                break
            chunks.append(part)
            n -= len(part)
        return b"".join(chunks)


def _take_slot() -> _Slot:
    with _LOCK:
        while _FREE:
            slot = _FREE.pop()
            if slot.alive():
                return slot
            slot.kill()
    return _Slot()


def run(name: str, kwargs: dict[str, Any], *, timeout: float) -> Any:
    """Call the registered function `name` in a worker. Raises what the function raised; BadInput on timeout."""
    if not _SEMAPHORE.acquire(timeout=timeout):
        raise BadInput(f"{name}: too many quant calls running at once; try again")
    slot: _Slot | None = None
    try:
        slot = _take_slot()
        try:
            data = pickle.dumps((name, kwargs), protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as exc:                               # noqa: BLE001
            raise BadInput(f"{name}: the arguments cannot be sent to a worker ({type(exc).__name__})") from exc
        assert slot.proc.stdin is not None
        import struct

        try:
            slot.proc.stdin.write(struct.pack(">Q", len(data)) + data)
            slot.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            slot.kill()
            slot = None
            raise BadInput(f"{name}: the worker is not available") from exc
        deadline_ok = True
        while True:
            try:
                reply = slot.replies.get(timeout=timeout)
            except queue.Empty:
                deadline_ok = False
                break
            if reply == ("ready",):                            # the first frame of a new worker
                continue
            break
        if not deadline_ok:
            slot.kill()
            slot = None
            raise BadInput(f"{name}: took longer than {timeout:g} seconds and was stopped; reduce the size of the inputs")
        if reply is None:
            slot.kill()
            slot = None
            raise BadInput(f"{name}: the worker stopped before answering (not enough memory or a crash)")
        kind = reply[0]  # type: ignore[index]
        with _LOCK:
            _FREE.append(slot)
        slot = None
        if kind == "ok":
            return reply[1]  # type: ignore[index]
        if kind == "exc":
            raise reply[1]  # type: ignore[index]
        raise RuntimeError(f"{reply[1]}: {reply[2]}")  # type: ignore[index]
    finally:
        if slot is not None:
            slot.kill()
        _SEMAPHORE.release()


def shutdown() -> None:
    """Stop every idle worker (tests and server shutdown)."""
    with _LOCK:
        slots = list(_FREE)
        _FREE.clear()
    for slot in slots:
        slot.kill()
