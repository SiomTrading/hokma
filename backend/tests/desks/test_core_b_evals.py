"""ED-040: evaluation harness: a good fake agent passes; one that invents a number without a source fails."""
from hokma.desks.core import evals

SNAPSHOT = {"symbol": "NQ", "last": 21450.25, "atr": 182.5, "change_pct": 0.012}
CASE = evals.EvalCase(id="golden-1", task="What is the last price of NQ and its ATR?", snapshot=SNAPSHOT,
                      must_call=("get_quote",), must_include=("NQ", "21450.25"), ignore_numbers_below=0)
TOOLS = {"get_quote": lambda symbol: {"symbol": symbol, "last": 21450.25, "atr": 182.5}}


def good_agent(task, tools):
    quote = tools["get_quote"]("NQ")
    return evals.AgentAnswer(f"NQ last {quote['last']} with an ATR of {quote['atr']}.")


def inventing_agent(task, tools):
    tools["get_quote"]("NQ")
    return evals.AgentAnswer("NQ last 21450.25 and the target is 22100 with 87% probability.")


def lazy_agent(task, tools):
    return evals.AgentAnswer("NQ last 21450.25 and ATR 182.5.")


def test_a_correct_agent_passes_the_golden_case_ED_040():
    result = evals.run_case(good_agent, CASE, TOOLS)
    assert result.passed and result.failures == () and result.tool_calls == ("get_quote",)


def test_an_agent_that_invents_a_number_with_no_source_fails_ED_040():
    result = evals.run_case(inventing_agent, CASE, TOOLS)
    assert not result.passed and set(result.unverified) == {"22100", "87%"}
    assert any("numbers with no source" in f for f in result.failures)


def test_an_agent_that_does_not_use_the_tool_fails_even_with_right_numbers_ED_040():
    result = evals.run_case(lazy_agent, CASE, TOOLS)
    assert not result.passed and "did not call the tool get_quote" in result.failures


def test_a_number_the_tool_returned_counts_as_sourced_ED_040():
    case = evals.EvalCase(id="c", task="Report the open interest.", must_call=("oi",), snapshot={})
    result = evals.run_case(lambda t, tools: evals.AgentAnswer(f"Open interest is {tools['oi']()}."), case, {"oi": lambda: 51234})
    assert result.passed


def test_a_number_given_in_the_task_counts_as_sourced_ED_040():
    case = evals.EvalCase(id="c", task="The stop is 21400. How far is it from the entry at 21450?", snapshot={})
    assert evals.run_case(lambda t, tools: evals.AgentAnswer("The stop 21400 is 50 points under the entry 21450."), case).unverified == ("50",)


def test_must_include_must_not_include_and_empty_answers_ED_040():
    case = evals.EvalCase(id="c", task="t", snapshot={"x": 1}, must_include=("risk",), must_not_include=("guaranteed",))
    assert evals.run_case(lambda t, k: evals.AgentAnswer("Risk is 1."), case).passed
    bad = evals.run_case(lambda t, k: evals.AgentAnswer("This is guaranteed."), case)
    assert not bad.passed and len(bad.failures) == 2
    assert not evals.run_case(lambda t, k: evals.AgentAnswer("  "), evals.EvalCase(id="c", task="t")).passed


def test_a_tool_outside_the_allowed_list_is_denied_and_fails_the_case_ED_040():
    case = evals.EvalCase(id="c", task="t", allowed_tools=("get_quote",))

    def agent(task, tools):
        try:
            tools["place_order"]()
        except PermissionError:
            pass
        return evals.AgentAnswer("done")

    result = evals.run_case(agent, case, {"get_quote": lambda: 1, "place_order": lambda: 1})
    assert not result.passed and "tried a tool that is not allowed: place_order" in result.failures


def test_a_crashing_agent_fails_its_case_without_raising_ED_040():
    def boom(task, tools):
        raise RuntimeError("model unavailable")

    result = evals.run_case(boom, CASE, TOOLS)
    assert not result.passed and "RuntimeError" in result.failures[0]


def test_the_time_limit_is_enforced_ED_040():
    ticks = iter([0.0, 12.0])
    case = evals.EvalCase(id="c", task="t", max_seconds=5)
    result = evals.run_case(lambda t, k: evals.AgentAnswer("ok"), case, clock=lambda: next(ticks))
    assert not result.passed and result.seconds == 12.0


def test_a_suite_reports_each_case_ED_040():
    report = evals.run_suite(good_agent, [CASE, evals.EvalCase(id="second", task="x", must_include=("zzz",))], TOOLS)
    assert (report.passed, report.failed, report.ok) == (1, 1, False)
    assert report.summary().splitlines()[0] == "1/2 cases passed" and "second" in report.summary()
    assert evals.run_suite(good_agent, [CASE], TOOLS).ok and not evals.run_suite(good_agent, []).ok
