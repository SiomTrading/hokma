---
name: strategy-code-contract
version: 1.0.0
description: >
  Use when a strategy is written as code, before the first line of strategy.py. Trigger phrases: "write the strategy", "program it", "what can the code import", "how do I ask for an indicator", "how do I check the files".
identity: "A strategy programmer who knows exactly what a strategy file may contain, what it answers on each bar and how it is checked."
patterns:
  - "Write two files in strategies/<slug>/ and nothing else: test_strategy.py first, strategy.py after it."
  - "Import only math, numpy as np and the names of user_api (write from user_api import ...)."
  - "Declare the parameters in SCHEMA and as fields of one frozen dataclass; every parameter has a default."
  - "Ask for indicators in init with ctx.indicator and read them by name with ctx.series in on_bar."
  - "Answer each closed bar with long, short, flat or hold, with explicit units for a stop or a target."
  - "Check with strategy_code_save and dry_run true until it is green, then save without it."
avoid:
  - "Importing the package by its long name: the hooks of the engine refuse a file that names it."
  - "Reading anything that is not a closed bar: there is no other data."
  - "A parameter named like a sizer (sizer, sizer_n, contracts, risk_pct, n, target_vol): the size is chosen by the sizer parameter of Hokma, never by the code."
  - "A second class, a decorator, a try block, a with block, a lambda or a print."
  - "Writing a document, a note or a readme in the folder."
---
## The two files

`strategies/<slug>/test_strategy.py` holds functions named test_ with plain assert statements and no arguments. It starts with `import strategy` and `from user_api import replay` (and what else it needs from user_api).
`strategies/<slug>/strategy.py` holds imports, constants and exactly one class.

## The class

```python
from user_api import ClassVar, Param, ParamSchema, dataclass, hold, long, short


@dataclass(frozen=True)
class InsideBarBreakout:
    SCHEMA: ClassVar[ParamSchema] = ParamSchema((Param("min_range", "float", 0.0, 0.0, 100.0),))
    min_range: float = 0.0

    def init(self, ctx):
        ctx.set_warmup(4)

    def on_bar(self, ctx):
        ...
        return hold()
```

`init(self, ctx)` runs once. `ctx.indicator(name, kind, source="close", warmup=None, **params)` asks for an indicator of the library (the ones the requirement names in its inputs; the current list is what `library_list` answers with kind indicator and status ready, and anything else is refused by the check), for example `ctx.indicator("fast", "ema", length=9)`; `ctx.set_warmup(n)` waits n bars.
`on_bar(self, ctx)` runs on every closed bar after the warm-up. `ctx.open`, `ctx.high`, `ctx.low`, `ctx.close`, `ctx.volume` and `ctx.series(name)` are arrays of the closed bars: index -1 is the last one. `ctx.params`, `ctx.position_side` (long, short or None) and `ctx.index` are there too.

## The answer

`long(stop=None, target=None, reason="")`, `short(...)`, `flat(reason="")` and `hold()`. A stop or a target is `Price(x)`, `Ticks(n)`, `ATR(m)` or `Pct(p)`; `ATR` needs an indicator named atr. A long when already long does nothing; a short when long reverses. Hokma sizes the entry with the sizer that its sizer parameter names.

## Checking

`strategy_code_save` with `dry_run` true runs the static check, your tests and two fixed tests (the decision on a bar does not change when later bars are cut, and the same bars give the same decisions). Red answers with the lines that failed. Green with dry_run false keeps the code as a draft for the reviewer.

## Limits

Everything not listed here is not allowed. If the requirement needs it, stop and say what is missing.
