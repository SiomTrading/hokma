"""Saving, versioning and reading the IPS of the fund, its tools and its kit files (SPEC-024)."""
import json
import re
from pathlib import Path

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.ips import method, store
from hokma.kit import hokma_tools, mirror
from hokma.kit.hokma_tools import Caller
from hokma.kit.sync import sync_kit
from hokma.projects import gitops

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "kit" / "template"
CASES = json.loads((Path(__file__).parent / "fixtures" / "ips_reference_cases.json").read_text(encoding="utf-8"))
FULL = CASES["full"]["inputs"]
CLOSING = ("This is educational material, not investment advice. Trading involves risk of loss, including the capital you put at risk. "
           "Hokma does not promise or guarantee returns.")


@pytest.fixture
def profiled(db, env):
    with db.write() as u:
        u.profile.save(owner_name="Ana", workdir=str(env / "work"))
        u.profile.complete_setup()
    return db


def run(name, args, agent="hokma"):
    text, is_error = hokma_tools.call(name, args, Caller({"HOKMA_AGENT": agent}))
    return (json.loads(text) if not is_error else text), is_error


def ips_folder():
    found = run("ips_read", {})[0]
    from hokma.db import get_database

    with get_database().read() as u:
        return Path(u.projects.get(found["project_id"]).path)


def save(inputs, mode="draft", **extra):
    out, err = run("ips_save", {"inputs": inputs, "mode": mode, **extra})
    assert not err, out
    return out


# ---------------------------------------------------------------------------------------------------- 5. saving

def test_a_draft_does_not_raise_the_version_and_an_adoption_does(profiled):
    first = save({"profile": FULL["profile"], "assets": FULL["assets"]}, changed=["profile", "assets"])
    assert first["status"] == "draft" and first["version"] == 0 and first["commit"] == "IPS draft: profile, assets"
    second = save({"cashflow": FULL["cashflow"]})
    assert second["version"] == 0 and second["commit"] == "IPS draft: cashflow"
    adopted = save({k: v for k, v in FULL.items() if k not in ("profile", "assets", "cashflow")}, "adopt", note="First version")
    assert adopted["status"] == "adopted" and adopted["version"] == 1 and adopted["closing"] == CLOSING
    data = run("ips_read", {})[0]["data"]
    assert data["version"] == 1 and data["status"] == "adopted"
    assert data["inputs"]["assets"] and data["inputs"]["cashflow"]["monthly_spend_usd"] == 3200       # the earlier drafts were kept
    assert [h["version"] for h in data["history"]] == [1] and data["history"][0]["note"] == "First version"
    again = save({"cashflow": {"monthly_spend_usd": 3300}}, "adopt", changed=["cashflow"], note="Higher spending")
    assert again["version"] == 2 and again["commit"] == "IPS v2: cashflow"
    assert [h["version"] for h in run("ips_read", {})[0]["data"]["history"]] == [1, 2]


def test_every_save_leaves_a_commit_in_the_ips_project(profiled):
    save({"profile": FULL["profile"]}, changed=["profile"])
    save({"assets": FULL["assets"]}, "adopt", changed=["assets"])
    subjects = gitops.log(ips_folder())
    assert subjects[:2] == ["IPS v1: assets", "IPS draft: profile"]


def test_the_project_of_kind_ips_is_created_once(profiled):
    save({"profile": FULL["profile"]})
    save({"assets": FULL["assets"]})
    projects = run("project_list", {})[0]
    assert len([p for p in (projects if isinstance(projects, list) else projects.get("projects", projects)) if p["kind"] == "ips"]) == 1


def test_results_limits_and_checks_sent_by_the_caller_are_ignored(profiled):
    lying = {**FULL, "results": {"net_worth": 1}, "limits": {"max_loss_usd": 99999999}, "checks": [{"id": "x", "status": "pass"}], "version": 77}
    save(lying, "adopt", changed=["assets"])
    data = run("ips_read", {})[0]["data"]
    assert data["version"] == 1 and data["results"]["net_worth"] == CASES["full"]["expected"]["net_worth"]
    assert data["limits"]["max_loss_usd"] != 99999999 and all(c["id"] != "x" for c in data["checks"])


