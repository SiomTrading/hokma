"""IPS.md: the readable IPS, generated from ips.json (SPEC-024). Never edited by hand: the data sheet at the end holds the answers."""
from __future__ import annotations

import json
from typing import Any

from hokma.ips.method import CLOSING, NO_DATA

BLOCK_TITLES = (
    ("profile", "1. Profile"), ("assets", "2. Assets"), ("debts", "3. Debts"), ("cashflow", "4. Spending, reserve and savings"),
    ("income", "5. Income and withdrawals"), ("conduct", "6. Conduct in the last 12 months"),
    ("prop", "7. Funded accounts and trading"), ("risk_level", "8. Risk level"), ("pockets", "9. Risk pockets"),
    ("objectives", "10. Objectives, horizon and liquidity"), ("restrictions", "11. Restrictions and limits"),
)
TBD = "To be defined"


def _fmt(value: Any, kind: str = "text") -> str:
    if value is None:
        return TBD
    if value == NO_DATA:
        return "No data"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        if kind == "usd":
            return f"{value:,.2f} USD"
        if kind == "pct":
            return f"{round(value, 2):g}%"
        return f"{round(value, 4):g}" if isinstance(value, float) else str(value)
    return str(value).replace("|", "/").replace("\n", " ")


def _table(rows: list[tuple[str, str]], head: tuple[str, str] = ("Item", "Value")) -> list[str]:
    return [f"| {head[0]} | {head[1]} |", "|---|---|", *[f"| {a} | {b} |" for a, b in rows], ""]


