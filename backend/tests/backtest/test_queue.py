"""The queue of long jobs (SPEC-020: B-27, L-47, N-37): several processes that load the data once each, the same result for one process and for
five, a failed package that does not stop the queue, a lost process that leaves the job degraded, and a job that goes on from its checkpoint."""
import os
from pathlib import Path

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.backtest import queue
from hokma.backtest.queue import JobState, QueueError
from hokma.quant import limits
from hokma.quant.errors import BadInput

PACKAGES = [{"n": n} for n in range(20)]


def lines(path: Path) -> list[str]:
    return path.read_text().split() if path.exists() else []


def make_loader(path: Path):
    def loader():
        with path.open("a") as handle:
            handle.write(f"{os.getpid()}\n")
        return {"tag": "bars"}
    return loader


def square(data, package):
    return {"n": package["n"], "square": package["n"] ** 2, "data": data["tag"]}


def values(status):
    return [r.value for r in status.results]


def submit(db, packages=PACKAGES, **kw):
    return queue.submit("optimize", packages, strategy_id="s", db=db, **kw)


def test_five_processes_load_the_data_once_each_and_give_the_result_of_one_process_B_27(db, tmp_path):
    five_loads, one_load = tmp_path / "five.txt", tmp_path / "one.txt"
    five = queue.run_job(submit(db), square, make_loader(five_loads), workers=5, db=db)
    one = queue.run_job(submit(db), square, make_loader(one_load), workers=1, db=db)
    assert five.state is JobState.DONE and (five.total, five.done, five.failed) == (20, 20, 0)
    assert len(lines(five_loads)) == 5 and len(set(lines(five_loads))) == 5          # 20 packages, 5 loads: one per process
    assert len(lines(one_load)) == 1
    assert values(five) == values(one) == [{"n": n, "square": n * n, "data": "bars"} for n in range(20)]      # in the order of the packages
    assert [r.index for r in five.results] == list(range(20))


def test_a_job_has_an_id_a_spec_hash_a_state_and_a_result_L_47(db, tmp_path):
    first, again, other = submit(db), submit(db), submit(db, [{"n": 1}])
    states = [queue.status(j, db=db) for j in (first, again, other)]
    assert [s.state for s in states] == [JobState.QUEUED] * 3 and [s.job_id for s in states] == [first, again, other]
    assert states[0].spec_hash == states[1].spec_hash != states[2].spec_hash          # the same packages are the same spec
    assert submit(db, seed=1) and queue.status(first + 3, db=db).spec_hash != states[0].spec_hash      # the seed is part of it
    assert (states[0].total, states[0].done, states[0].results) == (20, 0, ())
    done = queue.run_job(other, square, make_loader(tmp_path / "l.txt"), db=db)
    assert done.state is JobState.DONE and done.results[0].value == {"n": 1, "square": 1, "data": "bars"}
    with db.read() as u:
        row = u.session.one("SELECT kind, status, spec_hash, strategy_id, finished_at FROM backtest_runs WHERE id = :i", {"i": other})
    assert (row["kind"], row["status"], row["spec_hash"], row["strategy_id"]) == ("optimize", "done", done.spec_hash, "s") and row["finished_at"]
    assert queue.run_job(other, square, make_loader(tmp_path / "m.txt"), db=db).state is JobState.DONE            # a finished job is not run again
    assert lines(tmp_path / "m.txt") == []


def test_a_package_that_fails_does_not_stop_the_queue_B_27(db, tmp_path):
    def sometimes(data, package):
        if package["n"] in (4, 11):
            raise ValueError(f"package {package['n']} cannot be computed")
        return square(data, package)

    status = queue.run_job(submit(db), sometimes, make_loader(tmp_path / "l.txt"), workers=5, db=db)
    assert status.state is JobState.DONE and (status.done, status.failed) == (18, 2)
    bad = [r for r in status.results if r.status == "failed"]
    assert [(r.index, r.error) for r in bad] == [(4, "ValueError: package 4 cannot be computed"), (11, "ValueError: package 11 cannot be computed")]
    assert all(r.value is None for r in bad) and [r.value["n"] for r in status.results if r.status == "done"] == [n for n in range(20) if n not in (4, 11)]


def test_a_job_that_loses_a_process_is_degraded_and_finishes_N_37(db, tmp_path):
    marker = tmp_path / "died"

    def dies_once(data, package):
        if package["n"] == 3 and not marker.exists():
            marker.write_text("x")
            os._exit(1)                                   # the process of the worker is gone
        return square(data, package)

    loads = tmp_path / "l.txt"
    status = queue.run_job(submit(db), dies_once, make_loader(loads), workers=5, db=db)
    assert status.state is JobState.DEGRADED and status.workers_lost == 1 and (status.done, status.failed) == (20, 0)
    assert values(status)[3] == {"n": 3, "square": 9, "data": "bars"}              # its package went to another process
    assert len(lines(loads)) == 6                                                   # the replacement loaded the data too
    assert queue.status(status.job_id, db=db).state is JobState.DEGRADED


