"""AH-28: live desk instances: an archived desk does not appear and two desks share no state (decision 1088)."""
import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from desks_support import desks_db, make_desk  # noqa: F401  (fixture)

from hokma.desks import service
from hokma.desks.core import fund


class Statuses:
    def __init__(self, **by_id):
        self.by_id = {int(k[1:]): v for k, v in by_id.items()}

    def __call__(self, desk_id):
        return self.by_id.get(desk_id)


def test_two_desks_do_not_share_state_or_caches_AH_28():
    registry = fund.Fund(Statuses(d1="active", d2="active"))
    one, two = registry.instance(1), registry.instance(2)
    one.state["position"] = "long"
    one.cache("prices")["AAPL"] = 189.5
    assert one is not two and two.state == {} and two.cache("prices") == {} and two.caches is not one.caches


def test_the_same_desk_keeps_its_instance_and_caches_between_turns_AH_28():
    registry = fund.Fund(Statuses(d1="active"))
    first = registry.instance(1)
    first.cache("news")["x"] = 1
    assert registry.instance(1) is first and registry.instance(1).cache("news") == {"x": 1}


def test_an_archived_desk_does_not_appear_and_loses_its_instance_AH_28():
    status = {"d1": "active", "d2": "active"}
    states = Statuses(**status)
    registry = fund.Fund(states)
    registry.instance(1)
    two = registry.instance(2)
    two.cache("c")["k"] = 1
    states.by_id[2] = "archived"
    assert [i.desk_id for i in registry.live()] == [1]
    assert two.closed and two.caches == {}
    with pytest.raises(fund.DeskNotLive, match="archived"):
        registry.instance(2)


def test_a_paused_desk_is_still_live_and_a_missing_desk_is_not_AH_28():
    registry = fund.Fund(Statuses(d1="paused"))
    assert registry.instance(1).desk_id == 1
    with pytest.raises(fund.DeskNotLive, match="does not exist"):
        registry.instance(99)


def test_a_desk_that_comes_back_gets_a_fresh_instance_AH_28():
    states = Statuses(d1="active")
    registry = fund.Fund(states)
    first = registry.instance(1)
    first.state["old"] = True
    states.by_id[1] = "archived"
    registry.live()
    states.by_id[1] = "active"
    assert registry.instance(1) is not first and registry.instance(1).state == {}


def test_release_drops_an_instance_AH_28():
    registry = fund.Fund(Statuses(d1="active"))
    registry.instance(1)
    assert registry.release(1) is True and registry.release(1) is False and registry.live() == []


def test_with_the_database_an_archived_desk_is_not_live_AH_28(desks_db):
    a, b = make_desk(desks_db, name="A"), make_desk(desks_db, name="B")
    registry = fund.Fund()
    ia, ib = registry.instance(a["id"]), registry.instance(b["id"])
    ia.state["x"] = 1
    assert ib.state == {} and [i.desk_id for i in registry.live()] == sorted([a["id"], b["id"]])
    service.archive_desk(b["id"], db=desks_db)
    assert [i.desk_id for i in registry.live()] == [a["id"]]
    with pytest.raises(fund.DeskNotLive):
        registry.instance(b["id"])
