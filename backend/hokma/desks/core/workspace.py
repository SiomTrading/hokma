"""Working folder of a desk: structure, index and templates (SPEC-017: OA-12, OA-21, OA-22, OA-23, OA-24, OA-25).

`bootstrap_workspace` creates the folder of a desk with its structure and a marker that names the desk. Running it again
changes nothing, and a folder that belongs to another desk is refused. History is created through `hokma.projects.gitops`,
which runs git with a locked-down environment and captured output. `index_workspace` gives a bounded map of the folder so
an agent reads the index before it walks the tree. `apply_template` copies a template of `kit/template/desk/templates/`
only after its version and its content digest match the approved ones.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hokma.desks.core.hashing import content_hash
from hokma.desks.core.prompts import COMMON_RULES, kit_dir
from hokma.desks.core.signal import LINES
from hokma.kit import safefs
from hokma.projects import gitops

SCHEMA_VERSION = 1
STRUCTURE: tuple[str, ...] = ("reports", "journal", "data", "notes", "tasks")
MARKER = ".desk/workspace.json"
RECEIPT = ".desk/template.json"
TEMPLATE_README_DIR = ".desk/template"
GITIGNORE = """# Secrets and caches never go into the history of a desk.
.env
.env.*
*.pem
*.key
secrets/
credentials*
.desk/cache/
"""
MAX_INDEX_ENTRIES = 500
RECENT_FILES = 20


class WorkspaceError(Exception):
    """The folder cannot be prepared: it belongs to another desk, a path is unsafe or a file is unreadable."""


class TemplateError(WorkspaceError):
    """A template that does not match its approved version or digest, or that cannot be applied."""


@dataclass
class WorkspaceReport:
    root: Path
    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    git: str = "skipped"          # skipped | created | existing | unavailable | unsafe | failed

    @property
    def changed(self) -> bool:
        return bool(self.created) or self.git == "created"


def _read_marker(root: Path) -> dict[str, Any] | None:
    try:
        raw = safefs.read_regular(root, MARKER, max_bytes=64_000)
    except safefs.UnsafePath as exc:
        raise WorkspaceError(str(exc)) from exc
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise WorkspaceError(f"{MARKER} is not valid JSON") from exc
    if not isinstance(data, dict) or "desk_id" not in data:
        raise WorkspaceError(f"{MARKER} does not name a desk")
    return data


def _put(root: Path, rel: str, data: bytes, report: WorkspaceReport) -> None:
    """Write a file only when it does not exist. A file that is there is never replaced."""
    try:
        if os.path.lexists(root / rel):
            report.existing.append(rel)
            return
        safefs.write_atomic(root, rel, data)
    except safefs.UnsafePath as exc:
        raise WorkspaceError(str(exc)) from exc
    report.created.append(rel)


def bootstrap_workspace(root: Path | str, *, desk_id: int, desk_name: str, with_git: bool = True) -> WorkspaceReport:
    """Create the folder of a desk with its structure. Safe to run again: nothing that exists is changed."""
    root = Path(root)
    if root.is_symlink():
        raise WorkspaceError("the desk folder cannot be a symbolic link")
    root.mkdir(parents=True, exist_ok=True)
    report = WorkspaceReport(root)
    marker = _read_marker(root)
    if marker is not None and marker["desk_id"] != desk_id:
        raise WorkspaceError(f"this folder belongs to desk {marker['desk_id']}, not to desk {desk_id}")
    for name in STRUCTURE:
        if (root / name).is_dir():
            report.existing.append(f"{name}/")
        else:
            _put(root, f"{name}/.gitkeep", b"", report)
    if marker is None:
        body = {"schema_version": SCHEMA_VERSION, "desk_id": desk_id, "desk_name": desk_name}
        _put(root, MARKER, (json.dumps(body, indent=1, sort_keys=True) + "\n").encode("utf-8"), report)
    else:
        report.existing.append(MARKER)
    _put(root, ".gitignore", GITIGNORE.encode("utf-8"), report)
    if with_git:
        report.git = _ensure_history(root)
    return report


def _ensure_history(root: Path) -> str:
    if os.path.lexists(root / ".git"):
        state = gitops.repo_state(root)
        return "existing" if state.ok else "unsafe"
    if not gitops.git_installed():
        return "unavailable"
    done = gitops.init_repo(root, "Create the desk folder")
    if done.ok:
        return "created"
    return {"missing": "unavailable", "unsafe": "unsafe"}.get(done.reason, "failed")


@dataclass(frozen=True)
class WorkspaceIndex:
    root: str
    total_files: int
    total_bytes: int
    folders: dict[str, dict[str, int]]          # top level folder -> {"files": n, "bytes": n}
    recent: tuple[tuple[str, int], ...]          # (relative path, modified time), newest first
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {"root": self.root, "total_files": self.total_files, "total_bytes": self.total_bytes, "folders": self.folders,
                "recent": [{"path": p, "modified": m} for p, m in self.recent], "truncated": self.truncated}


def index_workspace(root: Path | str, *, max_entries: int = MAX_INDEX_ENTRIES) -> WorkspaceIndex:
    """A bounded map of the folder: files and bytes per top level folder and the most recent files.

    It reads names and sizes only, stops after `max_entries` files and skips the history folder and links, so an agent can
    decide what to open before it walks the tree."""
    root = Path(root)
    folders: dict[str, dict[str, int]] = {}
    seen: list[tuple[str, int]] = []
    total_files = total_bytes = 0
    truncated = False
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != ".git" and not os.path.islink(os.path.join(current, d)))
        for name in sorted(files):
            if name == ".gitkeep":
                continue
            path = Path(current, name)
            if path.is_symlink():
                continue
            if total_files >= max_entries:
                truncated = True
                break
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = path.relative_to(root).as_posix()
            top = rel.split("/", 1)[0] if "/" in rel else "."
            bucket = folders.setdefault(top, {"files": 0, "bytes": 0})
            bucket["files"] += 1
            bucket["bytes"] += stat.st_size
            total_files += 1
            total_bytes += stat.st_size
            seen.append((rel, int(stat.st_mtime)))
        if truncated:
            break
    recent = tuple(sorted(seen, key=lambda item: (-item[1], item[0]))[:RECENT_FILES])
    return WorkspaceIndex(str(root), total_files, total_bytes, dict(sorted(folders.items())), recent, truncated)


# ---- templates ---------------------------------------------------------------------------------------------------

def _normalised(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def _file_hashes(folder: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(p for p in folder.rglob("*") if p.is_file() and p.relative_to(folder).as_posix() != "template.json"):
        out[path.relative_to(folder).as_posix()] = hashlib.sha256(_normalised(path.read_bytes())).hexdigest()
    return out


def template_digest(files: dict[str, str]) -> str:
    """One digest for the whole content of a template: the hash of its sorted path and hash pairs."""
    return content_hash(sorted(files.items()))


def _template_dir(name: str, root: Path | None) -> Path:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise TemplateError(f"bad template name {name!r}")
    folder = kit_dir("templates", name, root=root)
    if not (folder / "template.json").is_file():
        raise TemplateError(f"template {name!r} does not exist")
    return folder


def list_templates(*, root: Path | None = None) -> list[str]:
    base = kit_dir("templates", root=root)
    return sorted(p.name for p in base.iterdir() if (p / "template.json").is_file()) if base.is_dir() else []


def load_template(name: str, *, root: Path | None = None) -> dict[str, Any]:
    folder = _template_dir(name, root)
    try:
        meta = json.loads((folder / "template.json").read_text(encoding="utf-8"))
    except ValueError as exc:
        raise TemplateError(f"template {name}: template.json is not valid JSON") from exc
    problems = []
    for key in ("schema_version", "name", "display_name", "description", "version", "lines", "inject_rules", "files", "approved_versions"):
        if key not in meta:
            problems.append(f"missing '{key}'")
    if problems:
        raise TemplateError(f"template {name}: " + ", ".join(problems))
    if meta["name"] != name:
        raise TemplateError(f"template {name}: the name inside template.json is {meta['name']!r}")
    if bad := [x for x in meta["lines"] if x not in LINES]:
        raise TemplateError(f"template {name}: unknown line(s) {bad}")
    if bad := [x for x in meta["inject_rules"] if x not in COMMON_RULES]:
        raise TemplateError(f"template {name}: unknown rule(s) {bad}")
    return meta


def verify_template(name: str, *, root: Path | None = None) -> list[str]:
    """Differences between a template folder and its manifest. Empty means the manifest is current."""
    folder, meta = _template_dir(name, root), load_template(name, root=root)
    actual, declared = _file_hashes(folder), meta["files"]
    problems = [f"{p}: not in the manifest" for p in sorted(set(actual) - set(declared))]
    problems += [f"{p}: in the manifest but missing" for p in sorted(set(declared) - set(actual))]
    problems += [f"{p}: content changed" for p in sorted(set(actual) & set(declared)) if actual[p] != declared[p]]
    approved = {a["version"]: a["digest"] for a in meta["approved_versions"]}
    if approved.get(meta["version"]) != template_digest(actual):
        problems.append(f"version {meta['version']} has no approved digest that matches the content")
    return problems


def write_template_manifest(name: str, *, root: Path | None = None) -> dict[str, Any]:
    """Refresh `files` and the approved digest of the current version after an intended change of the template."""
    folder, meta = _template_dir(name, root), load_template(name, root=root)
    files = _file_hashes(folder)
    meta["files"] = files
    approved = [a for a in meta["approved_versions"] if a["version"] != meta["version"]]
    approved.append({"version": meta["version"], "digest": template_digest(files)})
    meta["approved_versions"] = sorted(approved, key=lambda a: a["version"])
    (folder / "template.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return meta


@dataclass
class TemplateReport:
    name: str
    version: str
    copied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    already_applied: bool = False


def apply_template(name: str, workspace: Path | str, *, expected_version: str, root: Path | None = None) -> TemplateReport:
    """Copy a template into a desk folder. Nothing is copied unless the version is the expected one, the content digest is
    the approved digest of that version and every file matches the manifest. Files already in the folder are kept."""
    workspace = Path(workspace)
    folder, meta = _template_dir(name, root), load_template(name, root=root)
    if meta["version"] != expected_version:
        raise TemplateError(f"template {name} is version {meta['version']}, not the expected {expected_version}")
    if problems := verify_template(name, root=root):
        raise TemplateError(f"template {name} does not match its approved version: " + "; ".join(problems))
    if _read_marker(workspace) is None:
        raise TemplateError("prepare the desk folder with bootstrap_workspace before applying a template")
    digest = template_digest(meta["files"])
    try:
        receipt_raw = safefs.read_regular(workspace, RECEIPT, max_bytes=64_000)
    except safefs.UnsafePath as exc:
        raise TemplateError(str(exc)) from exc
    if receipt_raw is not None:
        receipt = json.loads(receipt_raw)
        if (receipt.get("name"), receipt.get("version"), receipt.get("digest")) == (name, meta["version"], digest):
            return TemplateReport(name, meta["version"], already_applied=True)
        raise TemplateError(f"this desk already uses template {receipt.get('name')} {receipt.get('version')}; changing it is an explicit upgrade")
    report = TemplateReport(name, meta["version"])
    pending = {rel: rel[len("files/"):] if rel.startswith("files/") else f"{TEMPLATE_README_DIR}/{rel}" for rel in meta["files"]}
    try:
        for rel, target in pending.items():
            data = (folder / rel).read_bytes()
            if os.path.lexists(workspace / target):
                report.skipped.append(target)
                continue
            safefs.write_atomic(workspace, target, data)
            report.copied.append(target)
        body = {"schema_version": SCHEMA_VERSION, "name": name, "version": meta["version"], "digest": digest}
        safefs.write_atomic(workspace, RECEIPT, (json.dumps(body, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    except safefs.UnsafePath as exc:
        raise TemplateError(str(exc)) from exc
    return report