def _block(key: str, data: dict) -> list[str]:
    inp, res = data["inputs"], data["results"]
    out: list[str] = []
    if key == "profile":
        p = inp["profile"]
        fx = p["fx"]
        out += _table([("Owner", _fmt(p["owner_name"])), ("Adopted on", _fmt(p["adopted_on"])),
                       ("Next review on", _fmt(p["next_review_on"])), ("Currency", "USD"),
                       ("Exchange rate used", TBD if fx is None else
                        f"{fx['units_per_usd']:g} {fx['currency']} per 1 USD, {fx['source']}, {fx['rate_date']}")])
    elif key == "assets":
        rows = inp["assets"]
        out += [TBD, ""] if rows is None else _table([(f"{a['name'] or TBD} ({_fmt(a['type'])})", _fmt(a["value_usd"], "usd")) for a in rows], ("Asset", "Value"))
        out += _table([("Counted assets", _fmt(res["counted_assets"], "usd")), ("Funded accounts (not counted)", _fmt(res["prop_excluded"], "usd")),
                       ("Net worth", _fmt(res["net_worth"], "usd")), ("Reserve now", _fmt(res["reserve_now"], "usd")),
                       ("Investable capital", _fmt(res["investable_capital"], "usd"))])
    elif key == "debts":
        rows = inp["debts"] or []
        out += _table([("Has debts", _fmt(inp["has_debts"])), ("Total debt", _fmt(res["total_debt"], "usd")),
                       ("Monthly payments", _fmt(res["total_payment"], "usd")), ("Debts that fund trading", _fmt(res["debts_funding_trading"])),
                       ("Check", _fmt(res["debt_status"]))])
        if rows:
            out += ["| Debt | Balance | Monthly payment | Annual rate | Funds trading |", "|---|---|---|---|---|",
                    *[f"| {_fmt(d['name'])} | {_fmt(d['balance_usd'], 'usd')} | {_fmt(d['monthly_payment_usd'], 'usd')} | "
                      f"{_fmt(d['annual_rate_pct'], 'pct')} | {_fmt(d['funds_trading'])} |" for d in rows], ""]
    elif key == "cashflow":
        c = inp["cashflow"]
        out += _table([("Monthly spending", _fmt(c["monthly_spend_usd"], "usd")), ("Reserve target (months)", _fmt(c["reserve_target_months"])),
                       ("Monthly savings, without trading", _fmt(c["monthly_savings_usd"], "usd")),
                       ("Months covered now", _fmt(res["months_covered"])), ("Reserve target", _fmt(res["reserve_target"], "usd")),
                       ("Reserve gap", _fmt(res["reserve_gap"], "usd")), ("Reserve status", _fmt(res["reserve_status"]))])
    elif key == "income":
        i = inp["income"]
        out += _table([("Monthly income, without trading", _fmt(i["monthly_income_usd"], "usd")), ("Stability", _fmt(i["stability"])),
                       ("People who depend on you", _fmt(i["dependents"])), ("Withdrawal habit", _fmt(i["withdrawal_habit"])),
                       ("Trading net, last 12 months", _fmt(res["trading_net_12m"], "usd")), ("Trading income per month", _fmt(res["trading_income_month"], "usd")),
                       ("Total income per month", _fmt(res["total_income_month"], "usd")), ("Share of trading in the income", _fmt(res["trading_share"])),
                       ("Savings used", _fmt(res["savings_used"], "usd")), ("Stability used", _fmt(res["stability_used"]))])
    elif key == "conduct":
        c = inp["conduct"]
        out += _table([("Days with more than one trade", _fmt(c["overtrading_days_pct"], "pct")), ("Stop moved or removed", _fmt(c["stop_moved_pct"], "pct")),
                       ("Size off the rule", _fmt(c["off_rule_size_pct"], "pct")), ("Re-entry after a loss", _fmt(c["revenge_reentry_pct"], "pct"))])
    elif key == "prop":
        h, e = inp["prop"]["history"], inp["prop"]["estimate"]
        out += _table([("Paid in the last 12 months", _fmt(h["paid_12m_usd"], "usd")), ("Evaluations bought", _fmt(h["evaluations"])),
                       ("Accounts that passed", _fmt(h["funded"])), ("Accounts with a payout", _fmt(h["paid_out_accounts"])),
                       ("Payouts received", _fmt(h["payouts_12m_usd"], "usd")), ("Payouts reinvested", _fmt(h["reinvest_pct"], "pct")),
                       ("Own accounts, net of 12 months", _fmt(inp["prop"]["own_accounts_net_12m_usd"], "usd")),
                       ("Estimate: evaluation", _fmt(e["evaluation_usd"], "usd")), ("Estimate: reset", _fmt(e["reset_usd"], "usd")),
                       ("Estimate: activation", _fmt(e["activation_usd"], "usd")), ("Estimate: resets per attempt", _fmt(e["resets_per_attempt"])),
                       ("Cost mode", _fmt(res["cost_mode"])), ("Cost per attempt", _fmt(res["cost_per_attempt"], "usd")),
                       ("Pass rate", _fmt(res["pass_rate"])), ("Payout multiple", _fmt(res["prop_multiple"])),
                       ("Signal", _fmt(res["prop_signal"])), ("Action", _fmt(res["prop_action"])),
                       ("Affordable attempts", _fmt(res["affordable_attempts"])), ("Expanded attempts", _fmt(res["expanded_attempts"]))])
    elif key == "risk_level":
        out += _table([("Capacity factors C1 to C5", ", ".join(_fmt(res[f"c{i}"]) for i in range(1, 6))),
                       ("Capacity score and level", f"{_fmt(res['capacity_score'])} of 10, {_fmt(res['capacity_level'])}"),
                       ("Willingness factors W1 to W5", ", ".join(_fmt(res[f"w{i}"]) for i in range(1, 6))),
                       ("Willingness score and level", f"{_fmt(res['willingness_score'])} of 10, {_fmt(res['willingness_level'])}"),
                       ("Risk level that governs (the lower)", _fmt(res["risk_level"])), ("Months to recover a loss", _fmt(res["recovery_months"])),
                       ("Loss method", _fmt(res["loss_method"])), ("Loss capacity", _fmt(res["loss_capacity"], "usd"))])
    elif key == "pockets":
        p = inp["pockets"]
        out += _table([("Operating pocket, max loss", _fmt(p["operating_max_loss_usd"], "usd")), ("Operating pocket, from payouts", _fmt(p["operating_from_payouts_usd"], "usd")),
                       ("Speculative pocket, max loss", _fmt(p["speculative_max_loss_usd"], "usd")), ("Speculative pocket, from payouts", _fmt(p["speculative_from_payouts_usd"], "usd")),
                       ("House loss, operating", _fmt(res["house_loss_operating"], "usd")), ("House loss, speculative", _fmt(res["house_loss_speculative"], "usd")),
                       ("Funding budget", _fmt(res["funding_budget"], "usd"))])
    elif key == "objectives":
        o = inp["objectives"]
        out += _table([("Goal", _fmt(o["goal"])), ("Target amount", _fmt(o["target_amount_usd"], "usd")), ("Horizon (years)", _fmt(o["horizon_years"])),
                       ("Desired return", _fmt(o["desired_return_pct"], "pct")), ("Liquidity needs", _fmt(o["liquidity_needs"]))])
    elif key == "restrictions":
        r = inp["restrictions"]
        out += [f"**Allowed:** {', '.join(r['allowed']) if r['allowed'] else TBD}", "", f"**Forbidden:** {', '.join(r['forbidden']) if r['forbidden'] else TBD}", ""]
    return out


