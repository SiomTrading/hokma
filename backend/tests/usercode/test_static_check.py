"""The static check of generated code by list of permitted names (SPEC-031 section 5.2): what it accepts and every rule that rejects."""
import pytest

from hokma.factory.usercode import static_check
from hokma.factory.usercode.static_check import check
from usercode.support import alpha, source

RULES = {"import_not_allowed", "name_not_allowed", "attribute_not_allowed", "node_not_allowed", "contract", "too_large", "syntax", "bad_character"}


def rules_of(src: str, kind: str = "alpha") -> set[str]:
    result = check(src, kind=kind)
    assert result.ok == (not result.findings)
    return {f.rule for f in result.findings}


@pytest.mark.parametrize("kind", ["alpha", "filter", "exit"])
def test_a_valid_rule_of_each_kind_passes(kind):
    result = check(source(kind), kind=kind)
    assert result.ok, [(f.line, f.rule, f.message) for f in result.findings]


def test_every_finding_has_a_line_a_column_a_stable_rule_and_a_short_message():
    result = check(source(extra_import="import os"), kind="alpha")
    assert not result.ok
    for finding in result.findings:
        assert finding.rule in RULES and finding.line >= 1 and finding.col >= 0 and 0 < len(finding.message) <= 200


@pytest.mark.parametrize("line", [
    "import os", "import sys", "import socket", "import subprocess", "import importlib", "import inspect", "import operator",
    "import threading", "import codeop", "import tempfile", "import ctypes", "import builtins", "import pickle", "import numpy.ctypeslib",
    "from numpy import load", "import numpy as n", "from hokma.exec import x", "from hokma.broker import x", "from hokma.db import x",
    "from hokma.factory.strategy import Composite", "from hokma.factory.user_api import *", "from . import x", "import math as m",
    "from hokma.factory.user_api import not_exported", "import hokma.factory.user_api", "import random",
])
def test_an_import_that_is_not_in_the_list_fails(line):
    assert "import_not_allowed" in rules_of(source(extra_import=line))


def test_an_import_inside_a_function_fails():
    assert "import_not_allowed" in rules_of(alpha("import os\nreturn ctx.abstain('x')"))


@pytest.mark.parametrize("body", [
    "return open('/etc/passwd')", "return eval('1')", "exec('x = 1')", "return compile('1', 'f', 'eval')", "return __import__('os')",
    "return getattr(np, 'lo' + 'ad')", "return globals()", "return locals()", "return vars(ctx)", "return type(ctx)", "return dir(ctx)",
    "print(1)", "return input()", "return object()", "return super()", "return setattr(ctx, 'a', 1)", "return delattr(ctx, 'a')", "return memoryview(b'x')",
    "return bytearray(3)", "return breakpoint()", "return help()", "return classmethod(len)", "return property(len)", "return __builtins__",
    "return undefined_name", "return NotImplemented", "return Exception('x')", "return id(ctx)", "return hash(ctx)", "return callable(ctx)",
])
def test_a_free_name_outside_the_builtins_of_the_list_fails(body):
    assert "name_not_allowed" in rules_of(alpha(body))


@pytest.mark.parametrize("body", [
    "return ().__class__.__mro__[1].__subclasses__()", "return (1).__class__", "return ctx.__class__", "return ctx.__dict__", "return np.__dict__",
    "return fact.__globals__", "return fact.__code__", "return fact.__closure__", "return ctx._private", "x = ctx.abstain\nreturn x.__self__",
    "g = (i for i in range(3))\nreturn g.gi_frame", "g = (i for i in range(3))\nreturn g.gi_code", "return ctx.f_back", "return ctx.tb_frame",
    "return '{0.__class__}'.format(ctx)", "return 'x'.format_map({})", "return ctx.mro()",
    "return np.load('f')", "return np.loadtxt('f')", "return np.fromfile('f')", "return np.array([1]).tofile('f')", "return np.memmap('f')",
    "return np.array([1]).ctypes", "return np.lib.format", "return np.random.rand(3)", "return np.ctypeslib", "return np.testing",
    "return np.savez('f')", "return np.save('f', 1)", "return np.frombuffer(b'x')", "return np.linalg.norm([1])", "return np.core",
    "return np.array([1]).dump('f')", "return np.array([1]).dumps()", "return np.show_config()", "return np.info(1)", "return np.source(1)",
    "return math.__name__", "return math.os", "return math.notafunction", "return np.os", "return np.sys", "return np.f2py", "return np.distutils",
])
def test_an_attribute_that_escapes_or_is_not_listed_fails(body):
    assert "attribute_not_allowed" in rules_of(alpha(body))


