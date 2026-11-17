"""One answer for every request the schema rejects: `{"detail": {"code", "message", "errors"}}` with the message in the
language of the request, instead of pydantic's English text. Same envelope as the routes' own `HTTPException`s, so the
panel shows `detail.message` without special cases. The status stays 422."""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from hokma.setup.messages import Msg, lang_of, t

CODE = "validation_error"


def _kind(error: dict[str, Any]) -> str:
    kind = str(error.get("type", ""))
    if kind == "missing":
        return "required"
    if kind == "json_invalid":
        return "bad_json"
    if kind in ("string_too_short", "too_short"):
        return "min_length"
    if kind in ("string_too_long", "too_long"):
        return "max_length"
    if kind == "string_pattern_mismatch":
        return "pattern"
    if kind.startswith(("greater_than", "less_than", "greater_than_equal", "less_than_equal")):
        return "range"
    if kind.endswith(("_type", "_parsing")) or kind in ("json_invalid", "model_attributes_type"):
        return "wrong_type"
    return "generic"


def _field(error: dict[str, Any]) -> str:
    names = [str(part) for part in error.get("loc", ()) if isinstance(part, str) and part not in ("body", "query", "path")]
    return ".".join(names) or "-"


def describe(error: dict[str, Any]) -> Msg:
    """The catalog message for one pydantic error."""
    kind, field, ctx = _kind(error), _field(error), error.get("ctx") or {}
    if kind == "bad_json":
        return t("validation.bad_json")
    if kind == "required":
        return t("validation.required", field=field)
    if kind == "min_length":
        return t("validation.min_length", field=field, limit=ctx.get("min_length", 1))
    if kind == "max_length":
        return t("validation.max_length", field=field, limit=ctx.get("max_length", ""))
    if kind == "pattern":
        return t("validation.pattern", field=field)
    if kind == "range":
        return t("validation.range", field=field)
    if kind == "wrong_type":
        return t("validation.wrong_type", field=field)
    return t("validation.generic", field=field)


async def handle(request: Request, exc: RequestValidationError) -> JSONResponse:
    lang = lang_of(request)
    errors = exc.errors()
    if not errors:
        return JSONResponse(status_code=422, content={"detail": {"code": CODE, "message": t("validation.generic", field="-").text(lang), "errors": []}})
    shown = [{"field": _field(e), "type": _kind(e), "message": describe(e).text(lang)} for e in errors]
    return JSONResponse(status_code=422, content={"detail": {"code": CODE, "message": shown[0]["message"], "errors": shown}})


def attach(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, handle)
