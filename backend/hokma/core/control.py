"""The local control channel of the supervisor: how `hokma stop` talks to a supervisor that has no terminal.

- POSIX: a Unix socket `<data>/control.sock`, created with mode 0600 (the process umask is tightened while it is
  bound, so there is no moment when another user could open it). On Linux the server also checks the user id of
  every peer (SO_PEERCRED) and drops a connection from any other user.
- Windows: the named pipe `\\\\.\\pipe\\hokma-control-{user}`, created with a DACL that gives access only to the
  current user, remote clients refused, and as the FIRST instance of that name, so nobody can take the name first.

The protocol is one line each way. The client sends a command word; the server answers one line of JSON. The
server knows exactly two words: `stop` and `status`. Anything else is answered with an error and nothing else
happens. The answers are built from a fixed list of keys (pid, port, start time, engine state): they can never
carry the session token, the one-time code or the owner confirmation, because the server never has them.

Standard library only.
"""
import json
import os
import re
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from hokma.core import config

COMMANDS = ("stop", "status")
STATUS_KEYS = ("pid", "port", "started_at", "engine")
MAX_REQUEST_BYTES = 64
MAX_REPLY_BYTES = 4096
CLIENT_TIMEOUT_SECONDS = 3.0
UNIX_PATH_LIMIT = 100                  # sun_path holds 104 or 108 bytes depending on the system


def pipe_name() -> str:
    import getpass

    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = "user"
    return r"\\.\pipe\hokma-control-" + re.sub(r"[^A-Za-z0-9_.-]", "_", user)


def answer(raw: bytes, status: Callable[[], dict], stop: Callable[[], None]) -> bytes:
    """The reply to one request line. Only `stop` and `status` do anything."""
    word = raw[:MAX_REQUEST_BYTES].split(b"\n", 1)[0].strip().decode("ascii", "replace")
    if word == "status":
        info = status()
        reply: dict = {"ok": True, **{k: info[k] for k in STATUS_KEYS if k in info}}
    elif word == "stop":
        stop()
        reply = {"ok": True, "stopping": True}
    else:
        reply = {"ok": False, "error": "unknown_command"}
    return json.dumps(reply).encode("ascii") + b"\n"


# ---- POSIX --------------------------------------------------------------------------------------------------

def _unix_address(path: Path) -> tuple[str, int | None]:
    """The address to bind or connect to. A path longer than the system allows is reached through the descriptor
    of its folder (Linux), which keeps the socket where the rest of the data is."""
    text = os.fspath(path)
    if len(text.encode()) < UNIX_PATH_LIMIT:
        return text, None
    if sys.platform.startswith("linux"):
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        return f"/proc/self/fd/{fd}/{path.name}", fd
    raise OSError("the control socket path is too long")


