"""SQLite concurrency: writers queue instead of failing with "database is locked"."""
import threading
import time

from dbsupport import env  # noqa: F401  (fixture)

from hokma.db import get_database, migrate


def _setup():
    migrate()
    db = get_database()
    with db.write() as u:
        u.session.execute("CREATE TABLE IF NOT EXISTS conc_probe (id INTEGER PRIMARY KEY AUTOINCREMENT, who TEXT)")
    return db


def test_parallel_writers_on_one_file_do_not_fail_with_locked(env):
    db = _setup()
    errors = []

    def run(name):
        try:
            for _ in range(25):
                with db.write() as u:
                    u.session.execute("INSERT INTO conc_probe (who) VALUES (:w)", {"w": name})
        except Exception as exc:                                    # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(f"t{i}",)) for i in range(6)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert not errors
    with db.read() as u:
        assert u.session.scalar("SELECT count(*) FROM conc_probe") == 150


def test_long_read_does_not_block_a_writer_and_keeps_its_snapshot(env):
    db = _setup()
    with db.read() as reader:
        before = reader.session.scalar("SELECT count(*) FROM conc_probe")
        with db.write() as u:                                       # commits while the read is still open
            u.session.execute("INSERT INTO conc_probe (who) VALUES ('w')")
        assert reader.session.scalar("SELECT count(*) FROM conc_probe") == before
    with db.read() as u:
        assert u.session.scalar("SELECT count(*) FROM conc_probe") == before + 1


def test_a_writer_waits_for_the_one_holding_the_lock(env):
    db = _setup()
    done = []

    def second():
        with db.write() as u:
            u.session.execute("INSERT INTO conc_probe (who) VALUES ('second')")
        done.append(time.monotonic())

    with db.write() as u:
        u.session.execute("INSERT INTO conc_probe (who) VALUES ('first')")
        th = threading.Thread(target=second)
        th.start()
        time.sleep(0.5)                                             # the second writer is queued, not failed
        assert not done
    th.join(10)
    assert len(done) == 1
