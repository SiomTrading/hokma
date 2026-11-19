"""SimBroker (task 3294, C7-5b): accounts, balances, reset and editable costs. The shared contract runs in test_broker_contract."""
import threading

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.broker.sim import SimBroker
from hokma.db.errors import ValidationError
from hokma.db.seeds import SIM_BROKER


def sim(u):
    return SimBroker(u.broker, u.broker.find_broker(SIM_BROKER))


def test_three_accounts_and_one_reset_do_not_touch_the_others(db):  # noqa: F811
    with db.write() as u:
        b = sim(u)
        ids = [b.create_account(n, c).id for n, c in (("A", 1000), ("B", 5000), ("C", 250))]
        u.broker.s.insert("account_ledger", {"account_id": ids[1], "kind": "open", "amount": 1, "created_at": "x"})
        b.reset(ids[1])
        assert [b.balance(i).cash for i in ids] == [1000, 5000, 250]
        assert [len(u.broker.ledger(i)) for i in ids] == [1, 2, 1]


def test_reset_twice_is_the_same_as_once(db):  # noqa: F811
    with db.write() as u:
        b = sim(u)
        a = b.create_account("A", 1000)
        b.reset(a.id), b.reset(a.id)
        assert [e.kind for e in u.broker.ledger(a.id)] == ["open", "reset"]
        assert b.balance(a.id).cash == 1000


def test_two_resets_at_once(db):  # noqa: F811
    with db.write() as u:
        a = sim(u).create_account("A", 1000)
    errors = []

    def run():
        try:
            with db.write() as u:
                sim(u).reset(a.id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads], [t.join() for t in threads]
    assert not errors
    with db.read() as u:
        assert [e.kind for e in u.broker.ledger(a.id)] == ["open", "reset"]
        assert sim(u).balance(a.id).cash == 1000


def test_repeated_name_and_capital_out_of_range(db):  # noqa: F811
    with db.write() as u:
        b = sim(u)
        b.create_account("A", 1000)
        for name, capital in (("A", 2000), ("B", 99), ("B", 1_000_000_001)):
            with pytest.raises(ValidationError):
                b.create_account(name, capital)
        assert [a.name for a in b.list_accounts()] == ["A"]


def test_costs_default_to_the_seed_and_can_be_edited_keeping_the_rest(db):  # noqa: F811
    with db.write() as u:
        b = sim(u)
        assert b.commission == {"type": "bps", "value": 1.0, "min": 0.0} and b.slippage_bps == 2.0
        b.set_costs(slippage_bps=5)
        b.set_costs(commission={"type": "per_share", "value": 0.005, "min": 1})
    with db.read() as u:
        b = sim(u)
        assert b.slippage_bps == 5.0 and b.commission == {"type": "per_share", "value": 0.005, "min": 1.0}


def test_bad_costs_are_refused_and_nothing_is_saved(db):  # noqa: F811
    with db.write() as u:
        b = sim(u)
        for kw in ({"slippage_bps": -1}, {"commission": {"type": "x", "value": 1}}, {"commission": {"type": "bps", "value": "a"}}):
            with pytest.raises(ValidationError):
                b.set_costs(**kw)
        assert b.slippage_bps == 2.0 and b.commission["value"] == 1.0
