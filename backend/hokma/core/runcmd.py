"""Run one command for an engine: time limit, live output, nothing to type, and a Stop that ends the whole tree.

Task 3175. `run_command` of the direct engines used `subprocess.run(capture_output=True)`: the output arrived only
at the end, a Stop could not reach it (the turn waited until the timeout) and a `git` waiting for a password held it
until then. Here:

* stdin is closed (DEVNULL) and the environment is the non-interactive one of `envfilter`;
* stdout and stderr come through one pipe, read in a thread and handed to `on_output` as they arrive;
* at the time limit, or when `cancel` is set, the process and everything it started are killed (`proctree`);
* a grandchild that keeps the pipe open after its parent ended cannot hold the call: after `DRAIN_WAIT` seconds the
  rest of the tree is killed and the call returns.
"""
from __future__ import annotations

import codecs
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

from hokma.core.proc import CREATE_NEW_PROCESS_GROUP, spawn_kwargs
from hokma.core.proctree import ProcessTree

DRAIN_WAIT = 2.0
POLL = 0.1
KEEP = 2_000_000                 # characters kept in memory; the caller cuts what the model sees


@dataclass
class Outcome:
    text: str
    exit_code: int | None
    timed_out: bool = False
    cancelled: bool = False


def run(argv: list[str], cwd: str, env: dict[str, str] | None, timeout: float,
        on_output: Callable[[str], None] | None = None, cancel: threading.Event | None = None) -> Outcome:
    posix_group = {"start_new_session": True} if os.name == "posix" else {}
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, **spawn_kwargs(extra_flags=CREATE_NEW_PROCESS_GROUP),
                            **posix_group)
    tree = ProcessTree.of(proc)
    tree.watch_pipe(proc.stdout.fileno() if proc.stdout is not None else None)
    pieces: list[str] = []
    size = [0]
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def add(text: str) -> None:
        if not text:
            return
        pieces.append(text)
        size[0] += len(text)
        while size[0] > KEEP and len(pieces) > 1:
            size[0] -= len(pieces.pop(0))
        if on_output is not None:
            try:
                on_output(text)
            except Exception:            # noqa: BLE001  a closed panel never stops the command
                pass

    def reader() -> None:
        stream = proc.stdout
        assert stream is not None
        fd = stream.fileno()
        while True:
            try:
                chunk = os.read(fd, 8192)
            except OSError:
                break
            if not chunk:
                break
            add(decoder.decode(chunk))
        add(decoder.decode(b"", final=True))

    thread = threading.Thread(target=reader, name="hokma-command-output", daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    timed_out = cancelled = False
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            if cancel is not None:
                cancel.wait(POLL)
            else:
                time.sleep(POLL)
        if timed_out or cancelled:
            tree.kill()
            try:
                proc.wait(DRAIN_WAIT * 2)
            except subprocess.TimeoutExpired:
                proc.kill()
        thread.join(DRAIN_WAIT)
        if thread.is_alive():            # something the command started still holds the output: end it too
            tree.kill()
            thread.join(DRAIN_WAIT)
    finally:
        if proc.poll() is None:
            tree.kill()
        tree.close()
        try:
            if proc.stdout is not None and not thread.is_alive():
                proc.stdout.close()
        except OSError:
            pass
    return Outcome("".join(pieces), proc.returncode, timed_out, cancelled)
