"""SPEC-015 18.7 round 3 (Vera). a) `python -m hokma.*` in every spelling (glued value, quotes, combined flags) is real money and
only `hokma doctor` stays free; b) code assembled at run time is the SAME block (rule `real_money`, allowed=False) with its own text
in Spanish and English (reason `dynamic_code`); c) a documentation URL is not code and not a Hokma path."""
import json
from pathlib import Path

import pytest
from hardblock_support import box  # noqa: F401  (fixture)

from hokma.kit import hardblock
from hokma.kit.hardblock import Block, check_python_module
from hokma.permissions.broker import BrokerCtx, check

CATALOG = Path(hardblock.__file__).resolve().parents[1] / "catalog"


@pytest.fixture
def bctx(box):
    return BrokerCtx(data=str(box.data), roots=[str(box.work)], protect=[str(box.data)], conversation_id="7")


def catalog(lang: str, key: str) -> str:
    return json.loads((CATALOG / f"{lang}.json").read_text(encoding="utf-8"))[key]


# ------------------------------------------------------------------------------------------------ a) python -m hokma.*

MODULE_BLOCKED = [
    "python -Im hokma.desks", "python -Im hokma.kit.hokma_tools", "python -um hokma.db", "python -mhokma.db",
    'python -m"hokma.db"', "python -m 'hokma.db'", 'python -m "hokma.db"', 'python -Im"hokma.db"', 'python -m"hokma"',
    "python -m hokma", "python -m hokma.core.config", "python -m hokma doctor.x", "python -Im hokma.broker", "python -mhokma.schwab",
]
MODULE_FREE = ["python -m hokma doctor", "python -Im hokma doctor", "python -mhokma doctor", 'python -m"hokma" doctor',
               "python -m hokma doctor --verbose", "python -m hokma.doctor", "python -m hokma.factory.rule_check x", "python -mjson.tool", 'python -m"json.tool"', "python -m hokma_extras"]


@pytest.mark.parametrize("command", MODULE_BLOCKED)
def test_python_dash_m_hokma_submodule_is_real_money(box, bctx, command):
    assert box.bash(command) == "real_money", command
    verdict = check("Bash", {"command": command}, cwd=str(box.work), ctx=bctx)
    assert (verdict.allowed, verdict.rule) == (False, "real_money"), command


@pytest.mark.parametrize("command", MODULE_FREE)
def test_python_dash_m_hokma_doctor_and_other_modules_stay_free(box, command):
    assert box.bash(command) is None, command


@pytest.mark.parametrize("args", [['"hokma.db"'], ["'hokma.db'"], ['"hokma"', "x"], ["hokma.desks"]])
def test_check_python_module_ignores_quotes(args):
    with pytest.raises(Block):
        check_python_module(args)


@pytest.mark.parametrize("args", [['"hokma"', "doctor"], ["hokma", '"doctor"'], ["json.tool"]])
def test_check_python_module_free_forms(args):
    check_python_module(args)


# ------------------------------------------------------------------------------------------------ b) dynamic code text

DYNAMIC = [
    "python -c \"exec('imp' + 'ort os')\"", "python -c \"eval(compile(src, 'x', 'exec'))\"",
    "python -c \"import importlib; importlib.import_module('ho' + 'kma.broker')\"",
    "python -c \"__import__(name)\"", "python -c \"import runpy; runpy.run_module(m)\"",
    "python -c \"from importlib import import_module as im\"", "python -c \"print(__builtins__)\"",
]
NAMES_HOKMA = ["python -c \"import importlib; importlib.import_module('hokma.broker')\"", "python -Im hokma.broker"]


@pytest.mark.parametrize("command", DYNAMIC)
def test_dynamic_code_is_the_same_block_with_its_own_reason(box, bctx, command):
    block = hardblock.evaluate({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(box.work)}, box.ctx())
    assert block is not None and (block.rule, block.reason) == ("real_money", "dynamic_code"), command
    assert box.bash(command) == "real_money"
    verdict = check("Bash", {"command": command}, cwd=str(box.work), ctx=bctx)
    assert (verdict.allowed, verdict.rule, verdict.reason) == (False, "real_money", "dynamic_code"), command


