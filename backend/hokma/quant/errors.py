"""Errors of the quant modules."""


class BlockedFunction(Exception):
    """A library function the module forbids. `replacement` names the function that takes its place."""

    def __init__(self, name: str, replacement: str = "") -> None:
        self.name = name
        self.replacement = replacement
        text = f"{name} is blocked"
        if replacement:
            text += f"; use {replacement}"
        super().__init__(text)


class BadInput(ValueError):
    """Wrong type or shape of an input. The MCP layer maps it to the class `bad_input`."""


class UnknownFunction(KeyError):
    """The name is not in the registry: it does not exist for agents."""
