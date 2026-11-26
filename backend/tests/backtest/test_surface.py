"""Surface of an optimization (SPEC-020, 4.5): B-29, L-42, L-43, L-44, L-45 and L-46. The surfaces are small and written by hand, so every
expected value is in the comments."""
import sys
import types
import xml.etree.ElementTree as ET

import numpy as np
import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from backtest import support
from backtest.search_support import FakeLedger
from hokma.backtest import optimize, surface
from hokma.backtest.optimize import Axis, Constraint, ParamSpace, SearchError
from hokma.backtest.spec import SpecError
from hokma.backtest.surface import ClusterUnavailable


def search(space, f, *, objective="sharpe", strategy_id="alpha", n=None):
    """A finished grid search of `f` over `space`."""
    return optimize.search(support.spec(strategy_id=strategy_id), space, method="grid", n_trials=n or space.size, objective=objective, presweep=False,
                             evaluate=f, ledger=FakeLedger())


def lattice(n_x, n_y):
    return ParamSpace((Axis("x", "int", 0, n_x - 1), Axis("y", "int", 0, n_y - 1)))


def kmeans(points, k, seed):
    """A small k-means with a farthest point start: it stands in for the one of the portfolio module (the adapter passes points, k and seed)."""
    rng = np.random.default_rng(seed)
    centers = [points[int(rng.integers(len(points)))]]
    while len(centers) < k:
        far = np.argmax(np.min([np.linalg.norm(points - c, axis=1) for c in centers], axis=0))
        centers.append(points[far])
    centers = np.array(centers, dtype=float)
    for _ in range(50):
        labels = np.argmin(np.linalg.norm(points[:, None, :] - centers[None, :, :], axis=2), axis=1)
        moved = np.array([points[labels == g].mean(axis=0) if (labels == g).any() else centers[g] for g in range(k)])
        if np.allclose(moved, centers):
            break
        centers = moved
    return labels, centers


# ---------------------------------------------------------------------------------------------------------------- B-29
def test_the_heat_map_has_one_cell_per_combination_with_the_requested_metric_B_29():
    space = ParamSpace((Axis("a", "int", 1, 4), Axis("b", "int", 10, 30, 10)))
    result = search(space, lambda p: p["a"] * 100 + p["b"])                          # 4 * 3 = 12 combinations; the value of (a, b) is 100 a + b
    heat = surface.heatmap(result, metric="sharpe")
    assert (heat.x, heat.y, heat.metric) == ("a", "b", "sharpe") and len(heat.cells) == 12
    assert {(c.x, c.y): c.value for c in heat.cells} == {(a, b): a * 100.0 + b for a in (1, 2, 3, 4) for b in (10, 20, 30)}
    assert all(c.status == "ok" for c in heat.cells) and heat.to_dict()["cells"][0] == {"x": 1, "y": 10, "value": 110.0, "status": "ok"}
    with pytest.raises(SearchError, match="metric_not_recorded"):
        surface.heatmap(result, metric="net_pnl")
    with pytest.raises(SearchError):
        surface.heatmap(result, "a", "a")


def test_the_heat_map_is_also_an_image_with_no_external_resource_and_escapes_its_text_B_29():
    space = ParamSpace((Axis("a", "int", 1, 3), Axis("b", "int", 1, 2)))
    result = search(space, lambda p: float(p["a"] * p["b"]), objective='x<y & "z"')
    svg = surface.heatmap(result).svg()
    root = ET.fromstring(svg)                                                        # well formed, so the escaping holds
    assert root.tag.endswith("svg") and svg.count("<rect") == 6
    assert "&lt;" in svg and "&amp;" in svg and 'x<y' not in svg
    for banned in ("href", "src=", "url(", "@import", "<script", "<image", "<link", "<style", "javascript:"):
        assert banned not in svg
    assert svg.count("http") == 1 and 'xmlns="http://www.w3.org/2000/svg"' in svg    # only the namespace, which loads nothing
    assert surface.heatmap(result).svg() == svg                                      # the same data gives the same image


