"""`hokma` command: start, doctor, version, update, ext and the installer helpers."""
import argparse
import json
import os
import sys
from pathlib import Path

from hokma import __version__
from hokma.core.config import DEFAULT_PORT, LAST_PORT
from hokma.i18n import t

# Commands that run without the integrity gate: they must work to diagnose or (re)install.
NO_INTEGRITY_GATE = {"version", "doctor", "write-manifest", "stop"}


def _ask(prompt: str) -> str:
    """One answer; an empty answer, a closed input or no answer at all mean "not given"."""
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def _mirror_owner_in_profile() -> None:
    """Keep the profile row in step with owner.json (the database is where the dashboard reads the names)."""
    from hokma.db.errors import DbError
    from hokma.db.migrate import current_version, migrate
    from hokma.db.database import get_database
    from hokma.kit import owner

    names = owner.load()
    if not names["owner_name"]:
        return
    try:
        if current_version() == 0:
            migrate()
        with get_database().write() as u:
            u.profile.set_owner(names["owner_name"], names["fund_name"] or None)
    except DbError as exc:
        print(f"{t('cli.tag.warn')} {exc}", file=sys.stderr)


def _store_owner(owner_name: str, fund_name: str) -> None:
    """Save the names in the protected data dir and refill the kit text that the owner did not edit."""
    from hokma.kit import owner
    from hokma.kit.integrity import app_root
    from hokma.kit.sync import sync_kit

    owner.save(owner_name, fund_name)
    _mirror_owner_in_profile()
    try:
        sync_kit(app_root() / "kit" / "template", version=__version__)
    except (OSError, ValueError) as exc:
        print(f"{t('cli.tag.warn')} {t('cli.owner.sync_failed', error=exc)}", file=sys.stderr)


def cmd_owner(_: argparse.Namespace) -> int:
    if not _require_human():
        return 1
    owner_name = _ask(t("cli.owner.ask_name"))
    fund_name = _ask(t("cli.owner.ask_fund"))
    _store_owner(owner_name, fund_name)
    print(t("cli.owner.saved"))
    return 0


EXIT_DB_MIGRATION = 4


def _migrate_db() -> int:
    """Bring the database to the latest schema (idempotent). 0 on success, 4 on failure with the catalog message."""
    from hokma.db.errors import MigrationError
    from hokma.db.migrate import migrate

    try:
        migrate()
    except MigrationError as exc:
        print(f"{t('cli.tag.fail')} {exc}", file=sys.stderr)
        return EXIT_DB_MIGRATION
    return 0


def cmd_db(_: argparse.Namespace) -> int:
    code = _migrate_db()
    if code == 0:
        from hokma.db.migrate import current_version

        print(t("db.migrate.done", version=current_version()))
    return code


def _handoff_token(args: argparse.Namespace) -> tuple[str, str, str] | None:
    """The session token, the owner confirmation hash and the hash of the one-time owner secret (the last two may
    be empty) a previous supervisor handed over (an inherited pipe, or standard input on Windows). None on a
    normal start, ("", "", "") when it cannot be read."""
    from hokma.core import session

    try:
        if args.handoff_fd is not None:
            with os.fdopen(args.handoff_fd, "rb") as pipe:
                line = pipe.readline().decode("ascii", "replace").strip()
                owner = pipe.readline().decode("ascii", "replace").strip()
                once = pipe.readline().decode("ascii", "replace").strip()
        elif args.handoff_stdin:
            line = sys.stdin.readline().strip()
            owner = sys.stdin.readline().strip()
            once = sys.stdin.readline().strip()
        else:
            return None
    except OSError:
        return "", "", ""
    if not session.TOKEN_PATTERN.fullmatch(line) or any(
            v and not session.OWNER_HASH_PATTERN.fullmatch(v) for v in (owner, once)):
        return "", "", ""
    return line, owner, once


def cmd_start(args: argparse.Namespace) -> int:
    """`hokma start` runs Hokma in the background and returns at once (see hokma.core.launcher). With
    `--foreground` it is the supervisor itself: it creates the session token, starts the engine as a child and
    starts it again after an update or a crash. `--no-browser` and the internal hand over options only make sense
    in the foreground, so they imply it. The first run (names, folder, database, Claude) happens in the app."""
    foreground = args.foreground or args.no_browser or args.handoff_fd is not None or args.handoff_stdin
    if not foreground:
        from hokma.core.launcher import start

        return start(args.port)
    from hokma.core.supervisor import run_supervisor

    handoff = _handoff_token(args)
    if handoff is not None and handoff[0] == "":
        print(f"{t('cli.tag.fail')} {t('cli.start.bad_handoff')}", file=sys.stderr)
        return 2
    token, owner_hash, owner_secret_hash = handoff or (None, "", "")
    return run_supervisor(port=args.port, token=token, print_url=args.no_browser, owner_hash=owner_hash,
                          owner_secret_hash=owner_secret_hash)


