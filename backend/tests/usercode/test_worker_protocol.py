"""The protocol of the isolated process (SPEC-031 section 5.5): one JSON line per message, 8 MB at most, the worker does not trust the parent."""
import json
import sys

import pytest

from hokma.factory.usercode.sandbox import MAX_MESSAGE, Sandbox, SandboxError
from usercode.support import ALPHA_BODY, INSIDE_UP, alpha, alpha_ctx, source


@pytest.fixture
def sb(tmp_path):
    box = Sandbox(workdir=tmp_path / "empty", call_timeout=5)
    box.start()
    yield box
    box.close()


def test_the_worker_says_ready_with_the_layers_that_are_active(sb):
    assert sb.alive and sb.layers["audit_hook"] is True
    if sys.platform != "win32":
        assert sb.layers["rlimits"] is True and sb.layers["net_namespace"] in ("active", "unavailable")
    assert sb.pid and sb.pid != __import__("os").getpid()


def test_the_manifest_of_an_alpha_has_everything_hokma_needs_to_build_its_delegate(sb):
    manifest = sb.load(source("alpha"), "alpha")
    assert manifest["kind"] == "alpha" and manifest["lookback"] == 50 and manifest["facts"] == ["high", "low", "close"]
    assert manifest["schema"] == [{"name": "lookback_bars", "kind": "int", "default": 3, "low": 2.0, "high": 50.0, "choices": []}]
    assert manifest["requests"] == [] and "signal" in manifest["methods"]
    assert json.dumps(manifest)


def test_the_manifest_of_a_filter_and_of_an_exit_name_their_methods(tmp_path):
    for kind, methods in (("filter", {"allows", "must_flatten"}), ("exit", {"levels"})):
        with Sandbox(workdir=tmp_path / "e", call_timeout=5) as box:
            manifest = box.load(source(kind), kind)
            assert methods <= set(manifest["methods"]) and manifest["fields"]


def test_an_alpha_gives_a_view_or_abstains_as_data(sb):
    sb.load(source("alpha"), "alpha")
    got = sb.call("signal", ctx=alpha_ctx(INSIDE_UP))
    assert got["value"] == 1.0 and "inside bar" in got["reason"] and got["criteria"] == []
    short = sb.call("signal", ctx=alpha_ctx({"high": [1.0], "low": [1.0], "close": [1.0]}))
    assert short == {"abstain": "not enough bars", "missing": []}


def test_the_parameters_are_checked_against_the_schema_of_the_rule(sb):
    sb.load(source("alpha"), "alpha")
    sb.call("signal", params={"lookback_bars": 5}, ctx=alpha_ctx(INSIDE_UP))
    for bad in ({"lookback_bars": 1}, {"lookback_bars": "x"}, {"other": 1}):
        with pytest.raises(SandboxError) as caught:
            sb.call("signal", params=bad, ctx=alpha_ctx(INSIDE_UP))
        assert caught.value.code == "bad_params"
    assert sb.alive


def test_a_filter_answers_allow_or_block_and_flatten_or_hold(sb):
    sb.load(source("filter"), "filter")
    quiet = {"high": [10.0 + 0.1 * (i % 2) for i in range(25)] + [10.05], "low": [9.0] * 25 + [9.99]}
    wide = {"high": [10.0] * 25 + [15.0], "low": [9.0] * 26}
    assert sb.call("allows", ctx={"series": quiet}, args={"side": "long", "state": {}}) == {"allow": True, "reason": None}
    assert sb.call("allows", ctx={"series": wide}, args={"side": "long", "state": {}}) == {"allow": False, "reason": "range above its average"}
    assert sb.call("must_flatten", ctx={"series": quiet}, args={}) == {"flatten": False, "reason": None}


def test_an_exit_answers_its_levels_as_units_in_data(sb):
    sb.load(source("exit"), "exit")
    got = sb.call("levels", ctx={"series": {"low": [9.0, 9.5, 9.25, 10.0]}}, args={"side": "long"})
    assert got == {"sl": {"type": "price", "value": 9.5}, "tp": {"type": "ticks", "value": 12}}
    assert sb.call("levels", ctx={"series": {"low": [9.0]}}, args={"side": "long"}) == {"sl": None, "tp": None}


def test_a_call_to_a_method_the_rule_does_not_have_is_refused_and_the_process_lives(sb):
    sb.load(source("alpha"), "alpha")
    for method in ("allows", "levels", "__init__", "__class__", "no_such", "_private"):
        with pytest.raises(SandboxError) as caught:
            sb.call(method, ctx=alpha_ctx(INSIDE_UP))
        assert caught.value.code == "bad_input"
    assert sb.alive


