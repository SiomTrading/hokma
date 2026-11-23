"""Metrics part B: reports, charts and the single-file HTML tearsheet (CC-15, FF-020, FF-026, QS-051 to QS-057, QS-062)."""
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hokma.quant import registry
from hokma.quant.errors import BadInput
from hokma.quant.metrics import report

from .metrics_b_qa import EXPECTED, assert_bad_data_gives_none, assert_close, assert_registered, benchmark, call, ensure_registered, returns

R = returns()
B = benchmark()
SNAPSHOT = Path(__file__).parent / "snapshots" / "metrics_b_report_example.html"
URL = re.compile(r"https?://")


def _html(**kwargs) -> str:
    res = call("metrics.report_html", **kwargs)
    assert res.value is not None, res.warnings
    return res.value["html"]


def test_template_has_23_markers_and_no_external_resources_QS_056():
    assert_registered("metrics.report_html", "QS-056")
    found = re.findall(r"\{\{(\w+)\}\}", report.TEMPLATE)
    assert set(found) == set(report.MARKERS) and len(report.MARKERS) == 23
    assert not URL.search(report.TEMPLATE)
    for forbidden in ("favicon", "<link", "<script", "src=", "@import", "url("):
        assert forbidden not in report.TEMPLATE


@pytest.mark.parametrize("with_benchmark", [True, False])
def test_html_is_one_file_without_urls_CC_15(with_benchmark):
    assert_registered("metrics.report_html", "CC-15")
    text = _html(returns=R, benchmark=B if with_benchmark else None, title="Candidate report")
    assert not URL.search(text), URL.search(text)
    assert "favicon" not in text and "<link" not in text and "<script" not in text and "src=" not in text
    assert "Candidate report" in text and text.startswith("<!DOCTYPE html>")


def test_markers_are_filled_or_na_QS_051():
    assert_registered("metrics.report_html", "QS-051")
    with_bench = _html(returns=R, benchmark=B)
    assert "{{" not in with_bench and "}}" not in with_bench
    without = _html(returns=R)
    assert "{{" not in without
    assert "n/a" in without                                            # beta chart and volatility-matched chart need a benchmark
    assert with_bench.count("<svg") >= 14
    assert "Benchmark" in with_bench and "Benchmark" not in re.sub(r"Benchmark", "", without) + ""
    res = call("metrics.report_html", returns=R, benchmark=B)
    assert res.value["bytes"] == len(res.value["html"].encode("utf-8")) and res.as_of is not None


def test_title_is_escaped_QS_051():
    text = _html(returns=R, title="<b>x</b> & y")
    assert "<b>x</b>" not in text and "&lt;b&gt;x&lt;/b&gt; &amp; y" in text


def test_internal_template_by_name_QS_051():
    text = _html(returns=R, title="Own layout", template="compact")
    assert "<h1>Own layout</h1>" in text and "<table>" in text and "2021-01-04 to 2023-11-27" in text
    assert "{{" not in text


def test_template_never_reads_a_path_QS_051(tmp_path):
    secret = tmp_path / "secret.html"
    secret.write_text("<html>{{title}} LEAK</html>", encoding="utf-8")
    messages = set()
    for name in (str(secret), str(secret)[:-5], "../templates/compact", "../../report", "compact.html", "nope", "", "A" * 80, 7):
        with pytest.raises(BadInput) as err:
            call("metrics.report_html", returns=R, template=name)
        messages.add(str(err.value))
    assert messages == {"template: unknown template"}
    with pytest.raises(BadInput):
        call("metrics.report_html", returns=R, template_path=str(secret))
    assert "template_path" not in registry.describe("metrics.report_html").inputs


def test_every_interpolated_value_is_escaped_QS_056():
    from hokma.quant.metrics import report as rep
    text = rep.render_html(R, None, "<script>alert(1)</script>")[0]
    assert "<script>" not in text and "&lt;script&gt;" in text
    # the values of date_range, matched_dates and params go through the same escaping as the title
    assert rep._esc("<b>") == "&lt;b&gt;"


