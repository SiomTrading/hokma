"""Helpers of the tests to put accounts in a desk the way the app does (SPEC-027): the first account of a desk is also the account of its
principal strategy. They replace the one-account-per-desk calls of the repository that were removed in piece 027e."""
from __future__ import annotations

from hokma.broker import desk_accounts


def link_principal(u, desk_id: int, account_id: int) -> None:
    """Put the account in the desk. When it is the first one, the principal strategy trades through it."""
    desk_accounts.link_account(u, desk_id, u.broker.get_account(account_id), "owner")


def unlink_all(u, desk_id: int) -> None:
    """A desk with no accounts and no strategy bound to one (what a desk that only analyzes looks like)."""
    for key in list(u.broker.strategy_bindings(desk_id)):
        u.broker.unbind_strategy(desk_id, key)
    for account in u.broker.accounts_of_desk(desk_id):
        u.broker.unlink(desk_id, account.id)