def test_the_heat_map_marks_failed_and_excluded_combinations_and_holds_the_other_parameters():
    space = ParamSpace((Axis("a", "int", 1, 3), Axis("b", "int", 1, 3), Axis("c", "int", 1, 2)), (Constraint("a", "ne", "b"),))

    def f(p):
        if (p["a"], p["b"]) == (1, 2):
            raise SpecError("bad_spec: no bars")
        return float(p["a"] + p["b"] + 10 * p["c"])
    result = search(space, f)
    heat = surface.heatmap(result, "a", "b")                                         # c is held at the best point: c = 2 (the larger value)
    assert heat.fixed == {"c": 2} and len(heat.cells) == 9
    state = {(c.x, c.y): c.status for c in heat.cells}
    assert state[(1, 2)] == "failed" and state[(1, 1)] == "excluded" and state[(3, 3)] == "excluded" and state[(2, 3)] == "ok"
    assert {(c.x, c.y): c.value for c in heat.cells}[(2, 3)] == 25.0                  # 2 + 3 + 10 * 2
    low = surface.heatmap(result, "a", "b", fixed={"c": 1})
    assert {(c.x, c.y): c.value for c in low.cells}[(2, 3)] == 15.0
    assert 'fill="#e53e3e"' in heat.svg() and "<title>" in heat.svg()
    flipped = surface.heatmap(result, "b", "a")
    assert {(c.x, c.y): c.value for c in flipped.cells}[(3, 2)] == 25.0              # the same point with the axes swapped
    with pytest.raises(SearchError, match="fixed"):
        surface.heatmap(result, "a", "b", fixed={"c": 9})


# ---------------------------------------------------------------------------------------------------------------- L-42
def test_a_surface_with_one_isolated_spike_marks_it_and_a_plateau_does_not_L_42():
    space = lattice(7, 7)
    spike = search(space, lambda p: 10.0 if (p["x"], p["y"]) == (3, 3) else 0.0)
    peaks = surface.isolated_peaks(spike)
    # the spike is 10, its four neighbours are 0: the prominence is (10 - 0) / (10 - 0) = 1
    assert len(peaks) == 1 and peaks[0].label == "isolated_peak" and peaks[0].params == {"x": 3, "y": 3}
    assert (peaks[0].value, peaks[0].neighbour_mean, peaks[0].prominence) == (10.0, 0.0, 1.0)
    plateau = search(space, lambda p: 10.0 if 2 <= p["x"] <= 4 and 2 <= p["y"] <= 4 else 0.0)
    assert surface.isolated_peaks(plateau) == ()                                     # equal neighbours: no strict maximum
    hill = search(space, lambda p: 100.0 - 5 * ((p["x"] - 3) ** 2 + (p["y"] - 3) ** 2))
    # the top is 100, its neighbours 95, the lowest corner 100 - 5 * 18 = 10: prominence 5 / 90 = 0.056, below 0.3
    assert surface.isolated_peaks(hill) == ()
    assert surface.isolated_peaks(hill, drop=0.05)[0].params == {"x": 3, "y": 3}
    assert surface.isolated_peaks(search(space, lambda p: 1.0)) == ()               # a flat surface has no range
    with pytest.raises(SearchError):
        surface.isolated_peaks(hill, drop=0)


def test_failed_points_are_not_neighbours_and_a_surface_with_no_value_is_refused():
    def f(p):
        raise SpecError("bad_spec: no bars")
    with pytest.raises(SearchError, match="no_valid_points"):
        surface.isolated_peaks(search(lattice(3, 3), f))


# ---------------------------------------------------------------------------------------------------------------- L-43
def seeded(p):
    """Two plateaus on an 11 by 11 lattice: 3 by 3 blocks around (3, 3) at 10.0 and around (8, 8) at 9.5; everything else is 0."""
    if 2 <= p["x"] <= 4 and 2 <= p["y"] <= 4:
        return 10.0
    return 9.5 if 7 <= p["x"] <= 9 and 7 <= p["y"] <= 9 else 0.0


