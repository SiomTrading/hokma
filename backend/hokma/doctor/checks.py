"""Installation checks behind `hokma doctor`. The text it prints comes from the message catalogs."""
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from hokma.core.proc import spawn_kwargs
from hokma.core.config import claude_config_dir, dashboard_dist, hokma_data_dir, hokma_home
from hokma.i18n import t

OK, WARN, FAIL = "ok", "warn", "fail"
CLAUDE_SETUP_URL = "https://docs.claude.com/en/docs/claude-code/setup"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _uv_path() -> str | None:
    local = hokma_data_dir() / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    if local.is_file():
        return str(local)
    return shutil.which("uv")


def check_uv() -> Check:
    path = _uv_path()
    if path:
        return Check("uv", OK, path)
    return Check("uv", FAIL, t("doctor.uv.missing"))


def check_python() -> Check:
    v = sys.version_info
    if v >= (3, 12):
        return Check("Python", OK, f"{v.major}.{v.minor}.{v.micro}")
    return Check("Python", FAIL, t("doctor.python.old", version=f"{v.major}.{v.minor}"))


def check_git() -> Check:
    path = shutil.which("git")
    if path:
        return Check("git", OK, path)
    return Check("git", FAIL, t("doctor.git.missing"))


def check_claude_cli() -> Check:
    name = t("doctor.claude_cli.name")
    path = shutil.which("claude")
    if path:
        return Check(name, OK, path)
    return Check(name, WARN, t("doctor.claude_cli.missing", url=CLAUDE_SETUP_URL))


def check_claude_session(timeout: float = 15.0) -> Check:
    """Ask `claude auth status` with Hokma's config dir. Never starts a login."""
    name = t("doctor.session.name")
    if not shutil.which("claude"):
        return Check(name, WARN, t("doctor.session.no_cli"))
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(claude_config_dir())}
    try:
        r = subprocess.run(["claude", "auth", "status"], env=env, capture_output=True,
                           text=True, timeout=timeout, stdin=subprocess.DEVNULL, **spawn_kwargs())
    except (subprocess.TimeoutExpired, OSError):
        return Check(name, WARN, t("doctor.session.unknown"))
    if r.returncode == 0 and '"loggedIn": false' not in r.stdout:
        return Check(name, OK, t("doctor.session.ok"))
    return Check(name, WARN, t("doctor.session.none", config=claude_config_dir()))


def check_engines() -> list[Check]:
    """One row per saved connection (binary, session or key as the last test saw them). No connection is a Warn, not a
    Fail: Hokma is healthy without an engine."""
    name = t("doctor.engines.name")
    try:
        from hokma.db import get_database

        with get_database().read() as u:
            rows = u.engine_connections.list()
    except Exception:
        return [check_claude_cli(), check_claude_session()]          # no database yet: the 0.0.8 checks
    if not rows:
        return [Check(name, WARN, t("doctor.engines.none"))]
    out = []
    for c in rows:
        label = f"{name}: {c.label}"
        if c.status == "connected":
            out.append(Check(label, OK, t("doctor.engines.ok")))
        elif c.status_detail == "region":
            out.append(Check(label, WARN, t("engines.err.region", provider=c.provider or c.engine)))
        elif c.status == "no_cli":
            out.append(Check(label, WARN, t("engines.err.not_found", cli=c.engine)))
        elif c.status == "no_session":
            out.append(Check(label, WARN, t("engines.err.auth", provider=c.provider or c.engine)))
        elif c.status == "error" and c.status_detail == "quota":
            out.append(Check(label, WARN, t("engines.err.quota", provider=c.provider or c.engine)))
        else:
            out.append(Check(label, WARN, t("doctor.engines.untested")))
    return out


def check_workdir() -> Check:
    name = t("doctor.workdir.name")
    home = hokma_home()
    if not home.is_dir():
        return Check(name, FAIL, t("doctor.workdir.missing", path=home))
    if not (home / ".git").exists():
        return Check(name, WARN, t("doctor.workdir.no_git", path=home))
    return Check(name, OK, str(home))


def check_dashboard() -> Check:
    name = t("doctor.dashboard.name")
    if (dashboard_dist() / "index.html").is_file():
        return Check(name, OK)
    return Check(name, FAIL, t("doctor.dashboard.missing"))


def check_runtime_outside_workdir() -> Check:
    """The agent works in the workdir: runtime and Claude config must not be reachable from there."""
    name = t("doctor.outside.name")
    work = hokma_home()
    bad = [str(p) for p in (hokma_data_dir(), claude_config_dir(), Path(sys.prefix), dashboard_dist())
           if _inside(p, work)]
    if _inside(work, hokma_data_dir()):
        bad.append(str(work))
    if bad:
        return Check(name, FAIL, t("doctor.outside.inside", paths=", ".join(bad)))
    return Check(name, OK, t("doctor.outside.ok", path=hokma_data_dir()))


