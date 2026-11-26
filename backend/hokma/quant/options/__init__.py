"""options module (SPEC-019, H3). Functions register as "options.<name>" in hokma.quant.registry."""
from __future__ import annotations

# Importing the submodules registers their functions.
from hokma.quant.options import (  # noqa: F401
    assignment, chain, detail, exotic, greeks, iv, liquidity, margin, payoff, position_greeks, pricing, strategies, structures, surface, trees,
    vol_analysis,
)
