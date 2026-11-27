"""Strategies written as code (SPEC-031 A2): the requirement, the code with its checks, the reviewer's verdict, the owner's approval and the load.

A strategy of the bank can be a file of Python that the strategy coder writes against a requirement, tests first. This module is the only door between
that file and the rest of Hokma. The requirement (a brief) is written by the strategy researcher and is the first gate: without a current one no code is
saved. The code is copied to the data folder read only, hashed and checked by `hokma.factory.rule_check` in a process of its own, and stored as a draft. The
code reviewer is the second gate: only an approved verdict makes a version `generated`, and in the same transaction the version of the bank, the saved spec
that the optimizer needs and the link between them are created (the base refuses the link and the state without that verdict). `owner_reviewed` comes only
from the owner. The agents never write the database: they ask these functions through the tools, and who asks and where it was created are set by the code.

A generated strategy runs in the process of the backtest, and only when its state is generated or owner_reviewed, the hash of its file is the one the base
keeps and the static check is green at the moment it is loaded.

Accepted risk (decided by the owner): on the PC of the student the generated code is loaded in the process of the backtest. What stands between the file and that
process is the static check, the code reviewer, the tests that Ben runs again and the approval of the owner. In the server (milestone B) the load moves to an isolated
worker. The roles are checked here, in the server, and not only by the hook of the engine: only the strategy researcher writes a requirement, only the strategy coder
saves code and only the strategy reviewer gives a verdict, and never the one that saved that very version.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import yaml

from hokma.core.config import hokma_data_dir, hokma_home
from hokma.core.proc import spawn_kwargs
from hokma.db import Database, get_database
from hokma.db.errors import IntegrityViolation
from hokma.desks.core.hashing import content_hash
from hokma.desks.trading import sizing
from hokma.factory import strategy_replay, user_api
from hokma.factory.indicator_library import LIBRARY
from hokma.factory.orders import ATR, Pct, Price, Ticks, buy, protective_price, reduce_intent, sell
from hokma.factory.params import Param, ParamSchema
from hokma.factory.parts import SIZERS, Env, build_part
from hokma.factory.registry import Entry, Registry
from hokma.factory.strategy import BadStrategy, BarContext, InitContext, NoEntry, apply_requests
from hokma.factory.strategy import IndicatorRequest as RealRequest
from hokma.factory.usercode import static_check
from hokma.sources.ingest import SourceError

SLUG = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
APPROVED = ("generated", "owner_reviewed")
MAX_REJECTIONS = 3
RESEARCHER = CODER = "strategy-creator"      # 1.3.0: one creator writes the requirement and the code
REVIEWER = "strategy-reviewer"      # the usual agents of each step; any agent may take a step (1.2.2)
STRATEGY_FOLDER_FILES = frozenset({"strategy.py", "test_strategy.py"})
CHECK_TIMEOUT = 120
STRATEGY_FILE = "strategy.py"
TEST_FILE = "test_strategy.py"
SEVERITIES = ("blocker", "major", "minor", "nit")
SIZER_KINDS = ("fixed_contracts", "fixed_risk", "vol_target")
BAR_FIELDS = ("open", "high", "low", "close", "volume")
PREMIUM_WORDS = re.compile(r"\b(?:sell(?:ing)? (?:a )?(?:premium|options?|puts?|calls?)|covered calls?|cash[- ]secured|short (?:strangle|straddle)|iron condor|credit spread|"
                           r"vender prima|venta de prima)\b", re.I)
MAX_TEXT = 1000
sys.modules.setdefault("user_api", user_api)       # the short spelling of the one module a generated file may import (the hooks refuse the long one)


class UserRuleError(SourceError):
    """A classified failure: bad_input, no_data or blocked."""


# ---------------------------------------------------------------------------------------------------- places
def drafts_dir() -> Path:
    """Where the coder writes: the working folder of the owner, one folder for each strategy."""
    return hokma_home() / "service" / "strategies"


def copies_dir() -> Path:
    """The official copies, written by this module only and never edited: one folder for each version and hash."""
    return hokma_data_dir() / "strategies" / "user"


def copy_folder(slug: str, version: int, sha: str) -> Path:
    return copies_dir() / slug / f"v{version}-{sha[:12]}"


def code_of(bank_id: int) -> str:
    """The code of a strategy: STR-{NNN}, NNN the id of its card in the bank with three digits."""
    return f"STR-{int(bank_id):03d}"


def strategy_folder(bank_id: int, slug: str) -> Path | None:
    """The folder of a strategy in the fund: service/strategies/STR-{NNN}-{slug}/, created with its card in the bank. It holds spec.md, checklist.md, plan.md, tasks.md, adr/, results/ and
    versions/v{N}/strategy.py with versions/v{N}/test_strategy.py. A version that was saved is never written over: the next one is v{N+1}."""
    folder = drafts_dir() / f"{code_of(bank_id)}-{slug}"
    try:
        check_inside(folder, drafts_dir())
    except UserRuleError:
        return None
    return folder if folder.is_dir() else None


def _is_junction(path: Path) -> bool:
    """A junction (Windows) or any reparse point: the folder is not what its name says."""
    try:
        if hasattr(os.path, "isjunction") and os.path.isjunction(path):
            return True
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & 0x400)      # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return False


def _is_link(path: Path) -> bool:
    return path.is_symlink() or _is_junction(path)


def check_inside(path: Path, root: Path) -> None:
    """Refuse a path that is, or goes through, a symlink or a junction below `root` (root included), or whose real path leaves `root`. A path that does not exist yet is checked up to what exists."""
    base = Path(os.path.abspath(root))
    target = Path(os.path.abspath(path))
    try:
        parts = target.relative_to(base).parts
    except ValueError:
        raise UserRuleError("blocked", "the folder of the strategy is outside the working folder") from None
    current = base
    for part in ("", *parts):
        current = current / part if part else current
        if _is_link(current):
            raise UserRuleError("blocked", f"{current.name} is a link (symlink or junction): the strategy is not written through links")
    real_root = base.resolve()
    if os.path.lexists(target) and not target.resolve().is_relative_to(real_root):
        raise UserRuleError("blocked", "the folder of the strategy leads outside the working folder")


def make_folder(path: Path, root: Path, *, new: bool = False) -> Path:
    """Create `path` (and what is missing above it, below `root`) one level at a time, checking each level. `new`: the last level must not exist yet."""
    root = Path(os.path.abspath(root))
    path = Path(os.path.abspath(path))
    root.mkdir(parents=True, exist_ok=True)
    check_inside(root, root)
    rel = path.relative_to(root).parts
    current = root
    for i, part in enumerate(rel):
        current = current / part
        last = i == len(rel) - 1
        if os.path.lexists(current):
            if new and last:
                raise UserRuleError("blocked", f"{current.name} already exists: a new strategy never takes a folder that was there")
            check_inside(current, root)
            if not current.is_dir():
                raise UserRuleError("blocked", f"{current.name} is not a folder")
        else:
            current.mkdir(exist_ok=False)
            check_inside(current, root)
    return path


def write_text_inside(path: Path, text: str, root: Path) -> None:
    """Write a file of a strategy: its folder is made and checked level by level, the file is never a link or shared with another name, and where the system has O_NOFOLLOW the open itself refuses a link."""
    make_folder(path.parent, root)
    check_inside(path, root)
    if os.path.lexists(path):
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            raise UserRuleError("blocked", f"{path.name} is not a plain file of this strategy")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o644)
    except OSError as exc:
        raise UserRuleError("blocked", f"{path.name} could not be written without following a link ({exc.strerror})") from None
    with os.fdopen(fd, "wb") as handle:
        handle.write(text.replace("\r\n", "\n").encode("utf-8"))


def spec_file(bank_id: int, slug: str) -> Path | None:
    folder = strategy_folder(bank_id, slug)
    path = None if folder is None else folder / "spec.md"
    return path if path is not None and path.is_file() and not _is_link(path) else None


def version_folder(bank_id: int, slug: str, version: int) -> Path:
    return drafts_dir() / f"{code_of(bank_id)}-{slug}" / "versions" / f"v{int(version)}"


def rel_folder(folder: Path) -> str:
    return folder.relative_to(hokma_home()).as_posix()


def _db(db: Database | None) -> Database:
    return db or get_database()


def require_role(caller: Any, allowed: str, what: str) -> str:
    """Any agent or chat may start and carry the flow (1.2.2): the caller only has to be named. The order rules stay: no code without a requirement, no version without an approved verdict,
    and the one that saved a version never gives its verdict."""
    if not isinstance(caller, str) or not caller.strip():
        raise UserRuleError("blocked", f"{what} needs the name of the agent that makes it")
    return caller


def _slug(value: Any) -> str:
    if not isinstance(value, str) or not SLUG.match(value):
        raise UserRuleError("bad_input", f"slug {str(value)[:80]!r} is not valid: lower case letters, digits and underscores, 2 to 40 characters, starting with a letter")
    return value


def rule_slug(bank_slug: str, bank_id: int) -> str:
    """The slug of the rule of a strategy of the bank, derived from the slug of the card (which may be up to 80 characters and start with a digit): underscores,
    a letter first, at most 40 characters, no trailing underscore. When the card slug had to be cut, the bank id closes it so two long titles never collide."""
    base = re.sub(r"[^a-z0-9_]+", "_", bank_slug.replace("-", "_").lower()).strip("_") or "strategy"
    if not base[0].isalpha():
        base = "s_" + base
    if len(base) < 2:
        base += "_s"
    if len(base) > 40:
        tail = f"_{int(bank_id)}"
        base = base[:40 - len(tail)].rstrip("_") + tail
    return _slug(base)


def _text(value: Any, name: str, low: int = 1, high: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not low <= len(value.strip()) <= high:
        raise UserRuleError("bad_input", f"{name} is text of {low} to {high} characters")
    return value.strip()


def _whole(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise UserRuleError("bad_input", f"{name} is a whole number from 1")
    return value


def _sha_of(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _event(session: Any, version_id: int, from_status: str | None, to_status: str, reason: str | None, actor: str, channel: str = "tool") -> None:
    session.insert("user_rule_events", {"rule_version_id": version_id, "from_status": from_status, "to_status": to_status, "reason": reason, "actor": actor, "channel": channel})


# ---------------------------------------------------------------------------------------------------- the requirement (gate 1)
def validate_brief(brief: Any) -> dict[str, Any]:
    """The brief of the base: the summary of the spec and, optionally, its path. The requirement itself is the numbered spec.md (spec-four-phases), never this record."""
    if not isinstance(brief, Mapping):
        raise UserRuleError("bad_input", "brief is a mapping")
    if "summary" not in brief:
        raise UserRuleError("bad_input", "brief needs summary")
    out: dict[str, Any] = {"summary": _text(brief["summary"], "summary", 10, 1000)}
    for key, high in (("quote", 300), ("market", 40), ("timeframe", 10)):          # all three optional; the timeframe is informative (what the source used), the run picks its own
        if key in brief:
            out[key] = _text(brief[key], key, 1, high)
    return out


def write_brief(*, brief: Any, caller: str, bank_id: int | None = None, slug: str | None = None, title: str | None = None, rule_id: int | None = None,
                origin: tuple[int | None, str | None] = (None, None), db: Database | None = None) -> dict[str, Any]:
    """The requirement of a strategy: the first time it creates the rule (kind strategy) with its first brief; later it is the next version, and the
    previous one stops being current. The folder where the coder works is created here."""
    who = require_role(caller, RESEARCHER, "the requirement")
    clean = validate_brief(brief)
    if PREMIUM_WORDS.search(" ".join([clean["summary"], clean.get("quote", ""), title or ""])):
        raise UserRuleError("blocked", "selling option premium is off: this strategy cannot be built")
    database = _db(db)
    with database.write() as u:
        s = u.session
        if rule_id is not None:
            rule = s.one("SELECT * FROM user_rules WHERE id = :i AND kind = 'strategy'", {"i": _whole(rule_id, "rule_id")})
            if rule is None:
                raise UserRuleError("no_data", f"there is no strategy rule {rule_id}")
            bank_ref = int(rule["bank_id"])
        else:
            if bank_id is None or title is None:
                raise UserRuleError("bad_input", "the first requirement needs bank_id and title")
            bank_ref = _whole(bank_id, "bank_id")
            rule = None
        bank = s.one("SELECT id, status, slug FROM strategy_bank WHERE id = :i", {"i": bank_ref})
        if bank is None:
            raise UserRuleError("no_data", f"there is no strategy {bank_ref} in the bank")
        if bank["status"] == "not_working":
            raise UserRuleError("blocked", "a strategy that does not work gets no code: start a new strategy that derives from it")
        last = s.one("SELECT id FROM strategy_bank_versions WHERE bank_id = :b ORDER BY version DESC LIMIT 1", {"b": bank_ref})
        if last is None:
            raise UserRuleError("no_data", "the strategy of the bank has no version to answer")
        if rule is None:
            name = rule_slug(bank["slug"], bank_ref)                          # derived from the slug of the card (the `slug` argument is not used); the folder stays STR-{NNN}-{slug of the card}
            if s.one("SELECT 1 AS x FROM user_rules WHERE slug = :s", {"s": name}) is not None:
                raise UserRuleError("bad_input", f"there is already a rule named {name}: give rule_id to write its next requirement")
            rule_id = s.insert("user_rules", {"slug": name, "kind": "strategy", "title": _text(title, "title", 1, 160), "bank_id": bank_ref, "component": "strategy",
                                              "created_by": who, "origin_desk_id": origin[0], "origin_desk_name": origin[1]})
            slug_out = name
        else:
            rule_id, slug_out = int(rule["id"]), rule["slug"]
        spec = spec_file(bank_ref, bank["slug"])
        if spec is None:
            raise UserRuleError("blocked", f"there is no spec for {code_of(bank_ref)}: write service/strategies/{code_of(bank_ref)}-{bank['slug']}/spec.md first (spec-four-phases), then the brief")
        clean["folder"], clean["spec_path"] = rel_folder(spec.parent), rel_folder(spec.parent) + "/spec.md"
        sha = _sha_of(clean)
        same = s.one("SELECT id, version, status FROM user_rule_briefs WHERE rule_id = :r AND brief_sha256 = :h", {"r": rule_id, "h": sha})
        if same is not None:
            if same["status"] != "current":
                raise UserRuleError("bad_input", f"this is the requirement of version {same['version']}, which was replaced: write the new one")
            brief_id, number, duplicate = int(same["id"]), int(same["version"]), True
        else:
            top = s.scalar("SELECT COALESCE(MAX(version), 0) FROM user_rule_briefs WHERE rule_id = :r", {"r": rule_id})
            s.execute("UPDATE user_rule_briefs SET status = 'superseded' WHERE rule_id = :r AND status = 'current'", {"r": rule_id})
            number, duplicate = int(top) + 1, False
            brief_id = s.insert("user_rule_briefs", {"rule_id": rule_id, "version": number, "request_version": int(last["id"]), "brief_json": json.dumps(clean, sort_keys=True),
                                                      "brief_sha256": sha, "status": "current", "origin_desk_id": origin[0], "origin_desk_name": origin[1], "created_by": who})
    return {"rule_id": rule_id, "brief_id": brief_id, "version": number, "slug": slug_out, "folder": clean["folder"], "spec_path": clean["spec_path"], "duplicate": duplicate}


def current_brief(session: Any, rule_id: int) -> dict[str, Any] | None:
    return session.one("SELECT * FROM user_rule_briefs WHERE rule_id = :r AND status = 'current'", {"r": rule_id})


# ---------------------------------------------------------------------------------------------------- the checks
def run_rule_check(folder: Path, timeout: int = CHECK_TIMEOUT) -> dict[str, Any]:
    """`hokma.factory.rule_check` on a folder in a process of its own (isolated interpreter, minimal environment, time limit). The report is what its last
    line says; the lines before it are the readable output."""
    keep = ("PATH", "SYSTEMROOT", "SystemRoot", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "HOKMA_LANG")
    env = {k: v for k, v in os.environ.items() if k in keep}
    try:
        proc = subprocess.run([sys.executable, "-I", "-B", "-m", "hokma.factory.rule_check", str(folder)], capture_output=True, text=True, timeout=timeout, env=env,
                              cwd=str(folder.parent), **spawn_kwargs())
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"the checks took more than {timeout} seconds", "output": ""}
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    try:
        report = json.loads(lines[-1]) if lines else {}
    except ValueError:
        report = {}
    if not isinstance(report, dict) or "ok" not in report:
        return {"ok": False, "error": "the checks did not answer", "output": (proc.stdout + proc.stderr)[-3000:]}
    report["output"] = "\n".join(lines[:-1])[-6000:]
    return report


def _only_two_files(folder: Path) -> None:
    """The folder of a strategy must exist. Its other files (spec, plan, tasks, adr) are documents of the flow and are not checked here."""
    if not folder.is_dir():
        raise UserRuleError("no_data", f"{rel_folder(folder) if folder.is_relative_to(hokma_home()) else folder.name} does not exist: write the spec first, then the test, then the strategy")


def _read_draft(bank_id: int, bank_slug: str, version: int) -> tuple[str, str]:
    """The two files of versions/v{N}/ as text. Only a regular file inside the folder is read; a link is refused."""
    folder = version_folder(bank_id, bank_slug, version)
    if not folder.is_dir() or folder.is_symlink():
        raise UserRuleError("no_data", f"{code_of(bank_id)}-{bank_slug}/versions/v{version}/ does not exist: write the spec first, then the test and the strategy of v{version}")
    texts = []
    for name in (STRATEGY_FILE, TEST_FILE):
        path = folder / name
        if path.is_symlink() or not path.is_file():
            raise UserRuleError("no_data", f"{rel_folder(folder)}/{name} does not exist: write the test first, then the strategy")
        raw = path.read_bytes()
        if len(raw) > static_check.MAX_BYTES:
            raise UserRuleError("bad_input", f"{name} is over {static_check.MAX_BYTES // 1024} KB")
        try:
            texts.append(static_check.normalize_source(raw.decode("utf-8")))
        except UnicodeDecodeError:
            raise UserRuleError("bad_input", f"{name} is not UTF-8 text") from None
    return texts[0], texts[1]


def save_code(*, rule_id: int, caller: str, dry_run: bool = False, origin: tuple[int | None, str | None] = (None, None), db: Database | None = None,
              check: Callable[..., dict[str, Any]] | None = None, version: int | None = None) -> dict[str, Any]:
    """Gate 1 and the checks. The code is saved only when a current requirement exists and every check is green; otherwise the answer is the output of the
    checks and nothing is stored. With dry_run the checks run on the draft and nothing is stored either way."""
    who = require_role(caller, CODER, "saving code")
    runner = check or run_rule_check
    database = _db(db)
    with database.read() as u:
        s = u.session
        rule = s.one("SELECT * FROM user_rules WHERE id = :i AND kind = 'strategy'", {"i": _whole(rule_id, "rule_id")})
        if rule is None:
            raise UserRuleError("no_data", f"there is no strategy rule {rule_id}")
        brief = current_brief(s, int(rule["id"]))
        rejected = int(s.scalar("SELECT COUNT(*) FROM user_rule_reviews r JOIN user_rule_versions v ON v.id = r.rule_version_id WHERE v.rule_id = :r AND r.verdict = 'rejected'",
                                {"r": int(rule["id"])}))
        bank_slug = s.one("SELECT slug FROM strategy_bank WHERE id = :i", {"i": int(rule["bank_id"])})["slug"]
        next_number = int(s.scalar("SELECT COALESCE(MAX(version), 0) FROM user_rule_versions WHERE rule_id = :r", {"r": int(rule["id"])})) + 1
    bank_ref = int(rule["bank_id"])
    if brief is None or spec_file(bank_ref, bank_slug) is None:
        raise UserRuleError("blocked", "no spec: the numbered spec.md (service/strategies/STR-{NNN}-{slug}/spec.md) is written first, and without it no code is saved")
    if rejected >= MAX_REJECTIONS:
        raise UserRuleError("blocked", f"the reviewer rejected this strategy {rejected} times: stop and tell the owner why")
    slug = rule["slug"]
    wanted = next_number if version is None else _whole(version, "version")
    if version is None and next_number > 1 and not version_folder(bank_ref, bank_slug, next_number).is_dir():
        wanted = next_number - 1                                    # v{N+1} is not written: the call is about the last saved version
    if wanted > next_number:
        raise UserRuleError("bad_input", f"the next version is v{next_number}: versions are numbered in sequence")
    strategy_text, test_text = _read_draft(bank_ref, bank_slug, wanted)
    findings = [{"file": STRATEGY_FILE, **f.to_data()} for f in static_check.check(strategy_text, kind="strategy").findings]
    findings += [{"file": TEST_FILE, **f.to_data()} for f in static_check.check(test_text, kind="test").findings]
    head = strategy_text.lstrip()[:400]
    if code_of(bank_ref) not in head or f"v{wanted}" not in head:
        findings.append({"file": STRATEGY_FILE, "line": 1, "col": 0, "rule": "contract", "message": f"strategy.py starts with a docstring that names the code {code_of(bank_ref)}, the version v{wanted} and the path of the spec"})
    if findings:
        return {"saved": False, "ok": False, "static": findings, "output": "\n".join(f"STATIC {f['file']} line {f['line']}: {f['message']}" for f in findings)}
    sha = static_check.code_sha256(strategy_text)
    if dry_run:
        report = runner(version_folder(bank_ref, bank_slug, wanted))
        return {"saved": False, "ok": bool(report.get("ok")), "tests": len(report.get("tests", [])), "output": report.get("output", report.get("error", "")), "report": _brief_report(report)}
    with database.read() as u:
        known = u.session.one("SELECT id, version, registry_id, status FROM user_rule_versions WHERE rule_id = :r AND code_sha256 = :h", {"r": int(rule["id"]), "h": sha})
    if known is None and wanted < next_number:
        raise UserRuleError("blocked", f"v{wanted} was saved with other code: a saved version is never written over, the change goes in v{next_number}")
    if known is not None:
        return {"saved": True, "duplicate": True, "rule_version_id": int(known["id"]), "version": int(known["version"]), "registry_id": known["registry_id"], "status": known["status"]}
    for attempt in range(3):
        number = next_number
        folder = copy_folder(slug, number, sha)
        _copy(slug, folder, strategy_text, test_text)
        report = runner(folder)
        if not report.get("ok") or report.get("strategy_sha256") != hashlib.sha256(strategy_text.encode("utf-8")).hexdigest():
            _discard(folder)
            return {"saved": False, "ok": False, "tests": len(report.get("tests", [])), "output": report.get("output", report.get("error", "")), "report": _brief_report(report)}
        registry_id = f"u_str{bank_ref:03d}_v{number}"
        try:
            with database.write() as u:
                s = u.session
                version_id = s.insert("user_rule_versions", {
                    "rule_id": int(rule["id"]), "version": number, "registry_id": registry_id, "request_version": int(brief["request_version"]), "code_sha256": sha,
                    "code_bytes": len(strategy_text.encode("utf-8")), "code_text": strategy_text,
                    "cases_json": json.dumps({"test_file": test_text, "test_sha256": hashlib.sha256(test_text.encode("utf-8")).hexdigest()}),
                    "manifest_json": json.dumps(report["manifest"], sort_keys=True), "check_json": "[]", "test_json": json.dumps({k: v for k, v in report.items() if k != "output"}, sort_keys=True),
                    "status": "draft", "brief_id": int(brief["id"]), "created_by": who,
                    "origin_desk_id": rule["origin_desk_id"] if rule["origin_desk_id"] is not None else origin[0],
                    "origin_desk_name": rule["origin_desk_name"] if rule["origin_desk_name"] is not None else origin[1]})
                _event(s, version_id, None, "draft", f"saved with {len(report['tests'])} tests, all green", who)
            versions_row(bank_ref, bank_slug, number, sha, author=who, reviewer="", verdict="pending")
            return {"saved": True, "duplicate": False, "rule_version_id": version_id, "version": number, "registry_id": registry_id, "code_sha256": sha, "status": "draft",
                    "str_code": code_of(bank_ref), "folder": rel_folder(version_folder(bank_ref, bank_slug, number)), "file": rel_folder(version_folder(bank_ref, bank_slug, number)) + "/strategy.py",
                    "tests": len(report["tests"]), "output": report.get("output", "")}
        except IntegrityViolation:
            _discard(folder)                                         # another save took the number: try again with the next one
    raise UserRuleError("blocked", "the version could not be saved: try again")


def versions_row(bank_id: int, bank_slug: str, version: int, sha: str, *, author: str, reviewer: str, verdict: str) -> None:
    """The row of the table Versions in the spec.md of the strategy: one row for each version, updated at each step (saved, reviewed). A problem writing it never undoes a save."""
    from datetime import datetime, timezone

    spec = spec_file(bank_id, bank_slug)
    if spec is None:
        return
    head = "| Version | File | sha256 | Author | Reviewer | Verdict | Date |"
    row = f"| v{version} | versions/v{version}/strategy.py | {sha} | {author} | {reviewer} | {verdict} | {datetime.now(timezone.utc).strftime('%Y-%m-%d')} |"
    try:
        lines = spec.read_text(encoding="utf-8").split("\n")
        at = next((i for i, line in enumerate(lines) if line.strip().lower() == "## versions"), None)
        if at is None:
            lines += ["", "## Versions", "", head, "|---|---|---|---|---|---|---|", row, ""]
        else:
            end = at + 1
            while end < len(lines) and (not lines[end].strip() or lines[end].lstrip().startswith("|")):
                end += 1
            body = lines[at + 1:end]
            if not any(line.startswith("| Version") for line in body):
                body = ["", head, "|---|---|---|---|---|---|---|"]
            mine = next((i for i, line in enumerate(body) if line.startswith(f"| v{version} |")), None)
            if mine is None:
                body.append(row)
            else:
                body[mine] = row
            lines[at + 1:end] = body
        write_text_inside(spec, "\n".join(lines), drafts_dir())
    except (OSError, UserRuleError):
        pass


def rule_of_bank(bank_id: Any, *, db: Database | None = None) -> int:
    """The strategy rule of a card of the bank (the last one)."""
    with _db(db).read() as u:
        row = u.session.one("SELECT id FROM user_rules WHERE kind = 'strategy' AND bank_id = :b ORDER BY id DESC LIMIT 1", {"b": _whole(bank_id, "bank_id")})
    if row is None:
        raise UserRuleError("no_data", "that strategy has no rule yet: write its spec.md and register it with strategy_brief_write")
    return int(row["id"])


def _discard(folder: Path) -> None:
    """Remove a copy that was not kept, and the folder of its rule when it is left empty."""
    shutil.rmtree(folder, ignore_errors=True)
    try:
        folder.parent.rmdir()
    except OSError:
        pass


def _brief_report(report: Mapping[str, Any]) -> dict[str, Any]:
    return {"ok": bool(report.get("ok")), "tests": [{"name": t.get("name"), "ok": t.get("ok")} for t in report.get("tests", [])], "fixed": {k: v.get("ok") for k, v in report.get("fixed", {}).items()}}


def _copy(slug: str, folder: Path, strategy_text: str, test_text: str) -> None:
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    for name, text in ((STRATEGY_FILE, strategy_text), (TEST_FILE, test_text)):
        path = folder / name
        path.write_bytes(text.encode("utf-8"))
        os.chmod(path, 0o444)


def _version_row(session: Any, *, rule_version_id: int | None = None, rule_id: int | None = None, registry_id: str | None = None, version: int | None = None) -> dict[str, Any]:
    sql = ("SELECT v.*, r.slug AS rule_slug, r.title AS rule_title, r.bank_id AS bank_id, (SELECT slug FROM strategy_bank WHERE id = r.bank_id) AS bank_slug FROM user_rule_versions v JOIN user_rules r ON r.id = v.rule_id WHERE r.kind = 'strategy' AND ")
    if rule_version_id is not None:
        row = session.one(sql + "v.id = :i", {"i": rule_version_id})
    elif registry_id is not None:
        row = session.one(sql + "v.registry_id = :i", {"i": registry_id})
    elif rule_id is not None:
        extra, params = ("", {"i": rule_id}) if not version else (" AND v.version = :n", {"i": rule_id, "n": version})
        row = session.one(sql + "v.rule_id = :i" + extra + " ORDER BY v.version DESC LIMIT 1", params)
    else:
        raise UserRuleError("bad_input", "give rule_version_id, registry_id or rule_id")
    if row is None:
        raise UserRuleError("no_data", "there is no such version of the strategy")
    return row


def _file_ok(row: Mapping[str, Any]) -> Path:
    """The copy of a version, after checking that its file is the one the base keeps."""
    folder = copy_folder(row["rule_slug"], int(row["version"]), row["code_sha256"])
    path = folder / STRATEGY_FILE
    try:
        text = static_check.normalize_source(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError):
        raise UserRuleError("blocked", f"the copy of {row['registry_id']} cannot be read") from None
    if static_check.code_sha256(text) != row["code_sha256"] or text != static_check.normalize_source(row["code_text"]):
        raise UserRuleError("blocked", f"the file of {row['registry_id']} is not the code the base keeps: it was changed after it was saved")
    return folder


# ---------------------------------------------------------------------------------------------------- reading
def list_code(*, bank_id: int | None = None, stage: str | None = None, origin: str | None = None, limit: int = 50, db: Database | None = None) -> dict[str, Any]:
    if stage is not None and stage not in ("draft", "failed", "generated", "owner_reviewed", "rejected", "retired"):
        raise UserRuleError("bad_input", "stage is draft, failed, generated, owner_reviewed, rejected or retired")
    if origin not in (None, "hokma", "desk"):
        raise UserRuleError("bad_input", "origin is hokma or desk")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise UserRuleError("bad_input", "limit is a whole number from 1 to 200")
    where, params = ["r.kind = 'strategy'"], {"limit": limit}
    if bank_id is not None:
        where.append("r.bank_id = :b")
        params["b"] = _whole(bank_id, "bank_id")
    if stage is not None:
        where.append("v.status = :st")
        params["st"] = stage
    if origin == "hokma":
        where.append("v.origin_desk_id IS NULL")
    elif origin == "desk":
        where.append("v.origin_desk_id IS NOT NULL")
    with _db(db).read() as u:
        rows = u.session.query("SELECT v.id, v.rule_id, v.version, v.registry_id, v.status, v.code_sha256, v.origin_desk_id, v.origin_desk_name, v.created_at, v.test_json, r.slug, r.title, r.bank_id, "
                               "b.brief_json, (SELECT x.reviewer FROM user_rule_reviews x WHERE x.rule_version_id = v.id ORDER BY x.id DESC LIMIT 1) AS reviewer "
                               "FROM user_rule_versions v JOIN user_rules r ON r.id = v.rule_id LEFT JOIN user_rule_briefs b ON b.id = v.brief_id WHERE " + " AND ".join(where) + " ORDER BY v.id DESC LIMIT :limit", params)
    return {"rows": [{"rule_version_id": int(r["id"]), "rule_id": int(r["rule_id"]), "bank_id": int(r["bank_id"]), "slug": r["slug"], "title": r["title"], "version": int(r["version"]),
                      "registry_id": r["registry_id"], "stage": r["status"], "code_sha256": r["code_sha256"], "origin": origin_of(r), "created_at": str(r["created_at"]),
                      "summary": json.loads(r["brief_json"])["summary"] if r["brief_json"] else None, "tests": len(json.loads(r["test_json"] or "{}").get("tests", [])),
                      "reviewer": r["reviewer"]} for r in rows]}


def origin_of(row: Mapping[str, Any]) -> dict[str, Any]:
    desk = row.get("origin_desk_id")
    return {"kind": "hokma"} if desk is None else {"kind": "desk", "desk_id": int(desk), "desk_name": row.get("origin_desk_name")}


def _read_text(path: Path, limit: int = 40000) -> str | None:
    try:
        return path.read_text(encoding="utf-8")[:limit] if path.is_file() and not path.is_symlink() else None
    except (OSError, UnicodeDecodeError):
        return None


def rule_view(s: Any, rule: Mapping[str, Any]) -> dict[str, Any]:
    """Everything a strategy has outside the database lock, in one answer: its folder, its spec, its files, the draft in the folder, the current brief and every saved version with its verdicts.
    It works when there is no code yet."""
    slug = s.one("SELECT slug FROM strategy_bank WHERE id = :i", {"i": int(rule["bank_id"])})["slug"]
    folder = strategy_folder(int(rule["bank_id"]), slug)
    brief = current_brief(s, int(rule["id"]))
    versions = s.query("SELECT v.id, v.version, v.registry_id, v.status, v.code_sha256, v.created_by FROM user_rule_versions v WHERE v.rule_id = :r ORDER BY v.version", {"r": int(rule["id"])})
    items = []
    for v in versions:
        reviews = s.query("SELECT verdict, findings_json, reviewer FROM user_rule_reviews WHERE rule_version_id = :i ORDER BY id", {"i": v["id"]})
        items.append({"rule_version_id": int(v["id"]), "version": int(v["version"]), "registry_id": v["registry_id"], "stage": v["status"], "code_sha256": v["code_sha256"], "saved_by": v["created_by"],
                      "load_problem": LOAD_PROBLEMS.get(v["registry_id"]),
                      "reviews": [{"verdict": r["verdict"], "reviewer": r["reviewer"], "findings": json.loads(r["findings_json"])} for r in reviews]})
    spec = None if folder is None else _read_text(folder / "spec.md")
    last = next((i for i in reversed(items) if i["stage"] in APPROVED), None)
    return {"str_code": code_of(int(rule["bank_id"])), "current": last, "rule_id": int(rule["id"]), "bank_id": int(rule["bank_id"]), "slug": slug, "title": rule["title"], "folder": None if folder is None else rel_folder(folder),
            "spec_path": None if spec is None else rel_folder(folder) + "/spec.md", "spec": spec,
            "files": [] if folder is None else sorted(p.relative_to(folder).as_posix() + ("/" if p.is_dir() else "") for p in folder.iterdir() if not p.name.startswith(".") and p.name != "__pycache__"),
            "brief": None if brief is None else {"brief_id": int(brief["id"]), "version": int(brief["version"]), "brief": json.loads(brief["brief_json"])},
            "draft_code": None if folder is None else _read_text(version_folder(int(rule["bank_id"]), slug, len(items) + 1) / STRATEGY_FILE),
            "draft_test": None if folder is None else _read_text(version_folder(int(rule["bank_id"]), slug, len(items) + 1) / TEST_FILE),
            "saved_files": [{"version": i["version"], "file": rel_folder(version_folder(int(rule["bank_id"]), slug, i["version"])) + "/strategy.py"} for i in items] if folder is not None else [],
            "stage": items[-1]["stage"] if items else ("no_code" if spec is not None else "no_spec"),
            "versions": items}


def creator_view(bank_id: int, *, db: Database | None = None) -> list[dict[str, Any]]:
    """The creator side of a strategy of the bank (bank_get and bank_list read it): one entry for each strategy rule it has."""
    try:
        with _db(db).read() as u:
            rules = u.session.query("SELECT * FROM user_rules WHERE kind = 'strategy' AND bank_id = :b ORDER BY id", {"b": int(bank_id)})
            return [rule_view(u.session, r) for r in rules]
    except Exception:                                                     # noqa: BLE001  a base without the tables has no creator side
        return []


def get_code(*, rule_version_id: int | None = None, rule_id: int | None = None, registry_id: str | None = None, rerun: bool = False, db: Database | None = None,
             check: Callable[..., dict[str, Any]] | None = None, bank_id: int | None = None, slug: str | None = None) -> dict[str, Any]:
    """The requirement, the code, the checks and the verdicts of a version (the last one of a rule by default). With rerun the checks run again on the copy
    and the answer carries their fresh output: this is how the reviewer runs the tests."""
    with _db(db).read() as u:
        s = u.session
        if rule_version_id is None and registry_id is None and rule_id is None and (bank_id is not None or slug is not None):
            found = s.one("SELECT * FROM user_rules WHERE kind = 'strategy' AND " + ("bank_id = :k" if bank_id is not None else "slug = :k") + " ORDER BY id DESC LIMIT 1", {"k": bank_id if bank_id is not None else slug})
            if found is None:
                raise UserRuleError("no_data", "that strategy has no rule yet: the spec is written first and registered with strategy_brief_write")
            rule_id = int(found["id"])
        if rule_version_id is None and registry_id is None and rule_id is not None:
            rule = s.one("SELECT * FROM user_rules WHERE id = :i AND kind = 'strategy'", {"i": _whole(rule_id, "rule_id")})
            if rule is None:
                raise UserRuleError("no_data", f"there is no strategy rule {rule_id}")
            if s.one("SELECT 1 AS x FROM user_rule_versions WHERE rule_id = :r", {"r": int(rule["id"])}) is None:
                return rule_view(s, rule)                                   # no code yet: the spec, the files and the draft still answer
        row = _version_row(s, rule_version_id=rule_version_id, rule_id=rule_id, registry_id=registry_id)
        view = rule_view(s, s.one("SELECT * FROM user_rules WHERE id = :i", {"i": int(row["rule_id"])}))
        brief = s.one("SELECT id, version, brief_json, status FROM user_rule_briefs WHERE id = :i", {"i": row["brief_id"]}) if row["brief_id"] is not None else None
        reviews = s.query("SELECT id, verdict, findings_json, reviewer, code_sha256, created_at FROM user_rule_reviews WHERE rule_version_id = :i ORDER BY id", {"i": row["id"]})
        link = s.one("SELECT strategy_id, spec_version FROM strategy_spec_rules WHERE rule_version_id = :i", {"i": row["id"]})
        bank = s.one("SELECT v.version, v.id FROM strategy_bank_versions v WHERE v.strategy_id = :s ORDER BY v.id DESC LIMIT 1", {"s": row["registry_id"]})
    out: dict[str, Any] = {
        "rule_id": int(row["rule_id"]), "rule_version_id": int(row["id"]), "slug": row["rule_slug"], "title": row["rule_title"], "bank_id": int(row["bank_id"]),
        "version": int(row["version"]), "registry_id": row["registry_id"], "stage": row["status"], "code_sha256": row["code_sha256"], "origin": origin_of(row),
        "requirement": None if brief is None else {"brief_id": int(brief["id"]), "version": int(brief["version"]), "current": brief["status"] == "current", "brief": json.loads(brief["brief_json"])},
        "code": row["code_text"], "test_file": json.loads(row["cases_json"]).get("test_file"), "checks": json.loads(row["test_json"]) if row["test_json"] else None,
        "reviews": [{"review_id": int(r["id"]), "verdict": r["verdict"], "findings": json.loads(r["findings_json"]), "reviewer": r["reviewer"], "code_sha256": r["code_sha256"],
                     "at": str(r["created_at"])} for r in reviews],
        "spec": None if link is None else {"strategy_id": link["strategy_id"], "spec_version": int(link["spec_version"])},
        "bank_version": None if bank is None else int(bank["version"]), "agent_reviewed": row["status"] in APPROVED, "owner_reviewed": row["status"] == "owner_reviewed"}
    out.update({k: view[k] for k in ("folder", "spec_path", "spec", "files", "draft_code", "draft_test", "versions")})
    if rerun:
        folder = copy_folder(row["rule_slug"], int(row["version"]), row["code_sha256"])
        report = (check or run_rule_check)(folder)
        out["rerun"] = {"ok": bool(report.get("ok")), "output": report.get("output", report.get("error", ""))}
    return out


# ---------------------------------------------------------------------------------------------------- the verdict (gate 2)
def _findings(items: Any, verdict: str) -> list[dict[str, Any]]:
    if items is None:
        items = []
    if not isinstance(items, list) or len(items) > 50:
        raise UserRuleError("bad_input", "findings is a list of up to 50 items")
    out = []
    for item in items:
        if not isinstance(item, Mapping) or item.get("severity") not in SEVERITIES or not isinstance(item.get("message"), str) or not 1 <= len(item["message"]) <= 600:
            raise UserRuleError("bad_input", f"a finding has severity ({', '.join(SEVERITIES)}), message and optionally line")
        line = item.get("line")
        if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1):
            raise UserRuleError("bad_input", "the line of a finding is a whole number from 1")
        out.append({"severity": item["severity"], "line": line, "message": item["message"]})
    if verdict == "rejected" and not out:
        raise UserRuleError("bad_input", "a rejection needs its findings: the coder fixes only what is pointed at")
    if verdict == "approved" and any(f["severity"] in ("blocker", "major") for f in out):
        raise UserRuleError("bad_input", "a code with a blocker or a major finding is not approved")
    return out


def verdict(*, rule_version_id: int, verdict: str, findings: Any, reviewer: str, db: Database | None = None, check: Callable[..., dict[str, Any]] | None = None,
            notify: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """The reviewer's verdict on a draft. A rejection marks it failed and counts. An approval needs the checks green on this same hash (they run again here), and
    in one transaction it generates the version, adds the version of the bank with its origin, saves the minimal spec and links them."""
    who = require_role(reviewer, REVIEWER, "the verdict")
    if verdict not in ("approved", "rejected"):
        raise UserRuleError("bad_input", "verdict is approved or rejected")
    items = _findings(findings, verdict)
    database = _db(db)
    with database.read() as u:
        row = _version_row(u.session, rule_version_id=_whole(rule_version_id, "rule_version_id"))
        total = int(u.session.scalar("SELECT COUNT(*) FROM user_rule_reviews r JOIN user_rule_versions v ON v.id = r.rule_version_id WHERE v.rule_id = :r AND r.verdict = 'rejected'",
                                     {"r": int(row["rule_id"])}))
    if row["created_by"] == who:
        raise UserRuleError("blocked", "the one that saved a version never gives its verdict")
    from hokma.factory import pandas_flow

    if pandas_flow.is_pandas(row["manifest_json"]):                              # 1.3.0: Ben judges the logic of a version that the checks already generated; he does not repeat the checks
        return pandas_flow.ben_verdict(database, row, who, verdict, items)
    if row["status"] != "draft":
        raise UserRuleError("bad_input", f"only a draft gets a verdict: this version is {row['status']}")
    folder = _file_ok(row)
    report = (check or run_rule_check)(folder)
    if verdict == "approved" and not report.get("ok"):
        raise UserRuleError("bad_input", "the checks are not green on this code, so it cannot be approved", [report.get("output", "")[-600:]])
    if verdict == "rejected":
        with database.write() as u:
            s = u.session
            s.insert("user_rule_reviews", {"rule_version_id": int(row["id"]), "brief_id": int(row["brief_id"]), "code_sha256": row["code_sha256"], "test_json": json.dumps({k: v for k, v in report.items() if k != "output"}),
                                           "verdict": "rejected", "findings_json": json.dumps(items), "reviewer": who})
            s.execute("UPDATE user_rule_versions SET status = 'failed' WHERE id = :i AND status = 'draft'", {"i": int(row["id"])})
            _event(s, int(row["id"]), "draft", "failed", "; ".join(f["message"] for f in items)[:500], who)
        _table_after_verdict(database, row, who, "rejected")
        return {"verdict": "rejected", "status": "failed", "attempts_left": max(0, MAX_REJECTIONS - total - 1), "rule_version_id": int(row["id"])}
    result = _approve(database, row, report, items, who)
    _table_after_verdict(database, row, who, "approved")
    if notify is not None:
        notify(result)
    return result


def _table_after_verdict(database: Database, row: Mapping[str, Any], who: str, verdict_name: str) -> None:
    with database.read() as u:
        bank = u.session.one("SELECT slug FROM strategy_bank WHERE id = :i", {"i": int(row["bank_id"])})
    if bank is not None:
        versions_row(int(row["bank_id"]), bank["slug"], int(row["version"]), row["code_sha256"], author=row["created_by"], reviewer=who,
                     verdict="approved, ready for backtest" if verdict_name == "approved" else "rejected")


def _approve(database: Database, row: Mapping[str, Any], report: Mapping[str, Any], items: list[dict[str, Any]], who: str) -> dict[str, Any]:
    manifest = json.loads(row["manifest_json"])
    registry_id = row["registry_id"]
    defaults = (schema_from_manifest(manifest["schema"]) if manifest.get("engine") else adapter_schema(schema_from_manifest(manifest["schema"]))).defaults()
    brief = None
    with database.write() as u:
        s = u.session
        brief = json.loads(s.one("SELECT brief_json FROM user_rule_briefs WHERE id = :i", {"i": row["brief_id"]})["brief_json"])
        s.insert("user_rule_reviews", {"rule_version_id": int(row["id"]), "brief_id": int(row["brief_id"]), "code_sha256": row["code_sha256"],
                                       "test_json": json.dumps({k: v for k, v in report.items() if k != "output"}), "verdict": "approved", "findings_json": json.dumps(items), "reviewer": who})
        if s.execute("UPDATE user_rule_versions SET status = 'generated' WHERE id = :i AND status = 'draft'", {"i": int(row["id"])}) != 1:
            raise UserRuleError("bad_input", "the version changed while it was being approved: read it again")
        _event(s, int(row["id"]), "draft", "generated", "approved by the code reviewer", who)
        bank = s.one("SELECT id, line, status FROM strategy_bank WHERE id = :i", {"i": int(row["bank_id"])})
        last = s.one("SELECT id, version, source_id, extraction_json, extraction_hash FROM strategy_bank_versions WHERE bank_id = :b ORDER BY version DESC LIMIT 1", {"b": int(row["bank_id"])})
        line = bank["line"] or "DT"
        hypothesis = brief["summary"][:2000]
        spec_yaml = yaml.safe_dump({"strategy_id": registry_id, "line": line, "alpha": registry_id, "params": defaults, "hypothesis": hypothesis}, sort_keys=False, width=10_000, allow_unicode=False)
        from hokma.factory.spec import StrategySpec

        spec_hash = StrategySpec(registry_id, line, registry_id, defaults, hypothesis, (), (), None).spec_hash
        s.insert("strategy_specs", {"strategy_id": registry_id, "version": 1, "line": line, "spec_yaml": spec_yaml, "spec_hash": spec_hash, "status": "draft", "proposed_by": None,
                                    "root_cause": None, "window_run_id": int(s.scalar("SELECT COALESCE(MAX(id), 0) FROM backtest_runs") or 0)})
        s.insert_row("strategy_spec_rules", {"strategy_id": registry_id, "spec_version": 1, "rule_version_id": int(row["id"]), "code_sha256": row["code_sha256"]})
        next_version = int(last["version"]) + 1
        bank_version_id = s.insert("strategy_bank_versions", {
            "bank_id": int(row["bank_id"]), "version": next_version, "source_id": int(last["source_id"]), "derived_from": int(last["id"]), "extraction_json": last["extraction_json"],
            "extraction_hash": last["extraction_hash"], "needs_rule_json": "[]", "strategy_id": registry_id, "spec_version": 1, "created_by": who,
            "origin_desk_id": row["origin_desk_id"], "origin_desk_name": row["origin_desk_name"]})
        if bank["status"] == "idea":
            s.execute("UPDATE strategy_bank SET status = 'draft', updated_at = CURRENT_TIMESTAMP WHERE id = :i AND status = 'idea'", {"i": int(row["bank_id"])})
            s.insert("strategy_bank_events", {"bank_id": int(row["bank_id"]), "from_status": "idea", "to_status": "draft", "reason": f"the code {registry_id} was approved by the reviewer", "actor": who})
    return {"verdict": "approved", "status": "generated", "rule_version_id": int(row["id"]), "registry_id": registry_id, "bank_id": int(row["bank_id"]), "bank_version": next_version,
            "bank_version_id": bank_version_id, "strategy_id": registry_id, "spec_version": 1, "title": row["rule_title"], "slug": row["rule_slug"], "tests": len(report.get("tests", [])),
            "origin": origin_of(row), "summary": brief["summary"]}


# ---------------------------------------------------------------------------------------------------- the owner
def owner_decision(*, rule_version_id: int, approve: bool, note: str | None = None, actor: str = "owner", db: Database | None = None) -> dict[str, Any]:
    """The owner's yes or no on a generated version: yes makes it owner_reviewed (the base refuses it without the reviewer's approval), no makes it rejected."""
    if note is not None and (not isinstance(note, str) or len(note) > 2000):
        raise UserRuleError("bad_input", "the note is text of up to 2000 characters")
    database = _db(db)
    with database.write() as u:
        s = u.session
        row = _version_row(s, rule_version_id=_whole(rule_version_id, "rule_version_id"))
        if row["status"] != "generated":
            raise UserRuleError("bad_input" if row["status"] != "draft" else "blocked", f"only a version that the code reviewer approved is decided by the owner: this one is {row['status']}")
        if approve:
            from hokma.factory import pandas_flow

            if pandas_flow.is_pandas(row["manifest_json"]) and not pandas_flow.ben_approved(s, int(row["id"]), row["code_sha256"]):
                raise UserRuleError("blocked", "this strategy only passed the checks of Hokma: the reviewer (Ben) must approve its logic against the spec before you can approve it for paper or real money")
            changed = s.execute("UPDATE user_rule_versions SET status = 'owner_reviewed', reviewed_by = :a, reviewed_at = CURRENT_TIMESTAMP, reviewed_sha256 = code_sha256, review_note = :n "
                                "WHERE id = :i AND status = 'generated'", {"a": actor, "n": note, "i": int(row["id"])})
            target = "owner_reviewed"
        else:
            changed = s.execute("UPDATE user_rule_versions SET status = 'rejected', review_note = :n WHERE id = :i AND status = 'generated'", {"n": note, "i": int(row["id"])})
            target = "rejected"
        if changed != 1:
            raise UserRuleError("bad_input", "the version changed while it was being decided: read it again")
        _event(s, int(row["id"]), "generated", target, note, actor, "app")
    return {"rule_version_id": int(row["id"]), "registry_id": row["registry_id"], "status": target}


# ---------------------------------------------------------------------------------------------------- what the rest of Hokma asks
def stage(registry_id: str, *, db: Database | None = None) -> str | None:
    """The state of the version behind a registry id, or None when no generated strategy has that id."""
    with _db(db).read() as u:
        row = u.session.one("SELECT status FROM user_rule_versions WHERE registry_id = :r", {"r": registry_id})
    return None if row is None else row["status"]


def is_generated_id(strategy_id: Any) -> bool:
    return isinstance(strategy_id, str) and strategy_id.startswith("u_")


def require_stage(registry_id: str, allowed: Sequence[str], what: str, *, db: Database | None = None) -> str:
    found = stage(registry_id, db=db)
    if found not in allowed:
        raise UserRuleError("blocked", f"{registry_id} is {found or 'not in the base'}: {what}")
    return found


def propose_gate(strategy_id: str, *, db: Database | None = None) -> None:
    """Only a version the owner approved can be proposed."""
    if is_generated_id(strategy_id):
        require_stage(strategy_id, ("owner_reviewed",), "it needs the owner's approval before it can be proposed", db=db)


def live_gate(strategy_id: str, *, db: Database | None = None) -> None:
    """The check a desk makes before it may run a strategy with real money: a generated one needs the owner's approval of this very code."""
    if is_generated_id(strategy_id):
        require_stage(strategy_id, ("owner_reviewed",), "it needs the owner's approval to run with real money", db=db)