def test_rf_and_periods_are_validated_before_rendering_QS_056():
    for bad in ({"rf": float("nan")}, {"rf": float("inf")}, {"rf": "0.1"}, {"periods_per_year": 0}, {"periods_per_year": -5},
                {"periods_per_year": 2.5}, {"periods_per_year": 10**9}, {"periods_per_year": True}):
        with pytest.raises(BadInput):
            call("metrics.report_html", returns=R, **bad)


def test_html_snapshot_QS_056_QS_062():
    text = _html(returns=R.iloc[:252], benchmark=B.iloc[:252], title="Snapshot report")
    if os.environ.get("UPDATE_METRICS_SNAPSHOTS") == "1" or not SNAPSHOT.exists():
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(text, encoding="utf-8")
    assert text == SNAPSHOT.read_text(encoding="utf-8")


def test_example_tearsheet_is_deterministic_QS_062():
    assert_registered("metrics.report_example", "QS-062")
    one, two = call("metrics.report_example"), call("metrics.report_example")
    assert one.value["html"] == two.value["html"] and one.inputs_hash == two.inputs_hash
    assert not URL.search(one.value["html"]) and "{{" not in one.value["html"]
    strategy, bench = report.reference_series()
    assert len(strategy) == len(bench) == 504 and strategy.index.equals(bench.index)


def test_bad_data_gives_none_QS_051():
    for fn in ("metrics.report_html", "metrics.report_full", "metrics.report_basic", "metrics.report_metrics", "metrics.report_plots"):
        assert_bad_data_gives_none(lambda s, fn=fn: call(fn, returns=s))


def test_unusable_benchmark_falls_back_to_report_without_it_QS_051():
    for bad in (pd.Series([], dtype=float), pd.Series(0.01, index=R.index), pd.Series(np.nan, index=R.index)):
        res = call("metrics.report_html", returns=R, benchmark=bad)
        assert res.value is not None and res.warnings
        assert "no benchmark" in res.value["html"] and "{{" not in res.value["html"]


def test_metrics_table_psr_row_comes_from_validation_QS_054():
    from hokma.quant.validation import overfit
    assert_registered("metrics.report_metrics", "QS-054")
    res = call("metrics.report_metrics", returns=R, benchmark=B)
    psr = overfit.psr(R, sr_benchmark=0.0).value
    assert_close(res.value["Prob. Sharpe Ratio %"]["Strategy"], psr * 100)
    blocks = call("metrics.summary_returns", returns=R).value
    assert_close(res.value["CAGR %"]["Strategy"], blocks["cagr"] * 100)
    assert_close(res.value["Cumulative Return %"]["Benchmark"], float(np.prod(1 + B.to_numpy()) - 1) * 100)
    versus = call("metrics.versus_benchmark", returns=R, benchmark=B).value
    assert_close(res.value["Beta"]["Strategy"], versus["beta"])
    assert res.value["Beta"]["Benchmark"] is None
    basic = call("metrics.report_metrics", returns=R, benchmark=B, mode="basic")
    assert set(basic.value) < set(res.value) and "Sharpe" in basic.value
    only = call("metrics.report_metrics", returns=R)
    assert all("Benchmark" not in v for v in only.value.values()) and only.value["Beta"]["Strategy"] is None
    with pytest.raises(BadInput):
        call("metrics.report_metrics", returns=R, mode="partial")


def test_console_reports_QS_052_QS_053():
    assert_registered("metrics.report_full", "QS-052")
    assert_registered("metrics.report_basic", "QS-053")
    full = call("metrics.report_full", returns=R, benchmark=B).value["text"]
    basic = call("metrics.report_basic", returns=R, benchmark=B).value["text"]
    assert len(full.splitlines()) > len(basic.splitlines()) > 10
    assert "Smart Sharpe" in full and "Smart Sharpe" not in basic
    sharpe = call("metrics.summary_ratios", returns=R).value["sharpe"]
    line = next(l for l in basic.splitlines() if l.startswith("Sharpe "))
    assert f"{sharpe:,.2f}" in line
    psr_line = next(l for l in basic.splitlines() if l.startswith("Prob. Sharpe Ratio %"))
    assert "n/a" not in psr_line


