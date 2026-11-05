"""HTTP surface of the Learn screen. Every route needs the session token."""
from fastapi import APIRouter, Depends, HTTPException, Path, Query

from hokma.i18n import LANGUAGES, language, t
from hokma.learn import content, progress
from hokma.status.auth import require_token

router = APIRouter(dependencies=[Depends(require_token)])


def _lang(value: str) -> str:
    return value if value in LANGUAGES else language()


@router.get("/api/learn/topics", summary="Topics of the guide, in order")
def list_topics(lang: str = Query(default="", max_length=5)) -> list[dict]:
    return [{"slug": x.slug, "title": x.title} for x in content.topics(_lang(lang))]


@router.get("/api/learn/topics/{slug}", summary="One topic as Markdown")
def get_topic(slug: str = Path(pattern=r"^[a-z0-9][a-z0-9-]{0,60}$"), lang: str = Query(default="", max_length=5)) -> dict:
    topic = content.get(_lang(lang), slug)
    if topic is None:
        raise HTTPException(status_code=404, detail=t("learn.api.unknown_topic"))
    return {"slug": topic.slug, "title": topic.title, "body": topic.body}


@router.get("/api/learn/search", summary="Search every topic of the guide")
def search(q: str = Query(default="", max_length=200), lang: str = Query(default="", max_length=5)) -> list[dict]:
    return content.search(_lang(lang), q)


@router.get("/api/learn/progress", summary="Progress of Your first day with Hokma")
def first_day() -> dict:
    return progress.collect()
