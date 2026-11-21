"""The repo and the release carry methods, never the data of the owner or of his accounts (SPEC-015, fixtures rule): every
Schwab fixture and every Schwab test is made-up data. This test fails on a forbidden string (a person, the app of the
owner, a real mailbox) and on an account number or hash that is not one of the made-up ones."""
import json
import re
from pathlib import Path

import pytest

TESTS = Path(__file__).parent
DASHBOARD = TESTS.parents[1] / "dashboard" / "src"
SELF = Path(__file__).name

# written in pieces so that this file does not hold the words it forbids
FORBIDDEN = [re.compile(p, re.I) for p in (
    "cris" + "tian", "iba" + "[n\u00f1]ez", "hokma " + "cris", "jen" + "ny", "zab" + "diel", "yarel",
    r"[\w.+-]+@siom" + r"trading\.com")]
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
OK_DOMAINS = ("example.test", "example.com", "example.org", "example.invalid")
MADE_UP_ACCOUNTS = {"12345678", "87654321"}
MADE_UP_HASHES = {"A1B2C3D4E5F60718293A4B5C6D7E8F90", "F0E1D2C3B4A5968778695A4B3C2D1E0F"}


def scanned() -> list[Path]:
    files = [p for p in (TESTS / "schwab_fixtures").glob("*.json")]
    files += [p for p in TESTS.glob("test_schwab_*.py") if p.name != SELF]
    files += [TESTS / "schwab_support.py"]
    if DASHBOARD.exists():
        files += [p for p in (DASHBOARD / "test").glob("*") if p.is_file()]
        for name in ("schwab*.test.*", "providerGuide.test.*", "connections.test.*", "singlePlace.test.*"):
            files += list((DASHBOARD / "screens" / "connections").glob(name))
        files += [p for p in (DASHBOARD / "screens" / "chart" / "options").glob("*.test.*")]
    return files


def test_there_is_something_to_scan():
    names = {p.name for p in scanned()}
    assert {"user_preference.json", "accounts.json", "schwab_support.py", "test_schwab_secrets.py"} <= names


@pytest.mark.parametrize("path", scanned(), ids=lambda p: p.name)
def test_no_forbidden_string_in_a_fixture_or_a_schwab_test(path):
    text = path.read_text(encoding="utf-8")
    for pattern in FORBIDDEN:
        assert not pattern.search(text), f"{path.name} matches {pattern.pattern}"
    for address in EMAIL.findall(text):
        domain = address.rsplit("@", 1)[1].lower()
        assert domain in OK_DOMAINS, f"{path.name} has the address {address}"


def walk(node, where=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, f"{where}.{key}")
            if key == "accountNumber":
                yield "account", where, str(value)
            if key == "hashValue":
                yield "hash", where, str(value)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from walk(item, f"{where}[{index}]")


@pytest.mark.parametrize("path", sorted((TESTS / "schwab_fixtures").glob("*.json")), ids=lambda p: p.name)
def test_every_account_number_and_hash_of_a_fixture_is_a_made_up_one(path):
    for kind, where, value in (w for w in walk(json.loads(path.read_text(encoding="utf-8"))) if isinstance(w, tuple)):
        allowed = MADE_UP_ACCOUNTS if kind == "account" else MADE_UP_HASHES
        assert value in allowed, f"{path.name}{where} has {kind} {value[:4]}..."