def check_config_isolation() -> Check:
    name = t("doctor.isolation.name")
    expected = claude_config_dir()
    current = os.environ.get("CLAUDE_CONFIG_DIR")
    personal = Path.home() / ".claude"
    if not current:
        return Check(name, WARN, t("doctor.isolation.unset", expected=expected))
    cur = Path(current).resolve()
    if cur == personal.resolve():
        return Check(name, FAIL, t("doctor.isolation.personal"))
    if _inside(cur, hokma_home()):
        return Check(name, FAIL, t("doctor.isolation.in_workdir", path=cur))
    if cur != expected.resolve():
        return Check(name, WARN, t("doctor.isolation.other", path=cur, expected=expected))
    if not cur.is_dir():
        return Check(name, WARN, t("doctor.isolation.absent", path=cur))
    return Check(name, OK, str(cur))


def check_integrity() -> Check:
    from hokma.kit import integrity

    name = t("doctor.integrity.name")
    if not integrity.is_installed_app():
        return Check(name, WARN, t("doctor.integrity.dev"))
    problems = integrity.verify()
    if problems:
        return Check(name, FAIL, t("doctor.integrity.bad", problems="; ".join(problems[:5])))
    return Check(name, OK, t("doctor.integrity.ok"))


def check_sandbox() -> Check:
    name = t("doctor.sandbox.name")
    if sys.platform == "win32":
        return Check(name, WARN, t("doctor.sandbox.windows"))
    if sys.platform.startswith("linux"):
        missing = [tool for tool in ("bwrap", "socat") if not shutil.which(tool)]
        if missing:
            return Check(name, WARN, t("doctor.sandbox.missing", tools=" / ".join(missing)))
    return Check(name, OK, t("doctor.sandbox.ok"))


def check_settings_lock() -> Check:
    """Every protected path must have its rules, its sandbox denyWrite and its hook argument."""
    from hokma.kit.settings import _abs_rule_path, build_settings, protected_paths, settings_path

    name = t("doctor.locks.name")
    path = settings_path()
    if not path.is_file():
        return Check(name, WARN, t("doctor.locks.absent"))
    try:
        actual = json.loads(path.read_text(encoding="utf-8"))
        deny = actual["permissions"]["deny"]
        deny_write = actual["sandbox"]["filesystem"]["denyWrite"]
        hook = actual["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    except (KeyError, IndexError, ValueError, OSError):
        return Check(name, WARN, t("doctor.locks.incomplete"))
    missing = []
    if actual.get("disableAllHooks") is not False:
        missing.append("disableAllHooks")
    commands = " ".join(h.get("command", "") for g in actual["hooks"]["PreToolUse"] for h in g.get("hooks", []))
    if "hardblock.py" not in commands:
        missing.append("hook hardblock")
    for p in protected_paths():
        rule = _abs_rule_path(p)
        for needed in (f"Edit({rule})", f"Edit({rule}/**)", f"Write({rule})", f"Write({rule}/**)"):
            if needed not in deny:
                missing.append(needed)
        if str(p) not in deny_write:
            missing.append(f"sandbox.denyWrite {p}")
        if "--protect" not in hook or str(p) not in hook:
            missing.append(f"hook {p}")
    if missing:
        return Check(name, WARN, t("doctor.locks.missing", rules="; ".join(missing[:4])))
    if actual != build_settings():
        return Check(name, WARN, t("doctor.locks.differs"))
    return Check(name, OK, t("doctor.locks.ok", rules=len(deny), paths=len(protected_paths())))


def check_extensions() -> Check:
    from hokma.kit import extensions

    name = t("doctor.extensions.name")
    data, problems = extensions.load()
    if problems:
        return Check(name, WARN, "; ".join(problems[:3]))
    if extensions.mirror_differs():
        return Check(name, WARN, t("doctor.extensions.mirror"))
    return Check(name, OK, t("doctor.extensions.ok", plugins=len(data["enabledPlugins"]),
                             marketplaces=len(data["extraKnownMarketplaces"]), servers=len(data["mcpServers"])))


def run_checks() -> list[Check]:
    return [check_uv(), check_python(), check_git(), *check_engines(),
            check_workdir(), check_dashboard(), check_runtime_outside_workdir(),
            check_config_isolation(), check_integrity(), check_sandbox(), check_settings_lock(),
            check_extensions()]