@pytest.mark.parametrize("command", NAMES_HOKMA)
def test_naming_hokma_keeps_the_real_money_text(box, command):
    block = hardblock.evaluate({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(box.work)}, box.ctx())
    assert block is not None and (block.rule, block.reason) == ("real_money", "hokma_money"), command          # 1.2.9: its own text, so the agent knows which module is the one that touches money


@pytest.mark.parametrize("lang", ["en", "es"])
def test_hook_prints_the_dynamic_code_text_in_the_language(box, lang):
    done = box.run_hook({"tool_name": "Bash", "tool_input": {"command": "python -c \"exec('a' + 'b')\""}, "cwd": str(box.work)},
                        {"HOKMA_LANG": lang})
    assert done.returncode == 2
    assert "HOKMA_BLOCKED real_money" in done.stderr                      # the rule is the same one
    assert catalog(lang, "hardblock.dynamic_code").split("(")[0].strip() in done.stderr
    assert catalog(lang, "hardblock.hint.dynamic_code") in done.stderr
    assert catalog(lang, "hardblock.hint.real_money") not in done.stderr
    log = [json.loads(x) for x in (box.data / "guard" / "blocks.jsonl").read_text().splitlines()][-1]
    assert (log["rule"], log["reason"]) == ("real_money", "dynamic_code")


def test_dynamic_code_texts_differ_by_language_and_name_the_way_out():
    en, es = catalog("en", "hardblock.hint.dynamic_code"), catalog("es", "hardblock.hint.dynamic_code")
    assert en != es and "literal import" in en and "owner" in en
    assert "{target}" in catalog("en", "hardblock.dynamic_code") and "{target}" in catalog("es", "hardblock.dynamic_code")
    assert "assembled at run time" in catalog("en", "hardblock.dynamic_code")


def test_a_plain_real_money_block_keeps_its_own_text(box):
    done = box.run_hook({"tool_name": "Bash", "tool_input": {"command": "python -Im hokma.broker"}, "cwd": str(box.work)})
    assert catalog("en", "hardblock.hint.hokma_money") in done.stderr and "real money, keys or the server" in done.stderr
    log = json.loads((box.data / "guard" / "blocks.jsonl").read_text().splitlines()[-1])
    assert log["reason"] == "hokma_money"


# ------------------------------------------------------------------------------------------------ c) documentation URLs

DOC_URLS = ["https://docs.python.org/3/library/importlib.html", "https://docs.python.org/3/library/functions.html",
            "https://docs.python.org/3/library/functions.html#eval", "https://docs.python.org/3/library/functions.html#exec",
            "https://docs.python.org/3/library/runpy.html#runpy.run_module", "https://pypi.org/project/hokma-extras/",
            "https://pypi.org/project/schwab-py/"]


@pytest.mark.parametrize("tool", ["web_fetch", "WebFetch", "webfetch"])
@pytest.mark.parametrize("url", DOC_URLS)
def test_documentation_urls_are_allowed(box, bctx, tool, url):
    verdict = check(tool, {"url": url}, cwd=str(box.work), ctx=bctx)
    assert verdict.allowed, (tool, url, verdict)
    assert box.hit("WebFetch", url=url) is None


@pytest.mark.parametrize("tool", ["web_fetch", "WebFetch"])
@pytest.mark.parametrize("url", ["https://api.schwabapi.com/trader/v1/accounts", "https://example.com/?m=hokma.broker",
                                 "https://example.com/?k=hokma-session"])
def test_bad_urls_stay_blocked(box, bctx, tool, url):
    verdict = check(tool, {"url": url}, cwd=str(box.work), ctx=bctx)
    assert not verdict.allowed and verdict.rule in ("real_money", "secrets"), (tool, url, verdict)
