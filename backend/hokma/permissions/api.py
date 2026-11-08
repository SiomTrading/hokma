"""Settings > General > Permissions, Settings > Trash and the restore points. Every route needs the session token
(`X-Hokma-Token` or `?token=`), the token that never reaches an agent, so no agent can change the permission mode,
restore, empty or delete for good. Errors are `{"detail": {"code", "message"}}`.

    GET  /api/profile/permission-mode            {mode, intro_seen}
    PUT  /api/profile/permission-mode            {mode?, intro_seen?} -> {mode, intro_seen, changed, turns_running}
    GET  /api/protections                        the always-on protections (read only, no switch)
    GET  /api/trash                              {items[], count, total_bytes}
    POST /api/session/owner-nonce     {nonce, once} -> {registered}  (once per start; `once` is the one-time secret of the start link; see hokma.core.session; a `remember` field is accepted and ignored)
    POST /api/session/handoff-code               -> {code, host}   (token + owner; only at 127.0.0.1 / localhost; the code is spent at `enter` on `host`)
    POST /api/session/enter           {code, nonce, public_key{x,y}} -> {token, persistent}   (only at the name of this installation; sets the cookie `hokma_resume`)
    POST /api/session/challenge                  -> {challenge}    (only at the name of this installation, same origin)
    POST /api/session/resume          {nonce, challenge, signature} -> {token}   (cookie `hokma_resume` + ECDSA signature instead of the token; see hokma.core.resume)
    POST /api/session/logout                     -> {logged_out}  (owner call: forgets the remembered session of this browser)
    POST /api/trash/{id}/restore                 -> {id, restored_to, renamed}
    POST /api/trash/{id}/purge        {confirm}  -> {id}            (delete for good)
    POST /api/trash/empty             {confirm}  -> {count}
    GET  /api/checkpoints                        ?project_id=  -> {project, points[{id, at, conversation_id}]}
    GET  /api/checkpoints/{point}/changes        ?project_id=  -> {deleted[], changed[], truncated}
    POST /api/checkpoints/{point}/restore-file   {path, project_id?} -> {restored[], renamed}
    POST /api/checkpoints/{point}/restore-deleted {project_id?}     -> {restored[], count}

The calls that change something the owner decides (the PUT of the mode, restore, purge and empty of the Trash, the two
restores of a restore point) also need `X-Hokma-Owner`: the confirmation the open tab registered. A token read from
the browser profile is not enough for them (403 `owner_confirmation_required`).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Cookie, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from hokma.core import config, handoff, p256, resume, session
from hokma.core.origin import token_matches
from hokma.db import Actor, DbError, current_version, get_database
from hokma.i18n import t
from hokma.permissions import checkpoints, guard_log, modes, trash
from hokma.rpc.ws import SESSION_TOKEN

HUMAN = Actor.human("owner")
RULES = ("delete", "truncate", "hokma_internal", "force_push", "outside_workdir", "secrets", "real_money")
TITLES = {
    "delete": lambda: t("protection.delete"), "truncate": lambda: t("protection.truncate"),
    "hokma_internal": lambda: t("protection.hokma_internal"), "force_push": lambda: t("protection.force_push"),
    "outside_workdir": lambda: t("protection.outside_workdir"), "secrets": lambda: t("protection.secrets"),
    "real_money": lambda: t("protection.real_money"),
}


class ModeBody(BaseModel):
    mode: str | None = Field(default=None, max_length=10)
    intro_seen: bool | None = None


class OwnerNonce(BaseModel):
    nonce: str = Field(max_length=64)
    once: str = Field(default="", max_length=64)
    remember: bool = False                    # read for compatibility and ignored: this route never issues a cookie


class PublicKey(BaseModel):
    x: str = Field(max_length=64)             # 32 bytes, base64url
    y: str = Field(max_length=64)


class EnterBody(BaseModel):
    code: str = Field(max_length=64)
    nonce: str = Field(max_length=64)
    public_key: PublicKey


class ResumeBody(BaseModel):
    nonce: str = Field(max_length=64)
    challenge: str = Field(max_length=64)
    signature: str = Field(max_length=128)    # 64 bytes r||s, base64url


class Confirm(BaseModel):
    confirm: bool = False


class RestoreFile(BaseModel):
    path: str = Field(max_length=1000)
    project_id: str | None = Field(default=None, max_length=32)


class ProjectRef(BaseModel):
    project_id: str | None = Field(default=None, max_length=32)


def _fail(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def _auth(header_token: str, query_token: str) -> None:
    if not token_matches(header_token or query_token, SESSION_TOKEN):
        raise _fail(401, "unauthorized", "bad token")


def _owner(owner_header: str) -> None:
    """Second layer after the token: the confirmation only the owner's open tab holds."""
    if not session.owner_matches(owner_header):
        raise _fail(403, "owner_confirmation_required", t("permissions.err.owner_confirmation_required"))


NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _resume_fail(code: str, message: str, *, clear_cookie: bool = False) -> HTTPException:
    """A refused `resume`: always 401, never a token, never cached."""
    headers = dict(NO_STORE)
    if clear_cookie:
        headers["Set-Cookie"] = resume.expired_cookie_header()
    return HTTPException(status_code=401, detail={"code": code, "message": message}, headers=headers)


def _own_name_and_same_origin(request: Request, *, same_origin: bool = True) -> None:
    """The routes of the remembered session only answer at the name of this installation (and, unless told
    otherwise, to a fetch of the page itself)."""
    if not resume.host_allows_cookie(request.headers.get("host")):
        raise _resume_fail("session.resume_denied", t("permissions.err.resume_denied"))
    if same_origin and request.headers.get("sec-fetch-site", "").lower() != "same-origin":
        raise _resume_fail("session.resume_denied", t("permissions.err.resume_denied"))


def _no_store(response: Response) -> None:
    for name, value in NO_STORE.items():
        response.headers[name] = value


def _db():
    if current_version() == 0:
        raise _fail(409, "no_database", t("permissions.err.not_found"))
    return get_database()


def _journal(db, title: str, refs: Any = None) -> None:
    try:
        with db.write() as u:
            u.journal.add("action", title[:200], actor=HUMAN, outcome="success", refs=refs)
    except DbError:
        pass                                    # the journal is a record, never a reason to undo what the owner did


def _record_revocation() -> None:
    """The row for `guard_events`. Without a database yet there is nothing to write to."""
    if current_version() == 0:
        return
    guard_log.record_session_revoked(
        get_database(), "An old copy of the remembered session cookie was used, so the saved session was closed.")


BAD_SIGNATURE_GAP_SECONDS = 30.0
_last_bad_signature = float("-inf")


def reset_bad_signature_throttle_for_tests() -> None:
    global _last_bad_signature
    _last_bad_signature = float("-inf")


def _record_bad_signature() -> None:
    """A cookie that matched the lineage came without a valid signature. One row per 30 seconds at most, so a
    process that keeps trying cannot fill the table. Nothing was rotated or revoked."""
    global _last_bad_signature
    now = time.monotonic()
    if current_version() == 0 or now - _last_bad_signature < BAD_SIGNATURE_GAP_SECONDS:
        return
    _last_bad_signature = now
    guard_log.record_session_unsigned(
        get_database(), "The saved session cookie was presented without a valid signature, so it was refused. "
                        "Nothing was changed.")


def _running_turns(engine_getter) -> int:
    try:
        if engine_getter is None:
            from hokma.engine.api import get_engine as engine_getter
        return sum(1 for s in engine_getter().sessions.values() if s.state == "running")
    except Exception:
        return 0


def _project_path(db, project_id: str | None):
    from hokma.engine.dbstore import DbProjects

    project = DbProjects(db).get(project_id)
    if project is None:
        raise _fail(404, "unknown_project", t("permissions.err.not_found"))
    return project


def _protect() -> list[str]:
    from hokma.kit.settings import protected_paths

    return [str(p) for p in protected_paths()]


def _trash_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except trash.TrashError as exc:
        status = 404 if exc.code == "not_found" else 400
        raise _fail(status, exc.code, exc.message) from None


def _point_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except checkpoints.CheckpointError as exc:
        status = 404 if exc.code == "unknown_point" else 409 if exc.code == "no_git" else 400
        raise _fail(status, exc.code, exc.message) from None


