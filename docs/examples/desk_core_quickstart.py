"""Minimal programmatic use of the desk core: a two analyst graph with a fake model and fixture data.

Run:  uv run python docs/examples/desk_core_quickstart.py
It needs no network and no model: the analysts are scripted. CI runs it and it must end without an error.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> dict:
    import pandas as pd

    from hokma.core import processguard

    processguard._role = processguard.SERVER            # the example runs as the Hokma process does in the tests
    from hokma.desks.core.hashing import content_hash
    from hokma.desks.core.signal import Signal
    from hokma.quant import registry

    registry.load_all()
    returns = pd.read_csv(ROOT / "backend" / "tests" / "quant" / "fixtures" / "returns_daily.csv", index_col=0, parse_dates=True)["return"]
    sharpe = registry.call("metrics.sharpe", returns=returns)
    var = registry.call("risk.var", returns=returns, level=0.95)
    psr = registry.call("validation.psr", returns=returns)
    facts = {"sharpe": sharpe.value, "var95": var.value, "psr": psr.value}
    as_of = datetime(2026, 1, 2, tzinfo=timezone.utc)
    signal = Signal(source="A00", desk_id=1, line="SWC", symbol="TEST", as_of=as_of, horizon="weeks", value=0.4, confidence=55,
                    abstained=False, criteria=(), data_missing=(), snapshot_hash=content_hash(facts),
                    reason="The Sharpe ratio is positive on the fixture. The probabilistic Sharpe supports it moderately.")
    assert Signal.from_json(signal.to_json()) == signal
    print(json.dumps({"facts": facts, "signal": json.loads(signal.to_json())}, indent=2, default=str))
    return facts


if __name__ == "__main__":
    main()