def test_an_invalid_answer_saves_nothing(profiled):
    text, err = run("ips_save", {"inputs": {"prop": {"history": {"evaluations": 2, "funded": 5}}}, "mode": "draft"})
    assert err and "cannot be more than the evaluations" in text
    assert run("ips_read", {})[1]            # there is not even an IPS project yet
    text, err = run("ips_compute", {"inputs": {"has_debts": False, "debts": [{"name": "Card", "balance_usd": 4}]}})
    assert err and "no debts" in text
    text, err = run("ips_save", {"mode": "draft"})
    assert err and "nothing to save" in text


# ---------------------------------------------------------------------------------------------------- 6. contract

def check_contract(data):
    def kind(value, *types):
        return value is None and "null" in types or any(
            isinstance(value, {"number": (int, float), "integer": int, "string": str, "bool": bool, "list": list, "dict": dict}[t])
            and not (t in ("number", "integer") and isinstance(value, bool)) for t in types if t != "null")

    assert data["schema"] == "hokma.ips/1" and isinstance(data["version"], int) and data["status"] in ("adopted", "draft")
    assert data["currency"] == "USD" and re.match(r"\d{4}-\d\d-\d\dT", data["created_at"]) and re.match(r"\d{4}-\d\d-\d\dT", data["updated_at"])
    assert set(data) == {"schema", "version", "status", "created_at", "updated_at", "currency", "inputs", "results", "limits", "checks", "missing", "history"}
    lim = data["limits"]
    assert set(lim) == {"investable_capital_usd", "max_loss_usd", "capital_floor_usd", "max_drawdown_pct", "max_risk_per_trade_pct",
                        "max_open_positions", "pockets_house_total_usd", "desk_allocation"}
    for key in ("investable_capital_usd", "max_loss_usd", "capital_floor_usd", "pockets_house_total_usd", "max_risk_per_trade_pct"):
        assert kind(lim[key], "number", "null"), key
    assert kind(lim["max_drawdown_pct"], "number", "string", "null") and kind(lim["max_open_positions"], "integer", "null")
    for desk in lim["desk_allocation"]:
        assert set(desk) == {"desk", "capital_source", "max_loss_usd", "funding_budget_usd", "avg_account_cost_usd",
                             "prop_max_drawdown_per_account_usd", "house_limit_usd", "max_accounts"}
        assert isinstance(desk["desk"], str) and desk["capital_source"] in ("own", "funded")
        for key in ("max_loss_usd", "funding_budget_usd", "avg_account_cost_usd", "prop_max_drawdown_per_account_usd", "house_limit_usd"):
            assert kind(desk[key], "number", "null"), key
        assert kind(desk["max_accounts"], "integer", "string", "null")
    for value in data["results"].values():
        assert kind(value, "number", "string", "null")
    assert {c["id"] for c in data["checks"]} >= {
        "prop_excluded_from_net_worth", "risk_level_is_minimum", "loss_within_investable", "pockets_within_loss_capacity",
        "funding_spend_within_budget", "no_debt_funds_trading", "reserve_covers_target", "risk_per_trade_within_drawdown",
        "desk_house_limits_within_max_loss", "pocket_payouts_within_payouts"}
    for c in data["checks"]:
        assert set(c) == {"id", "status", "numbers"} and c["status"] in ("pass", "fail", "no_data") and isinstance(c["numbers"], str)
    assert isinstance(data["missing"], list) and all(isinstance(x, str) for x in data["missing"])
    for h in data["history"]:
        assert set(h) == {"version", "date", "changed", "note"} and isinstance(h["changed"], list)


def test_ips_json_follows_the_contract_and_ips_read_returns_it(profiled):
    inputs = {**FULL, "declared_limits": {"max_risk_per_trade_pct": 1, "max_open_positions": 3, "desk_allocation": [
        {"desk": "Own", "capital_source": "own", "max_loss_usd": 500},
        {"desk": "Futures", "capital_source": "funded", "funding_budget_usd": 1000, "avg_account_cost_usd": 50, "prop_max_drawdown_per_account_usd": 2000}]}}
    save(inputs, "adopt", changed=["assets"])
    read = run("ips_read", {})[0]
    check_contract(read["data"])
    on_disk = json.loads((ips_folder() / "ips.json").read_text(encoding="utf-8"))
    assert on_disk == read["data"]
    assert read["data"]["limits"]["desk_allocation"][1]["max_accounts"] == 20
    save({"profile": {"owner_name": "Ana"}})                                  # a draft after the adoption is valid too
    check_contract(run("ips_read", {})[0]["data"])