def test_plots_QS_055_QS_057():
    assert_registered("metrics.report_plots", "QS-055")
    assert_registered("metrics.report_plots", "QS-057")
    res = call("metrics.report_plots", returns=R, benchmark=B)
    assert len(res.value) >= 16
    for name, svg in res.value.items():
        assert svg.startswith("<svg") or svg == "n/a", name
        if svg != "n/a":
            ET.fromstring(svg)                                       # well-formed
            assert not URL.search(svg), name
    assert all(v != "n/a" for v in res.value.values())
    solo = call("metrics.report_plots", returns=R, names=["returns", "rolling_beta"])
    assert set(solo.value) == {"returns", "rolling_beta"} and solo.value["rolling_beta"] == "n/a"
    with pytest.raises(BadInput):
        call("metrics.report_plots", returns=R, names=["pie"])


def test_returns_chart_draws_both_curves_QS_057():
    svg = call("metrics.report_plots", returns=R, benchmark=B, names=["returns"]).value["returns"]
    assert svg.count("<polyline") == 2
    one = call("metrics.report_plots", returns=R, names=["returns"]).value["returns"]
    assert one.count("<polyline") == 1


def test_monthly_heatmap_FF_020():
    assert_registered("metrics.report_heatmap", "FF-020")
    res = call("metrics.report_heatmap", returns=R)
    svg = res.value["svg"]
    ET.fromstring(svg)
    for month in ("Jan", "Jun", "Dec"):
        assert f">{month}<" in svg
    for year in ("2021", "2022", "2023"):
        assert f">{year}<" in svg
    table = report.monthly_table(R)
    jan = float(np.prod(1 + R[(R.index.year == 2021) & (R.index.month == 1)].to_numpy()) - 1)
    assert_close(table.loc[2021, "Jan"], jan)
    assert f"{jan * 100:,.2f}%" in svg


def test_correlation_heatmap_FF_020():
    frame = pd.DataFrame({"strategy": R, "benchmark": B})
    res = call("metrics.report_heatmap", returns=frame)
    svg = res.value["svg"]
    ET.fromstring(svg)
    corr = float(np.corrcoef(R, B)[0, 1])
    assert f"{corr:.2f}" in svg and svg.count(">1.00<") == 2
    assert res.value["kind"] == "correlation"
    with pytest.raises(BadInput):
        report.corr_heatmap(R)
    assert call("metrics.report_heatmap", returns=frame.iloc[:2]).value is None
    flat = call("metrics.report_heatmap", returns=pd.Series(0.01, index=R.index))
    assert flat.value is None


def test_pattern_every_function_of_part_b_follows_the_contract_FF_026():
    ensure_registered()
    for name, pieces in EXPECTED.items():
        spec = registry.describe(name)
        assert spec.module == "metrics" and spec.summary and not spec.blocked, name
        assert set(pieces) <= set(spec.piece_ids), name
        for arg, meta in spec.inputs.items():
            assert isinstance(meta, dict) and "type" in meta and "required" in meta, f"{name}.{arg}"
    ids = {p for pieces in EXPECTED.values() for p in pieces}
    required = {"QS-006", "QS-007", "QS-008", "QS-022a", "QS-023", "QS-025", "QS-027", "QS-031", "QS-033", "QS-034", "QS-037a", "QS-037b", "QS-038", "QS-039",
                "QS-040a", "QS-040b", "QS-041", "QS-042", "QS-043", "QS-044a", "QS-044b", "QS-045", "QS-046", "QS-048", "QS-050", "QS-051", "QS-052", "QS-053",
                "QS-054", "QS-055", "QS-056", "QS-057", "QS-059a", "QS-059b", "QS-062", "CC-01", "CC-15", "AH-36", "FF-001", "FF-002", "FF-003", "FF-020",
                "FT-069", "FT-070", "FT-073", "FT-075", "FT-076", "SK-081", "SK-082", "SK-083", "SK-084"}
    assert required <= ids, sorted(required - ids)
