"""Session token check shared by the routes of this package (same token as /rpc and /ws/chat)."""
from fastapi import Header, HTTPException, Query

from hokma.core.origin import token_matches
from hokma.i18n import t
from hokma.rpc.ws import SESSION_TOKEN


def token_ok(candidate: str | None) -> bool:
    """Constant-time compare on bytes, so a non-ASCII candidate is a plain mismatch and never an error."""
    return token_matches(candidate, SESSION_TOKEN)


def require_token(x_hokma_token: str = Header(default=""), token: str = Query(default="")) -> None:
    if not token_ok(x_hokma_token or token):
        raise HTTPException(status_code=401, detail=t("status.api.bad_token"))
