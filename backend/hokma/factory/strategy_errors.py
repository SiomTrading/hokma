"""The error of a strategy that does not satisfy the contract, in a module of its own.

`hokma.factory.strategy` imports the desks, which only the Hokma server may load. The indicator library and the checks of a generated strategy run in a
process that is not the server and still need this error, so it lives here and `strategy` re-exports it.
"""
from __future__ import annotations


class BadStrategy(TypeError):
    """The object does not satisfy the strategy contract. `code` is stable: bad_strategy."""

    code = "bad_strategy"
