"""Screens over daily closed bars (SPEC-019). Importing the submodules registers "indicators.screens.<name>"."""
from __future__ import annotations

from hokma.quant.indicators.screens import (  # noqa: F401
    distribution_days,
    earnings_reaction,
    episodic_pivot,
    momentum_burst,
    pead,
    vcp,
    weekly_structure,
)