def render(data: dict) -> str:
    """The IPS.md text of an ips.json document."""
    lim = data["limits"]
    status = "Adopted" if data["status"] == "adopted" else "Draft, not adopted yet"
    lines = ["# Investment Policy Statement", "",
             f"Version {data['version']} | {status} | Updated {str(data['updated_at'])[:10]} | Method: Hokma risk capacity method | All amounts in USD", ""]
    for key, title in BLOCK_TITLES:
        lines += [f"## {title}", "", *_block(key, data)]
    lines += ["## Limits of the fund", "", "All limits are measured in the house's money.", ""]
    lines += _table([("Investable capital", _fmt(lim["investable_capital_usd"], "usd")), ("Max loss", _fmt(lim["max_loss_usd"], "usd")),
                     ("Capital floor", _fmt(lim["capital_floor_usd"], "usd")), ("Max drawdown", _fmt(lim["max_drawdown_pct"], "pct")),
                     ("Max risk per trade", _fmt(lim["max_risk_per_trade_pct"], "pct")), ("Max open positions", _fmt(lim["max_open_positions"])),
                     ("House total of the pockets", _fmt(lim["pockets_house_total_usd"], "usd"))])
    if lim["desk_allocation"]:
        lines += ["| Desk | Capital | House limit | Funding budget | Account cost | Max accounts | Firm drawdown per account |", "|---|---|---|---|---|---|---|"]
        for d in lim["desk_allocation"]:
            lines.append(f"| {_fmt(d['desk'])} | {_fmt(d['capital_source'])} | {_fmt(d['house_limit_usd'], 'usd')} | {_fmt(d['funding_budget_usd'], 'usd')} | "
                         f"{_fmt(d['avg_account_cost_usd'], 'usd')} | {_fmt(d['max_accounts'])} | {_fmt(d['prop_max_drawdown_per_account_usd'], 'usd')} |")
        lines.append("")
    lines += ["## Checks", "", "| Check | Status | Numbers |", "|---|---|---|",
              *[f"| {c['id']} | {c['status']} | {_fmt(c['numbers'])} |" for c in data["checks"]], ""]
    lines += ["## Still to be defined", "", ", ".join(data["missing"]) if data["missing"] else "Nothing.", ""]
    if data["history"]:
        lines += ["## History", "", "| Version | Date | Changed | Note |", "|---|---|---|---|",
                  *[f"| {h['version']} | {h['date']} | {_fmt(', '.join(h['changed']))} | {_fmt(h['note'] or None)} |" for h in data["history"]], ""]
    lines += ["---", "", CLOSING, "", "## Data sheet (do not edit by hand)", "", "```json",
              json.dumps(data["inputs"], indent=1, ensure_ascii=False), "```", ""]
    return "\n".join(lines)
