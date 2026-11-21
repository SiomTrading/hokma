"""Shared pieces of the Connections tests that need money desks: the `desks_db` fixture and a desk factory."""
import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.desks import service

SLUGS = ("desk-chief", "hugo", "risk", "iris", "signals", "axel", "vera", "bull-case", "bear-case", "judge")


@pytest.fixture
def desks_db(db):
    """The `db` fixture plus the ten agents the desk templates use, all turned on."""
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Fondo Prueba")
        for slug in SLUGS:
            u.agents.upsert_kit(slug, slug.title(), f"{slug} agent", "balanced", f"agents/{slug}.md")
            u.agents.set_status(slug, "active")
    return db


def make_desk(db, **kw):
    kw.setdefault("name", "Futuros NQ")
    kw.setdefault("capital", 10000)
    return service.create_desk(db=db, **kw)
