"""Optional parts with native code never stop Hokma from starting (task 3288).

A student's engine died with 0xC0000005 (access violation) a few seconds after the plugin migration, four times in a
row, and the supervisor gave up. The cause is a native library (a loadable SQLite extension, onnxruntime, a TLS
binding) that crashes on that computer; Python cannot catch it, so the whole engine ends.

The rule: an optional native part is tried first in a short child process (`hokma.core.proc.spawn_kwargs`, no window).
Only when the child ends well is the part loaded in the engine. A child that dies or takes too long leaves the part
off: engine.log gets its name and exit code, Hokma starts anyway and the app says it in one line. The answer is
remembered in `<data>/native-probe.json` (per Hokma version and per Python), so the probe does not run at every start
and runs again after an update.

A part that only timed out (slow computer, first start) is tried again at the next start; one that crashed stays off
until an update. The embedder gets 90 s, the first start loads a 100+ MB model.

Safe mode: when the engine still crashes four times in a row, the supervisor writes `safe_mode` in the same file and
starts it again with every optional native part off (nothing is probed, nothing is loaded). An update clears it.

`HOKMA_NATIVE_PROBE=off` stops the probing (tests, development): every part is allowed, except in safe mode.
Standard library only, so the supervisor can import it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from hokma.core.proc import spawn_kwargs

SQLITE_VEC, EMBEDDER, TRUSTSTORE, VOICE = "sqlite_vec", "embedder", "truststore", "voice"
PARTS = (SQLITE_VEC, EMBEDDER, TRUSTSTORE, VOICE)
TIMEOUT_SECONDS = {SQLITE_VEC: 20.0, EMBEDDER: 90.0, TRUSTSTORE: 15.0, VOICE: 20.0}
STARTUP_PARTS = (SQLITE_VEC, TRUSTSTORE)         # probed before the database opens; the others when first needed
ENV_SWITCH = "HOKMA_NATIVE_PROBE"
STDERR_TAIL = 300

Runner = Callable[[str, float], "tuple[int | None, str]"]

_lock = threading.RLock()
_state: dict[str, Any] | None = None             # the file, read once per process
_part_locks = {part: threading.Lock() for part in PARTS}


def enabled() -> bool:
    return os.environ.get(ENV_SWITCH, "").strip().lower() not in ("off", "0", "false", "no")


def _version() -> str:
    from hokma import __version__

    return __version__


def state_path() -> Path:
    from hokma.core import config

    return config.hokma_data_dir() / "native-probe.json"


def _fresh() -> dict[str, Any]:
    return {"version": _version(), "python": sys.executable, "safe_mode": False, "parts": {}}


def _load() -> dict[str, Any]:
    """The remembered answers; an empty record when the file is missing, damaged, or from another Hokma or Python."""
    global _state
    with _lock:
        if _state is None:
            try:
                data = json.loads(state_path().read_text(encoding="utf-8"))
                same = data.get("version") == _version() and data.get("python") == sys.executable
                _state = data if same and isinstance(data.get("parts"), dict) else _fresh()
            except (OSError, ValueError, AttributeError):
                _state = _fresh()
            _state.setdefault("safe_mode", False)
            # A part that was off only because it was slow (timeout) is tried again at the next start: a first start on a
            # slow disk or behind an antivirus scan is not a crash. Crashes stay off until an update (Version/Python change).
            _state["parts"] = {name: entry for name, entry in _state["parts"].items()
                               if not (isinstance(entry, dict) and not entry.get("ok") and entry.get("reason") == "timeout")}
        return _state


def _save(state: dict[str, Any]) -> None:
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass                                              # not remembering only means probing again next time


def reset_for_tests() -> None:
    global _state
    with _lock:
        _state = None


reload = reset_for_tests              # the supervisor reads the file again after the engine changed it (retry, update)


def clear() -> None:
    """"Try again" of the panel: forget every answer and safe mode (delete native-probe.json); the next start probes again."""
    try:
        state_path().unlink(missing_ok=True)
    except OSError:
        pass
    reset_for_tests()


# ---------------------------------------------------------------------------------------------------- log

def _log(message: str) -> None:
    try:
        from hokma.engine import errlog

        errlog.log(message, scope="native")
    except Exception:                                     # noqa: BLE001  (a log problem never stops the start)
        pass


def format_code(code: int | None) -> str:
    """`0xC0000005` for a Windows status, `-11` for a POSIX signal, `timeout` when it never ended."""
    if code is None:
        return "timeout"
    return f"0x{code & 0xFFFFFFFF:08X}" if code > 255 or code < -255 else str(code)


# ---------------------------------------------------------------------------------------------------- the child

def run_child(part: str, timeout: float) -> tuple[int | None, str]:
    """Run the probe of `part` in a new Python. Returns (exit code or None on timeout, tail of its standard error)."""
    argv = [sys.executable, "-I", "-m", "hokma.core.nativeprobe", part]
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              timeout=timeout, **spawn_kwargs())
    except subprocess.TimeoutExpired:
        return None, ""
    except OSError as exc:                                # the interpreter itself cannot start: treat as a failure
        return 127, f"{type(exc).__name__}: {exc}"
    return done.returncode, (done.stderr or b"").decode("utf-8", "replace").strip()[-STDERR_TAIL:]


def _exercise(part: str) -> None:
    """What the child does: the first real use of the native code, nothing else. Any Python error ends it with 1."""
    if part == SQLITE_VEC:
        from hokma.db import vecext

        support = vecext.smoke()
        if not support.ok:
            raise RuntimeError(f"{support.code}: {support.detail}")
    elif part == EMBEDDER:
        from hokma.library import runtime
        from hokma.library.embedder import Embedder

        manifest = runtime.load_manifest()
        directory = runtime.model_directory()
        embedder = Embedder(directory / manifest["files"][0]["name"], directory / manifest["files"][1]["name"])
        embedder.embed_query("probe")
        embedder.close()
    elif part == TRUSTSTORE:
        import ssl

        import truststore

        truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    elif part == VOICE:
        import ctranslate2  # noqa: F401
        import faster_whisper  # noqa: F401
        import onnxruntime  # noqa: F401
    else:
        raise ValueError(part)


# ---------------------------------------------------------------------------------------------------- the answers

def _key(part: str) -> str:
    """Anything that makes an old answer stale: the embedder is probed with the model files, so a new model id asks again."""
    if part == EMBEDDER:
        try:
            from hokma.library import runtime

            return runtime.model_id()
        except Exception:                                 # noqa: BLE001
            return ""
    return ""


def probe(part: str, runner: Runner | None = None, *, force: bool = False) -> dict[str, Any]:
    """The remembered answer for `part`, probing first when there is none. {"ok": bool, "code": int | None, ...}.
    A part that is off is written to engine.log with its name and code, once per probe."""
    state = _load()
    with _part_locks[part]:
        known = state["parts"].get(part)
        if known is not None and not force and known.get("key", "") == _key(part):
            return known
        code, tail = (runner or run_child)(part, TIMEOUT_SECONDS[part])
        entry: dict[str, Any] = {"ok": code == 0, "code": code, "key": _key(part)}
        if code != 0:
            entry["reason"] = "timeout" if code is None else "crash"
            if tail:
                entry["detail"] = tail
        with _lock:
            state["parts"][part] = entry
            _save(state)
        if code != 0:
            _log(f"native part off: {part} ({'timed out' if code is None else 'ended with code ' + format_code(code)}); "
                 "Hokma starts without it" + (f"\n{tail}" if tail else ""))
        return entry


def allowed(part: str, runner: Runner | None = None) -> bool:
    """May this process load the optional native `part`? Safe mode: never. Otherwise only when its probe child ended well."""
    if _load().get("safe_mode"):                          # the supervisor's decision: it holds whatever the switch says
        return False
    if not enabled():
        return True
    return bool(probe(part, runner)["ok"])


def probe_startup(runner: Runner | None = None) -> None:
    """Probe the parts the start needs, side by side (the first start after an install or an update; later ones read the
    remembered answer). Never raises."""
    if not enabled() or _load().get("safe_mode"):
        return
    try:
        with ThreadPoolExecutor(max_workers=len(STARTUP_PARTS)) as pool:
            list(pool.map(lambda p: probe(p, runner), STARTUP_PARTS))
    except Exception:                                     # noqa: BLE001
        pass


# ---------------------------------------------------------------------------------------------------- safe mode

def safe_mode() -> bool:
    return bool(_load().get("safe_mode"))


def set_safe_mode(on: bool = True) -> None:
    """Called by the supervisor (another process than the engine) before it starts the engine again."""
    state = _load()
    with _lock:
        state["safe_mode"] = bool(on)
        _save(state)


def off_parts() -> list[dict[str, Any]]:
    """Parts the answers say are off: [{"part", "code", "reason"}]."""
    if not enabled():
        return []
    return [{"part": name, "code": entry.get("code"), "reason": entry.get("reason", "crash")}
            for name, entry in _load()["parts"].items() if not entry.get("ok")]


def _notice(part: str) -> str:
    """The sentence for a part that is off. Literal keys, so the catalog check sees them."""
    from hokma.i18n import t

    return {
        SQLITE_VEC: lambda: t("native.off.sqlite_vec"),
        EMBEDDER: lambda: t("native.off.embedder"),
        TRUSTSTORE: lambda: t("native.off.truststore"),
        VOICE: lambda: t("native.off.voice"),
    }[part]()


def notices() -> list[str]:
    """One plain line per thing that is off, in the student's language (for /health and the Library)."""
    from hokma.i18n import t

    lines = [t("native.safe_mode")] if safe_mode() else []
    return lines + [_notice(item["part"]) for item in off_parts() if item["part"] in PARTS]


def summary() -> dict[str, Any]:
    return {"safe_mode": safe_mode(), "off": [item["part"] for item in off_parts()], "notices": notices()}


if __name__ == "__main__":                                # the probe child: `python -I -m hokma.core.nativeprobe <part>`
    _exercise(sys.argv[1])
