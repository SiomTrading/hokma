"""Pseudo-terminal back ends: POSIX `pty` (Linux, macOS) and `pywinpty` (Windows).

Both expose the same small surface and push what the shell prints, as bytes, into an asyncio queue.
`None` in the queue means end of output.
"""
import asyncio
import os
import signal
import subprocess
import sys
import threading

IS_WINDOWS = sys.platform == "win32"
READ_SIZE = 65536
HIGH_WATER = 32          # queued chunks before the reader pauses (back pressure on a flooding command)


class PtyUnavailable(RuntimeError):
    """No pseudo-terminal support on this machine (pywinpty missing on Windows)."""


class PosixPty:
    def __init__(self, argv: list[str], cwd: str, env: dict[str, str], cols: int, rows: int) -> None:
        import fcntl
        import pty
        import struct
        import termios

        self._fcntl, self._struct, self._termios = fcntl, struct, termios
        master, slave = pty.openpty()
        self._master = master
        self._set_size_fd(slave, cols, rows)

        def become_tty_owner() -> None:      # runs in the child, after setsid
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        try:
            self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
                                         close_fds=True, start_new_session=True, preexec_fn=become_tty_owner)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self.pid = self.proc.pid
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._reading = False
        self._pending = bytearray()
        self._writing = False
        self._closed = False
        self._master_open = True

    def _close_master(self) -> None:
        if self._master_open:
            self._master_open = False
            try:
                os.close(self._master)
            except OSError:
                pass

    def _set_size_fd(self, fd: int, cols: int, rows: int) -> None:
        self._fcntl.ioctl(fd, self._termios.TIOCSWINSZ, self._struct.pack("HHHH", rows, cols, 0, 0))

    # reading
    def start_reading(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        self._loop, self._queue = loop, queue
        self._add_reader()

    def _add_reader(self) -> None:
        if self._loop and not self._reading and not self._closed:
            self._loop.add_reader(self._master, self._on_readable)
            self._reading = True

    def _remove_reader(self) -> None:
        if self._loop and self._reading:
            self._loop.remove_reader(self._master)
            self._reading = False

    def _on_readable(self) -> None:
        try:
            data = os.read(self._master, READ_SIZE)
        except BlockingIOError:
            return
        except OSError:                      # EIO: every holder of the slave side is gone
            data = b""
        assert self._queue is not None
        if not data:
            self._remove_reader()
            self._queue.put_nowait(None)
            return
        self._queue.put_nowait(data)
        if self._queue.qsize() >= HIGH_WATER:
            self._remove_reader()

    def drained(self) -> None:
        """The consumer took a chunk: resume reading if it was paused."""
        if self._queue is not None and self._queue.qsize() < HIGH_WATER // 2:
            self._add_reader()

    # writing
    def write(self, data: bytes) -> None:
        if self._closed:
            return
        self._pending += data
        self._flush()

    def _flush(self) -> None:
        while self._pending and not self._closed:
            try:
                written = os.write(self._master, self._pending)
            except BlockingIOError:
                if self._loop and not self._writing:
                    self._loop.add_writer(self._master, self._flush)
                    self._writing = True
                return
            except OSError:
                self._pending.clear()
                break
            del self._pending[:written]
        if self._writing and self._loop:
            self._loop.remove_writer(self._master)
            self._writing = False

    def resize(self, cols: int, rows: int) -> None:
        if self._closed:
            return
        self._set_size_fd(self._master, cols, rows)
        try:
            os.killpg(self.pid, signal.SIGWINCH)
        except OSError:
            pass

    # lifetime
    def exit_code(self) -> int | None:
        return self.proc.poll()

    async def wait_exit(self, timeout: float) -> int | None:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while self.proc.poll() is None and loop.time() < end:
            await asyncio.sleep(0.02)
        return self.proc.poll()

    def _signal_group(self, sig: int) -> None:
        try:
            os.killpg(self.pid, sig)
        except OSError:
            pass
        for member in self._session_members():       # background jobs of an interactive shell have their own group
            try:
                os.kill(member, sig)
            except OSError:
                pass

    def _session_members(self) -> list[int]:
        """Processes still in the shell's session (the shell is the session leader, so session id == its pid)."""
        found: list[int] = []
        try:
            if os.path.isdir("/proc/self"):
                for entry in os.listdir("/proc"):
                    if not entry.isdigit() or int(entry) == self.pid:
                        continue
                    try:
                        with open(f"/proc/{entry}/stat", "rb") as handle:
                            fields = handle.read().rsplit(b")", 1)[1].split()
                    except OSError:
                        continue
                    if len(fields) > 3 and fields[3] == str(self.pid).encode():
                        found.append(int(entry))
            else:                                    # macOS and others: same session token as the leader
                out = subprocess.run(["ps", "-axo", "pid=,sess="], capture_output=True, text=True, timeout=3).stdout
                rows = [line.split() for line in out.splitlines() if len(line.split()) == 2]
                leader = next((sess for pid, sess in rows if pid == str(self.pid)), None)
                if leader is not None:
                    found = [int(pid) for pid, sess in rows if sess == leader and int(pid) != self.pid]
        except Exception:
            pass
        return found

    async def terminate(self, grace: float = 1.5) -> None:
        """Hang up the whole process group, then kill what is left. Always reaps the child."""
        self._remove_reader()
        if self._loop and self._writing:
            self._loop.remove_writer(self._master)
            self._writing = False
        self._closed = True
        self._close_master()                 # the shell gets SIGHUP when the master goes away
        self._signal_group(signal.SIGHUP)
        self._signal_group(signal.SIGCONT)
        if await self.wait_exit(grace) is None:
            self._signal_group(signal.SIGKILL)
            await self.wait_exit(grace)
        # the leader is gone; hang up anything left in its group
        self._signal_group(signal.SIGHUP)
        self._signal_group(signal.SIGKILL)

    def kill_now(self) -> None:
        """Synchronous kill for process exit or a cancelled handler."""
        self._closed = True
        try:
            self._remove_reader()
        except Exception:
            pass
        self._signal_group(signal.SIGKILL)
        try:
            self.proc.wait(timeout=2)
        except Exception:
            pass
        self._close_master()


class WinPty:
    def __init__(self, argv: list[str], cwd: str, env: dict[str, str], cols: int, rows: int) -> None:
        try:
            from winpty import PtyProcess      # pywinpty; only installed with sys_platform == 'win32'
        except ImportError as exc:
            raise PtyUnavailable("pywinpty is not installed") from exc
        self._proc = PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(rows, cols))
        self.pid = self._proc.pid
        self._closed = False

    def start_reading(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        def pump() -> None:
            try:
                while not self._closed:
                    text = self._proc.read(READ_SIZE)
                    if not text:
                        continue
                    loop.call_soon_threadsafe(queue.put_nowait, text.encode("utf-8", "replace"))
            except (EOFError, OSError):
                pass
            finally:
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, None)
                except RuntimeError:      # loop already closed
                    pass

        threading.Thread(target=pump, name="hokma-winpty-read", daemon=True).start()

    def drained(self) -> None:
        return None

    def write(self, data: bytes) -> None:
        if not self._closed:
            self._proc.write(data.decode("utf-8", "replace"))

    def resize(self, cols: int, rows: int) -> None:
        if not self._closed:
            self._proc.setwinsize(rows, cols)

    def exit_code(self) -> int | None:
        return None if self._proc.isalive() else (self._proc.exitstatus or 0)

    async def wait_exit(self, timeout: float) -> int | None:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while self._proc.isalive() and loop.time() < end:
            await asyncio.sleep(0.05)
        return self.exit_code()

    async def terminate(self, grace: float = 1.5) -> None:
        self._closed = True
        try:
            self._proc.terminate(force=True)
        except Exception:
            pass
        await self.wait_exit(grace)

    def kill_now(self) -> None:
        self._closed = True
        try:
            self._proc.terminate(force=True)
        except Exception:
            pass


def open_pty(argv: list[str], cwd: str, env: dict[str, str], cols: int, rows: int):
    cls = WinPty if IS_WINDOWS else PosixPty
    return cls(argv, cwd, env, cols, rows)