def cmd_stop(_: argparse.Namespace) -> int:
    """Close Hokma cleanly through the supervisor's local control channel."""
    from hokma.core.launcher import stop

    return stop()


def cmd_restart(args: argparse.Namespace) -> int:
    from hokma.core.launcher import restart

    return restart(args.port)


def cmd_engine(args: argparse.Namespace) -> int:
    """The engine, started by the supervisor. The token arrives on standard input."""
    from hokma.core.engine_main import main as engine_main

    code = _migrate_db()
    if code:
        return code
    return engine_main(args.port)


def cmd_doctor(_: argparse.Namespace) -> int:
    from hokma.doctor.checks import FAIL, OK, WARN, run_checks

    marks = {OK: t("cli.tag.ok"), WARN: t("cli.tag.warn"), FAIL: t("cli.tag.fail")}
    results = run_checks()
    for c in results:
        print(f"{marks[c.status]} {c.name}" + (f": {c.detail}" if c.detail else ""))
    return 1 if any(c.status == FAIL for c in results) else 0


def cmd_init_config(_: argparse.Namespace) -> int:
    from hokma.kit.settings import write_settings

    print(f"settings written: {write_settings()}")
    return 0


def cmd_prepare_claude(_: argparse.Namespace) -> int:
    """Regenerate the Claude settings and print how to launch `claude` (JSON)."""
    from dataclasses import asdict

    from hokma.kit.launch import prepare_claude_launch

    print(json.dumps(asdict(prepare_claude_launch()), indent=2))
    return 0


def cmd_write_manifest(_: argparse.Namespace) -> int:
    from hokma.kit.integrity import write_manifest

    print(f"manifest written: {write_manifest()}")
    return 0


def cmd_verify_install(_: argparse.Namespace) -> int:
    print("integrity ok")  # the integrity gate in main() already refused otherwise
    return 0


def cmd_version(_: argparse.Namespace) -> int:
    print(f"hokma {__version__}")
    return 0


def cmd_kit_sync(args: argparse.Namespace) -> int:
    from hokma.kit.integrity import app_root
    from hokma.kit.sync import sync_kit

    report = sync_kit(app_root() / "kit" / "template", version=__version__)
    print(json.dumps(report.to_dict()))
    return 0


def cmd_post_update(_: argparse.Namespace) -> int:
    """Run by the NEW runtime right after the pointer flips: kit sync + settings. Prints the kit report."""
    from hokma.kit.integrity import app_root
    from hokma.kit.settings import write_settings
    from hokma.kit.sync import sync_kit

    report = sync_kit(app_root() / "kit" / "template", version=__version__)
    write_settings()
    code = _migrate_db()                                  # after the kit sync: new agent files are seeded too
    if code:
        return code
    _ensure_shortcuts()
    print(json.dumps(report.to_dict()))
    return 0


def _ensure_shortcuts() -> None:
    """Create the Hokma shortcuts that are missing (an owner coming from an older version has none). Never touches the
    database or the working folder, never fails the update, and does nothing outside an installed Hokma."""
    from hokma.core import shortcuts
    from hokma.kit.integrity import is_installed_app

    if not is_installed_app():
        return
    try:
        result = shortcuts.install(only_missing=True)
    except shortcuts.ShortcutError as exc:
        print(f"{t('cli.tag.warn')} " + t("cli.shortcuts.failed", error=exc), file=sys.stderr)
        return
    _warn_legacy_left(result)


def _warn_legacy_left(result) -> None:
    """Old Shut down and Restart shortcuts that could not be deleted: a warning, never a failure."""
    left = getattr(result, "legacy_left", None)
    if left:
        print(f"{t('cli.tag.warn')} " + t("cli.shortcuts.legacy_left", paths=", ".join(str(p) for p in left)),
              file=sys.stderr)


def cmd_shortcuts(args: argparse.Namespace) -> int:
    from hokma.core import shortcuts

    try:
        result = shortcuts.install(only_missing=not args.force)
    except shortcuts.ShortcutError as exc:
        print(f"{t('cli.tag.warn')} " + t("cli.shortcuts.failed", error=exc), file=sys.stderr)
        return 1
    _warn_legacy_left(result)
    print(t("cli.shortcuts.created"))
    return 0


