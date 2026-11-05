"""Localized text for the setup endpoints: the language comes from the request, not from the process.

`t(key, **params)` here does not translate at once: it returns a `Msg` that is rendered later, in the language of the
request that raised it (a module-level singleton has no request).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from hokma.i18n import LANGUAGES, catalog, language


def lang_of(request: Request) -> str:
    """The language the client asks for (Accept-Language); without one, the language of the process."""
    header = request.headers.get("accept-language", "").strip().lower()
    first = header.split(",")[0].split(";")[0].strip()
    code = first.split("-")[0]
    return code if code in LANGUAGES else language()


@dataclass(frozen=True)
class Msg:
    key: str
    params: dict = field(default_factory=dict)

    def text(self, lang: str) -> str:
        text = catalog(lang).get(self.key) or catalog("en").get(self.key, self.key)
        try:
            return text.format(**self.params)
        except (KeyError, IndexError, ValueError):
            return text


def t(key: str, /, **params: object) -> Msg:
    return Msg(key, params)


def tr(lang: str, msg: Msg) -> str:
    return msg.text(lang)


class SetupError(Exception):
    """A refusal with a stable code (the panel switches on it) and a catalog message."""

    def __init__(self, status: int, message: Msg) -> None:
        super().__init__(message.key)
        self.status, self.message = status, message

    @property
    def code(self) -> str:
        return self.message.key.rsplit(".", 1)[-1]

    def http(self, lang: str) -> HTTPException:
        return HTTPException(status_code=self.status, detail={"code": self.code, "message": self.message.text(lang)})
