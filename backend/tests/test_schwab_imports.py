"""The Schwab door and the market package import each other lazily (SPEC-015): whichever is imported first must work,
in a fresh interpreter, with no import order trick."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).parents[1]
FIRST = ["hokma.schwab.http", "hokma.schwab.endpoints", "hokma.schwab.reader", "hokma.schwab.routes",
         "hokma.market.options", "hokma.market", "hokma.schwab.analytics", "schwab_support"]


@pytest.mark.parametrize("module", FIRST)
def test_each_module_imports_first_in_a_fresh_interpreter(module):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(BACKEND), str(Path(__file__).parent)])}
    stand_in = "from hokma.core import processguard; processguard._role = processguard.SERVER; "    # the server's mark (core/processguard.py)
    done = subprocess.run([sys.executable, "-c", stand_in + f"import {module}; import hokma.market; import hokma.schwab.reader"],
                          cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-800:]
