"""Desk settings with an environment override (SPEC-017: TA-86).

Each setting has a name, a type, a default and an environment variable. The value comes from, in this order: the default,
the settings file, the environment variable. The environment always wins. A value that cannot be read as the type of its
setting (a misspelled boolean, a number that is not a number) fails at startup with a message that names the variable;
it is never replaced silently with the default, because an unattended run with a wrong setting is worse than no run.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

ENV_PREFIX = "HOKMA_DESK_"
BOOL_TRUE = ("true", "1", "yes", "on")
BOOL_FALSE = ("false", "0", "no", "off")
CONFIG_FILE = "desk-settings.json"


class ConfigError(ValueError):
    """A setting has a value that cannot be used. The message names the setting and where the value came from."""


@dataclass(frozen=True)
class Setting:
    name: str
    kind: type                         # bool, int, float or str
    default: Any
    doc: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    nullable: bool = False             # None is a valid value (the engine's own default applies)

    @property
    def env(self) -> str:
        return ENV_PREFIX + self.name.upper()


SETTINGS: tuple[Setting, ...] = (
    Setting("output_language", str, "auto", "Language of the reports the student reads; \"auto\" follows the student."),
    Setting("max_debate_rounds", int, 1, "Rounds of the bull and bear debate.", minimum=0, maximum=10),
    Setting("max_risk_rounds", int, 1, "Rounds of the risk discussion.", minimum=0, maximum=10),
    Setting("max_recursion", int, 100, "Upper bound of steps in one graph run.", minimum=1, maximum=1000),
    Setting("holding_period_days", int, 5, "Trading days after a decision over which its outcome is measured.",
            minimum=1, maximum=365),
    Setting("benchmark_symbol", str, "", "Benchmark for alpha; empty means the default of the asset class.", nullable=True),
    Setting("temperature", float, None, "Sampling temperature; None leaves the engine at its own default.",
            minimum=0.0, maximum=2.0, nullable=True),
    Setting("llm_max_retries", int, 2, "Retries of a model call that failed in a way that can pass.", minimum=0, maximum=10),
    Setting("max_tokens", int, None, "Cap on output tokens of one model call; None leaves the engine default.",
            minimum=1, maximum=1_000_000, nullable=True),
    Setting("checkpoint_enabled", bool, False, "Save the state of a run after each step so a crash can resume."),
    Setting("news_article_limit", int, 20, "Articles read per symbol.", minimum=1, maximum=200),
    Setting("global_news_article_limit", int, 10, "Articles read for the macro view.", minimum=1, maximum=200),
    Setting("global_news_lookback_days", int, 7, "Days back for the macro view.", minimum=1, maximum=90),
    Setting("memory_max_entries", int, None, "Cap on settled lessons kept in context; None keeps all.",
            minimum=1, maximum=100_000, nullable=True),
    Setting("headless_timeout_seconds", int, 900, "Time limit of an unattended desk run.", minimum=10, maximum=86_400),
)
BY_NAME: dict[str, Setting] = {s.name: s for s in SETTINGS}


def parse_bool(raw: Any, *, where: str) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in BOOL_TRUE:
        return True
    if text in BOOL_FALSE:
        return False
    raise ConfigError(f"{where}: expected a boolean ({'/'.join(BOOL_TRUE + BOOL_FALSE)}), got {raw!r}")


def _coerce(setting: Setting, raw: Any, where: str) -> Any:
    if raw is None or (isinstance(raw, str) and raw.strip() == "" and setting.kind is not str):
        if setting.nullable or setting.default is None:
            return None
        raise ConfigError(f"{where}: a value is required")
    kind = setting.kind
    try:
        if kind is bool:
            value: Any = parse_bool(raw, where=where)
        elif kind is int:
            if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
                raise ValueError
            value = int(str(raw).strip()) if isinstance(raw, str) else int(raw)
        elif kind is float:
            if isinstance(raw, bool):
                raise ValueError
            value = float(raw)
        else:
            value = str(raw).strip()
    except ConfigError:
        raise
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: expected {kind.__name__}, got {raw!r}") from None
    if kind in (int, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ConfigError(f"{where}: {raw!r} is not a finite number")
        if setting.minimum is not None and value < setting.minimum:
            raise ConfigError(f"{where}: {value} is below the minimum {setting.minimum}")
        if setting.maximum is not None and value > setting.maximum:
            raise ConfigError(f"{where}: {value} is above the maximum {setting.maximum}")
    if setting.choices and value not in setting.choices:
        raise ConfigError(f"{where}: {value!r} is not one of {', '.join(setting.choices)}")
    return value


@dataclass(frozen=True)
class DeskConfig:
    """The resolved settings plus where each value came from (`default`, `file` or `env`)."""
    values: Mapping[str, Any]
    sources: Mapping[str, str] = field(default_factory=dict)

    def __getitem__(self, name: str) -> Any:
        return self.values[name]

    def get(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.values)


def load_config(file_values: Mapping[str, Any] | None = None, env: Mapping[str, str] | None = None) -> DeskConfig:
    """Resolve the settings. `file_values` are the contents of the settings file; `env` defaults to the process environment.

    Raises ConfigError for an unknown key in the file, or for any value that cannot be read."""
    environment = os.environ if env is None else env
    file_values = dict(file_values or {})
    unknown = sorted(set(file_values) - set(BY_NAME))
    if unknown:
        raise ConfigError(f"settings file: unknown setting(s): {', '.join(unknown)}")
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for setting in SETTINGS:
        value, source = setting.default, "default"
        if setting.name in file_values:
            value, source = _coerce(setting, file_values[setting.name], f"settings file: {setting.name}"), "file"
        raw_env = environment.get(setting.env)
        if raw_env is not None and raw_env != "":
            value, source = _coerce(setting, raw_env, f"Invalid value for {setting.env}"), "env"
        values[setting.name] = value
        sources[setting.name] = source
    return DeskConfig(values, sources)


def read_settings_file(path: Path) -> dict[str, Any]:
    """The JSON object in the settings file; {} when the file does not exist. A file that is not a JSON object is an error."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"settings file {path}: {exc}") from exc
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        raise ConfigError(f"settings file {path}: not valid JSON ({exc.msg})") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"settings file {path}: expected a JSON object")
    return data


def load_default_config(env: Mapping[str, str] | None = None) -> DeskConfig:
    """Settings from `<data>/desk-settings.json` and the environment."""
    from hokma.core.config import hokma_data_dir

    return load_config(read_settings_file(hokma_data_dir() / CONFIG_FILE), env)