def _print_update(result) -> None:
    if result.status == "up-to-date":
        print(t("cli.update.up_to_date", version=result.from_version))
        return
    print(t("cli.update.done", old=result.from_version, new=result.to_version))
    kit = result.kit or {}
    if kit.get("added"):
        print(t("cli.update.kit_added", files=", ".join(kit["added"])))
    if kit.get("updated"):
        print(t("cli.update.kit_updated", files=", ".join(kit["updated"])))
    if kit.get("conflicts"):
        print(t("cli.update.kit_conflicts"))
        for f in kit["conflicts"]:
            print(f"  {f}  ->  {f}.hokma-new")
    for note in kit.get("notes", []):
        print(f"{t('cli.tag.warn')} {note}")
    if kit.get("added") or kit.get("updated") or kit.get("conflicts"):
        print(t("cli.update.git_hint"))


def cmd_update(args: argparse.Namespace) -> int:
    from hokma.update.runner import run_update
    from hokma.update.source import UpdateError, resolve_source

    progress = None
    if args.progress_file:
        from hokma.update.state import ProgressWriter

        progress = ProgressWriter(Path(args.progress_file))
    try:
        result = run_update(resolve_source(args.source), force=args.force, progress=progress)
    except UpdateError as exc:
        print(f"{t('cli.tag.fail')} {t('cli.update.failed', error=exc, version=__version__)}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result.to_dict()))
    else:
        _print_update(result)
    return 0


def _ext_done(warnings: list[str]) -> None:
    print(t("cli.ext.done"))
    for w in warnings:
        print(f"{t('cli.tag.warn')} {w}", file=sys.stderr)


def _require_human() -> bool:
    """Changing what Hokma runs needs a real terminal: the agent's shell has none (stdin and stdout are pipes)."""
    if sys.stdin.isatty() and sys.stdout.isatty():
        return True
    print(f"{t('cli.tag.fail')} {t('cli.ext.need_terminal')}", file=sys.stderr)
    return False


def _confirm_by_id(req) -> bool:
    """The approval IS the control: show exactly what will run, ask for the id, and for the accept word when the
    program could be changed by the agent. Everything printed was escaped by Request.summary()."""
    print("\n" + req.summary() + "\n\n" + t("cli.ext.request_line", id=req.id, when=req.requested_at))
    word = t("cli.ext.accept_word")
    try:
        if input(t("cli.ext.confirm_id", id=req.id)).strip() != req.id:
            return False
        if req.risks:
            return input(t("cli.ext.confirm_accept", word=word)).strip() == word
    except EOFError:                                  # the terminal closed: nothing is confirmed
        return False
    return True


MUTATING_EXT = {"enable", "disable", "add-marketplace", "approve", "reject"}


def cmd_ext(args: argparse.Namespace) -> int:
    from hokma.kit import extensions, requests

    if args.ext_cmd in MUTATING_EXT and not _require_human():
        return 1
    try:
        if args.ext_cmd == "enable":
            _ext_done(extensions.set_plugin(args.plugin, True))
        elif args.ext_cmd == "disable":
            _ext_done(extensions.set_plugin(args.plugin, False))
        elif args.ext_cmd == "add-marketplace":
            source = {"source": "github", "repo": args.github} if args.github else {"source": "directory", "path": args.path}
            _ext_done(extensions.add_marketplace(args.name, source))
        elif args.ext_cmd == "request":
            if args.request_kind == "plugin":
                req = requests.request_plugin(args.name)
            else:
                try:
                    config = json.loads(args.config)
                except ValueError as exc:
                    raise requests.RequestError(t("cli.ext.bad_json", error=exc)) from exc
                req = requests.request_mcp(args.name, config)
            print(t("cli.ext.registered", id=req.id))
        elif args.ext_cmd == "requests":
            pending = requests.list_pending()
            for r in pending:
                print(f"{r.id}  {r.kind}  {r.name}")
            if not pending:
                print(t("cli.ext.none_pending"))
        elif args.ext_cmd == "approve":
            _ext_done(requests.approve_request(args.id, _confirm_by_id))
        elif args.ext_cmd == "reject":
            requests.reject_request(args.id)
            print(t("cli.ext.rejected"))
        else:
            data, problems = extensions.load()
            print(json.dumps(data, indent=2))
            for p in problems:
                print(f"{t('cli.tag.warn')} {p}", file=sys.stderr)
    except ValueError as exc:      # RequestError is a ValueError
        print(f"{t('cli.tag.fail')} {exc}", file=sys.stderr)
        return 1
    return 0


def _integrity_gate() -> int:
    from hokma.kit.launch import IntegrityError, check_integrity

    try:
        check_integrity()
    except IntegrityError as exc:
        print(f"{t('cli.tag.fail')} {t('cli.integrity.modified', detail=exc)}", file=sys.stderr)
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):      # accented text must never crash a legacy console
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    p = argparse.ArgumentParser(prog="hokma")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="Start Hokma in the background and open the dashboard")
    s.add_argument("--port", type=int, default=None, help=f"default: the first free port from {DEFAULT_PORT} to {LAST_PORT}")
    s.add_argument("--no-browser", action="store_true", help="foreground: print the address instead of opening it")
    s.add_argument("--foreground", action="store_true", help="stay in this terminal (tests, watching the log)")
    s.add_argument("--handoff-fd", type=int, help=argparse.SUPPRESS)           # internal: supervisor hand over
    s.add_argument("--handoff-stdin", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_start)
    sub.add_parser("stop", help="Close Hokma cleanly").set_defaults(fn=cmd_stop)
    rs = sub.add_parser("restart", help="Close Hokma and open it again with a new session")
    rs.add_argument("--port", type=int, default=None)
    rs.set_defaults(fn=cmd_restart)
    en = sub.add_parser("engine", help=argparse.SUPPRESS)                      # internal: started by `hokma start`
    en.add_argument("--port", type=int, default=DEFAULT_PORT)
    en.set_defaults(fn=cmd_engine)
    sub.add_parser("doctor", help="Check the installation").set_defaults(fn=cmd_doctor)
    sub.add_parser("owner", help="Change your name and your fund's name (needs an interactive terminal)"
                   ).set_defaults(fn=cmd_owner)
    sub.add_parser("version", help="Print the version").set_defaults(fn=cmd_version)
    d = sub.add_parser("db", help="Database tools")
    dsub = d.add_subparsers(dest="db_cmd", required=True)
    dsub.add_parser("migrate", help="Apply pending database migrations").set_defaults(fn=cmd_db)

    u = sub.add_parser("update", help="Update Hokma (runtime only; your work is never touched)")
    u.add_argument("--from", dest="source", help="override: local manifest or https URL (tests)")
    u.add_argument("--force", action="store_true", help="reinstall the same or an older version")
    u.add_argument("--json", action="store_true")
    u.add_argument("--progress-file", help=argparse.SUPPRESS)                  # internal: stage reports for the app
    u.set_defaults(fn=cmd_update)

    e = sub.add_parser("ext", help="Manage your extensions (plugins, marketplaces, MCP servers)")
    esub = e.add_subparsers(dest="ext_cmd", required=True)
    for name in ("enable", "disable"):
        esub.add_parser(name, help="needs an interactive terminal").add_argument("plugin", help="name@marketplace")
    am = esub.add_parser("add-marketplace", help="needs an interactive terminal")
    am.add_argument("name")
    grp = am.add_mutually_exclusive_group(required=True)
    grp.add_argument("--github", help="owner/repo")
    grp.add_argument("--path", help="local directory marketplace (outside your working folder)")
    esub.add_parser("list")
    rq = esub.add_parser("request", help="ask the student to activate a plugin or add an MCP server")
    rqsub = rq.add_subparsers(dest="request_kind", required=True)
    rqsub.add_parser("plugin").add_argument("name", help="name@marketplace")
    rm = rqsub.add_parser("mcp")
    rm.add_argument("name")
    rm.add_argument("config", help="JSON definition of the server")
    esub.add_parser("requests", help="list pending requests")
    for name in ("approve", "reject"):
        esub.add_parser(name, help="needs an interactive terminal").add_argument("id")
    e.set_defaults(fn=cmd_ext)

    # Used by the installer and the updater:
    sub.add_parser("init-config", help="Write Hokma's Claude settings").set_defaults(fn=cmd_init_config)
    sub.add_parser("prepare-claude", help="Regenerate Claude settings and print the launch spec (JSON)"
                   ).set_defaults(fn=cmd_prepare_claude)
    sub.add_parser("write-manifest", help="Write the sha256 integrity manifest").set_defaults(fn=cmd_write_manifest)
    sub.add_parser("verify-install", help="Exit 0 only if the install matches its manifest"
                   ).set_defaults(fn=cmd_verify_install)
    k = sub.add_parser("kit-sync", help="Install or update the kit in your working folder")
    k.set_defaults(fn=cmd_kit_sync)
    sc = sub.add_parser("shortcuts", help="Create the Hokma shortcut and remove the old Shut down and Restart ones")
    sc.add_argument("--force", action="store_true", help="recreate the ones that already exist")
    sc.set_defaults(fn=cmd_shortcuts)
    sub.add_parser("post-update", help="Kit sync + settings after an update").set_defaults(fn=cmd_post_update)

    args = p.parse_args(argv)
    if args.cmd not in NO_INTEGRITY_GATE:
        code = _integrity_gate()
        if code:
            return code
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