def test_a_package_that_kills_its_worker_twice_is_a_failed_package_N_37(db, tmp_path):
    def always_dies(data, package):
        if package["n"] == 2:
            os._exit(1)
        return square(data, package)

    status = queue.run_job(submit(db, PACKAGES[:6]), always_dies, make_loader(tmp_path / "l.txt"), workers=2, db=db)
    assert status.state is JobState.DEGRADED and status.workers_lost == 2 and (status.done, status.failed) == (5, 1)
    assert status.results[2].status == "failed" and status.results[2].error.startswith("worker_lost")


def test_a_job_that_cannot_load_its_data_is_failed_with_the_reason_N_37(db, tmp_path):
    def broken():
        raise RuntimeError("no bars for CONT:NQ between the dates")

    status = queue.run_job(submit(db), square, broken, workers=3, db=db)
    assert status.state is JobState.FAILED and status.done == 0 and status.results == ()
    assert status.reason == "data_load_failed: RuntimeError: no bars for CONT:NQ between the dates"
    with db.read() as u:
        assert u.session.one("SELECT status, error FROM backtest_runs WHERE id = :i", {"i": status.job_id})["status"] == "failed"
        assert u.session.one("SELECT status FROM run_records WHERE kind = 'backtest_job'")["status"] == "failed"


def test_a_stopped_job_goes_on_from_its_last_checkpoint_and_gives_the_same_result_B_27(db, tmp_path):
    ran, loads = tmp_path / "ran.txt", tmp_path / "l.txt"

    def tracked(data, package):
        with ran.open("a") as handle:
            handle.write(f"{package['n']}\n")
        return square(data, package)

    job = submit(db)
    half = queue.run_job(job, tracked, make_loader(loads), workers=1, budget=8, db=db)
    assert half.state is JobState.RUNNING and half.done == 8 and [r.index for r in half.results] == list(range(8))
    rest = queue.run_job(job, tracked, make_loader(loads), workers=3, db=db)
    assert rest.state is JobState.DONE and rest.done == 20
    assert sorted(int(x) for x in lines(ran)) == list(range(20))                       # no package ran twice
    straight = queue.run_job(submit(db), square, make_loader(tmp_path / "s.txt"), workers=1, db=db)
    assert values(rest) == values(straight)


def test_a_job_that_was_left_running_by_a_dead_server_goes_on_when_it_is_run_again_B_27(db, tmp_path):
    from hokma.backoffice import runs

    job = submit(db, PACKAGES[:4])
    queue.run_job(job, square, make_loader(tmp_path / "l.txt"), workers=1, budget=2, db=db)
    with db.read() as u:
        record = u.session.one("SELECT id FROM run_records WHERE kind = 'backtest_job'")["id"]
    runs.resume(record, db=db)                                          # the record says running: the process that ran it is gone
    done = queue.run_job(job, square, make_loader(tmp_path / "m.txt"), workers=2, db=db)
    assert done.state is JobState.DONE and done.done == 4


def test_the_limits_of_the_queue_are_the_ones_of_quant_B_27(db, tmp_path, monkeypatch):
    assert {"n_workers", "n_packages"} <= set(limits.PARAM_CAPS) and limits.MAX_OPEN_JOBS == 8
    job = submit(db, PACKAGES[:2])
    with pytest.raises(BadInput):
        queue.run_job(job, square, make_loader(tmp_path / "l.txt"), workers=limits.PARAM_CAPS["n_workers"] + 1, db=db)
    with pytest.raises(QueueError):
        queue.run_job(job, square, make_loader(tmp_path / "l.txt"), workers=0, db=db)
    monkeypatch.setitem(limits.PARAM_CAPS, "n_packages", 3)
    with pytest.raises(BadInput):
        submit(db, PACKAGES[:4])
    monkeypatch.undo()
    for _ in range(limits.MAX_OPEN_JOBS - 1):
        submit(db, PACKAGES[:1])
    with pytest.raises(QueueError) as error:                            # the job above and these seven are the eight open ones
        submit(db, PACKAGES[:1])
    assert error.value.code == "too_many_jobs"
    with pytest.raises(QueueError):
        queue.status(9999, db=db)
    with pytest.raises(QueueError):
        queue.submit("nope", PACKAGES[:1], db=db)
    with pytest.raises(QueueError):
        queue.submit("optimize", [], db=db)


def test_a_result_that_cannot_be_kept_is_a_failed_package_and_a_machine_without_fork_runs_in_one_process(db, tmp_path, monkeypatch):
    status = queue.run_job(submit(db, PACKAGES[:2]), lambda data, package: object(), make_loader(tmp_path / "l.txt"), workers=2, db=db)
    assert status.state is JobState.DONE and status.failed == 2 and status.results[0].error.startswith("unserializable_result")
    import multiprocessing

    monkeypatch.setattr(multiprocessing, "get_all_start_methods", lambda: ["spawn"])
    loads = tmp_path / "serial.txt"
    serial = queue.run_job(submit(db), square, make_loader(loads), workers=5, db=db)
    assert serial.state is JobState.DONE and len(lines(loads)) == 1 and values(serial) == [{"n": n, "square": n * n, "data": "bars"} for n in range(20)]
