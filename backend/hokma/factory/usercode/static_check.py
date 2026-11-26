"""Static check of generated code by a LIST OF PERMITTED names (SPEC-031 section 5.2, decision T6).

What is not on a list is out: a list of forbidden things always has a residue, a list of permitted things has none by construction. The check runs twice: in the parent before anything is stored, and again in
the isolated process before the code is compiled (the process does not trust its parent). It is the first layer and never the only one: section 6 of the spec
assumes it can be fooled, and the adversarial tests skip it on purpose to prove the layers that run behind it.

Standard library plus hokma.factory.user_api (for the list of names a rule may import). It must load inside the isolated process.
"""
from __future__ import annotations

import ast
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

from hokma.factory import user_api

MAX_BYTES = 32 * 1024
MAX_LINES = 600
MAX_FINDINGS = 100
USER_FILENAME = "<user_rule>"      # the worker lets CPython open this name (it does that to read the line of a SyntaxError) and nothing else
KINDS = ("alpha", "filter", "exit")
STRATEGY_KINDS = ("strategy", "test")     # a whole strategy and the test file that comes with it (SPEC-031 A2)
ALL_KINDS = KINDS + STRATEGY_KINDS
LOOKBACK_MAX = 2000

# The free names a rule may use. `str` is added to the list of the spec: it is the annotation of a text parameter and the argument of isinstance.
BUILTINS = frozenset({"abs", "all", "any", "bool", "dict", "enumerate", "float", "int", "isinstance", "len", "list", "max", "min", "range", "reversed",
                      "round", "set", "sorted", "str", "sum", "tuple", "zip", "ValueError", "True", "False", "None"})

# The first attribute of np and of math that a rule may touch. A test checks that each one exists and that the worker runs each one under the audit hook.
NUMPY_ATTRS = frozenset({
    "abs", "sign", "log", "exp", "sqrt", "cumsum", "cumprod", "diff", "mean", "std", "var", "max", "min", "argmax", "argmin", "sum", "prod", "median",
    "nanmean", "nanstd", "nansum", "nanmax", "nanmin", "isfinite", "isnan", "isinf", "sort", "argsort", "floor", "ceil", "square", "log1p", "zeros_like",
    "ones_like", "asarray", "array", "all", "any", "roll", "clip", "where", "maximum", "minimum", "concatenate", "convolve", "percentile", "quantile",
    "full", "zeros", "ones", "arange", "power", "float64", "int64", "linspace", "nan", "inf"})
MATH_ATTRS = frozenset({"sqrt", "log", "exp", "floor", "ceil", "fabs", "fsum", "hypot", "log10", "log2", "pow", "copysign", "trunc", "tanh", "pi", "e",
                        "isfinite"})

# Attributes that are never allowed on any object (frame, code and file reaching names, formatting that reads attributes, numpy input and output).
VETOED_ATTRS = frozenset({
    "gi_frame", "gi_code", "gi_yieldfrom", "cr_frame", "cr_code", "cr_await", "ag_frame", "ag_code", "f_back", "f_globals", "f_locals", "f_builtins",
    "f_code", "tb_frame", "tb_next", "co_code", "format", "format_map", "mro", "ctypes", "tofile", "dump", "dumps", "load", "loads", "save", "savez",
    "savez_compressed", "savetxt", "loadtxt", "genfromtxt", "fromfile", "fromregex", "memmap", "lib", "ctypeslib", "f2py", "testing", "distutils",
    "DataSource", "frombuffer", "from_dlpack", "show_config", "info", "source", "cell_contents", "pidfd_open", "pidfd_send_signal",
    "base", "flags", "setflags"})   # base reaches the whole array behind a revealed view (the future bars); flags can make a read only view writable

# A rule does not size a position: no parameter or field may be named like the parameters of the sizers of the factory (SPEC-031 5.2 point 7).
SIZING_NAMES = frozenset({"risk_pct", "contracts", "target_vol", "n"})
ALLOWED_FROM_USER_API = frozenset(user_api.__all__)
UNITS = frozenset({"Price", "Ticks", "ATR", "Pct"})

