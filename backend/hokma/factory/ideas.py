"""The idea library as data (SPEC-020: the files of kit/template/service/strategies/ideas/).

An idea file says what a family of strategies is, for which lines, what it needs and how to try it: recipes (variants) that name an alpha of the
registry with its parameters, or an alpha that is planned and not built yet. This loader reads the folder at run time for `factory_list` and
`factory_describe`, and for Ian. It is strict: unknown keys, a wrong type, an alpha that does not exist or parameters its schema refuses all fail
with the file named. The folder is the one of the kit; no agent chooses a path.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from hokma.desks.core.signal import LINES
from hokma.factory.params import ParamError, validate
from hokma.factory.registry import Registry

MAX_FILE_BYTES = 64 * 1024
MAX_FILES = 64
REQUIRED = frozenset({"id", "title", "ref", "lines", "asset_classes", "requires_premium_switch", "requires_event_study", "summary", "needs", "data",
                      "variants"})
OPTIONAL = frozenset({"context_only", "study_only"})
VARIANT_KEYS = frozenset({"line", "sleeve", "vehicle", "alpha", "planned_alpha", "params", "rebalance", "rules", "costs"})
VARIANT_REQUIRED = frozenset({"line", "vehicle", "rebalance", "rules"})
REBALANCE = ("daily", "weekly", "monthly", "quarterly", "yearly")
_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_REF = re.compile(r"^idea-\d{3}$")
_PLANNED = re.compile(r"^[a-z][a-z0-9_]{1,47}$")


class IdeaError(ValueError):
    """An idea file cannot be used. `code` is stable: bad_idea. The message starts with the file name."""

    code = "bad_idea"

    def __init__(self, file: str, message: str) -> None:
        self.file = file
        super().__init__(f"{file}: {message}")


@dataclass(frozen=True)
class Variant:
    line: str
    vehicle: str
    rebalance: str
    rules: tuple[str, ...]
    sleeve: str | None = None
    alpha: str | None = None
    planned_alpha: str | None = None
    params: Mapping[str, Any] | None = None
    costs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Idea:
    id: str
    title: str
    ref: str
    lines: tuple[str, ...]
    asset_classes: tuple[str, ...]
    requires_premium_switch: bool
    requires_event_study: bool
    summary: str
    needs: tuple[str, ...]
    data: tuple[str, ...]
    variants: tuple[Variant, ...]
    context_only: bool = False
    study_only: tuple[str, ...] = ()
    source: str = ""

    def summary_row(self) -> dict[str, Any]:
        """What `factory_list` shows: enough to choose, not the whole recipe."""
        return {"id": self.id, "kind": "idea", "title": self.title, "lines": list(self.lines), "asset_classes": list(self.asset_classes),
                "requires_premium_switch": self.requires_premium_switch, "requires_event_study": self.requires_event_study,
                "context_only": self.context_only, "variants": len(self.variants), "source": self.source}

    def to_dict(self) -> dict[str, Any]:
        """What `factory_describe` shows: the whole idea as plain data."""
        rows = [{k: (list(v) if isinstance(v, tuple) else dict(v) if isinstance(v, Mapping) else v) for k, v in vars(variant).items() if v not in (None, ())}
                for variant in self.variants]
        return {**self.summary_row(), "summary": self.summary, "needs": list(self.needs), "data": list(self.data), "study_only": list(self.study_only),
                "variant_list": rows}


def _texts(value: Any, file: str, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise IdeaError(file, f"{key} is a list of texts")
    return tuple(value)


def _variant(raw: Any, lines: Sequence[str], registry: Registry | None, file: str) -> Variant:
    if not isinstance(raw, Mapping) or not VARIANT_REQUIRED <= set(raw) <= VARIANT_KEYS:
        raise IdeaError(file, f"a variant has the keys {sorted(VARIANT_REQUIRED)} and optionally {sorted(VARIANT_KEYS - VARIANT_REQUIRED)}")
    if raw["line"] not in lines:
        raise IdeaError(file, f"a variant line is one of the lines of the idea: {', '.join(lines)}")
    if raw["rebalance"] not in REBALANCE:
        raise IdeaError(file, f"rebalance is one of {', '.join(REBALANCE)}")
    rules = _texts(raw["rules"], file, "rules")
    if not rules or not isinstance(raw["vehicle"], str):
        raise IdeaError(file, "a variant has a vehicle and at least one rule")
    if "alpha" in raw and "planned_alpha" in raw:
        raise IdeaError(file, "a variant names an alpha or a planned alpha, not both")
    if "params" in raw and "alpha" not in raw:
        raise IdeaError(file, "parameters need an alpha that exists")
    alpha, planned = raw.get("alpha"), raw.get("planned_alpha")
    if alpha is not None and registry is not None:
        try:
            entry = registry.get(alpha)
            validate(entry.cls.SCHEMA, {**entry.params, **(raw.get("params") or {})})
        except KeyError:
            raise IdeaError(file, f"the alpha {alpha!r} is not in the registry") from None
        except ParamError as exc:
            raise IdeaError(file, str(exc)) from None
    if planned is not None and (not isinstance(planned, str) or not _PLANNED.match(planned)):
        raise IdeaError(file, "planned_alpha is a lower case name")
    return Variant(raw["line"], raw["vehicle"], raw["rebalance"], rules, raw.get("sleeve"), alpha, planned, raw.get("params"),
                   _texts(raw.get("costs", []), file, "costs"))


def parse_idea(text: str, file: str = "<idea>", registry: Registry | None = None) -> Idea:
    """One idea from its YAML text. With a `registry`, the alphas that variants name must exist and accept their parameters."""
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise IdeaError(file, f"the file is larger than {MAX_FILE_BYTES} bytes")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        raise IdeaError(file, "not valid YAML") from None
    if not isinstance(data, Mapping) or not REQUIRED <= set(data) <= REQUIRED | OPTIONAL:
        raise IdeaError(file, f"an idea has the keys {sorted(REQUIRED)} and optionally {sorted(OPTIONAL)}")
    if not isinstance(data["id"], str) or not _ID.match(data["id"]):
        raise IdeaError(file, "id is lower case letters, digits and underscores")
    if not isinstance(data["ref"], str) or not _REF.match(data["ref"]):
        raise IdeaError(file, "ref looks like idea-001")
    lines = _texts(data["lines"], file, "lines")
    if not lines or not set(lines) <= set(LINES):
        raise IdeaError(file, f"lines are some of {', '.join(LINES)}")
    for key in ("requires_premium_switch", "requires_event_study"):
        if not isinstance(data[key], bool):
            raise IdeaError(file, f"{key} is true or false")
    for key in ("title", "summary"):
        if not isinstance(data[key], str) or not 10 < len(data[key]) < 1200:
            raise IdeaError(file, f"{key} is text of 11 to 1199 characters")
    if not data["variants"] or not isinstance(data["variants"], list):
        raise IdeaError(file, "an idea has at least one variant")
    variants = tuple(_variant(v, lines, registry, file) for v in data["variants"])
    if {v.line for v in variants} != set(lines):
        raise IdeaError(file, "every declared line has a variant")
    return Idea(data["id"], data["title"], data["ref"], lines, _texts(data["asset_classes"], file, "asset_classes"), data["requires_premium_switch"],
                data["requires_event_study"], data["summary"].strip(), _texts(data["needs"], file, "needs"), _texts(data["data"], file, "data"),
                variants, bool(data.get("context_only", False)), _texts(data.get("study_only", []), file, "study_only"), file)


def default_folder() -> Path:
    from hokma.kit.integrity import app_root

    return app_root() / "kit" / "template" / "service" / "strategies" / "ideas"


def load_ideas(folder: str | Path | None = None, registry: Registry | None = None) -> tuple[Idea, ...]:
    """Every *.yaml file of the folder, in name order. Two files with one id are an error."""
    paths = sorted(Path(folder or default_folder()).glob("*.yaml"))
    if len(paths) > MAX_FILES:
        raise IdeaError(str(folder), f"at most {MAX_FILES} idea files")
    ideas: list[Idea] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise IdeaError(path.name, f"cannot read the file ({exc.strerror or exc})") from exc
        ideas.append(parse_idea(text, path.name, registry))
    ids = [i.id for i in ideas]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise IdeaError("ideas", f"repeated id(s): {dupes}")
    return tuple(ideas)


def get_idea(idea_id: str, folder: str | Path | None = None, registry: Registry | None = None) -> Idea:
    """One idea by id. `KeyError` when there is none."""
    for idea in load_ideas(folder, registry):
        if idea.id == idea_id:
            return idea
    raise KeyError(f"no idea with id {idea_id!r}")


def idea_rows(folder: str | Path | None = None, *, line: str | None = None, registry: Registry | None = None) -> list[dict[str, Any]]:
    """The ideas as the rows of `factory_list`, sorted by id, optionally the ones that have a recipe for `line`."""
    if line is not None and line not in LINES:
        raise ValueError(f"line is one of {', '.join(LINES)}, got {line!r}")
    return sorted((i.summary_row() for i in load_ideas(folder, registry) if line is None or line in i.lines), key=lambda r: r["id"])
