"""Helpers that touch the database and a file together."""
from __future__ import annotations

import os
import tempfile

from hokma.core.config import hokma_data_dir
from hokma.db.database import Database
from hokma.db.errors import ValidationError
from hokma.db.models import Profile


def save_owner(db: Database, owner_name: str, fund_name: str | None) -> Profile:
    """Store the names in the profile and in `<data>/owner.json` (the file the kit text is filled from)."""
    from hokma.kit import owner

    with db.write() as u:
        profile = u.profile.set_owner(owner_name, fund_name)
    owner.save(profile.owner_name, profile.fund_name or "")
    return profile


def set_language(db: Database, language: str) -> Profile:
    """Store the language in the profile and in `<data>/language` (what the message catalogs read)."""
    if language not in ("es", "en"):
        raise ValidationError("language must be es or en")
    with db.write() as u:
        profile = u.profile.save(language=language)
    target = hokma_data_dir() / "language"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".language-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(language + "\n")
    os.replace(tmp, target)
    return profile
