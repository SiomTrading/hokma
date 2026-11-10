"""Static files of the dashboard with cache headers that survive an update.

After an update the new slot ships new hashed assets and the old ones are gone. A browser that kept the old
index.html (no Cache-Control lets it reuse the page heuristically) asks for assets that no longer exist and shows
a blank page. So:

- HTML (index.html, also when served for "/" or a folder): `no-store`, always fetched again.
- `/assets/*` (file names carry the content hash, Vite build): cached for a year, `immutable`.
- Anything else (licenses, icons): `no-cache`, revalidated on every use.
"""
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

NO_STORE = "no-store"
IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"


def cache_control_for(path: str, media_type: str | None) -> str:
    if (media_type or "").startswith("text/html"):
        return NO_STORE
    if path.replace("\\", "/").lstrip("/").startswith("assets/"):
        return IMMUTABLE
    return REVALIDATE


class DashboardFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        policy = cache_control_for(path, response.headers.get("content-type"))
        response.headers["Cache-Control"] = policy
        if policy == NO_STORE:
            response.headers["Pragma"] = "no-cache"
            # a 304 would hand back the page the browser already has; no-store must always send the body
            for header in ("etag", "last-modified"):
                if header in response.headers:
                    del response.headers[header]
        return response
