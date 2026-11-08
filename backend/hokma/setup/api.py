"""HTTP surface of the first run and Settings > General. Every route needs the session token."""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request

from hokma.core.origin import token_matches
from hokma import __version__
from hokma.core.config import hokma_home
from hokma.db import Actor, DbError, current_version, get_database, migrate
from hokma.db.services import save_owner, set_language
from hokma.kit import owner as owner_names
from hokma.kit.integrity import app_root
from hokma.kit.sync import render, sync_kit
from hokma.rpc.ws import SESSION_TOKEN
from hokma.setup import claude, database, workdir
from hokma.setup.messages import SetupError, lang_of, t

HUMAN = Actor.human("owner")
_COMMENT = re.compile(r"\A\s*<!--.*?-->\s*", re.S)
MAX_PREVIEW = 20000


def require_token(request: Request, x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> None:
    if not token_matches(x_hokma_token or token, SESSION_TOKEN):
        raise HTTPException(status_code=401, detail={"code": "unauthorized",
                                                     "message": t("setup.err.bad_token").text(lang_of(request))})


def _db():
    if current_version() == 0:
        migrate()
    return get_database()


def _journal(db, lang: str, msg, *, body: str | None = None, refs: Any = None) -> None:
    try:
        with db.write() as u:
            u.journal.add("action", msg.text(lang)[:200], actor=HUMAN, outcome="success", body=body, refs=refs)
    except DbError:
        pass          # the journal is a record, never a reason to undo what the owner just did


def _run(request: Request, fn):
    """Run `fn(lang)` turning SetupError and database errors into HTTP answers in the request language."""
    lang = lang_of(request)
    try:
        return fn(lang)
    except SetupError as exc:
        raise exc.http(lang) from None
    except DbError as exc:
        raise SetupError(400, t("setup.err.db_failed", error=str(exc)[:200])).http(lang) from None


def _state() -> dict:
    db = _db()
    with db.read() as u:
        profile = u.profile.get()
        agents_total = u.agents.count()
    names = owner_names.load()
    home = hokma_home()
    return {
        "completed": bool(profile and profile.setup_completed_at),
        "profile": profile.to_dict() if profile else None,
        "owner": {"owner_name": profile.owner_name if profile else names["owner_name"],
                  "fund_name": (profile.fund_name or "") if profile else names["fund_name"]},
        "workdir": {"path": str(home), "exists": home.is_dir(), "git": workdir.is_git_repo(home),
                    "manual": (home / "CLAUDE.md").is_file(), "options": workdir.options()},
        "database": {"engine": database.engine_in_use(), "location": database.location_shown()},
        "agents": {"total": agents_total},
        "claude": {"state": profile.claude_status if profile else "unknown",
                   "plan": profile.claude_plan if profile else None},
        "version": __version__,
    }


def _sync_engine(u, lang: str) -> bool:
    """Make profile.database_engine say which engine is really in use. True when it changed."""
    actual = database.engine_in_use()
    if u.profile.get_or_default().database_engine == actual:
        return False
    u.profile.save(database_engine=actual)
    u.journal.add("action", t("setup.journal.database", engine=actual).text(lang), actor=HUMAN, outcome="success")
    return True


def _remember_claude(state: claude.ClaudeState) -> None:
    db = _db()
    try:
        with db.write() as u:
            if u.profile.get() is not None:
                u.profile.set_claude_status(state.state, state.plan)
    except DbError:
        pass


router = APIRouter(dependencies=[Depends(require_token)])


@router.get("/api/setup")
def get_setup(request: Request) -> dict:
    return _run(request, lambda lang: _state())


@router.post("/api/setup")
def complete_setup(request: Request) -> dict:
    def go(lang: str) -> dict:
        db = _db()
        with db.write() as u:
            profile = u.profile.get()
            if profile is None:
                raise SetupError(409, t("setup.err.no_profile"))
            if not profile.workdir:
                u.profile.save(workdir=str(hokma_home()))
            _sync_engine(u, lang)
            if not profile.setup_completed_at and not profile.permission_mode_intro_seen:
                # A new installation starts in "Do everything without asking" (the "Ready" card shows the switch and
                # the owner may have changed it already: that sets intro_seen). One that updated from 0.0.5 had
                # finished this step long ago, so it keeps "ask" and sees the one-time notice in the chat.
                u.profile.set_permission_mode("auto", intro_seen=True)
            u.profile.complete_setup()
        _remember_claude(claude.detect())
        _journal(db, lang, t("setup.journal.done"))
        return _state()

    return _run(request, go)


# ---------------------------------------------------------------- Claude

@router.get("/api/claude")
def get_claude() -> dict:
    state = claude.detect()
    _remember_claude(state)
    return state.to_dict()


@router.post("/api/claude/login")
def claude_login(request: Request) -> dict:
    return _run(request, lambda lang: claude.start_login())


@router.post("/api/claude/login/code")
def claude_login_code(request: Request, body: dict = Body(...)) -> dict:
    return _run(request, lambda lang: claude.submit_code(body.get("code", "")))


# ---------------------------------------------------------------- working folder

@router.get("/api/fs/dirs")
def fs_dirs(request: Request, path: str | None = Query(default=None)) -> dict:
    return _run(request, lambda lang: workdir.list_dirs(path))


@router.post("/api/fs/dirs", status_code=201)
def fs_make_dir(request: Request, body: dict = Body(...)) -> dict:
    return _run(request, lambda lang: workdir.make_dir(body.get("parent"), body.get("name")))


@router.get("/api/workdir")
def get_workdir(request: Request) -> dict:
    def go(lang: str) -> dict:
        home = hokma_home()
        return {"path": str(home), "exists": home.is_dir(), "git": workdir.is_git_repo(home),
                "options": workdir.options()}

    return _run(request, go)


@router.post("/api/workdir")
def set_workdir(request: Request, body: dict = Body(...)) -> dict:
    def go(lang: str) -> dict:
        result = workdir.apply(body.get("path"))
        db = _db()
        with db.write() as u:
            if u.profile.get() is not None:
                u.profile.save(workdir=result["path"])
            run_seeds_for(u)
        _journal(db, lang, t("setup.journal.workdir", path=result["path"]), refs={"previous": result["previous"]})
        return result

    return _run(request, go)


def run_seeds_for(unit) -> None:
    """Read the agents of the (new) working folder into the database."""
    from hokma.db.seeds import seed_agents

    seed_agents(unit.session)


# ---------------------------------------------------------------- owner and fund

def _template_text() -> str:
    path = app_root() / "kit" / "template" / "HOKMA.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


@router.get("/api/owner/preview")
def owner_preview(request: Request, owner_name: str = Query(default=""), fund_name: str = Query(default="")) -> dict:
    """The manual of the kit with the names filled in, exactly as kit sync would write it."""
    owner, fund = owner_names.clean(owner_name), owner_names.clean(fund_name)
    values = {"owner_name": owner or owner_names.DEFAULT_OWNER, "fund_name": fund or owner_names.DEFAULT_FUND}
    text = render(_template_text().encode("utf-8"), "HOKMA.md", values).decode("utf-8")
    return {"text": _COMMENT.sub("", text, count=1)[:MAX_PREVIEW], "owner_name": owner, "fund_name": fund}


@router.post("/api/owner")
def set_owner(request: Request, body: dict = Body(...)) -> dict:
    def go(lang: str) -> dict:
        owner = owner_names.clean(body.get("owner_name"))
        fund = owner_names.clean(body.get("fund_name"))
        if not owner:
            raise SetupError(400, t("setup.err.name_required"))
        language = body.get("language")
        theme = body.get("theme")
        if language is not None and language not in ("es", "en"):
            raise SetupError(400, t("setup.err.bad_value", field="language"))
        if theme is not None and theme not in ("dark", "light"):
            raise SetupError(400, t("setup.err.bad_value", field="theme"))
        db = _db()
        existing_language = None
        with db.read() as u:
            existing = u.profile.get()
            existing_language = existing.language if existing else None
        save_owner(db, owner, fund or None)
        set_language(db, language or existing_language or lang)
        with db.write() as u:
            fields: dict[str, Any] = {}
            if theme:
                fields["theme"] = theme
            if not u.profile.get_or_default().workdir:
                fields["workdir"] = str(hokma_home())
            fields["database_engine"] = database.engine_in_use()
            if fields:
                u.profile.save(**fields)
        try:
            sync_kit(app_root() / "kit" / "template", version=__version__)
        except (OSError, ValueError) as exc:
            raise SetupError(500, t("setup.err.kit_sync_failed", error=str(exc)[:200])) from exc
        with db.write() as u:
            run_seeds_for(u)
        _journal(db, lang, t("setup.journal.owner", owner=owner, fund=f" / {fund}" if fund else ""))
        return _state()

    return _run(request, go)


@router.patch("/api/profile")
def patch_profile(request: Request, body: dict = Body(...)) -> dict:
    """Theme and language follow the owner across restarts. Needs the profile (created when the name is saved)."""
    def go(lang: str) -> dict:
        db = _db()
        with db.read() as u:
            if u.profile.get() is None:
                raise SetupError(409, t("setup.err.no_profile"))
        if "theme" in body:
            if body["theme"] not in ("dark", "light"):
                raise SetupError(400, t("setup.err.bad_value", field="theme"))
            with db.write() as u:
                u.profile.save(theme=body["theme"])
        if "database_engine" in body:
            with db.write() as u:          # the value sent is not trusted: the profile follows the real engine
                _sync_engine(u, lang)
        if "language" in body:
            if body["language"] not in ("es", "en"):
                raise SetupError(400, t("setup.err.bad_value", field="language"))
            set_language(db, body["language"])
        return _state()

    return _run(request, go)


def shutdown() -> None:
    claude.stop_login()


router.add_event_handler("shutdown", shutdown)