CONTRACT: dict[str, dict[str, Any]] = {
    "alpha": {"attrs": ("SCHEMA", "FACTS", "LOOKBACK"),
              "methods": {"requests": ((), True), "signal": (("ctx",), True)}},
    "filter": {"attrs": ("SCHEMA", "FIELDS", "LOOKBACK"),
               "methods": {"requests": ((), True), "allows": ((), True), "must_flatten": ((), False)}},
    "exit": {"attrs": ("SCHEMA", "FIELDS", "LOOKBACK"),
             "methods": {"requests": ((), True), "levels": ((), True), "manage": ((), False)}},
    "strategy": {"attrs": ("SCHEMA",), "methods": {"init": ((), True), "on_bar": ((), True)}},
}
KWONLY = {"allows": ("side", "ctx", "state"), "must_flatten": ("ctx",), "levels": ("side", "ctx"), "manage": ("side", "ctx", "state")}
POSITIONAL = {"requests": ("self",), "signal": ("self", "ctx"), "init": ("self", "ctx"), "on_bar": ("self", "ctx"), "__post_init__": ("self",)}

ALLOWED_NODES: tuple[type, ...] = (
    ast.Module, ast.Expr, ast.Constant, ast.Name, ast.Load, ast.Store, ast.Attribute, ast.Call, ast.keyword, ast.BinOp, ast.UnaryOp, ast.BoolOp,
    ast.Compare, ast.IfExp, ast.Subscript, ast.Slice, ast.Tuple, ast.List, ast.Set, ast.Dict, ast.ListComp, ast.SetComp, ast.DictComp,
    ast.GeneratorExp, ast.comprehension, ast.JoinedStr, ast.FormattedValue, ast.Starred, ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Return,
    ast.If, ast.For, ast.While, ast.Break, ast.Continue, ast.Pass, ast.FunctionDef, ast.arguments, ast.arg, ast.ClassDef, ast.Import,
    ast.ImportFrom, ast.alias, ast.Raise,
    ast.And, ast.Or, ast.Add, ast.Sub, ast.Mult, ast.MatMult, ast.Div, ast.Mod, ast.Pow, ast.LShift, ast.RShift, ast.BitOr, ast.BitXor, ast.BitAnd,
    ast.FloorDiv, ast.Invert, ast.Not, ast.UAdd, ast.USub, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot, ast.In, ast.NotIn)

Rule = Literal["import_not_allowed", "name_not_allowed", "attribute_not_allowed", "node_not_allowed", "contract", "too_large", "syntax", "bad_character"]


@dataclass(frozen=True)
class Finding:
    line: int
    col: int
    rule: str
    message: str

    def to_data(self) -> dict[str, Any]:
        return {"line": self.line, "col": self.col, "rule": self.rule, "message": self.message}


@dataclass
class CheckResult:
    ok: bool
    findings: list[Finding] = field(default_factory=list)


class _Stop(Exception):
    pass