class _UnixServer:
    def __init__(self, status: Callable[[], dict], stop: Callable[[], None]) -> None:
        self._status, self._stop = status, stop
        self._sock: socket.socket | None = None
        self._closing = threading.Event()
        self._inode: int | None = None

    def start(self) -> None:
        path = config.control_socket_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.unlink()                          # a leftover of a supervisor that died; the lock says nobody is alive
        except FileNotFoundError:
            pass
        address, fd = _unix_address(path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            sock.bind(address)
        except OSError:
            sock.close()
            raise
        finally:
            os.umask(old_umask)
            if fd is not None:
                os.close(fd)
        os.chmod(path, 0o600)
        self._inode = os.stat(path).st_ino
        sock.listen(4)
        sock.settimeout(0.5)
        self._sock = sock
        threading.Thread(target=self._serve, name="control", daemon=True).start()

    def _serve(self) -> None:
        sock = self._sock
        while not self._closing.is_set() and sock is not None:
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(2.0)
                    if not self._peer_is_me(conn):
                        continue
                    raw = self._read_line(conn)
                    conn.sendall(answer(raw, self._status, self._stop))
                except OSError:
                    continue

    @staticmethod
    def _read_line(conn: socket.socket) -> bytes:
        data = b""
        while b"\n" not in data and len(data) < MAX_REQUEST_BYTES:
            chunk = conn.recv(MAX_REQUEST_BYTES - len(data))
            if not chunk:
                break
            data += chunk
        return data

    @staticmethod
    def _peer_is_me(conn: socket.socket) -> bool:
        option = getattr(socket, "SO_PEERCRED", None)
        if option is None:
            return True                            # the 0600 mode is the guard where the system gives no credentials
        _, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i")))
        return uid == os.getuid()

    def close(self) -> None:
        self._closing.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        path = config.control_socket_path()
        try:
            if self._inode is not None and path.stat().st_ino == self._inode:
                path.unlink()
        except OSError:
            pass


def _unix_request(command: str, timeout: float) -> dict | None:
    path = config.control_socket_path()
    if not path.exists():
        return None
    fd = None
    try:
        address, fd = _unix_address(path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(address)
            sock.sendall(command.encode("ascii") + b"\n")
            data = b""
            while b"\n" not in data and len(data) < MAX_REPLY_BYTES:
                chunk = sock.recv(MAX_REPLY_BYTES - len(data))
                if not chunk:
                    break
                data += chunk
        return _parse_reply(data)
    except OSError:
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _parse_reply(data: bytes) -> dict | None:
    try:
        reply = json.loads(data.split(b"\n", 1)[0])
    except ValueError:
        return None
    return reply if isinstance(reply, dict) else None


# ---- Windows (not run by the Linux tests) -------------------------------------------------------------------

class _PipeServer:
    """One instance of the named pipe, served one client at a time. Written against the Win32 documentation and
    NOT run on Windows by the automated tests."""

    PIPE_ACCESS_DUPLEX = 0x00000003
    FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
    PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
    ERROR_PIPE_CONNECTED = 535
    CREATE_RETRY_SECONDS = 5.0

    def __init__(self, status: Callable[[], dict], stop: Callable[[], None]) -> None:
        self._status, self._stop = status, stop
        self._handle = None
        self._closing = threading.Event()
        self._security = None

    def start(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._k = k = ctypes.WinDLL("kernel32", use_last_error=True)
        void = ctypes.c_void_p
        k.CreateNamedPipeW.restype = void
        k.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, void]
        k.ConnectNamedPipe.argtypes = [void, void]
        k.DisconnectNamedPipe.argtypes = [void]
        k.ReadFile.argtypes = [void, void, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), void]
        k.WriteFile.argtypes = [void, void, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), void]
        k.FlushFileBuffers.argtypes = [void]
        k.CloseHandle.argtypes = [void]
        self._security = self._user_only_security()
        deadline = time.monotonic() + self.CREATE_RETRY_SECONDS          # a supervisor that is handing over still holds the name
        while True:
            handle = k.CreateNamedPipeW(
                pipe_name(), self.PIPE_ACCESS_DUPLEX | self.FILE_FLAG_FIRST_PIPE_INSTANCE,
                self.PIPE_REJECT_REMOTE_CLIENTS, 1, MAX_REPLY_BYTES, MAX_REQUEST_BYTES, 0,
                ctypes.cast(ctypes.byref(self._security), void))
            if handle and handle != void(-1).value:
                break
            if time.monotonic() > deadline:
                raise ctypes.WinError(ctypes.get_last_error())
            time.sleep(0.2)
        self._handle = handle
        threading.Thread(target=self._serve, name="control", daemon=True).start()

    def _user_only_security(self):
        """SECURITY_ATTRIBUTES whose DACL allows only the current user (protected, nothing inherited)."""
        import ctypes
        from ctypes import wintypes

        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        k = self._k
        void = ctypes.c_void_p
        k.GetCurrentProcess.restype = void
        k.LocalFree.argtypes = [void]
        adv.OpenProcessToken.argtypes = [void, wintypes.DWORD, ctypes.POINTER(void)]
        adv.GetTokenInformation.argtypes = [void, ctypes.c_int, void, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        adv.ConvertSidToStringSidW.argtypes = [void, ctypes.POINTER(wintypes.LPWSTR)]
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(void), void]
        token = void()
        TOKEN_QUERY, TOKEN_USER = 0x0008, 1
        if not adv.OpenProcessToken(k.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = wintypes.DWORD()
            adv.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not adv.GetTokenInformation(token, TOKEN_USER, buffer, size, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(void))[0]
            sid_text = wintypes.LPWSTR()
            if not adv.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
                raise ctypes.WinError(ctypes.get_last_error())
            sid = sid_text.value
            k.LocalFree(ctypes.cast(sid_text, void))
        finally:
            k.CloseHandle(token)
        descriptor = void()
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(f"D:P(A;;GA;;;{sid})", 1,
                                                                         ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", void), ("bInheritHandle", wintypes.BOOL)]

        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        self._descriptor = descriptor              # kept alive for the life of the pipe
        return attributes

    def _serve(self) -> None:
        import ctypes
        from ctypes import wintypes

        k, handle = self._k, self._handle
        while not self._closing.is_set():
            connected = k.ConnectNamedPipe(handle, None) or ctypes.get_last_error() == self.ERROR_PIPE_CONNECTED
            if self._closing.is_set():
                return
            if connected:
                try:
                    buffer = ctypes.create_string_buffer(MAX_REQUEST_BYTES)
                    count = wintypes.DWORD()
                    if k.ReadFile(handle, buffer, MAX_REQUEST_BYTES, ctypes.byref(count), None):
                        reply = answer(buffer.raw[:count.value], self._status, self._stop)
                        k.WriteFile(handle, reply, len(reply), ctypes.byref(count), None)
                        k.FlushFileBuffers(handle)
                except Exception:                  # one bad client must not end the channel
                    pass
            k.DisconnectNamedPipe(handle)

    def close(self) -> None:
        self._closing.set()
        try:
            with open(pipe_name(), "r+b", buffering=0):      # wakes up ConnectNamedPipe
                pass
        except OSError:
            pass
        if self._handle is not None:
            self._k.CloseHandle(self._handle)
            self._handle = None


def _pipe_request(command: str, timeout: float) -> dict | None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with open(pipe_name(), "r+b", buffering=0) as pipe:
                pipe.write(command.encode("ascii") + b"\n")
                return _parse_reply(pipe.read(MAX_REPLY_BYTES))
        except OSError:
            if time.monotonic() > deadline:
                return None
            time.sleep(0.1)                        # the pipe is serving somebody else for a moment


# ---- the two entry points -----------------------------------------------------------------------------------

class ControlServer:
    """Open it in the supervisor: `ControlServer(status, stop).start()`. `status()` returns a dict with the keys of
    STATUS_KEYS; `stop()` starts a clean shutdown and returns at once."""

    def __init__(self, status: Callable[[], dict], stop: Callable[[], None]) -> None:
        self._impl = (_PipeServer if sys.platform == "win32" else _UnixServer)(status, stop)

    def start(self) -> None:
        self._impl.start()

    def close(self) -> None:
        self._impl.close()


def request(command: str, timeout: float = CLIENT_TIMEOUT_SECONDS) -> dict | None:
    """Send `stop` or `status` to the supervisor of this data folder. None when nobody answers."""
    if command not in COMMANDS:
        raise ValueError(f"unknown control command {command!r}")
    return _pipe_request(command, timeout) if sys.platform == "win32" else _unix_request(command, timeout)
