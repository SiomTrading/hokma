"""The engine process: what `hokma engine` runs, always as a child of the supervisor (`hokma start`).

Order matters: the session token is read from standard input and fixed BEFORE the application is imported,
because the application reads it once at import time.
"""
import os
import sys
import threading

from hokma.core import session

PARENT_GONE_GRACE_SECONDS = 5.0          # the supervisor died: the engine is gone within this time
CLEAN_STOP_GRACE_SECONDS = 9.0           # the supervisor asked for a stop: a little less than its own 10 s


def read_token(stream) -> str:
    """One line from the supervisor. Anything that is not a valid token is refused."""
    line = stream.readline().strip()
    if not session.TOKEN_PATTERN.fullmatch(line):
        raise ValueError("no valid session token on standard input")
    return line


def read_owner_hash(stream) -> str:
    """The second line from the supervisor: the hash of the owner confirmation, or an empty line when none yet."""
    line = stream.readline().strip()
    if line and not session.OWNER_HASH_PATTERN.fullmatch(line):
        raise ValueError("no valid owner confirmation hash on standard input")
    return line


def read_owner_secret_hash(stream) -> str:
    """The third line: the hash of the one-time secret that still waits for its registration, or empty."""
    line = stream.readline().strip()
    if line and not session.OWNER_HASH_PATTERN.fullmatch(line):
        raise ValueError("no valid owner secret hash on standard input")
    return line


def read_channel_key(stream) -> str:
    """The fourth line: the key that signs the note to the supervisor, or empty (tests)."""
    line = stream.readline().strip()
    if line and not session.CHANNEL_KEY_PATTERN.fullmatch(line):
        raise ValueError("no valid channel key on standard input")
    return line


def watch_parent(stream, on_gone, on_stop=None) -> threading.Thread:
    """Watch the supervisor's end of standard input. Its end means the supervisor is gone: call `on_gone`. Any data
    (the supervisor writes a `stop` line before it closes the pipe) means a clean stop was asked: call `on_stop`
    when given. Without `on_stop` data is ignored, as it always was."""
    def watch() -> None:
        try:
            while True:
                data = stream.read(1)
                if not data:
                    break
                if on_stop is not None:
                    on_stop()
                    return
        except (OSError, ValueError):
            pass
        on_gone()

    thread = threading.Thread(target=watch, name="parent-watch", daemon=True)
    thread.start()
    return thread


def main(port: int) -> int:
    if sys.stdin.isatty():
        print("engine: this command is started by `hokma start`", file=sys.stderr)
        return 2
    try:
        token = read_token(sys.stdin)
        owner_hash = read_owner_hash(sys.stdin)
        owner_secret_hash = read_owner_secret_hash(sys.stdin)
        session.adopt(token, owner_hash, owner_secret_hash, read_channel_key(sys.stdin))
    except (ValueError, RuntimeError) as exc:
        print(f"engine: {exc}", file=sys.stderr)
        return 2
    from hokma.core.lifecycle import Lifecycle, checkpoint_database
    from hokma.core.server import run

    lifecycle = Lifecycle()

    def end_within(seconds: float) -> None:
        lifecycle.stop()
        timer = threading.Timer(seconds, lambda: os._exit(1))
        timer.daemon = True
        timer.start()

    watch_parent(sys.stdin, lambda: end_within(PARENT_GONE_GRACE_SECONDS),
                 lambda: end_within(CLEAN_STOP_GRACE_SECONDS))
    code = run(lifecycle=lifecycle, port=port)
    checkpoint_database()                  # the server has ended: nothing writes any more
    return code
