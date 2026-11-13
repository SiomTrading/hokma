"""sha256 manifest of an installed app (which includes the package templates).

Each runtime slot has its own manifest next to its `app/` folder: <data>/runtime/<slot>/manifest.json.
"""
import hashlib
import json
from pathlib import Path

from hokma.core.config import runtime_root
from hokma.i18n import t

SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv"}


def app_root() -> Path:
    """Folder that contains backend/, cli/, dashboard/, kit/ (this file is backend/hokma/kit/)."""
    return Path(__file__).resolve().parents[3]


def manifest_path() -> Path:
    return app_root().parent / "manifest.json"


def is_installed_app() -> bool:
    """True when running from <data>/runtime/<slot>/app (an installed Hokma), False in a dev checkout."""
    root = app_root()
    return root.name == "app" and root.parent.parent == runtime_root().resolve()


def compute(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix == ".pyc" or SKIP_DIRS & set(p.relative_to(root).parts):
            continue
        try:
            files[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            files[p.relative_to(root).as_posix()] = ""      # antivirus or system blocked it (bug 1113): verify() names it
    return files


def write_manifest() -> Path:
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    files = compute(app_root())
    blocked = [f for f, digest in files.items() if not digest]
    if blocked:
        raise OSError(t("integrity.unreadable", file=", ".join(blocked)))
    path.write_text(json.dumps({"version": 1, "files": files}, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")
    return path


def find_problems() -> list[tuple[str, str]]:
    """(text, file) of every difference with the manifest; the file is "" for the manifest problems."""
    path = manifest_path()
    if not path.is_file():
        return [(t("integrity.missing_manifest"), "")]
    try:
        expected = json.loads(path.read_text(encoding="utf-8"))["files"]
    except (ValueError, KeyError, OSError):
        return [(t("integrity.bad_manifest"), "")]
    actual = compute(app_root())
    problems = [(t("integrity.unreadable", file=f), f) for f, digest in actual.items() if not digest]
    problems += [(t("integrity.modified", file=f), f) for f in expected if actual.get(f) and actual[f] != expected[f]]
    problems += [(t("integrity.absent", file=f), f) for f in expected if f not in actual]
    problems += [(t("integrity.new", file=f), f) for f in actual if actual[f] and f not in expected]
    return problems


def verify() -> list[str]:
    """Return a list of problems; empty means the installed app matches its manifest."""
    return [text for text, _ in find_problems()]