def test_two_seeded_plateaus_give_two_groups_and_their_centres_L_43():
    result = search(lattice(11, 11), seeded)
    asked = []

    def spy(points, k, seed):
        asked.append(k)
        return kmeans(points, k, seed)
    groups = surface.plateaus(result, cluster=spy)
    assert asked == [1, 2]                                                           # one group is not tight (the blocks are 7 steps apart); two are
    assert len(groups) == 2 and all(g.tight and g.size == 9 for g in groups)
    assert [dict(g.center) for g in groups] == [{"x": 3.0, "y": 3.0}, {"x": 8.0, "y": 8.0}]        # the middle of each block, in parameter units
    assert [g.mean_value for g in groups] == [10.0, 9.5]
    assert surface.plateaus(result, cluster=spy) == groups                           # the same surface and seed give the same groups


def test_the_plateau_centres_are_in_the_units_of_the_parameters():
    space = ParamSpace((Axis("fast", "int", 10, 60, 10), Axis("slow", "float", 0.5, 3.0, 0.5)))
    result = search(space, lambda p: 5.0 if p["fast"] in (20, 30) and p["slow"] in (1.0, 1.5) else 0.0)
    (group,) = surface.plateaus(result, cluster=kmeans)
    assert dict(group.center) == {"fast": 25.0, "slow": 1.25} and group.size == 4        # the middle of fast 20..30 and slow 1.0..1.5


def test_the_grouping_is_the_one_of_the_portfolio_module_and_without_it_the_part_is_unavailable(monkeypatch):
    result = search(lattice(11, 11), seeded)
    monkeypatch.setitem(sys.modules, "hokma.quant.portfolio.cluster", None)          # the import fails: that module is not built yet
    with pytest.raises(ClusterUnavailable) as err:
        surface.plateaus(result)
    assert err.value.code == "cluster_unavailable"
    fake = types.ModuleType("hokma.quant.portfolio.cluster")
    fake.kmeans = kmeans
    monkeypatch.setitem(sys.modules, "hokma.quant.portfolio.cluster", fake)
    assert len(surface.plateaus(result)) == 2                                        # found by its path, called as (points, k, seed)


def test_plateaus_refuse_a_choice_parameter_and_bad_arguments():
    space = ParamSpace((Axis("x", "int", 0, 4), Axis("m", "choice", choices=("a", "b"))))
    with pytest.raises(SearchError, match="plateaus_need_numeric_axes"):
        surface.plateaus(search(space, lambda p: float(p["x"])), cluster=kmeans)
    with pytest.raises(SearchError):
        surface.plateaus(search(lattice(3, 3), lambda p: 1.0), band=0, cluster=kmeans)


# ---------------------------------------------------------------------------------------------------------------- L-44
def test_the_slices_hold_the_other_parameters_at_the_best_point_and_a_parameter_with_no_effect_is_flat_L_44():
    space = ParamSpace((Axis("x", "int", 0, 5), Axis("y", "int", 0, 4)))
    result = search(space, lambda p: 3.0 * p["x"])                                    # y does nothing; the best point is x = 5 and the first y, 0
    by_name = {s.param: s for s in surface.slices(result)}
    assert by_name["x"].values == (0, 1, 2, 3, 4, 5) and by_name["x"].objective == (0.0, 3.0, 6.0, 9.0, 12.0, 15.0)
    assert by_name["x"].effect == 15.0 and by_name["x"].flat is False
    assert by_name["y"].objective == (15.0,) * 5 and by_name["y"].effect == 0.0 and by_name["y"].flat is True
    moved = {s.param: s for s in surface.slices(result, at={"y": 3})}
    assert moved["x"].objective == (0.0, 3.0, 6.0, 9.0, 12.0, 15.0)
    with pytest.raises(SearchError):
        surface.slices(result, at={"y": 99})


def test_a_slice_with_a_failed_point_has_a_hole_and_one_point_is_not_judged():
    def f(p):
        if p["x"] == 2:
            raise SpecError("bad_spec: no bars")
        return float(p["x"] + p["y"])
    result = search(ParamSpace((Axis("x", "int", 0, 3), Axis("y", "int", 0, 1))), f)
    by_name = {s.param: s for s in surface.slices(result)}
    assert by_name["x"].objective == (1.0, 2.0, None, 4.0)                           # the best point is (3, 1): x runs with y = 1
    single = search(ParamSpace((Axis("x", "int", 0, 2), Axis("y", "int", 4, 4))), lambda p: float(p["x"]))
    assert {s.param: s for s in surface.slices(single)}["y"].flat is None


