"""Publish a Hokma release:  python scripts/publish_release.py [--notes-file FILE] [--dry-run]

Run from a clean clone of `main`. Reads `__version__`, builds `dist/hokma-X.Y.Z.tar.gz` with `git archive`
(`dashboard/dist` is versioned, so it is included), writes `dist/hokma-X.Y.Z.tar.gz.sha256` and checks the
package with the same `extract()` the updater uses. Without --dry-run it then creates the GitHub Release with
both files; `gh release create --target <HEAD>` creates the tag `vX.Y.Z` on the remote together with the
release, so no tag is sent separately. `gh` must be logged in with write access to the repository (the dry
run checks that too).

git and gh are started from absolute paths on a safe PATH with a cleaned environment (hokma.update.safe_exec).
git works on the clone you run this from (that is the point); gh runs in a fresh temporary folder and names
the repository explicitly.

Release notes are read by students: English, no references to where ideas came from.
"""
import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from hokma.update.archive import extract  # noqa: E402
from hokma.update.github import RELEASE_REPO  # noqa: E402
from hokma.update.safe_exec import clean_env, find_executable, run_isolated  # noqa: E402
from hokma.update.source import UpdateError  # noqa: E402

VERSION_LINE = re.compile(r'^__version__\s*=\s*"(\d+\.\d+\.\d+)"', re.M)


class PublishError(RuntimeError):
    pass


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    exe = find_executable("git")
    if exe is None:
        raise PublishError("git was not found on a safe PATH")
    result = subprocess.run([exe, "-C", str(root), *args], capture_output=True, text=True, cwd=root,
                            env=clean_env(root))
    if check and result.returncode != 0:
        raise PublishError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def repo_root() -> Path:
    exe = find_executable("git")
    if exe is None:
        raise PublishError("git was not found on a safe PATH")
    result = subprocess.run([exe, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                            env=clean_env(Path.cwd()))
    if result.returncode != 0:
        raise PublishError("run this from inside a git clone")
    return Path(result.stdout.strip())


def read_version(root: Path) -> str:
    match = VERSION_LINE.search((root / "backend/hokma/__init__.py").read_text(encoding="utf-8"))
    if not match:
        raise PublishError("could not read __version__ from backend/hokma/__init__.py")
    return match.group(1)


def check_state(root: Path, tag: str, dry_run: bool) -> list[str]:
    """Raise when the clone is not publishable; return notes about checks that a dry run skips."""
    notes = []
    if git(root, "status", "--porcelain").stdout.strip():
        raise PublishError("the working tree is not clean")
    if "origin" in git(root, "remote").stdout.split():
        git(root, "fetch", "--quiet", "--tags", "origin")          # compare with the real remote state
    elif dry_run:
        notes.append("no origin remote: the real run requires one, and HEAD must be origin/main")
    else:
        raise PublishError("no origin remote")
    if git(root, "rev-parse", "-q", "--verify", f"refs/tags/{tag}", check=False).returncode == 0:
        raise PublishError(f"the tag {tag} already exists")
    origin = git(root, "rev-parse", "-q", "--verify", "origin/main", check=False)
    if origin.returncode != 0:
        if not dry_run:
            raise PublishError("origin/main not found")
        notes.append("origin/main not found: the real run requires HEAD to be origin/main")
    elif origin.stdout.strip() != git(root, "rev-parse", "HEAD").stdout.strip():
        raise PublishError("HEAD is not origin/main")
    return notes


def check_gh_login() -> None:
    result = run_isolated("gh", ["auth", "status", "--hostname", "github.com"], timeout=30)
    if result is None:
        raise PublishError("gh was not found on a safe PATH")
    if result.returncode != 0:
        raise PublishError("gh is not authenticated: run `gh auth login` with write access to the repository")


def build_package(root: Path, version: str) -> tuple[Path, str]:
    dist = root / "dist"
    dist.mkdir(exist_ok=True)
    package = dist / f"hokma-{version}.tar.gz"
    git(root, "archive", "--format=tar.gz", f"--prefix=hokma-{version}/", "-o", str(package), "HEAD")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    (dist / f"{package.name}.sha256").write_text(f"{digest}  {package.name}\n", encoding="ascii")
    with tempfile.TemporaryDirectory() as tmp:           # self-check with the updater's own extraction
        try:
            extract(package, Path(tmp) / "release")
        except UpdateError as exc:
            raise PublishError(f"the package fails the updater check: {exc}") from None
    return package, digest


def publish(root: Path, version: str, package: Path, notes_file: str | None) -> str:
    tag = f"v{version}"
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    notes = ["--notes-file", str(Path(notes_file).resolve())] if notes_file else ["--notes", f"Hokma {version}"]
    result = run_isolated(
        "gh", ["release", "create", tag, str(package.resolve()), f"{package.resolve()}.sha256",
               "--repo", RELEASE_REPO, "--target", head, "--title", f"Hokma {version}", "--latest", *notes],
        timeout=600)
    if result is None:
        raise PublishError("gh was not found on a safe PATH")
    if result.returncode != 0:
        remove_orphan_tag(root, tag)
        raise PublishError(f"creating the release failed: {result.stderr.strip()}")
    return result.stdout.strip()


def remove_orphan_tag(root: Path, tag: str) -> None:
    """The tag did not exist before this run. Remove it from the remote only when it is an orphan.

    Orphan means: the tag exists remotely and gh answers explicitly that the release was not found. Any other
    answer (network, permissions, rate limit, gh missing) proves nothing, so nothing is deleted and a warning
    tells the operator to check by hand.
    """
    if not git(root, "ls-remote", "--tags", "origin", f"refs/tags/{tag}", check=False).stdout.strip():
        return
    view = run_isolated("gh", ["release", "view", tag, "--repo", RELEASE_REPO], timeout=60)
    if view is not None and view.returncode != 0 and "release not found" in view.stderr.lower():
        git(root, "push", "origin", f":refs/tags/{tag}", check=False)
        return
    detail = "gh was not found" if view is None else view.stderr.strip() or f"exit code {view.returncode}"
    print(f"warning: the tag {tag} exists on the remote and I could not confirm that no release uses it "
          f"({detail}). Check it by hand; nothing was deleted.", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and publish a Hokma release")
    parser.add_argument("--notes-file", help="file with the release notes (English)")
    parser.add_argument("--dry-run", action="store_true", help="build and check the package, publish nothing")
    args = parser.parse_args(argv)
    try:
        root = repo_root()
        version = read_version(root)
        for note in check_state(root, f"v{version}", args.dry_run):
            print(f"note: {note}")
        check_gh_login()
        package, digest = build_package(root, version)
        print(f"version: {version}\nsha256: {digest}\npackage: {package}")
        if args.dry_run:
            print("dry run: nothing was tagged or published")
        else:
            print(f"release: {publish(root, version, package, args.notes_file)}")
    except PublishError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