@pytest.mark.parametrize("body", [
    "global x\nreturn ctx.abstain('x')", "f = lambda: 0\nreturn ctx.abstain('x')",
    "with ctx as c:\n    pass\nreturn ctx.abstain('x')", "try:\n    pass\nexcept ValueError:\n    pass\nreturn ctx.abstain('x')",
    "async def f():\n    pass\nreturn ctx.abstain('x')", "yield 1", "del ctx\nreturn 1", "raise ValueError('x')", "assert True\nreturn 1",
    "class Inner:\n    pass\nreturn ctx.abstain('x')", "nonlocal x\nreturn 1",
    "@np.vectorize\ndef f(x):\n    return x\nreturn ctx.abstain('x')", "return await ctx",
    "return [x async for x in ctx]", "return (yield)", "x = 1\ny = x if x else 2\nreturn f'{x:{y}}'",
])
def test_a_node_of_the_forbidden_list_fails(body):
    assert "node_not_allowed" in rules_of(alpha(body))


def test_a_raise_of_value_error_is_allowed_only_in_post_init():
    cls = source("alpha", cls='''
@dataclass(frozen=True)
class R:
    window: int = 3
    SCHEMA: ClassVar[ParamSchema] = ParamSchema((Param("window", "int", 3, 2, 50),))
    FACTS: ClassVar[tuple] = ("close",)
    LOOKBACK: ClassVar[int] = 50

    def __post_init__(self):
        if self.window < 2:
            raise ValueError("window must be at least 2")

    def requests(self):
        return ()

    def signal(self, ctx):
        return ctx.abstain("x")
''')
    assert check(cls, kind="alpha").ok


@pytest.mark.parametrize("module_extra,rule", [
    ("X = 1\nY = 'a'\nZ = (1, 2.5)", None),
    ("X = open('f')", "node_not_allowed"), ("print(1)", "node_not_allowed"), ("np.mean([1])", "node_not_allowed"), ("X = np.mean", "node_not_allowed"),
    ("for i in range(3):\n    pass", "node_not_allowed"), ("if True:\n    pass", "node_not_allowed"), ("X = [i for i in range(3)]", "node_not_allowed"),
    ("def helper(x):\n    return x + 1", None), ("def helper(x):\n    return open(x)", "name_not_allowed"),
    ("@dataclass(frozen=True)\nclass Second:\n    pass", "contract"), ("class Plain:\n    pass", "contract"),
])
def test_the_module_level_holds_only_imports_constants_helpers_and_one_class(module_extra, rule):
    found = rules_of(source(module_extra=module_extra))
    assert (rule in found) if rule else not found


def test_a_dunder_name_or_method_other_than_post_init_fails():
    for body in ("def __init__(self):\n        pass", "def __getattr__(self, name):\n        return 1", "def __reduce__(self):\n        return 1"):
        src = source("alpha", cls=source("alpha").split("    def signal")[0].split(HEAD_MARK)[1] + "    " + body + "\n    def signal(self, ctx):\n        return ctx.abstain('x')\n")
        assert rules_of(src) & {"name_not_allowed", "contract", "node_not_allowed"}
    assert "name_not_allowed" in rules_of(alpha("__x__ = 1\nreturn ctx.abstain('x')"))


HEAD_MARK = "from hokma.factory.user_api import ClassVar, IndicatorRequest, Param, ParamSchema, Pct, Price, Ticks, dataclass, fact, view\n\n\n"


def test_the_class_must_be_a_frozen_dataclass_and_there_is_exactly_one():
    base = source("alpha")
    assert "contract" in rules_of(base.replace("@dataclass(frozen=True)\n", ""))
    assert "contract" in rules_of(base.replace("@dataclass(frozen=True)", "@dataclass"))
    assert "contract" in rules_of(base.replace("@dataclass(frozen=True)", "@dataclass(frozen=False)"))
    assert "contract" in rules_of(base.replace("@dataclass(frozen=True)", "@dataclass(frozen=True, slots=True)"))
    assert "contract" in rules_of(base.split("@dataclass(frozen=True)")[0])


