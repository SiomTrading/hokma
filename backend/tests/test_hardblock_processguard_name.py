"""Naming the runtime mark of the order modules (hokma.core.processguard) is the same block as importing Hokma."""
import pytest
from hardblock_support import box  # noqa: F401  (fixture)

from hokma.permissions.broker import BrokerCtx, check


@pytest.fixture
def bctx(box):
    return BrokerCtx(data=str(box.data), roots=[str(box.work)], protect=[str(box.data)], conversation_id="7")


@pytest.mark.parametrize("command", [
    "python -c \"import processguard; processguard._role = 'server'\"",
    "python -c \"print(__import__('sys').modules['hokma.core.processguard'])\"",
    "echo processguard._role > /dev/null",
])
def test_naming_processguard_is_real_money(box, bctx, command):
    verdict = check("Bash", {"command": command}, cwd=str(box.work), ctx=bctx)
    assert verdict.allowed is False and verdict.rule == "real_money"


def test_an_unrelated_word_is_free(box, bctx):
    assert check("Bash", {"command": "echo guard process"}, cwd=str(box.work), ctx=bctx).allowed is True
