"""The libraries of the back office are reached only where the spec allows (SPEC-018 rule 4), and nothing here sends an order to a broker."""
import re
from pathlib import Path

import pytest

BACKOFFICE = Path(__file__).resolve().parents[2] / "hokma" / "backoffice"
CLERK = ("recon", "rollforward", "accruals", "attribution", "weekly", "setup_book", "statements", "exports", "clerk_roles")
LIBRARIES = re.compile(r"^\s*(?:import|from)\s+(openpyxl|financetoolkit|pypdf|PyPDF2|xlrd|pandas|numpy)\b", re.M)
ORDERS = re.compile(r"\b(place_order|replace_order|cancel_order)\b")


@pytest.mark.parametrize("name", [*CLERK, "reports/account_report"])
def test_no_library_import_and_no_order_call(name):
    source = (BACKOFFICE / f"{name}.py").read_text(encoding="utf-8")
    assert LIBRARIES.findall(source) == [], name
    assert ORDERS.findall(source) == [], name
    assert "\u2014" not in source
