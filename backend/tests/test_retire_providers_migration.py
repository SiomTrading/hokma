"""0019_retire_market_providers.sql (Hokma 1.0.8): the desk connections of Databento and Massive are removed, the
others stay, and running it again changes nothing."""
import shutil

from dbsupport import env  # noqa: F401  (fixture)
from test_db_migrate import _desk, _desk_project, raw

from hokma.db import migrate
from hokma.db.migrate import MIGRATIONS_DIR


def _upto_18(env):
    old = env / "old18"
    old.mkdir()
    for f in MIGRATIONS_DIR.glob("00*.sql"):
        if int(f.name[:4]) <= 18:
            shutil.copy(f, old / f.name)
    assert migrate(migrations_dir=old, seed=False).version == 18


def _providers(con):
    return con.execute("SELECT desk_id, provider_id, enabled FROM desk_connections ORDER BY desk_id, provider_id").fetchall()


def test_the_retired_providers_are_removed_from_every_desk_and_the_others_stay(env):
    _upto_18(env)
    con = raw()
    d1, d2 = _desk(con, _desk_project(con, "a")), _desk(con, _desk_project(con, "b"))
    for desk in (d1, d2):
        for provider, enabled in (("databento", 1), ("massive", 0), ("alpaca", 1), ("finnhub", 0)):
            con.execute("INSERT INTO desk_connections (desk_id, provider_id, enabled) VALUES (?, ?, ?)",
                        (desk, provider, enabled))
    con.commit()
    con.close()
    assert migrate().applied == [19]
    con = raw()
    assert _providers(con) == [(d1, "alpaca", 1), (d1, "finnhub", 0), (d2, "alpaca", 1), (d2, "finnhub", 0)]
    assert con.execute("SELECT count(*) FROM desks").fetchone()[0] == 2                 # nothing else is touched
    con.close()


def test_running_the_file_again_does_not_fail_and_changes_nothing(env):
    _upto_18(env)
    con = raw()
    desk = _desk(con, _desk_project(con))
    con.execute("INSERT INTO desk_connections (desk_id, provider_id) VALUES (?, 'databento')", (desk,))
    con.execute("INSERT INTO desk_connections (desk_id, provider_id) VALUES (?, 'alpaca')", (desk,))
    con.commit()
    con.close()
    assert migrate().applied == [19]
    assert migrate().applied == []                                                       # the runner does not repeat it
    con = raw()
    sql = (MIGRATIONS_DIR / "0019_retire_market_providers.sql").read_text(encoding="utf-8")
    con.executescript(sql)                                                               # and the SQL itself is idempotent
    con.executescript(sql)
    assert _providers(con) == [(desk, "alpaca", 1)]
    con.close()
