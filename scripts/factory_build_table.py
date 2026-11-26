"""Builds the table of strategies and edge ideas of the factory:  python scripts/factory_build_table.py [--out FILE] [--min-t 3]

Reads every YAML file under kit/template/service/strategies/ (the rewritten library, the ideas and the registered entries) and
backend/hokma/factory/data/literature_edges.csv, and prints two markdown tables: the strategies by folder and the published edges by
asset class with their gross Sharpe ratio, t statistic and years tested. The output is the same for the same files: sorted, no dates,
no network. Nothing is written unless --out is given.
"""
import argparse
import csv
import html
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
STRATEGIES = Path("kit/template/service/strategies")
EDGES = Path("backend/hokma/factory/data/literature_edges.csv")
CLASS_ORDER = ("equities", "bonds", "commodities", "currencies", "crypto", "derivatives", "multi_asset")


def cell(value: object) -> str:
    """One markdown table cell: lists joined, markup escaped, pipes and line breaks removed."""
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value)
    return html.escape(str(value if value is not None else "")).replace("|", "/").replace("\n", " ").strip()


def entries(path: Path) -> list[dict]:
    """The mappings declared by one YAML file: a single mapping or a list of mappings. Anything else is ignored."""
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    items = loaded if isinstance(loaded, list) else [loaded]
    return [item for item in items if isinstance(item, dict)]


def strategy_rows(root: Path) -> list[tuple[str, str, str, str, str]]:
    """(folder, id, lines, asset class, what it is) for every entry under the strategies folder, sorted."""
    base = root / STRATEGIES
    rows = []
    for path in sorted(base.rglob("*.yaml")):
        folder = path.parent.relative_to(base).as_posix()
        for item in entries(path):
            entry_id = item.get("id") or path.stem
            lines = item.get("lines") if item.get("lines") is not None else item.get("line")
            what = item.get("name") or item.get("description") or ""
            rows.append((folder if folder != "." else "registered", str(entry_id), cell(lines), cell(item.get("asset_class", "")), cell(what)))
    return sorted(rows)


def edge_rows(root: Path, min_t: float) -> list[dict]:
    with open(root / EDGES, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return [r for r in rows if float(r["t_stat"]) >= min_t]


def build_table(root: Path = ROOT, min_t: float = 0.0) -> str:
    out = ["# Strategies of the factory", "", "| Folder | Id | Lines | Asset class | What it is |", "|---|---|---|---|---|"]
    for folder, entry_id, lines, asset, what in strategy_rows(root):
        out.append(f"| {cell(folder)} | {cell(entry_id)} | {lines} | {asset} | {what} |")
    out += ["", f"# Published edge ideas (t statistic at or above {min_t:g})", "",
            "The Sharpe ratio is the gross figure the source reports, before any trading cost. No net value is shown or derived.", ""]
    edges = edge_rows(root, min_t)
    for asset in CLASS_ORDER:
        chosen = sorted((r for r in edges if r["asset_class"] == asset), key=lambda r: (-float(r["t_stat"]), r["edge_id"]))
        if not chosen:
            continue
        out += [f"## {asset}", "", "| Idea | Sharpe (gross) | t statistic | Years tested | Ref |", "|---|---|---|---|---|"]
        out += [f"| {cell(r['idea'])} | {cell(r['sharpe_reported'])} {cell(r['sharpe_basis'])} | {cell(r['t_stat'])} | {cell(r['years_tested'])} | {cell(r['ref'])} |"
                for r in chosen]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", help="write the tables to this file instead of printing them")
    parser.add_argument("--min-t", type=float, default=0.0, help="keep the edge ideas with a t statistic at or above this value")
    args = parser.parse_args(argv)
    table = build_table(ROOT, args.min_t)
    if args.out:
        Path(args.out).write_text(table, encoding="utf-8")
    else:
        sys.stdout.write(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