def test_a_partial_draft_still_follows_the_contract(profiled):
    save({"profile": {"owner_name": "Ana"}})
    data = run("ips_read", {})[0]["data"]
    check_contract(data)
    assert data["limits"]["max_loss_usd"] is None and "loss_capacity" in data["missing"]


def test_ips_read_has_no_data_before_the_first_save(profiled):
    run("project_create", {"name": "Fund", "kind": "ips"})
    assert run("ips_read", {})[0]["data"] is None


# ---------------------------------------------------------------------------------------------------- 7. currency

def test_amounts_in_another_currency_are_saved_in_usd_with_the_rate(profiled):
    fx = {"currency": "COP", "units_per_usd": 4000, "source": "the owner's bank", "rate_date": "2026-11-20"}
    out = save({"cashflow": {"monthly_spend_usd": 12000000, "reserve_target_months": 6},
                "assets": [{"name": "Savings", "type": "reserve", "value_usd": 40000000}]}, fx=fx)
    data = run("ips_read", {})[0]["data"]
    assert data["inputs"]["cashflow"]["monthly_spend_usd"] == 3000 and data["inputs"]["cashflow"]["reserve_target_months"] == 6
    assert data["inputs"]["assets"][0]["value_usd"] == 10000
    assert data["inputs"]["profile"]["fx"] == fx and out["warnings"] == []
    # the next call is already in USD and the rate stays recorded
    save({"cashflow": {"monthly_savings_usd": 200}})
    inputs = run("ips_read", {})[0]["data"]["inputs"]
    assert inputs["cashflow"] == {"monthly_spend_usd": 3000, "reserve_target_months": 6, "monthly_savings_usd": 200}
    assert inputs["profile"]["fx"] == fx
    assert "IPS.md" and "per 1 USD" in (ips_folder() / "IPS.md").read_text(encoding="utf-8")


def test_ips_compute_converts_without_saving(profiled):
    fx = {"currency": "COP", "units_per_usd": 4000, "source": "bank", "rate_date": "2026-11-20"}
    out, err = run("ips_compute", {"inputs": {"cashflow": {"monthly_spend_usd": 8000000}}, "fx": fx})
    assert not err and out["inputs"]["cashflow"]["monthly_spend_usd"] == 2000 and out["inputs"]["profile"]["fx"]["currency"] == "COP"
    assert run("ips_read", {})[1]                                  # nothing was created
    for bad in ({"currency": "COP", "units_per_usd": 0, "source": "x", "rate_date": "2026-11-20"},
                {"currency": "COP", "units_per_usd": 4000},
                {"currency": "USD", "units_per_usd": 1, "source": "x", "rate_date": "2026-11-20"}):
        assert run("ips_compute", {"inputs": {}, "fx": bad})[1]


# ---------------------------------------------------------------------------------------------------- 8. IPS.md

