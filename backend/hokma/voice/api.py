"""POST /api/transcribe, GET /api/transcribe/status, POST /api/transcribe/model. All need the per-boot session token."""
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from hokma.core.origin import token_matches
from hokma import i18n
from hokma.engine import attachments as att
from hokma.rpc.ws import SESSION_TOKEN
from hokma.voice.audio import EXTENSIONS, sniff_audio
from hokma.voice.transcriber import DEFAULT_LANGUAGE, LANGUAGE_CHOICES, FasterWhisperTranscriber, Transcriber

_transcriber: Transcriber | None = None


def get_transcriber() -> Transcriber:
    global _transcriber
    if _transcriber is None:
        _transcriber = FasterWhisperTranscriber()
    return _transcriber


def configure(transcriber: Transcriber | None) -> None:
    """Replaces the process-wide transcriber (tests); None restores the real one on next use."""
    global _transcriber
    _transcriber = transcriber


def _lang(accept_language: str) -> str:
    first = accept_language.split(",")[0].strip().lower()[:2]
    return first if first in i18n.LANGUAGES else i18n.language()


def _texts(t) -> dict[str, str]:
    return {
        "bad_token": t("voice.error.bad_token"),
        "bad_language": t("voice.error.bad_language"),
        "too_large": t("voice.error.too_large"),
        "no_audio": t("voice.error.no_audio"),
        "not_audio": t("voice.error.not_audio"),
        "engine_missing": t("voice.error.engine_missing"),
        "model_missing": t("voice.error.model_missing"),
        "transcribe_failed": t("voice.error.transcribe_failed"),
        "offline": t("voice.error.offline"),
        "disk": t("voice.error.disk"),
        "download_failed": t("voice.error.download_failed"),
        "tls_conflict": t("voice.error.tls_conflict"),
    }


def _fail(status: int, code: str, accept_language: str, **extra) -> HTTPException:
    catalog = i18n.catalog(_lang(accept_language))
    texts = _texts(lambda key: catalog.get(key) or i18n.catalog("en").get(key, key))
    return HTTPException(status_code=status, detail={"code": code, "message": texts.get(code, code), **extra})


def create_router(transcriber_getter=None, engine_getter=None) -> APIRouter:
    router = APIRouter()
    get_t = transcriber_getter or get_transcriber

    def get_engine():
        if engine_getter is not None:
            return engine_getter()
        from hokma.engine.api import get_engine as default_engine
        return default_engine()

    def check_token(header: str, query: str, lang: str) -> None:
        if not token_matches(header or query, SESSION_TOKEN):
            raise _fail(401, "bad_token", lang)

    def status_body(t: Transcriber, lang: str) -> dict:
        data = dict(t.status())
        code = data.pop("error_code", None)
        detail = data.pop("error_detail", None)
        data["error"] = {**_fail(500, code, lang).detail, **({"detail": detail} if detail else {})} if code else None
        return data

    @router.get("/api/transcribe/status")
    async def transcribe_status(x_hokma_token: str = Header(default=""), token: str = Query(default=""),
                                accept_language: str = Header(default="")):
        check_token(x_hokma_token, token, accept_language)
        return status_body(get_t(), accept_language)

    @router.post("/api/transcribe/model", status_code=202)
    async def download_model(x_hokma_token: str = Header(default=""), token: str = Query(default=""),
                             accept_language: str = Header(default="")):
        check_token(x_hokma_token, token, accept_language)
        t = get_t()
        if not t.available():
            raise _fail(501, "engine_missing", accept_language)
        t.start_download()
        return status_body(t, accept_language)

    @router.post("/api/transcribe", status_code=201)
    async def transcribe(request: Request, x_hokma_token: str = Header(default=""), token: str = Query(default=""),
                         x_filename: str = Header(default=""), accept_language: str = Header(default=""),
                         language: str = Query(default=DEFAULT_LANGUAGE)):
        lang = accept_language
        check_token(x_hokma_token, token, lang)
        if language not in LANGUAGE_CHOICES:
            raise _fail(400, "bad_language", lang)
        limit = att.MAX_ATTACHMENT_BYTES + 64 * 1024
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise _fail(413, "too_large", lang)
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                raise _fail(413, "too_large", lang)
        content_type = request.headers.get("content-type", "")
        parsed = att.parse_multipart(content_type, bytes(body))
        if parsed is not None:
            name, declared_type, data = parsed
        elif content_type.lower().startswith("multipart/"):
            raise _fail(400, "no_audio", lang)
        else:
            name, declared_type, data = x_filename or "voice-note", content_type.split(";")[0], bytes(body)
        if not data:
            raise _fail(400, "no_audio", lang)
        if len(data) > att.MAX_ATTACHMENT_BYTES:
            raise _fail(413, "too_large", lang)
        mime = sniff_audio(data, declared_type)
        if mime is None:
            raise _fail(415, "not_audio", lang)

        t = get_t()
        if not t.available():
            raise _fail(501, "engine_missing", lang)
        if t.status().get("state") != "ready":
            raise _fail(409, "model_missing", lang)

        digest = att.save_blob(data)
        blob = att.attachments_dir() / digest
        try:
            result = await run_in_threadpool(t.transcribe, Path(blob), language)
        except HTTPException:
            raise
        except Exception:
            raise _fail(500, "transcribe_failed", lang)

        stem = att.safe_name(name).rsplit(".", 1)[0] or "voice-note"
        store = get_engine().store
        record = store.add_attachment(digest, f"{stem}.{EXTENSIONS.get(mime, 'bin')}", mime, len(data))
        save = getattr(store, "set_transcript", None)
        if save is not None and result.text:
            save(record.id, result.text)
        return JSONResponse({"text": result.text, "attachment_id": record.id, "name": record.name, "mime": mime,
                             "size": len(data), "language": result.language, "duration": round(result.duration, 2)},
                            status_code=201)

    return router


router = create_router()