def test_an_exception_of_the_rule_comes_back_as_data_cut_to_300_characters_and_the_process_lives(sb):
    sb.load(alpha("zero = 0\nreturn int('x' * 1000) if zero == 0 else 1 / zero"), "alpha")
    with pytest.raises(SandboxError) as caught:
        sb.call("signal", ctx=alpha_ctx(INSIDE_UP))
    assert caught.value.code == "rule_error" and caught.value.message.startswith("ValueError") and len(caught.value.message) <= 340
    assert sb.alive


def test_a_value_that_is_not_finite_is_an_error_of_the_rule_and_the_process_lives(sb):
    sb.load(alpha("return view(ctx, 'u', float('nan'), (), 'nan')"), "alpha")
    with pytest.raises(SandboxError) as caught:
        sb.call("signal", ctx=alpha_ctx(INSIDE_UP))
    assert caught.value.code == "rule_error" and "finite" in caught.value.message
    assert sb.alive


def test_an_answer_that_is_not_a_view_is_a_bad_answer(sb):
    sb.load(alpha("return 5"), "alpha")
    with pytest.raises(SandboxError) as caught:
        sb.call("signal", ctx=alpha_ctx(INSIDE_UP))
    assert caught.value.code == "bad_answer"


def test_a_call_before_the_load_and_a_second_load_are_refused(sb):
    with pytest.raises(SandboxError) as caught:
        sb.call("signal", ctx=alpha_ctx(INSIDE_UP))
    assert caught.value.code == "not_loaded"
    sb.load(source("alpha"), "alpha")
    with pytest.raises(SandboxError) as caught:
        sb.load(source("alpha"), "alpha")
    assert caught.value.code == "already_loaded"
    assert sb.alive


def test_the_worker_checks_the_code_again_and_does_not_trust_the_parent(sb):
    with pytest.raises(SandboxError) as caught:
        sb.load(source("alpha", extra_import="import os"), "alpha")
    assert caught.value.code == "static_check"
    assert caught.value.findings and caught.value.findings[0]["rule"] == "import_not_allowed"
    assert sb.alive
    sb.load(source("alpha"), "alpha")                    # the process is still clean and takes the corrected code


def test_a_syntax_error_is_a_finding_and_not_a_violation_of_the_file_layer(sb):
    with pytest.raises(SandboxError) as caught:
        sb.load("def broken(:\n    pass\n", "alpha")
    assert caught.value.code == "static_check" and caught.value.findings[0]["rule"] == "syntax"
    assert sb.alive
    sb.load(source("alpha"), "alpha")


def test_the_parent_is_not_needed_to_run_the_check_the_worker_runs_it_alone(sb):
    sb.send_raw(json.dumps({"load": {"source": source("alpha", extra_import="import socket"), "kind": "alpha"}}).encode() + b"\n")
    answer = sb.read_raw()
    assert answer["error"]["type"] == "static_check" and answer["error"]["findings"]


def test_a_rule_that_cannot_be_built_with_its_defaults_fails_the_load(sb):
    cls = source("alpha").split('"""A rule written for the tests."""')[1]
    cls = cls[cls.index("@dataclass"):].replace("    def requests(self):", "    def __post_init__(self):\n        raise ValueError('defaults rejected')\n\n    def requests(self):", 1)
    with pytest.raises(SandboxError) as caught:
        sb.load(source("alpha", cls=cls), "alpha")
    assert caught.value.code == "load_error" and "defaults rejected" in caught.value.message
    assert sb.alive


def test_malformed_messages_are_answered_and_do_not_end_the_process(sb):
    for raw in (b"not json\n", b"[1, 2]\n", b"{}\n", b'{"unknown": 1}\n', b'{"load": 5}\n', b'{"load": {"source": 5, "kind": "alpha"}}\n',
                b'{"call": {"method": 5}}\n', b"\xff\xfe\n", b'{"load": {"source": "x", "kind": "sizing"}}\n'):
        sb.send_raw(raw)
        answer = sb.read_raw()
        assert "error" in answer and answer["error"]["type"] in ("bad_message", "bad_input", "not_loaded", "static_check"), (raw, answer)
        assert sb.alive


def test_a_message_over_8_mb_is_refused_by_the_worker_and_the_process_lives(sb):
    sb.send_raw(b"x" * (MAX_MESSAGE + 1000) + b"\n")
    answer = sb.read_raw()
    assert answer["error"]["type"] == "message_too_large" and sb.alive
    sb.load(source("alpha"), "alpha")


def test_the_parent_refuses_to_send_over_8_mb(sb):
    sb.load(source("alpha"), "alpha")
    with pytest.raises(SandboxError) as caught:
        sb.call("signal", ctx=alpha_ctx({"high": ["x" * MAX_MESSAGE]}))
    assert caught.value.code == "bad_input" and sb.alive


