"""HTTP surface of the library model (SPEC-010 §4.8). There is no endpoint that starts the download: Hokma does it."""
from fastapi import APIRouter

from hokma.library.model_download import downloader
from hokma.projects.api import TokenRoute
from hokma.projects.errors import ProjectError

router = APIRouter(route_class=TokenRoute)


@router.get("/api/library/model")
def model_status() -> dict:
    return downloader().status()


@router.post("/api/library/model/retry")
def model_retry() -> dict:
    if not downloader().retry():
        raise ProjectError("model_not_failed", status=409)
    return downloader().status()