@pytest.mark.parametrize("kind,old,new", [
    ("alpha", "    SCHEMA: ClassVar[ParamSchema] = ParamSchema((Param(\"lookback_bars\", \"int\", 3, 2, 50),))\n", ""),
    ("alpha", "    FACTS: ClassVar[tuple] = (\"high\", \"low\", \"close\")\n", ""),
    ("alpha", "    LOOKBACK: ClassVar[int] = 50\n", ""),
    ("alpha", "LOOKBACK: ClassVar[int] = 50", "LOOKBACK: ClassVar[int] = 5000"),
    ("alpha", "LOOKBACK: ClassVar[int] = 50", "LOOKBACK: ClassVar[int] = 0"),
    ("alpha", "LOOKBACK: ClassVar[int] = 50", "LOOKBACK: ClassVar[int] = len('x')"),
    ("alpha", "FACTS: ClassVar[tuple] = (\"high\", \"low\", \"close\")", "FACTS: ClassVar[tuple] = (1, 2)"),
    ("alpha", "    def requests(self):\n        return ()\n", ""),
    ("alpha", "    def requests(self):\n        return ()\n", "    def requests(self):\n        return [1]\n"),
    ("alpha", "    def requests(self):\n        return ()\n", "    def requests(self):\n        x = 1\n        return ()\n"),
    ("alpha", "def signal(self, ctx):", "def signal(self):"),
    ("alpha", "def signal(self, ctx):", "def signal(self, ctx, extra):"),
    ("filter", "def allows(self, *, side, ctx, state):", "def allows(self, side, ctx, state):"),
    ("filter", "    def allows(self, *, side, ctx, state):", "    def allows_not(self, *, side, ctx, state):"),
    ("filter", "    FIELDS: ClassVar[tuple] = (\"high\", \"low\")\n", ""),
    ("exit", "    def levels(self, *, side, ctx):", "    def levels(self, ctx):"),
    ("exit", "    def levels(self, *, side, ctx):", "    def signal(self, ctx):"),
    ("exit", "ParamSchema((Param(\"extra_ticks\", \"int\", 1, 0, 20),))", "ParamSchema((Param(\"risk_pct\", \"float\", 1.0, 0.1, 5.0),))"),
])
def test_the_contract_of_the_kind_is_enforced(kind, old, new):
    src = source(kind)
    assert old in src, old
    assert "contract" in rules_of(src.replace(old, new), kind)


def test_a_rule_of_another_kind_does_not_satisfy_the_contract_asked():
    assert "contract" in rules_of(source("filter"), "alpha")
    assert "contract" in rules_of(source("alpha"), "exit")


@pytest.mark.parametrize("name", ["risk_pct", "contracts", "target_vol", "n"])
def test_a_parameter_that_sizes_risk_fails_in_the_schema_and_as_a_field(name):
    src = source("alpha").replace("lookback_bars", name)
    assert "contract" in rules_of(src)


def test_a_source_over_32_kb_or_600_lines_or_with_control_or_bidi_characters_fails():
    base = source("alpha")
    assert "too_large" in rules_of(base + "# " + "x" * 33000 + "\n")
    assert "too_large" in rules_of(base + "\n" * 600)
    assert "bad_character" in rules_of(base + "# a\x00b\n")
    assert "bad_character" in rules_of(base + "# a\x1bb\n")
    assert "bad_character" in rules_of(base + "# a\u202eb\n")
    assert "bad_character" in rules_of(base + "# a\u200bb\n")
    assert "bad_character" in rules_of(base.replace("\n", "\r\n"))
    assert "syntax" in rules_of("def broken(:\n")
    assert check("", kind="alpha").ok is False
    assert "too_large" in rules_of("x = 1\n" * 1 + "\u00e9" * 20000)


def test_a_non_text_source_is_refused_without_raising():
    result = check(b"x = 1", kind="alpha")      # type: ignore[arg-type]
    assert not result.ok and result.findings[0].rule == "syntax"
    assert not check(source("alpha"), kind="sizing").ok      # type: ignore[arg-type]


def test_the_allowed_names_of_numpy_and_math_are_a_fixed_list_and_each_one_exists():
    import math

    import numpy as np

    assert static_check.NUMPY_ATTRS and static_check.MATH_ATTRS
    assert not {n for n in static_check.NUMPY_ATTRS if not hasattr(np, n)}
    assert not {n for n in static_check.MATH_ATTRS if not hasattr(math, n)}
    for dangerous in ("load", "save", "fromfile", "memmap", "random", "ctypeslib", "lib", "testing", "f2py", "frombuffer", "linalg", "core", "os", "sys"):
        assert dangerous not in static_check.NUMPY_ATTRS
    assert not (static_check.NUMPY_ATTRS | static_check.MATH_ATTRS) & static_check.VETOED_ATTRS


def test_the_builtins_of_the_list_are_exactly_these():
    assert static_check.BUILTINS == frozenset({
        "abs", "all", "any", "bool", "dict", "enumerate", "float", "int", "isinstance", "len", "list", "max", "min", "range", "reversed",
        "round", "set", "sorted", "str", "sum", "tuple", "zip", "ValueError", "True", "False", "None"})


def test_a_rule_with_numpy_and_math_calls_of_the_list_passes():
    body = """\
        close = fact(ctx, "close", 5)
        if close is None:
            return ctx.abstain("short")
        a = np.diff(close)
        b = np.where(a > 0, 1.0, -1.0)
        c = float(np.nanmean(close)) + math.sqrt(abs(float(np.std(close)))) + math.floor(2.5) + np.nan * 0
        d = close.max() - close.min() + float(np.sum(b)) + close.shape[0] + len(close)
        e = [float(x) for x in a if x > 0]
        f = {k: v for k, v in zip(range(2), e)}
        s = f"{c:.2f} {d:.1f}"
        return view(ctx, "u_x", float(np.clip(c, -1, 1)), (), s)"""
    assert rules_of(alpha(body)) == set()
