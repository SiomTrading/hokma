"""indicators module (SPEC-019, H3). Functions register as "indicators.<name>" in hokma.quant.registry."""
from __future__ import annotations

# Importing the submodules registers their functions.
from hokma.quant.indicators import breadth, catalog, momentum, overlap, screens, signal_processing, volatility  # noqa: F401
