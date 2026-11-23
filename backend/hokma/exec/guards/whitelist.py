"""Only what the desk may trade (SPEC-018: OA-46). The list is `desk_assets`; a desk with no list has no restriction."""
from __future__ import annotations

from hokma.exec.guards import GuardContext, GuardResult, blocked, passed, root_of


def _norm(symbol: str) -> str:
    text = symbol.strip().upper().lstrip("/").removesuffix("=F").removesuffix("=X")
    return text


class SymbolWhitelistGuard:
    name = "whitelist"
    applies_to_closing = False              # closing a position that already exists outside the list is always allowed

    def check(self, ctx: GuardContext) -> GuardResult:
        allowed = {_norm(a) for a in ctx.assets}
        if not allowed:
            return passed(self.name, "no_list")
        outside = sorted({root_of(g.contract_id) for g in ctx.proposal.legs if _norm(root_of(g.contract_id)) not in allowed})
        if outside:
            return blocked(self.name, "symbol_not_allowed", f"{', '.join(outside)} is not in the list of symbols of this desk")
        return passed(self.name)