def test_ips_md_has_the_fixed_closing_the_version_and_the_data_sheet(profiled):
    save(FULL, "adopt", changed=["assets"])
    text = (ips_folder() / "IPS.md").read_text(encoding="utf-8")
    assert CLOSING in text and "Version 1" in text and "Adopted" in text
    assert "## Data sheet (do not edit by hand)" in text and '"monthly_spend_usd": 3200' in text
    for title in ("Profile", "Assets", "Debts", "Income and withdrawals", "Risk level", "Risk pockets", "Limits of the fund", "Checks"):
        assert title in text
    save({"profile": {"owner_name": "Ana"}})
    assert "Draft, not adopted yet" in (ips_folder() / "IPS.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------------- 11. the chat

def test_create_my_ips_then_update_my_ips_lowering_a_debt(profiled):
    start = {**FULL, "debts": [{"name": "Card", "balance_usd": 20000, "monthly_payment_usd": 1000, "annual_rate_pct": 24, "funds_trading": False}]}
    computed, err = run("ips_compute", {"inputs": start})
    assert not err and computed["results"]["c2"] == 1                         # 1000 against 5675.01 of income: 17.6%
    for block in ("profile", "assets", "debts"):                              # the interview: a draft after each block
        keys = {"profile": ["profile"], "assets": ["assets"], "debts": ["has_debts", "debts"]}[block]
        save({k: start[k] for k in keys}, changed=[block])
    save({k: v for k, v in start.items() if k not in ("profile", "assets", "has_debts", "debts")}, "adopt",
         changed=list(method.METHOD_TABLES["blocks"]), note="Created by chat")
    v1 = run("ips_read", {})[0]["data"]
    assert v1["version"] == 1 and v1["results"]["c2"] == 1

    # update my IPS: the owner pays the card down
    lower = [{"name": "Card", "balance_usd": 5000, "monthly_payment_usd": 400, "annual_rate_pct": 24, "funds_trading": False}]
    after, err = run("ips_compute", {"inputs": {**v1["inputs"], "debts": lower}})
    assert not err and after["results"]["c2"] == 2
    save({"debts": lower}, "adopt", changed=["debts"], note="Paid the card down")
    v2 = run("ips_read", {})[0]["data"]
    assert v2["version"] == 2 and v2["results"]["c2"] == 2 and v2["results"]["c2"] != v1["results"]["c2"]
    assert v2["limits"]["investable_capital_usd"] > v1["limits"]["investable_capital_usd"]
    assert v2["limits"] != v1["limits"]
    assert gitops.log(ips_folder())[0] == "IPS v2: debts"
    assert [h["changed"] for h in v2["history"]][-1] == ["debts"]


# ---------------------------------------------------------------------------------------------------- 10. agents

def tools_of(slug):
    text = (TEMPLATE / "agents" / f"{slug}.md").read_text(encoding="utf-8")
    return set(re.search(r"^tools: (.*)$", text, re.M).group(1).replace(" ", "").split(","))


def test_only_hokma_can_save_the_ips():
    assert {"mcp__hokma__ips_compute", "mcp__hokma__ips_save", "mcp__hokma__ips_read"} <= tools_of("hokma")
    for path in sorted((TEMPLATE / "agents").glob("*.md")):
        if path.stem != "hokma":
            assert not {t for t in tools_of(path.stem) if t.endswith(("ips_save", "ips_compute"))}, path.stem
    assert "fund-ips" in (TEMPLATE / "agents" / "hokma.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------------------------------- kit: the skill reaches the fund

def test_the_skill_is_installed_in_the_fund_and_mirrored_for_the_engine(env):
    work, config = env / "work", env / "claude-config"
    work.mkdir(exist_ok=True)
    report = sync_kit(TEMPLATE, version="1.0.15", workdir=work)
    assert ".claude/skills/fund-ips/SKILL.md" in report.added
    installed = work / ".claude" / "skills" / "fund-ips" / "SKILL.md"
    assert installed.read_text(encoding="utf-8") == (TEMPLATE / ".claude" / "skills" / "fund-ips" / "SKILL.md").read_text(encoding="utf-8")
    assert "fund-ips" in mirror.sync(work, config)["skills"]
    mirrored = (config / "skills" / "fund-ips" / "SKILL.md").read_text(encoding="utf-8")
    assert mirrored.startswith("---\nname: fund-ips\n") and "description:" in mirrored and "ips_compute" in mirrored


# ---------------------------------------------------------------------------------------------------- 9. text rules

NEW_FILES = ["backend/hokma/ips/__init__.py", "backend/hokma/ips/method.py", "backend/hokma/ips/document.py", "backend/hokma/ips/store.py",
             "kit/template/.claude/skills/fund-ips/SKILL.md", "kit/template/docs/learn/en/26-ips.md", "kit/template/docs/learn/es/26-ips.md",
             "backend/tests/test_ips_method.py", "backend/tests/test_ips_store.py"]
ORIGINS = ["Ofi" + "cina de Control", "SI" + "OM", "Siom" + "Immersive", "mi" + "-ips", "Reto " + "100K", "Mi " + "IPS"]
SPANISH = re.compile("[" + "".join(map(chr, (0xE1, 0xE9, 0xED, 0xF3, 0xFA, 0xF1, 0xFC, 0xC1, 0xC9, 0xCD, 0xD3, 0xDA, 0xD1, 0xBF, 0xA1))) + "]")


@pytest.mark.parametrize("rel", NEW_FILES)
def test_new_files_have_no_long_dash_and_no_origin_names(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert chr(0x2014) not in text
    assert not [o for o in ORIGINS if o in text] and "mi" + "-ips" not in text.lower()
    if "/learn/es/" not in rel:
        assert not SPANISH.search(text)