class _Checker:
    engine = False

    def __init__(self, tree: ast.Module, kind: str) -> None:
        self.tree, self.kind = tree, kind
        self.findings: list[Finding] = []
        self._seen: set[tuple[int, int, str]] = set()
        self.defined: set[str] = set()
        self.functions: set[str] = set()
        self._collect_defined()

    # ---- helpers
    def add(self, node: ast.AST | None, rule: str, message: str) -> None:
        line, col = getattr(node, "lineno", 1), getattr(node, "col_offset", 0)
        key = (line, col, rule)
        if key in self._seen:
            return
        self._seen.add(key)
        self.findings.append(Finding(line, col, rule, message[:200]))
        if len(self.findings) >= MAX_FINDINGS:
            raise _Stop

    def _collect_defined(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                self.defined.add(node.id)
            elif isinstance(node, ast.arg):
                self.defined.add(node.arg)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.defined.add(node.name)
                if isinstance(node, ast.FunctionDef):
                    self.functions.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    self.defined.add(alias.asname or alias.name.split(".")[0])

    # ---- entry
    def run(self) -> list[Finding]:
        try:
            self.module()
            self.walk_all()
        except _Stop:
            pass
        except RecursionError:
            self.add(self.tree, "syntax", "the code is nested too deeply to be checked")
        return self.findings

    # ---- the module level (5.2 point 3)
    def module(self) -> None:
        classes = []
        for index, stmt in enumerate(self.tree.body):
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str) and index == 0:
                continue
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                self.import_statement(stmt)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                if not self.is_constant_assignment(stmt):
                    self.add(stmt, "node_not_allowed", "at module level only constants (numbers, texts, tuples of numbers) can be assigned")
            elif isinstance(stmt, ast.FunctionDef):
                if stmt.decorator_list:
                    self.add(stmt, "node_not_allowed", "a helper function has no decorators")
                a = stmt.args
                if self.kind == "test" and stmt.name.startswith("test_") and (a.args or a.posonlyargs or a.kwonlyargs or a.vararg or a.kwarg):
                    self.add(stmt, "contract", "a test function takes no arguments")
            elif isinstance(stmt, ast.ClassDef):
                classes.append(stmt)
            else:
                self.add(stmt, "node_not_allowed", f"{type(stmt).__name__} cannot run when the module loads: only imports, constants, helper functions and one class")
        if self.kind == "test":
            for cls in classes:
                self.add(cls, "contract", "a test file defines no class")
            if not any(isinstance(s, ast.FunctionDef) and s.name.startswith("test_") for s in self.tree.body):
                self.add(self.tree, "contract", "the test file needs at least one function named test_...")
            return
        if len(classes) != 1:
            self.add(classes[1] if len(classes) > 1 else self.tree, "contract", f"the code must define exactly one class, found {len(classes)}")
        if classes:
            self.the_class(classes[0])

    def import_statement(self, node: ast.Import | ast.ImportFrom) -> None:
        if isinstance(node, ast.Import):
            ok = len(node.names) == 1 and ((node.names[0].name == "math" and node.names[0].asname is None)
                                           or (node.names[0].name == "numpy" and node.names[0].asname == "np")
                                           or (self.kind == "test" and node.names[0].name == "strategy" and node.names[0].asname is None))
            if not ok:
                self.add(node, "import_not_allowed", "only 'import math' and 'import numpy as np' are allowed")
            return
        if node.level:
            self.add(node, "import_not_allowed", "relative imports are not allowed")
        elif node.module == "__future__":
            if [a.name for a in node.names] != ["annotations"]:
                self.add(node, "import_not_allowed", "from __future__ allows only 'annotations'")
        elif node.module in ("hokma.factory.user_api", "user_api"):          # the short spelling is the one an agent can write: the hooks refuse a file that names the package
            for alias in node.names:
                if alias.name not in ALLOWED_FROM_USER_API:
                    self.add(node, "import_not_allowed", f"{alias.name!r} is not a public name of hokma.factory.user_api")
                if alias.asname:
                    self.add(node, "import_not_allowed", "names of user_api are imported without an alias")
        elif self.kind == "test" and node.module == "strategy":
            for alias in node.names:
                if alias.asname:
                    self.add(node, "import_not_allowed", "names of the strategy are imported without an alias")
        else:
            self.add(node, "import_not_allowed", f"module {node.module!r} is not on the list: math, numpy as np, user_api")

    @staticmethod
    def is_constant_assignment(stmt: ast.Assign | ast.AnnAssign) -> bool:
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        if not all(isinstance(t, ast.Name) and not t.id.startswith("__") for t in targets):
            return False
        return stmt.value is not None and _is_plain_constant(stmt.value)

    # ---- the class (5.2 point 7)
    def the_class(self, cls: ast.ClassDef) -> None:
        decorators = cls.decorator_list
        good = (len(decorators) == 1 and isinstance(decorators[0], ast.Call) and isinstance(decorators[0].func, ast.Name)
                and decorators[0].func.id == "dataclass" and not decorators[0].args and len(decorators[0].keywords) == 1
                and decorators[0].keywords[0].arg == "frozen" and isinstance(decorators[0].keywords[0].value, ast.Constant)
                and decorators[0].keywords[0].value.value is True)
        if not good:
            self.add(cls, "contract", "the class must be decorated exactly with @dataclass(frozen=True)")
        if cls.bases or cls.keywords:
            self.add(cls, "contract", "the class has no base classes")
        spec = CONTRACT.get(self.kind)
        if spec is None:
            self.add(cls, "contract", f"kind must be one of {', '.join(KINDS)}")
            return
        seen_attrs: dict[str, ast.AST] = {}
        methods: dict[str, ast.FunctionDef] = {}
        for index, stmt in enumerate(cls.body):
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str) and index == 0:
                continue
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                self.class_attribute(stmt, seen_attrs)
            elif isinstance(stmt, ast.FunctionDef):
                methods[stmt.name] = stmt
                if stmt.decorator_list:
                    self.add(stmt, "node_not_allowed", "a method has no decorators")
            else:
                self.add(stmt, "node_not_allowed", f"{type(stmt).__name__} is not allowed in the body of the class")
        for name in spec["attrs"]:
            if name not in seen_attrs:
                self.add(cls, "contract", f"the class must define {name}")
        self.check_attributes(seen_attrs)
        wanted = spec["methods"]
        for name, (_args, required) in wanted.items():
            if required and name not in methods:
                self.add(cls, "contract", f"the class must define the method {name}")
        for name, fn in methods.items():
            if name in wanted or name == "__post_init__":
                self.signature(fn)
            elif name.startswith("_") and not name.startswith("__"):
                continue
            else:
                self.add(fn, "contract", f"method {name!r} is not part of the contract of a {self.kind} rule")
        if "requests" in methods:
            self.requests_body(methods["requests"])
        if "__post_init__" in methods:
            self.post_init_body(methods["__post_init__"])

    def class_attribute(self, stmt: ast.Assign | ast.AnnAssign, seen: dict[str, ast.AST]) -> None:
        if isinstance(stmt, ast.Assign):
            if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
                self.add(stmt, "node_not_allowed", "one name is assigned at a time in the class")
                return
            name, value, annotation = stmt.targets[0].id, stmt.value, None
        else:
            if not isinstance(stmt.target, ast.Name):
                self.add(stmt, "node_not_allowed", "a class attribute is a plain name")
                return
            name, value, annotation = stmt.target.id, stmt.value, stmt.annotation
        if name.startswith("_"):
            self.add(stmt, "name_not_allowed", f"the attribute {name!r} starts with an underscore")
            return
        seen[name] = stmt
        if value is not None and not _is_static_expression(value):
            self.add(stmt, "contract", f"the value of {name} must be a literal (numbers, texts, tuples, Param, ParamSchema, IndicatorRequest)")
        if annotation is not None:
            for node in ast.walk(annotation):
                if isinstance(node, ast.Name) and node.id not in BUILTINS and node.id not in ("ClassVar", "ParamSchema", "Param"):
                    self.add(node, "name_not_allowed", f"{node.id!r} cannot appear in an annotation")

    def check_attributes(self, attrs: dict[str, ast.AST]) -> None:
        for name, stmt in attrs.items():
            value = stmt.value
            if name == "LOOKBACK" and value is not None:
                if not (isinstance(value, ast.Constant) and isinstance(value.value, int) and not isinstance(value.value, bool)
                        and 1 <= value.value <= LOOKBACK_MAX):
                    self.add(stmt, "contract", f"LOOKBACK must be a whole number literal from 1 to {LOOKBACK_MAX}")
            elif name in ("FACTS", "FIELDS") and value is not None:
                if not (isinstance(value, ast.Tuple) and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts)):
                    self.add(stmt, "contract", f"{name} must be a tuple of text literals")
            elif name == "SCHEMA" and value is not None:
                self.check_schema(stmt, value)
            # a field of the dataclass (annotated, not a ClassVar) is a parameter of the rule: it cannot size risk either
            if isinstance(stmt, ast.AnnAssign) and not _is_classvar(stmt.annotation) and (name in SIZING_NAMES or (self.kind == "strategy" and name.startswith("sizer"))):
                self.add(stmt, "contract", f"{name!r} is the name of a sizing parameter of the factory: a rule does not size a position")

    def check_schema(self, stmt: ast.AST, value: ast.expr) -> None:
        if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "ParamSchema"):
            self.add(stmt, "contract", "SCHEMA must be a ParamSchema(...) literal")
            return
        for call in [n for n in ast.walk(value) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "Param"]:
            first = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "name"), None)
            if isinstance(first, ast.Constant) and (first.value in SIZING_NAMES or (self.kind == "strategy" and isinstance(first.value, str) and first.value.startswith("sizer"))):
                self.add(call, "contract", f"{first.value!r} is the name of a sizing parameter of the factory: a rule does not size a position")

    def signature(self, fn: ast.FunctionDef) -> None:
        args = fn.args
        shape = ([a.arg for a in args.posonlyargs + args.args], [a.arg for a in args.kwonlyargs])
        extra = args.vararg or args.kwarg or args.defaults or any(d is not None for d in args.kw_defaults) or args.posonlyargs
        positional = POSITIONAL.get(fn.name, ("self",))
        keyword = KWONLY.get(fn.name, ())
        if extra or tuple(shape[0]) != positional or tuple(shape[1]) != keyword:
            sig = ", ".join([*positional, *(["*", *keyword] if keyword else [])])
            self.add(fn, "contract", f"{fn.name} must be declared as def {fn.name}({sig})")

    def requests_body(self, fn: ast.FunctionDef) -> None:
        body = [s for i, s in enumerate(fn.body) if not (i == 0 and isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        ok = (len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Tuple)
              and all(isinstance(e, ast.Call) and isinstance(e.func, ast.Name) and e.func.id == "IndicatorRequest" for e in body[0].value.elts))
        if not ok:
            self.add(fn, "contract", "requests must be a single 'return (...)' with a tuple of IndicatorRequest(...) calls, or ()")

    def post_init_body(self, fn: ast.FunctionDef) -> None:
        for node in ast.walk(fn):
            if isinstance(node, ast.Raise) and not (isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name)
                                                    and node.exc.func.id == "ValueError" and node.cause is None):
                self.add(node, "node_not_allowed", "__post_init__ may only raise ValueError(...)")

    # ---- every node (5.2 points 4, 5 and 6)
    def walk_all(self) -> None:
        self.visit(self.tree, in_post_init=False)

    def visit(self, node: ast.AST, *, in_post_init: bool) -> None:
        if not (isinstance(node, ALLOWED_NODES) or (self.kind == "test" and isinstance(node, ast.Assert))):
            self.add(node, "node_not_allowed", f"{type(node).__name__} is not allowed in a rule")
        post = in_post_init or (isinstance(node, ast.FunctionDef) and node.name == "__post_init__")
        if isinstance(node, ast.Raise) and not post and not self.engine:
            self.add(node, "node_not_allowed", "raise is allowed only as ValueError(...) in __post_init__")
        elif isinstance(node, (ast.Import, ast.ImportFrom)) and not self.is_top_level_import(node):
            self.add(node, "import_not_allowed", "imports go at the top of the module, never inside a function or a class")
        elif isinstance(node, ast.FunctionDef):
            self.function(node)
        elif isinstance(node, ast.ClassDef) and node not in self.tree.body:
            self.add(node, "node_not_allowed", "a class cannot be nested")
        elif isinstance(node, ast.Name):
            self.name(node)
        elif isinstance(node, ast.Attribute):
            self.attribute(node)
        elif isinstance(node, ast.Call):
            self.call(node)
        elif isinstance(node, ast.FormattedValue):
            if node.format_spec is not None and not all(isinstance(v, ast.Constant) for v in node.format_spec.values):      # type: ignore[attr-defined]
                self.add(node, "node_not_allowed", "a format specifier in an f-string must be a literal")
        elif isinstance(node, ast.comprehension) and node.is_async:
            self.add(node, "node_not_allowed", "async comprehensions are not allowed")
        for child in ast.iter_child_nodes(node):
            self.visit(child, in_post_init=post)

    def is_top_level_import(self, node: ast.AST) -> bool:
        return node in self.tree.body

    def function(self, node: ast.FunctionDef) -> None:
        if node.decorator_list and node not in self.tree.body and not any(node in c.body for c in self.tree.body if isinstance(c, ast.ClassDef)):
            self.add(node, "node_not_allowed", "decorators are not allowed")
        elif node.decorator_list and node in self.tree.body:
            self.add(node, "node_not_allowed", "decorators are not allowed")
        if node.name.startswith("__") and node.name != "__post_init__":
            self.add(node, "name_not_allowed", f"the dunder method {node.name!r} is not allowed")
        if node.returns is not None or any(a.annotation is not None for a in node.args.args + node.args.kwonlyargs):
            for sub in [x for x in ast.walk(node.args) if isinstance(x, ast.Name)] + ([x for x in ast.walk(node.returns) if isinstance(x, ast.Name)] if node.returns else []):
                if sub.id not in BUILTINS and sub.id not in self.defined:
                    self.add(sub, "name_not_allowed", f"{sub.id!r} is not a permitted name")

    def name(self, node: ast.Name) -> None:
        if node.id.startswith("__"):
            self.add(node, "name_not_allowed", f"the name {node.id!r} is not allowed")
        elif isinstance(node.ctx, ast.Load):
            if node.id in ("np", "math"):
                return                                  # it must be the base of an attribute: visit() of the Attribute checks the parent
            if node.id not in self.defined and node.id not in BUILTINS:
                self.add(node, "name_not_allowed", f"the name {node.id!r} is not a permitted builtin or a name defined in the code")
        elif node.id in ("np", "math") or (node.id in BUILTINS and node.id not in ("True", "False", "None") and False):
            self.add(node, "name_not_allowed", f"{node.id!r} cannot be reassigned")

    def attribute(self, node: ast.Attribute) -> None:
        attr = node.attr
        if attr.startswith("_"):
            self.add(node, "attribute_not_allowed", f"the attribute {attr!r} starts with an underscore")
        elif attr in VETOED_ATTRS:
            self.add(node, "attribute_not_allowed", f"the attribute {attr!r} is not allowed on any object")
        elif isinstance(node.value, ast.Name) and node.value.id == "np" and attr not in NUMPY_ATTRS:
            self.add(node, "attribute_not_allowed", f"np.{attr} is not on the list of numpy names")
        elif isinstance(node.value, ast.Name) and node.value.id == "math" and attr not in MATH_ATTRS:
            self.add(node, "attribute_not_allowed", f"math.{attr} is not on the list of math names")

    def call(self, node: ast.Call) -> None:
        spread = any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords)
        if spread and not (isinstance(node.func, ast.Name) and (node.func.id in BUILTINS or node.func.id in self.functions)):
            self.add(node, "node_not_allowed", "* and ** arguments are allowed only in calls to builtins and to functions defined in the code")

    # np and math as bare names: reported here because a Name does not know its parent
    def check_bare(self) -> None:
        parents: dict[int, ast.AST] = {}
        for parent in ast.walk(self.tree):
            for child in ast.iter_child_nodes(parent):
                parents[id(child)] = parent
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name) and node.id in ("np", "math") and isinstance(node.ctx, ast.Load):
                parent = parents.get(id(node))
                if not (isinstance(parent, ast.Attribute) and parent.value is node):
                    self.add(node, "name_not_allowed", f"{node.id} can only be used as {node.id}.<name>")


