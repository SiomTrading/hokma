"""indicators module (SPEC-019, H3). Functions register as "indicators.<name>" in hokma.quant.registry."""
from __future__ import annotations

# Importing the submodules registers their functions.
from hokma.quant.indicators import (  # noqa: F401
    breadth, catalog, momentum, overlap, patterns, profile, screens, signal_processing, td_sequential, volatility, zigzag,
)