def require_green(registry_id: str, *, db: Database | None = None) -> None:
    """What stands for the signal and size tests of a generated strategy (SPEC-031 R8): its version is approved and the green run of the checks that was saved is
    on the same hash as its code."""
    with _db(db).read() as u:
        row = u.session.one("SELECT code_sha256, status, test_json FROM user_rule_versions WHERE registry_id = :r", {"r": registry_id})
    if row is None or row["status"] not in APPROVED:
        raise UserRuleError("blocked", f"{registry_id} is not an approved strategy")
    report = json.loads(row["test_json"] or "{}")
    if not report.get("ok") or report.get("strategy_sha256") != hashlib.sha256(static_check.normalize_source(_code_of(registry_id, db)).encode("utf-8")).hexdigest():
        raise UserRuleError("blocked", f"{registry_id} has no green run of its checks on this code")


def _code_of(registry_id: str, db: Database | None) -> str:
    with _db(db).read() as u:
        return u.session.one("SELECT code_text FROM user_rule_versions WHERE registry_id = :r", {"r": registry_id})["code_text"]


# ---------------------------------------------------------------------------------------------------- the adapter (R2)
def schema_from_manifest(items: Sequence[Mapping[str, Any]]) -> ParamSchema:
    return ParamSchema(tuple(Param(i["name"], i["kind"], i.get("default"), i.get("low"), i.get("high"), tuple(i.get("choices") or ())) for i in items))


