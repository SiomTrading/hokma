"""System status: the checks behind `hokma doctor` plus the database check."""
from hokma import __version__
from hokma.core.config import hokma_home
from hokma.doctor.checks import FAIL, OK, WARN, Check, run_checks
from hokma.i18n import t


def check_database() -> Check:
    """The SQLite file exists, has the latest schema and can be read."""
    from hokma.db import current_version, get_database
    from hokma.db.migrate import discover

    name = t("status.db.name")
    try:
        version = current_version()
        latest = discover()[-1][0]
    except Exception as exc:                        # a damaged file must show up as a failed check, not as a 500
        return Check(name, FAIL, t("status.db.unreadable", error=type(exc).__name__))
    if version == 0:
        return Check(name, WARN, t("status.db.absent"))
    if version < latest:
        return Check(name, WARN, t("status.db.old", version=version, latest=latest))
    try:
        db = get_database()
        with db.read() as unit:
            unit.profile.get_or_default()
        return Check(name, OK, t("status.db.ok", engine=db.engine, version=version, path=db.path))
    except Exception as exc:
        return Check(name, FAIL, t("status.db.unreadable", error=type(exc).__name__))


def collect() -> dict:
    checks = run_checks() + [check_database()]
    return {
        "version": __version__,
        "workdir": str(hokma_home()),
        "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks],
        "summary": {s: sum(1 for c in checks if c.status == s) for s in (OK, WARN, FAIL)},
    }
