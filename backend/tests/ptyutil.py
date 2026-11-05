"""Run a command attached to a pseudo-terminal, like a student typing in a real terminal."""
import os
import pty
import subprocess


def run_tty(args, env, text: str = "", cwd=None, timeout: int = 120) -> tuple[int, str]:
    master, slave = pty.openpty()
    proc = subprocess.Popen([str(a) for a in args], stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd,
                            close_fds=True, start_new_session=True)
    os.close(slave)
    if text:
        text += "\x04"          # Ctrl-D at the start of a line: end of input, so a prompt never blocks the test
        os.write(master, text.encode())
    out = b""
    try:
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:      # EIO when the child closed the terminal
                break
            if not chunk:
                break
            out += chunk
        proc.wait(timeout=timeout)
    finally:
        os.close(master)
    return proc.returncode, out.decode(errors="replace")