SIZER_PARAMS = (Param("sizer", "choice", "fixed_contracts", choices=SIZER_KINDS), Param("sizer_n", "int", 1, 1, sizing.HARD_MAX_CONTRACTS),
                Param("sizer_risk_pct", "float", 0.5, 0.001, 100.0), Param("sizer_target_vol", "float", 0.15, 0.001, 5.0),
                Param("sizer_decay_pct", "int", 94, 50, 99), Param("sizer_periods", "int", 252, 1, 366))


def adapter_schema(user_schema: ParamSchema) -> ParamSchema:
    return ParamSchema((*user_schema.params, *SIZER_PARAMS))


class _Levels:
    """The exit rule that the sizer by risk reads: the stop that the strategy just asked for."""

    protective = True

    def __init__(self) -> None:
        self.stop: Any = None

    def levels(self, *, side: Any, ctx: Any) -> tuple[Any, Any]:
        return self.stop, None


class UserStrategy:
    """A strategy written as code, as the factory sees it: the decision of the code (long, short, flat or hold) becomes OrderIntent values, and the size
    comes from the sizer of the factory that the parameter sizer names. A class is made for each generated strategy with `build_adapter`."""

    registry_id: str = ""
    user_class: Any = None
    SCHEMA: ParamSchema = ParamSchema(())
    _user_names: frozenset[str] = frozenset()
    _protective = False

    def __init__(self, **values: Any) -> None:
        self.values = dict(values)
        self.protective_orders = bool(self._protective)
        self._user_values = {k: v for k, v in values.items() if k in self._user_names}
        self._user = self.user_class(**self._user_values)
        self._names: tuple[str, ...] = ()
        self._atr: str | None = None
        self._sheet: Any = None
        self._sizer: Any = None
        self._levels = _Levels()

    def init(self, ctx: InitContext) -> None:
        light = user_api.StrategyInit(self._user_values)
        self._user.init(light)
        requests = [RealRequest(r.name, r.kind, r.source, r.params, r.warmup) for r in light.requests]
        if self.values.get("sizer") == "vol_target":
            requests.append(RealRequest("sizer_vol", "ewma_vol", "close", (("decay_pct", int(self.values["sizer_decay_pct"])),), 21))
        apply_requests(ctx, requests, LIBRARY)
        ctx.set_warmup(light.extra_warmup)
        self._names = (*BAR_FIELDS, *(r.name for r in light.requests))
        self._atr = "atr" if any(r.name == "atr" for r in light.requests) else None
        self._sheet = self._sizer = None

    def _sizer_for(self, ctx: BarContext) -> Any:
        if self._sizer is None:
            from hokma.backtest.runner import sheet_of

            self._sheet = sheet_of(ctx.contract_id, ctx.as_of.date())
            kind = self.values.get("sizer", "fixed_contracts")
            params = {"fixed_contracts": {"n": self.values["sizer_n"]}, "fixed_risk": {"risk_pct": self.values["sizer_risk_pct"]},
                      "vol_target": {"target_vol": self.values["sizer_target_vol"], "decay_pct": self.values["sizer_decay_pct"], "periods_per_year": self.values["sizer_periods"]}}[kind]
            self._sizer = build_part(SIZERS, "sizer", kind, params, Env(self._sheet, "DT", self._levels))
        return self._sizer

    def _unit(self, unit: Any, kind: str, side: str, ctx: BarContext, entry: Decimal) -> Price:
        real = {"price": Price, "ticks": Ticks, "atr": ATR, "pct": Pct}[unit.TYPE](Decimal(repr(float(unit.value))))
        atr = None
        if isinstance(real, ATR):
            if self._atr is None:
                raise BadStrategy("an ATR stop or target needs the strategy to ask for an indicator named atr")
            value = float(ctx.series(self._atr).last)
            if not np.isfinite(value) or value <= 0:
                raise NoEntry("the ATR is not available yet or is zero")
            atr = Decimal(repr(value))
        return Price(protective_price(real, side=side, kind=kind, entry=entry, sheet=self._sheet, atr=atr))

    def on_bar(self, ctx: BarContext) -> tuple[Any, ...]:
        series = {name: np.asarray(ctx.series(name)) for name in self._names}
        held = 0 if ctx.position is None or ctx.position.qty == 0 else (1 if ctx.position.qty > 0 else -1)
        side_held = None if held == 0 else ("long" if held > 0 else "short")
        light = user_api.StrategyContext(index=ctx.index, as_of=ctx.as_of.isoformat(), symbol=ctx.contract_id, params=self._user_values, position_side=side_held,
                                         equity=float(ctx.equity), series=series)
        decision = self._user.on_bar(light)
        if not isinstance(decision, user_api.Decision):
            raise BadStrategy(f"{type(self._user).__name__}.on_bar must return a decision (long, short, flat or hold)")
        wanted = {"long": 1, "short": -1, "flat": 0, "hold": None}[decision.action]
        if wanted is None or wanted == held:
            return ()
        out: list[Any] = []
        if held != 0 and ctx.position is not None and ctx.position.side is not None:
            out.append(reduce_intent(ctx.contract_id, ctx.position.side, abs(ctx.position.qty), tag="exit"))
        if wanted == 0:
            return tuple(out)
        side = "buy" if wanted > 0 else "sell"
        try:
            sizer = self._sizer_for(ctx)
            entry = Decimal(repr(float(ctx.series("close").last)))
            stop = None if decision.stop is None else self._unit(decision.stop, "sl", side, ctx, entry)
            target = None if decision.target is None else self._unit(decision.target, "tp", side, ctx, entry)
            self._levels.stop = stop
            size = sizer.size(signal=SimpleNamespace(value=float(wanted)), ctx=ctx)
            if size is None:
                return tuple(out)
        except NoEntry:
            return tuple(out)
        make = buy if side == "buy" else sell
        out.append(make(ctx.contract_id, size, sl=stop, tp=target, tag=self.registry_id[:64]))
        return tuple(out)