def test_an_answer_over_8_mb_is_not_accepted(sb):
    sb.load(alpha("return ctx.abstain('x', ['y' * 1000] * 10000)"), "alpha")
    with pytest.raises(SandboxError) as caught:
        sb.call("signal", ctx=alpha_ctx(INSIDE_UP))
    assert caught.value.code == "bad_answer" and "too large" in caught.value.message
    assert sb.alive


def test_an_orderly_close_ends_the_process_with_exit_zero(tmp_path):
    box = Sandbox(workdir=tmp_path / "empty", call_timeout=5)
    box.start()
    box.load(source("alpha"), "alpha")
    box.close()
    assert not box.alive and box.exit_code == 0
    box.close()                                            # closing twice is harmless


def test_a_process_that_is_not_started_refuses_calls(tmp_path):
    box = Sandbox(workdir=tmp_path / "empty")
    with pytest.raises(SandboxError) as caught:
        box.call("signal", ctx={})
    assert caught.value.code == "unavailable"


def test_a_worker_that_cannot_start_is_unavailable_and_says_why(tmp_path):
    box = Sandbox(workdir=tmp_path / "empty", worker_argv=[sys.executable, "-I", "-c", "import sys; sys.exit(3)"], start_timeout=5)
    with pytest.raises(SandboxError) as caught:
        box.start()
    assert caught.value.code == "unavailable"


ONE = ["abs", "sign", "log", "exp", "sqrt", "cumsum", "cumprod", "diff", "mean", "std", "var", "max", "min", "argmax", "argmin", "sum", "prod",
       "median", "nanmean", "nanstd", "nansum", "nanmax", "nanmin", "isfinite", "isnan", "isinf", "sort", "argsort", "floor", "ceil", "square",
       "log1p", "zeros_like", "ones_like", "asarray", "array", "all", "any", "roll"]
EXTRA = ["clip", "where", "maximum", "minimum", "concatenate", "convolve", "percentile", "quantile", "full", "zeros", "ones", "arange", "power",
         "float64", "int64", "linspace", "nan", "inf"]
MATH = ["sqrt", "log", "exp", "floor", "ceil", "fabs", "fsum", "hypot", "log10", "log2", "pow", "copysign", "trunc", "tanh", "pi", "e", "isfinite"]


def test_the_lists_of_the_static_check_are_exactly_the_names_this_suite_runs():
    from hokma.factory.usercode import static_check

    assert static_check.NUMPY_ATTRS == frozenset(ONE) | frozenset(EXTRA) and static_check.MATH_ATTRS == frozenset(MATH)


def test_every_numpy_and_math_name_of_the_list_runs_under_the_audit_hook(sb):
    """A lazy import or an audited event inside numpy would turn a legal rule into a violation: run each name of the list once."""
    one = ONE
    lines = ["x = np.array([1.0, 2.0, 4.0, 3.0, 5.0])", "t = 0.0"]
    for name in one:
        arg = "x, 1" if name == "roll" else "x"
        lines.append(f"t = t + float(np.sum(np.asarray(np.{name}({arg}), dtype=float)))")
    lines += ["t = t + float(np.sum(np.clip(x, 1.0, 3.0))) + float(np.sum(np.where(x > 2.0, x, 0.0))) + float(np.sum(np.maximum(x, 2.0))) + float(np.sum(np.minimum(x, 2.0)))",
              "t = t + float(np.sum(np.concatenate((x, x)))) + float(np.sum(np.convolve(x, x))) + float(np.percentile(x, 50)) + float(np.quantile(x, 0.5))",
              "t = t + float(np.sum(np.full(3, 2.0))) + float(np.sum(np.zeros(3))) + float(np.sum(np.ones(3))) + float(np.sum(np.arange(3.0))) + float(np.sum(np.power(x, 2)))",
              "t = t + float(np.float64(2)) + float(np.int64(2)) + float(np.linspace(0.0, 1.0, 5)[-1]) + float(np.isfinite(np.inf)) + float(np.nan > 0) + float(np.inf > 0)",
              "t = t + math.sqrt(4.0) + math.log(2.0) + math.exp(0.0) + math.floor(2.5) + math.ceil(2.5) + math.fabs(-1.0) + math.fsum([1.0, 2.0]) + math.hypot(3.0, 4.0)",
              "t = t + math.log10(100.0) + math.log2(8.0) + math.pow(2.0, 3.0) + math.copysign(1.0, -2.0) + math.trunc(2.7) + math.tanh(0.5) + math.pi + math.e + float(math.isfinite(1.0))",
              "return view(ctx, 'u_all', 0.0, (), str(round(t, 6)))"]
    sb.load(alpha("\n".join(lines)), "alpha")
    got = sb.call("signal", ctx=alpha_ctx(INSIDE_UP))
    assert float(got["reason"]) > 0 and sb.alive


def test_the_default_body_of_the_samples_is_the_one_the_tests_rely_on():
    assert "inside" in ALPHA_BODY
