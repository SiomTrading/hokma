"""Kit installation and updates that never overwrite the student's work and never run git.

The kit template (kit/template in the app) is copied into the student's working folder.
`kit-baseline.json` (in the protected data dir) remembers the sha256 of each kit file as last
installed, so a later update can tell "the student changed this file" from "the kit changed it":

- new kit file                                    -> copied
- kit file changed, student did not touch it      -> replaced
- kit file changed AND the student modified it    -> the student's file stays; the new version is written
                                                     next to it as <name>.hokma-new and reported
- kit file unchanged between versions             -> nothing happens, even if the student edited it
- file the student deleted                        -> not brought back

Markdown files carry the owner and fund markers: they are filled from `<data>/owner.json` (defaults "the owner"
and "the fund") BEFORE the hash is computed, so changing a name is just another kit change.

`.hokma/extensions.json` (informational mirror) is never overwritten after the first copy.

Hokma does NOT run git on the student's repo here: the repo is the student's (and their agent's), who can plant
hooks and config in it, and running git there would execute that code outside the sandbox. The student or their
agent commits the changes whenever they want. What changed is recorded in `<data>/kit-report.json`
(protected data dir) so the UI can show it later.

All reads and writes go through hokma.kit.safefs: a symlink is never followed, a file or folder that is a link
is skipped and reported, and files are replaced through a temporary file in the same folder.
"""
import hashlib
import json
import re
import stat
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from hokma.core.config import hokma_home, kit_baseline_path, kit_report_path
from hokma.i18n import t
from hokma.kit import owner as owner_names
from hokma.kit import safefs

USER_OWNED = {".hokma/extensions.json"}
SIDE_SUFFIX = ".hokma-new"
MARKER = re.compile(r"\{\{(owner_name|fund_name)\}\}")


@dataclass
class KitReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)      # student's file kept; new one at <f>.hokma-new
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_baseline() -> dict[str, str]:
    try:
        return json.loads(kit_baseline_path().read_text(encoding="utf-8"))["files"]
    except (OSError, ValueError, KeyError):
        return {}


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _mode(src: Path) -> int:
    return 0o755 if src.stat().st_mode & stat.S_IXUSR else 0o644


def render(data: bytes, rel: str, values: dict[str, str]) -> bytes:
    """Fill the owner and fund markers of a Markdown file; other files stay byte for byte."""
    if not rel.endswith(".md"):
        return data
    return MARKER.sub(lambda m: values[m.group(1)], data.decode("utf-8")).encode("utf-8")


def sync_kit(template: Path, *, version: str, workdir: Path | None = None,
             values: dict[str, str] | None = None) -> KitReport:
    workdir = workdir or hokma_home()
    values = values or owner_names.marker_values()
    report = KitReport()
    baseline = _load_baseline()
    new_baseline = dict(baseline)
    workdir.mkdir(parents=True, exist_ok=True)

    for src in sorted(p for p in template.rglob("*") if p.is_file()):
        rel = src.relative_to(template).as_posix()
        data, mode = render(src.read_bytes(), rel, values), _mode(src)   # the hash is of the filled text
        new_hash, base_hash = _sha(data), baseline.get(rel)
        try:
            current = safefs.read_regular(workdir, rel)

            if rel in USER_OWNED:
                if current is None and base_hash is None:
                    safefs.write_atomic(workdir, rel, data, mode)
                    report.added.append(rel)
                new_baseline[rel] = new_hash
                continue

            if current is None:
                if base_hash is None:                     # genuinely new for this student
                    safefs.write_atomic(workdir, rel, data, mode)
                    report.added.append(rel)
                new_baseline[rel] = new_hash              # deleted by the student: stays deleted
                continue

            cur_hash = _sha(current)
            if cur_hash == new_hash or base_hash == new_hash:
                new_baseline[rel] = new_hash              # nothing new in the kit for this file
            elif base_hash is not None and cur_hash == base_hash:
                safefs.write_atomic(workdir, rel, data, mode)   # student never touched it: take the new one
                report.updated.append(rel)
                new_baseline[rel] = new_hash
            else:                                         # student modified it: keep theirs, new one beside it
                safefs.write_atomic(workdir, rel + SIDE_SUFFIX, data, mode)
                report.conflicts.append(rel)
                new_baseline[rel] = new_hash
        except safefs.UnsafePath as exc:
            report.notes.append(t("kit.skipped", path=rel, error=exc))
            # baseline untouched: the file is retried on the next update

    _write_json_atomic(kit_baseline_path(), {"version": version, "files": new_baseline})
    _write_json_atomic(kit_report_path(), {"version": version, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                           **report.to_dict()})
    return report