# ---------------------------------------------------------------------------------------------------------------- L-45
def test_the_failed_runs_come_with_their_errors_L_45():
    def f(p):
        if p["x"] in (1, 3):
            raise SpecError(f"bad_spec: no bars at {p['x']}")
        return 1.0
    result = search(lattice(4, 1), f)
    found = surface.failed(result)
    assert [(dict(x.params), x.error) for x in found] == [({"x": 1, "y": 0}, "SpecError: bad_spec: no bars at 1"), ({"x": 3, "y": 0}, "SpecError: bad_spec: no bars at 3")]
    assert surface.failed(search(lattice(2, 1), lambda p: 1.0)) == ()


# ---------------------------------------------------------------------------------------------------------------- L-46
def test_analyze_returns_the_four_parts_of_a_saved_optimization_L_46(db):
    spec = support.spec(strategy_id="alpha")

    def f(p):
        if (p["x"], p["y"]) == (0, 0):
            raise SpecError("bad_spec: no bars")
        return 10.0 if (p["x"], p["y"]) == (3, 3) else 0.0
    result = search(lattice(7, 7), f)
    opt_id = optimize.save(db, spec, result, engine_version="engine-1.0")
    analysis = surface.analyze(opt_id, db=db, cluster=kmeans)
    assert tuple(analysis.parts) == ("peaks", "plateaus", "slices", "failed") and analysis.opt_id == opt_id and analysis.warnings == ()
    assert all(part.status == "ok" for part in analysis.parts.values())
    assert [p.params for p in analysis.parts["peaks"].value] == [{"x": 3, "y": 3}]
    assert len(analysis.parts["plateaus"].value) == 1 and len(analysis.parts["slices"].value) == 2 and len(analysis.parts["failed"].value) == 1
    assert surface.analyze(opt_id, db=db, cluster=kmeans) == analysis
    with pytest.raises(SearchError, match="bad_run"):
        surface.analyze(opt_id + 50, db=db)


def test_a_part_that_cannot_be_computed_is_reported_missing_with_its_reason_not_left_out(db, monkeypatch):
    spec = support.spec(strategy_id="alpha")
    monkeypatch.setitem(sys.modules, "hokma.quant.portfolio.cluster", None)
    opt_id = optimize.save(db, spec, search(lattice(5, 5), lambda p: float(p["x"])), engine_version="engine-1.0")
    analysis = surface.analyze(opt_id, db=db)
    assert tuple(analysis.parts) == ("peaks", "plateaus", "slices", "failed")
    assert analysis.parts["plateaus"].status == "missing" and analysis.parts["plateaus"].reason.startswith("cluster_unavailable")
    assert analysis.warnings == ("analysis_part_missing:plateaus",) and analysis.parts["peaks"].status == "ok"

    def f(p):
        raise SpecError("bad_spec: no bars")
    broken = optimize.save(db, spec, search(lattice(2, 2), f), engine_version="engine-1.0")
    nothing = surface.analyze(broken, db=db, cluster=kmeans)
    assert [nothing.parts[n].status for n in ("peaks", "plateaus", "slices", "failed")] == ["missing", "missing", "missing", "ok"]
    assert all(nothing.parts[n].reason.startswith("no_valid_points") for n in ("peaks", "plateaus", "slices")) and len(nothing.parts["failed"].value) == 4
    assert nothing.warnings == ("analysis_part_missing:peaks", "analysis_part_missing:plateaus", "analysis_part_missing:slices")


def test_the_analysis_carries_the_edge_of_space_flag():
    result = search(ParamSpace((Axis("slow", "int", 391, 400),)), lambda p: float(p["slow"]))
    assert surface.analyze_result(result, cluster=kmeans).edge_of_space == ("slow",)
