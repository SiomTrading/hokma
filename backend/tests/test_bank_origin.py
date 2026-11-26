"""Where a strategy of the bank was created (SPEC-031 A2, R10): the rows of the first milestone say Hokma, a strategy created from a desk says the desk, and every desk sees the whole bank."""
import json

from bank_support import IDEA, pending_sheet
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixture)
from e2e_h4.support import caller, ok

from hokma.db import Actor
from hokma.factory import bank
from hokma.kit import hokma_tools
from hokma.kit.hokma_tools import Caller


def made_by(db, agent, task=None):
    """bank_create of a pending sheet through the entry point, as the agent (inside the project of a task when one is given)."""
    env = {"HOKMA_AGENT": agent, **({"HOKMA_TASK_ID": str(task)} if task else {})}
    call = lambda name, args: json.loads(hokma_tools.call(name, args, Caller(env))[0])  # noqa: E731
    source = call("source_ingest", {"action": "ingest", "kind": "text", "text": IDEA + (f" ({agent} {task})" if task else "")})
    pid = source["passages"][0]["passage_id"]
    return call("bank_create", {"title": f"Delta {task or 'main'}", "source_id": source["source_id"], "extraction": pending_sheet(pid)})


def test_a_strategy_created_in_the_main_chat_says_hokma_and_one_created_from_a_desk_says_the_desk_and_every_desk_sees_both(desks_db):
    desk = make_desk(desks_db)
    other = make_desk(desks_db, name="Swing")
    with desks_db.write() as u:
        task = u.tasks.create("Source", actor=Actor.system(), project_id=desk["project_id"], assigned_agent="desk-chief", status="ready").id
    main = made_by(desks_db, "hokma")
    from_desk = made_by(desks_db, "desk-chief", task)
    rows = {r["bank_id"]: r for r in bank.list_bank(db=desks_db)["rows"]}
    assert rows[main["bank_id"]]["origin"] == {"kind": "hokma"}
    assert rows[from_desk["bank_id"]]["origin"] == {"kind": "desk", "desk_id": desk["id"], "desk_name": desk["name"]}
    assert bank.get(from_desk["bank_id"], db=desks_db)["version"]["origin"]["desk_name"] == desk["name"]
    assert [r["bank_id"] for r in bank.list_bank(origin="desk", db=desks_db)["rows"]] == [from_desk["bank_id"]]
    assert [r["bank_id"] for r in bank.list_bank(origin="hokma", db=desks_db)["rows"]] == [main["bank_id"]]
    with desks_db.write() as u:
        task_other = u.tasks.create("Look", actor=Actor.system(), project_id=other["project_id"], assigned_agent="desk-chief", status="ready").id
    seen = json.loads(hokma_tools.call("bank_list", {}, Caller({"HOKMA_AGENT": "desk-chief", "HOKMA_TASK_ID": str(task_other)}))[0])
    assert {r["bank_id"] for r in seen["rows"]} == {main["bank_id"], from_desk["bank_id"]}      # a desk does not see only its own: there is one bank


def test_the_rows_of_the_first_milestone_have_a_null_origin_that_reads_as_hokma(db):
    first = ok("source_ingest", {"action": "ingest", "kind": "text", "text": IDEA}, caller("hokma"))
    pid = first["passages"][0]["passage_id"]
    made = ok("bank_create", {"title": "Delta", "source_id": first["source_id"], "extraction": pending_sheet(pid)}, caller("strategy-researcher"))
    with db.read() as u:
        row = u.session.one("SELECT origin_desk_id, origin_desk_name FROM strategy_bank WHERE id = :i", {"i": made["bank_id"]})
    assert (row["origin_desk_id"], row["origin_desk_name"]) == (None, None)
    assert bank.get(made["bank_id"], db=db)["version"]["origin"] == {"kind": "hokma"}
