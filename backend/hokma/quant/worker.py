"""Worker process of the quant calls: `python -I -m hokma.quant.worker`.

It reads framed pickles from stdin, runs a registered function and writes the framed reply to a private copy of
stdout (the real stdout is redirected to stderr so that library output cannot corrupt the protocol). The parent
(`hokma.quant.runner`) can kill this process when a call takes too long, which a thread could not be.

Frame: 8 bytes big endian length, then the pickle. Request: (name, kwargs). Reply: ("ok", result) or ("exc", exception)
or ("err", class name, message).

Trust note: the pickles travel only between Hokma and its own worker over private pipes; nothing from outside is ever unpickled here.
"""
from __future__ import annotations

import os
import pickle
import struct
import sys


def read_frame(stream) -> object | None:
    header = stream.read(8)
    if len(header) < 8:
        return None
    (size,) = struct.unpack(">Q", header)
    data = stream.read(size)
    if len(data) < size:
        return None
    return pickle.loads(data)


def write_frame(stream, obj: object) -> None:
    data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(struct.pack(">Q", len(data)) + data)
    stream.flush()


def main() -> int:
    reply = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    request_stream = sys.stdin.buffer
    from hokma.quant import registry

    registry.load_all()
    write_frame(reply, ("ready",))
    while True:
        message = read_frame(request_stream)
        if message is None:
            return 0
        name, kwargs = message
        try:
            spec = registry.describe(name)
            result = spec.fn(**kwargs)
            write_frame(reply, ("ok", result))
        except BaseException as exc:                      # noqa: BLE001  (every failure goes back to the caller)
            try:
                pickle.dumps(exc)
                write_frame(reply, ("exc", exc))
            except Exception:                             # noqa: BLE001
                write_frame(reply, ("err", type(exc).__name__, str(exc)[:500]))


if __name__ == "__main__":
    sys.exit(main())
