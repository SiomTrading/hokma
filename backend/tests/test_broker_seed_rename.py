"""1.0.9: the built-in broker is called "Hokma", not "Hokma Simulated". A base that still has the old name is renamed once
and keeps its id, its costs and its accounts; no second broker appears."""
import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.db.seeds import LEGACY_SIM_BROKER, SIM_BROKER, seed_broker


def test_a_fresh_base_has_one_broker_named_hokma(db):  # noqa: F811
    with db.write() as u:
        assert [b.name for b in u.broker.list_brokers()] == [SIM_BROKER] == ["Hokma"]


def test_the_old_name_is_renamed_without_losing_costs_or_accounts(db):  # noqa: F811
    with db.write() as u:
        broker = u.broker.find_broker(SIM_BROKER)
        u.broker.set_broker_config(broker.id, {"commission": {"type": "bps", "value": 7.0, "min": 0.0}, "slippage_bps": 9.0})
        account = u.broker.create_account(broker.id, "Mine", 5000)
        u.broker.rename_broker(broker.id, LEGACY_SIM_BROKER)
        assert u.broker.find_broker(SIM_BROKER) is None
        assert seed_broker(u.broker.s) is False
        rows = u.broker.list_brokers()
        assert [(b.id, b.name) for b in rows] == [(broker.id, "Hokma")]
        assert u.broker.get_broker(broker.id).config["slippage_bps"] == 9.0
        assert [a.id for a in u.broker.list_accounts(broker.id)] == [account.id]


def test_a_blank_name_is_refused(db):  # noqa: F811
    from hokma.db.errors import ValidationError

    with db.write() as u:
        with pytest.raises(ValidationError):
            u.broker.rename_broker(u.broker.find_broker(SIM_BROKER).id, "  ")