def build_adapter(registry_id: str, user_class: Any, manifest: Mapping[str, Any]) -> type:
    """The class that the registry builds for a generated strategy: the parameters of the code plus the ones of the sizer."""
    names = frozenset(p.name for p in user_class.SCHEMA.params)
    return type(f"Generated_{registry_id}", (UserStrategy,), {"registry_id": registry_id, "user_class": user_class, "SCHEMA": adapter_schema(user_class.SCHEMA), "_user_names": names,
                                                              "_protective": bool(manifest.get("protective")), "__doc__": f"Strategy written as code ({registry_id}); its decision becomes orders through the sizer of the factory."})


# ---------------------------------------------------------------------------------------------------- the load (R3)
_LOADED: dict[tuple[str, str], type] = {}
LOAD_PROBLEMS: dict[str, str] = {}


def load_class(registry_id: str, *, db: Database | None = None) -> type:
    """The registry class of a generated strategy. It loads only when the state is generated or owner_reviewed, the file is the one whose hash the base keeps and
    the static check is green now. The module is named after the registry id; the file is the read only copy, never the draft."""
    with _db(db).read() as u:
        row = u.session.one("SELECT v.*, r.slug AS rule_slug, r.title AS rule_title, r.bank_id AS bank_id, (SELECT slug FROM strategy_bank WHERE id = r.bank_id) AS bank_slug FROM user_rule_versions v JOIN user_rules r ON r.id = v.rule_id WHERE v.registry_id = :r",
                            {"r": registry_id})
    if row is None or row["status"] not in APPROVED:
        raise UserRuleError("blocked", f"{registry_id} is not an approved strategy: it does not load")
    fund = version_folder(int(row["bank_id"]), row["bank_slug"], int(row["version"])) / STRATEGY_FILE
    try:
        in_fund = static_check.code_sha256(fund.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError):
        in_fund = None
    if in_fund != row["code_sha256"]:
        raise UserRuleError("blocked", f"{registry_id} does not load: {rel_folder(fund) if fund.is_relative_to(hokma_home()) else fund} is not the file that was saved (its sha256 changed or it is gone)")
    if json.loads(row["manifest_json"] or "{}").get("flow") == "pandas":                  # 1.3.0: a strategy in pandas; its class asks the loader for the signals
        from hokma.factory import pandas_flow

        return pandas_flow.load_class(row)
    key = (registry_id, row["code_sha256"])
    if key in _LOADED:
        _file_ok(row)                                                   # a cached class is still only served while its file is intact
        return _LOADED[key]
    folder = _file_ok(row)
    text = static_check.normalize_source((folder / STRATEGY_FILE).read_bytes().decode("utf-8"))
    if static_check.check(text, kind="strategy").findings:
        raise UserRuleError("blocked", f"the static check of {registry_id} is not green")
    if static_check.is_engine_source(text) and not json.loads(row["manifest_json"] or "{}").get("engine"):
        raise UserRuleError("blocked", f"{registry_id} imports the engine but its manifest says it does not")
    name = f"hokma.factory.strategies.user.{registry_id}"
    module = types.ModuleType(name)
    sys.modules[name] = module
    try:
        exec(compile(text, str(folder / STRATEGY_FILE), "exec"), module.__dict__)
    except Exception as exc:
        sys.modules.pop(name, None)
        raise UserRuleError("blocked", f"{registry_id} does not load: {type(exc).__name__}: {exc}") from None
    classes = [v for v in vars(module).values() if isinstance(v, type) and v.__module__ == name]
    if len(classes) != 1:
        raise UserRuleError("blocked", f"{registry_id} must define exactly one class")
    manifest = json.loads(row["manifest_json"] or "{}")
    built = classes[0] if manifest.get("engine") else build_adapter(registry_id, classes[0], manifest)
    _LOADED[key] = built
    return built


