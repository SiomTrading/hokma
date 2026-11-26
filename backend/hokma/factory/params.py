"""Parameter schema and validation with errors a model can read (SPEC-020: B-03).

Every error names the parameter, the allowed range or type and the value received, in one sentence.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from hokma.quant.errors import BadInput
from hokma.quant.limits import as_int, check_param

KINDS = ("int", "float", "bool", "str", "choice")
MAX_PARAMS = 64
MAX_TEXT = 128
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_TEXT = re.compile(r"^[A-Za-z0-9_.:\- ]*$")


class ParamError(ValueError):
    """A parameter is unknown, of the wrong type, out of range or missing. `code` is stable: bad_param."""

    code = "bad_param"

    def __init__(self, name: str, message: str) -> None:
        self.name = name
        super().__init__(message)


@dataclass(frozen=True)
class Param:
    name: str
    kind: Literal["int", "float", "bool", "str", "choice"]
    default: Any = None
    low: float | None = None
    high: float | None = None
    choices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise ValueError(f"parameter name {self.name!r} must be lower case letters, digits and underscores")
        if self.kind not in KINDS:
            raise ValueError(f"parameter {self.name}: kind must be one of {', '.join(KINDS)}")
        if self.kind == "choice" and not self.choices:
            raise ValueError(f"parameter {self.name}: a choice needs its options")
        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError(f"parameter {self.name}: low is above high")
        if self.default is not None:
            object.__setattr__(self, "default", self.coerce(self.default))

    def coerce(self, value: Any) -> Any:
        """The value as the declared type, or ParamError with the sentence the model reads."""
        name = self.name
        if self.kind == "int":
            try:
                number: Any = as_int(name, value)
            except BadInput:
                raise ParamError(name, f"parameter '{name}' must be a whole number, got {value!r} ({type(value).__name__})") from None
        elif self.kind == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ParamError(name, f"parameter '{name}' must be a finite number, got {value!r} ({type(value).__name__})")
            number = float(value)
        elif self.kind == "bool":
            if not isinstance(value, bool):
                raise ParamError(name, f"parameter '{name}' must be true or false, got {value!r} ({type(value).__name__})")
            return value
        else:
            if not isinstance(value, str):
                raise ParamError(name, f"parameter '{name}' must be text, got {value!r} ({type(value).__name__})")
            if self.kind == "choice":
                if value not in self.choices:
                    raise ParamError(name, f"parameter '{name}' must be one of {', '.join(self.choices)}, got {value!r}")
            elif len(value) > MAX_TEXT or not _TEXT.match(value):
                raise ParamError(name, f"parameter '{name}' must be up to {MAX_TEXT} letters, digits, spaces or _ . : -, got {value!r}")
            return value
        if (self.low is not None and number < self.low) or (self.high is not None and number > self.high):
            low = "-inf" if self.low is None else self._fmt(self.low)
            high = "inf" if self.high is None else self._fmt(self.high)
            raise ParamError(name, f"parameter '{name}' must be between {low} and {high}, got {self._fmt(number)}")
        if self.kind == "int":
            try:
                check_param(name, number)
            except BadInput as exc:
                raise ParamError(name, f"parameter '{name}': {exc}") from None
        return number

    def _fmt(self, number: float) -> str:
        return str(int(number)) if self.kind == "int" else repr(float(number))


@dataclass(frozen=True)
class ParamSchema:
    params: tuple[Param, ...] = ()

    def __post_init__(self) -> None:
        names = [p.name for p in self.params]
        if len(set(names)) != len(names):
            raise ValueError("a parameter name appears twice in the schema")
        if len(names) > MAX_PARAMS:
            raise ValueError(f"a schema has up to {MAX_PARAMS} parameters")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.params)

    def defaults(self) -> dict[str, Any]:
        return {p.name: p.default for p in self.params if p.default is not None}


def validate(schema: ParamSchema, values: Mapping[str, Any] | None) -> dict[str, Any]:
    """The values checked against the schema, with defaults filled in. The first problem raises ParamError."""
    raw = dict(values or {})
    if len(raw) > MAX_PARAMS:
        raise ParamError("", f"at most {MAX_PARAMS} parameters are accepted, got {len(raw)}")
    known = {p.name: p for p in schema.params}
    for key in raw:
        if not isinstance(key, str) or key not in known:
            raise ParamError(str(key), f"parameter '{key}' is unknown; allowed: {', '.join(schema.names) or 'none'}")
    out: dict[str, Any] = {}
    for param in schema.params:
        if param.name in raw:
            out[param.name] = param.coerce(raw[param.name])
        elif param.default is not None:
            out[param.name] = param.default
        else:
            raise ParamError(param.name, f"parameter '{param.name}' is required")
    return out