# ---------------------------------------------------------------------------------------------------- the strategy written against the engine (1.2.3)
ENGINE_MODULES = frozenset({"hokma.factory.strategy", "hokma.factory.orders", "hokma.factory.indicator_library", "hokma.factory.params"})
TEST_ENGINE_MODULES = ENGINE_MODULES | {"hokma.factory.strategy_replay"}
PLAIN_FROM = {"dataclasses": frozenset({"dataclass"}), "decimal": frozenset({"Decimal"}), "typing": frozenset({"ClassVar"}), "__future__": frozenset({"annotations"})}


def _engine_module(module: str | None) -> bool:
    return (module or "").startswith("hokma.") and module != "hokma.factory.user_api"


def is_engine_source(source: Any) -> bool:
    """A strategy that imports from hokma.factory.* is written against the contract of the engine (strategy.py and orders.py), not against user_api."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError, TypeError):
        return False
    return any(isinstance(n, ast.ImportFrom) and _engine_module(n.module) for n in tree.body)


def _engine_name_ok(module: str, name: str) -> bool:
    """A name taken from a module of the engine must be something that module itself defines (a class, a function, a constant), never a module or a library it imported. The file of the
    module is read, never imported: the check loads nothing."""
    from pathlib import Path

    try:
        path = Path(__file__).resolve().parents[2].joinpath(*module.split(".")[1:]).with_suffix(".py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:                                                  # noqa: BLE001
        return False
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == name:
            return True
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return True
    return False


class _EngineChecker(_Checker):
    """Same permitted nodes, names and attributes as the other check; what changes is the list of imports and the shape of the class: any class that follows the contract
    of the engine (SCHEMA, init(ctx), on_bar(ctx) returning OrderIntent values). No broker, no exec, no network, no files."""

    engine = True

    def module(self) -> None:
        classes = []
        for index, stmt in enumerate(self.tree.body):
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str) and index == 0:
                continue
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                self.import_statement(stmt)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                continue
            elif isinstance(stmt, ast.FunctionDef):
                if stmt.decorator_list:
                    self.add(stmt, "node_not_allowed", "a helper function has no decorators")
            elif isinstance(stmt, ast.ClassDef):
                classes.append(stmt)
            else:
                self.add(stmt, "node_not_allowed", f"{type(stmt).__name__} cannot run when the module loads: only imports, assignments, helper functions and one class")
        if self.kind == "test":
            if not any(isinstance(s, ast.FunctionDef) and s.name.startswith("test_") for s in self.tree.body):
                self.add(self.tree, "contract", "the test file needs at least one function named test_...")
            return
        if len(classes) != 1:
            self.add(classes[1] if len(classes) > 1 else self.tree, "contract", f"the code must define exactly one class, found {len(classes)}")
        if classes:
            self.the_class(classes[0])

    def import_statement(self, node: ast.Import | ast.ImportFrom) -> None:
        if isinstance(node, ast.Import):
            ok = len(node.names) == 1 and ((node.names[0].name == "math" and node.names[0].asname is None) or (node.names[0].name == "numpy" and node.names[0].asname == "np")
                                           or (self.kind == "test" and node.names[0].name == "strategy" and node.names[0].asname is None))
            if not ok:
                self.add(node, "import_not_allowed", "only 'import math', 'import numpy as np' and, in a test, 'import strategy' are allowed")
            return
        if node.level:
            self.add(node, "import_not_allowed", "relative imports are not allowed")
            return
        module = node.module or ""
        names = [a.name for a in node.names]
        if any(a.asname for a in node.names):
            self.add(node, "import_not_allowed", "names are imported without an alias")
        elif module in PLAIN_FROM:
            if not set(names) <= PLAIN_FROM[module]:
                self.add(node, "import_not_allowed", f"from {module} allows only {', '.join(sorted(PLAIN_FROM[module]))}")
        elif module in (TEST_ENGINE_MODULES if self.kind == "test" else ENGINE_MODULES):
            for name in names:
                if name.startswith("_") or not _engine_name_ok(module, name):
                    self.add(node, "import_not_allowed", f"{name!r} is not a public name defined by {module}")
        elif self.kind == "test" and module == "strategy":
            return
        else:
            self.add(node, "import_not_allowed", f"module {module!r} is not on the list: math, numpy as np, hokma.factory.strategy, orders, indicator_library and params")

    def the_class(self, cls: ast.ClassDef) -> None:
        d = cls.decorator_list
        frozen = (len(d) == 1 and isinstance(d[0], ast.Call) and isinstance(d[0].func, ast.Name) and d[0].func.id == "dataclass" and not d[0].args
                  and len(d[0].keywords) == 1 and d[0].keywords[0].arg == "frozen" and isinstance(d[0].keywords[0].value, ast.Constant) and d[0].keywords[0].value.value is True)
        if not frozen:
            self.add(cls, "contract", "the class must be decorated exactly with @dataclass(frozen=True)")
        if cls.bases or cls.keywords:
            self.add(cls, "contract", "the class has no base classes")
        attrs: set[str] = set()
        methods: dict[str, ast.FunctionDef] = {}
        for index, stmt in enumerate(cls.body):
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str) and index == 0:
                continue
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                attrs.add(stmt.targets[0].id)
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                attrs.add(stmt.target.id)
            elif isinstance(stmt, ast.FunctionDef):
                methods[stmt.name] = stmt
                if stmt.decorator_list:
                    self.add(stmt, "node_not_allowed", "a method has no decorators")
            else:
                self.add(stmt, "node_not_allowed", f"{type(stmt).__name__} is not allowed in the body of the class")
        if "SCHEMA" not in attrs:
            self.add(cls, "contract", "the class must define SCHEMA = ParamSchema(...)")
        for name in ("init", "on_bar"):
            if name not in methods:
                self.add(cls, "contract", f"the class must define the method {name}")
            else:
                self.signature(methods[name])
        for name, fn in methods.items():
            if name not in ("init", "on_bar", "__post_init__") and name.startswith("__"):
                self.add(fn, "contract", f"the dunder method {name!r} is not allowed")


def _is_plain_constant(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (int, float, str, bool)) or node.value is None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, (int, float)) and not isinstance(node.operand.value, bool)
    if isinstance(node, ast.Tuple):
        return all(_is_plain_constant(e) for e in node.elts)
    return False


def _is_static_expression(node: ast.expr) -> bool:
    """A literal, or a call of Param, ParamSchema or IndicatorRequest whose arguments are literals: what a class attribute may hold."""
    if _is_plain_constant(node):
        return True
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(_is_static_expression(e) for e in node.elts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("Param", "ParamSchema", "IndicatorRequest"):
        return all(_is_static_expression(a) for a in node.args) and all(_is_static_expression(k.value) for k in node.keywords if k.arg)
    return False


def _is_classvar(annotation: ast.expr | None) -> bool:
    node = annotation.value if isinstance(annotation, ast.Subscript) else annotation
    return isinstance(node, ast.Name) and node.id == "ClassVar"


def normalize_source(source: str) -> str:
    """The text of a rule as the parent stores, hashes, checks and sends it: line ends as \\n. A rule typed on Windows or pasted from a page arrives with
    CRLF; the static check refuses a carriage return (it is an invisible character), so the parent turns each CRLF into LF BEFORE the hash and the check
    and every later step works on the same bytes. A lone carriage return is not touched: it stays a finding."""
    return source.replace("\r\n", "\n") if isinstance(source, str) else source


def code_sha256(source: str) -> str:
    """The sha256 (hex) of the normalized source in UTF-8: the hash that is stored, shown to the owner and compared at every load."""
    import hashlib

    return hashlib.sha256(normalize_source(source).encode("utf-8")).hexdigest()


def check(source: str, *, kind: str) -> CheckResult:
    """The findings of the static check. `ok` is true only when there are none. Never raises."""
    findings = _check(source, kind)
    return CheckResult(ok=not findings, findings=findings)


def _check(source: Any, kind: Any) -> list[Finding]:
    if not isinstance(source, str):
        return [Finding(1, 0, "syntax", "the source must be text")]
    if len(source.encode("utf-8", "replace")) > MAX_BYTES:
        return [Finding(1, 0, "too_large", f"the source is over {MAX_BYTES // 1024} KB")]
    if source.count("\n") + 1 > MAX_LINES:
        return [Finding(MAX_LINES, 0, "too_large", f"the source is over {MAX_LINES} lines")]
    bad = _bad_characters(source)
    if bad:
        return bad
    try:
        tree = ast.parse(source, filename=USER_FILENAME)
    except SyntaxError as exc:
        return [Finding(exc.lineno or 1, max((exc.offset or 1) - 1, 0), "syntax", f"syntax error: {exc.msg}"[:200])]
    except (ValueError, RecursionError, MemoryError) as exc:
        return [Finding(1, 0, "syntax", f"the source cannot be parsed: {type(exc).__name__}")]
    engine = kind in STRATEGY_KINDS and any(isinstance(n, ast.ImportFrom) and _engine_module(n.module) for n in tree.body)
    checker = (_EngineChecker if engine else _Checker)(tree, kind if isinstance(kind, str) else "")
    if kind not in ALL_KINDS:
        checker.add(tree, "contract", f"kind must be one of {', '.join(ALL_KINDS)}")
        return checker.findings
    try:
        checker.check_bare()
    except _Stop:
        return checker.findings
    except RecursionError:
        return [Finding(1, 0, "syntax", "the code is nested too deeply to be checked")]
    return sorted(checker.run(), key=lambda f: (f.line, f.col, f.rule))


def _bad_characters(source: str) -> list[Finding]:
    out = []
    for number, line in enumerate(source.split("\n"), start=1):
        for col, char in enumerate(line):
            if char == "\t":
                continue
            category = unicodedata.category(char)
            if category in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
                out.append(Finding(number, col, "bad_character", f"the character U+{ord(char):04X} is not allowed (control, format or invisible)"))
                if len(out) >= 20:
                    return out
    return out