def create_router(engine_getter=None) -> APIRouter:
    router = APIRouter()

    @router.post("/api/session/owner-nonce")
    def register_owner(body: OwnerNonce, x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
        """The open tab registers its confirmation. The first registration needs the one-time secret of the start link
        (`#t=<token>&o=<secret>`), which is consumed; afterwards only the same value again is fine (a reload). A
        process that only holds the token has no secret, so it cannot take the place. It never issues a cookie: the
        remembered session starts at `handoff-code` and `enter`."""
        _auth(x_hokma_token, token)
        if not session.OWNER_NONCE_PATTERN.fullmatch(body.nonce):
            raise _fail(400, "bad_owner_nonce", t("permissions.err.bad_owner_nonce"))
        result = session.register_owner(body.nonce, body.once)
        if result == session.SECRET_NEEDED:
            raise _fail(403, "owner_secret_required", t("permissions.err.owner_secret_required"))
        if result != session.REGISTERED:
            raise _fail(409, "owner_already_registered", t("permissions.err.owner_already_registered"))
        return {"registered": True}

    @router.post("/api/session/handoff-code")
    def handoff_code(request: Request, response: Response, x_hokma_token: str = Header(default=""),
                     x_hokma_owner: str = Header(default=""), token: str = Query(default="")) -> dict:
        """The registered tab asks for a code of one use (60 seconds) and the name to carry it to. Only from
        127.0.0.1 or localhost, where the tab registered: from the name of this installation it makes no sense and is
        refused. A new code cancels the previous one."""
        _auth(x_hokma_token, token)
        _owner(x_hokma_owner)
        host = request.headers.get("host", "")
        if resume.host_allows_cookie(host):
            raise _fail(403, "session.handoff_origin", t("permissions.err.handoff_origin"))
        port = host.rpartition(":")[2] if ":" in host else ""
        _no_store(response)
        return {"code": handoff.book().new_code(),
                "host": config.browser_host() + (f":{port}" if port.isdigit() else "")}

    @router.post("/api/session/enter")
    def enter_session(body: EnterBody, request: Request, response: Response) -> dict:
        """Spend a handoff code at the name of this installation: the engine stores the public key of this browser,
        creates the lineage, sets the cookie and gives the token. No token is needed (the code is the proof), and
        a wrong, expired or used code gets 401 and nothing else."""
        _own_name_and_same_origin(request, same_origin=False)
        denied = _resume_fail("session.resume_denied", t("permissions.err.resume_denied"))
        if not session.OWNER_NONCE_PATTERN.fullmatch(body.nonce) or not p256.public_key_is_valid(
                body.public_key.x, body.public_key.y):
            raise denied
        if not handoff.book().check_code(body.code):
            raise denied
        issued = resume.store().issue((body.public_key.x, body.public_key.y), body.nonce)
        if issued is None:
            raise denied
        _no_store(response)
        response.headers.append("set-cookie", resume.cookie_header(issued.value))
        return {"token": SESSION_TOKEN, "persistent": issued.persistent}

    @router.post("/api/session/challenge")
    def session_challenge(request: Request, response: Response,
                          hokma_resume: str | None = Cookie(default=None)) -> dict:
        """A challenge of one use (30 seconds) for the page to sign with the private key of its origin. Only a holder
        of a cookie the engine issued gets one, and it is bound to that cookie, so a flood cannot take the place of
        the legitimate tab (new ones are refused when the limit is reached; pending ones are never evicted)."""
        _own_name_and_same_origin(request)
        _no_store(response)
        if not resume.store().knows(hokma_resume):
            raise _resume_fail("session.resume_denied", t("permissions.err.resume_denied"))
        challenge = handoff.book().new_challenge(resume.digest_of(hokma_resume))
        if challenge is None:
            raise _resume_fail("session.resume_denied", t("permissions.err.resume_denied"))
        return {"challenge": challenge}

    @router.post("/api/session/resume")
    def resume_session(body: ResumeBody, request: Request, response: Response,
                       hokma_resume: str | None = Cookie(default=None)) -> dict:
        """Give the token back to a tab that holds the remembered-session cookie AND the private key of the lineage.
        Order: the name of this installation and `Sec-Fetch-Site: same-origin`; the challenge (spent in every case); the
        shape of the confirmation, the signature and the cookie; the signature against the public key of the lineage
        the cookie belongs to; and only then the rotation (or, for an old value, the revocation). A cookie without
        the key gets 401 and changes nothing."""
        _own_name_and_same_origin(request)
        denied = lambda clear=False: _resume_fail("session.resume_denied", t("permissions.err.resume_denied"),
                                                  clear_cookie=clear)
        if not handoff.book().consume_challenge(body.challenge, resume.digest_of(hokma_resume) if isinstance(hokma_resume, str) else None):
            raise denied()
        if not session.OWNER_NONCE_PATTERN.fullmatch(body.nonce) or p256.b64url_decode(
                body.signature, p256.SIGNATURE_BYTES) is None:
            raise denied()
        origin = "http://" + request.headers.get("host", "").lower()          # exact origin, port included
        message = f"hokma-resume:{origin}:{body.challenge}:{body.nonce}".encode("utf-8")
        outcome = resume.store().resume(
            hokma_resume, lambda x, y: p256.verify_raw(x, y, message, body.signature), body.nonce)
        if outcome.bad_signature:
            _record_bad_signature()
            raise denied()
        if outcome.status == resume.REVOKED:
            if outcome.fresh_revocation:
                _record_revocation()
            raise _resume_fail("session.revoked", t("permissions.err.session_revoked"), clear_cookie=True)
        if outcome.status != resume.OK or not outcome.lineage:
            raise denied(outcome.clear_cookie)
        _no_store(response)
        if outcome.cookie:
            response.headers.append("set-cookie", resume.cookie_header(outcome.cookie))
        return {"token": SESSION_TOKEN}

    @router.post("/api/session/logout")
    def logout_session(response: Response, x_hokma_token: str = Header(default=""),
                       x_hokma_owner: str = Header(default=""), token: str = Query(default="")) -> dict:
        """Settings > General > close the session of this browser: the verifier is deleted and the cookie expires."""
        _auth(x_hokma_token, token)
        _owner(x_hokma_owner)
        resume.store().logout()
        for name, value in NO_STORE.items():
            response.headers[name] = value
        response.headers.append("set-cookie", resume.expired_cookie_header())
        return {"logged_out": True}

    @router.get("/api/profile/permission-mode")
    def get_mode(x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict[str, Any]:
        _auth(x_hokma_token, token)
        db = _db()
        with db.read() as u:
            profile = u.profile.get_or_default()
        return {"mode": profile.permission_mode, "intro_seen": profile.permission_mode_intro_seen}

    @router.put("/api/profile/permission-mode")
    def put_mode(body: ModeBody, x_hokma_token: str = Header(default=""),
            x_hokma_owner: str = Header(default=""), token: str = Query(default="")) -> dict:
        _auth(x_hokma_token, token)
        _owner(x_hokma_owner)
        if body.mode is None and body.intro_seen is None:
            raise _fail(400, "bad_mode", t("permissions.err.bad_mode"))
        if body.mode is not None and body.mode not in modes.MODES:
            raise _fail(400, "bad_mode", t("permissions.err.bad_mode"))
        db = _db()
        try:
            with db.write() as u:
                before = u.profile.get()
                if before is None:
                    raise _fail(409, "no_profile", t("permissions.err.not_found"))
                intro = body.intro_seen if body.intro_seen is not None else (True if body.mode else None)
                if body.mode is not None:
                    profile = u.profile.set_permission_mode(body.mode, intro_seen=intro)
                else:
                    profile = u.profile.save(permission_mode_intro_seen=intro)
        except DbError as exc:
            raise _fail(400, "bad_mode", str(exc)[:200]) from None
        changed = before.permission_mode != profile.permission_mode
        if changed:
            _journal(db, t("permissions.journal.mode", mode=profile.permission_mode),
                     {"from": before.permission_mode, "to": profile.permission_mode})
        return {"mode": profile.permission_mode, "intro_seen": profile.permission_mode_intro_seen, "changed": changed,
                "turns_running": _running_turns(engine_getter)}

    @router.get("/api/protections")
    def protections(x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict[str, Any]:
        """The always-on protections, for Settings > Permissions. Read only: nothing here can be switched."""
        _auth(x_hokma_token, token)
        return {"always_on": True, "rules": [{"id": r, "title": TITLES[r]()} for r in RULES]}

    # -------------------------------------------------------------------------------------------- trash

    @router.get("/api/trash")
    def trash_list(x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict[str, Any]:
        _auth(x_hokma_token, token)
        return trash.listing(_db())

    @router.post("/api/trash/empty")
    def trash_empty(body: Confirm, x_hokma_token: str = Header(default=""),
            x_hokma_owner: str = Header(default=""), token: str = Query(default="")) -> dict:
        _auth(x_hokma_token, token)
        _owner(x_hokma_owner)
        if not body.confirm:
            raise _fail(400, "confirm_required", t("permissions.err.confirm_required"))
        db = _db()
        count = _trash_call(trash.empty, db)
        _journal(db, t("permissions.journal.trash_emptied", count=count))
        return {"count": count}

    @router.post("/api/trash/{item_id}/restore")
    def trash_restore(item_id: int, x_hokma_token: str = Header(default=""),
            x_hokma_owner: str = Header(default=""), token: str = Query(default="")) -> dict:
        _auth(x_hokma_token, token)
        _owner(x_hokma_owner)
        db = _db()
        result = _trash_call(trash.restore, db, item_id, protect=_protect())
        _journal(db, t("permissions.journal.trash_restored", name=result["restored_to"].replace("\\", "/").split("/")[-1]),
                 {"trash_item": item_id})
        return result

    @router.post("/api/trash/{item_id}/purge")
    def trash_purge(item_id: int, body: Confirm, x_hokma_token: str = Header(default=""),
            x_hokma_owner: str = Header(default=""),
                    token: str = Query(default="")) -> dict:
        _auth(x_hokma_token, token)
        _owner(x_hokma_owner)
        if not body.confirm:
            raise _fail(400, "confirm_required", t("permissions.err.confirm_required"))
        db = _db()
        result = _trash_call(trash.purge, db, item_id)
        _journal(db, t("permissions.journal.trash_purged", name=result["name"]), {"trash_item": item_id})
        return {"id": item_id}

    # ---------------------------------------------------------------------------------- restore points

    @router.get("/api/checkpoints")
    async def points_list(project_id: str | None = Query(default=None, max_length=32),
                          x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
        _auth(x_hokma_token, token)
        project = _project_path(_db(), project_id)
        points = await asyncio.to_thread(_point_call, checkpoints.listing, project.path)
        return {"project": {"id": project.id, "name": project.name}, "points": points}

    @router.get("/api/checkpoints/{point_id}/changes")
    async def points_changes(point_id: str, project_id: str | None = Query(default=None, max_length=32),
                             x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> dict:
        _auth(x_hokma_token, token)
        project = _project_path(_db(), project_id)
        return await asyncio.to_thread(_point_call, checkpoints.changes, project.path, point_id)

    @router.post("/api/checkpoints/{point_id}/restore-file")
    async def points_restore_file(point_id: str, body: RestoreFile, x_hokma_token: str = Header(default=""),
            x_hokma_owner: str = Header(default=""),
                                  token: str = Query(default="")) -> dict:
        _auth(x_hokma_token, token)
        _owner(x_hokma_owner)
        db = _db()
        project = _project_path(db, body.project_id)
        result = await asyncio.to_thread(_point_call, checkpoints.restore_file, project.path, point_id, body.path,
                                         _protect())
        _journal(db, t("permissions.journal.checkpoint_restored", name=body.path.split("/")[-1]),
                 {"checkpoint": point_id})
        return result

    @router.post("/api/checkpoints/{point_id}/restore-deleted")
    async def points_restore_deleted(point_id: str, body: ProjectRef, x_hokma_token: str = Header(default=""),
            x_hokma_owner: str = Header(default=""),
                                     token: str = Query(default="")) -> dict:
        _auth(x_hokma_token, token)
        _owner(x_hokma_owner)
        db = _db()
        project = _project_path(db, body.project_id)
        result = await asyncio.to_thread(_point_call, checkpoints.restore_deleted, project.path, point_id, _protect())
        _journal(db, t("permissions.journal.checkpoint_restored", name=f"{result['count']} files"),
                 {"checkpoint": point_id})
        return result

    @router.get("/api/guard-events")
    def blocks_list(limit: int = Query(default=50, ge=1, le=500), x_hokma_token: str = Header(default=""),
                    token: str = Query(default="")) -> dict[str, Any]:
        """Recent refusals of the always-on protections, newest first (what the chat cards were built from)."""
        _auth(x_hokma_token, token)
        db = _db()
        guard_log.drain(db)
        with db.read() as u:
            rows = [r for r in u.guard_events.list(decision="blocked", limit=limit * 4) if r.guard in RULES][:limit]
        return {"events": [{"id": r.id, "rule": r.guard, "tool": r.tool, "target": r.reason, "at": r.created_at}
                           for r in rows]}

    return router


router = create_router()
