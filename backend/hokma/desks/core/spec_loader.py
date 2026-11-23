"""Load a strategy or desk spec from YAML (SPEC-017: AH-27).

A spec is data. Unknown fields fail at load time, with a readable message that names the field, not later when a run
trades. A strategy needs a universe: a run with nothing to look at is a mistake in the file.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from hokma.desks.core.signal import Horizon, Line

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-=^/]{0,14}$")


class SpecError(ValueError):
    """A spec file that cannot be loaded. The message lists each problem as `field: reason`."""


class ModelSpec(BaseModel):
    """One source of views in a strategy: a persona or a code model, by registry name."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="agent or model code, e.g. A16")
    weight: float = Field(default=1.0, gt=0, description="blend weight")
    params: dict[str, Any] = Field(default_factory=dict)


class BlendPolicy(BaseModel):
    """How the views of a strategy combine into one view."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["conviction_weighted", "equal"] = "conviction_weighted"


class StrategySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str | None = None
    line: Line
    horizon: Horizon
    universe: list[str] = Field(min_length=1, description="symbols the strategy looks at")
    models: list[ModelSpec] = Field(min_length=1)
    blend: BlendPolicy = Field(default_factory=BlendPolicy)
    benchmark: str | None = None

    @field_validator("name")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not _SLUG.match(value):
            raise ValueError("must be lowercase words joined by hyphens")
        return value

    @field_validator("universe")
    @classmethod
    def _universe(cls, value: list[str]) -> list[str]:
        clean: list[str] = []
        for raw in value:
            symbol = str(raw).strip().upper()
            if not _SYMBOL.match(symbol):
                raise ValueError(f"bad symbol {raw!r}")
            if symbol not in clean:
                clean.append(symbol)
        return clean

    @field_validator("models")
    @classmethod
    def _unique_models(cls, value: list[ModelSpec]) -> list[ModelSpec]:
        names = [m.name for m in value]
        if dupes := sorted({n for n in names if names.count(n) > 1}):
            raise ValueError(f"duplicate model names: {dupes}")
        return value

    @field_validator("benchmark")
    @classmethod
    def _benchmark(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else value

    @property
    def title(self) -> str:
        return self.display_name or self.name.replace("-", " ").title()

    @property
    def model_weights(self) -> dict[str, float]:
        return {m.name: m.weight for m in self.models}


class DeskSpec(BaseModel):
    """The mandate of a desk: its strategies, the capital it may use and how often it runs."""

    model_config = ConfigDict(extra="forbid")

    name: str
    strategies: list[StrategySpec] = Field(min_length=1)
    capital: float = Field(gt=0)
    rebalance: Literal["daily", "weekly", "monthly"] = "weekly"
    benchmark: str = "SPY"

    @field_validator("benchmark")
    @classmethod
    def _benchmark(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("strategies")
    @classmethod
    def _unique_strategies(cls, value: list[StrategySpec]) -> list[StrategySpec]:
        names = [s.name for s in value]
        if dupes := sorted({n for n in names if names.count(n) > 1}):
            raise ValueError(f"duplicate strategy names: {dupes}")
        return value


def _describe(error: ValidationError) -> str:
    lines = []
    for item in error.errors():
        where = ".".join(str(p) for p in item["loc"]) or "(file)"
        kind = item["type"]
        if kind == "extra_forbidden":
            lines.append(f"{where}: unknown field")
        elif kind == "missing":
            lines.append(f"{where}: required field is missing")
        else:
            lines.append(f"{where}: {item['msg']}")
    return "; ".join(lines)


def _read(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecError(f"{path}: cannot read the file ({exc.strerror or exc})") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecError(f"{path}: not valid YAML ({exc})") from exc
    if not isinstance(data, dict):
        raise SpecError(f"{path}: the file must hold a mapping of fields")
    return data


def load_strategy(path: str | Path) -> StrategySpec:
    """Load one strategy from a YAML file."""
    data = _read(path)
    try:
        return StrategySpec(**data)
    except ValidationError as exc:
        raise SpecError(f"{path}: {_describe(exc)}") from exc


def load_spec(path: str | Path) -> DeskSpec:
    """Load the spec of a desk (its strategies, capital and cadence) from a YAML file."""
    data = _read(path)
    try:
        return DeskSpec(**data)
    except ValidationError as exc:
        raise SpecError(f"{path}: {_describe(exc)}") from exc