def database_exists() -> bool:
    from hokma.db.dbconfig import load_config
    from hokma.db.paths import db_path

    config = load_config()
    return config.engine == "postgres" or db_path().exists()


def add_user_rules(registry: Registry, *, db: Database | None = None, stages: Sequence[str] = APPROVED) -> int:
    """Register the generated strategies that may be used. One that does not pass its checks is left out of the registry and the reason is kept in `LOAD_PROBLEMS`,
    which `bank_get` shows as `load_problem` of that version: an approved version that does not load is never silent. The registry of the desks (live) passes only
    owner_reviewed: a strategy the owner did not approve is not in it."""
    if db is None and not database_exists():
        return 0
    try:
        with _db(db).read() as u:
            rows = u.session.query("SELECT v.registry_id, b.line AS line, r.title AS title FROM user_rule_versions v JOIN user_rules r ON r.id = v.rule_id LEFT JOIN strategy_bank b ON b.id = r.bank_id "
                                   "WHERE r.kind = 'strategy' AND v.status IN (" + ", ".join(f"'{s}'" for s in stages if s in APPROVED) + ") ORDER BY v.id")
    except Exception:                                    # the base is not there yet or has no such table: no generated strategies
        return 0
    added = 0
    for row in rows:
        try:
            cls = load_class(row["registry_id"], db=db)
            registry.add(Entry(row["registry_id"], "strategy", cls, row["line"], f"Generated strategy: {row['title']}"[:300], cls.SCHEMA.defaults(), f"user:{row['registry_id']}"))
            LOAD_PROBLEMS.pop(row["registry_id"], None)
        except UserRuleError as exc:
            LOAD_PROBLEMS[row["registry_id"]] = str(exc)                 # it is not loaded and it says why: bank_get shows it
            continue
        except Exception as exc:                                         # noqa: BLE001  (a class that breaks when it is read or added is one more reason, not a failure of every other strategy)
            LOAD_PROBLEMS[row["registry_id"]] = f"{row['registry_id']} does not register: {type(exc).__name__}: {str(exc)[:300]}"
            continue
        added += 1
    return added
