"""E2E: a connector the agent can modify needs the accept word on top of the id; a malformed one is refused at request time."""
import json
import os
import sys

import pytest

from helpers import t
from ptyutil import run_tty
from test_installer_e2e_update import Box, run

pytestmark = pytest.mark.skipif(os.environ.get("HOKMA_E2E") != "1" or sys.platform == "win32",
                                reason="set HOKMA_E2E=1 (Linux/macOS, network) to run")


def test_risky_connector_needs_the_accept_word_and_malformed_ones_are_refused(tmp_path):
    box = Box(tmp_path)
    inbox = box.home / ".local" / "share" / "hokma-requests"
    for bad in ('{"command": "npx", "args": "not-a-list"}', '{"type": "http", "url": "http://x.test"}'):
        r = run([box.hokma, "ext", "request", "mcp", "bad", bad], box.env)
        assert r.returncode == 1 and t("cli.tag.fail") in r.stderr, (bad, r.stderr)
    assert not list(inbox.glob("*.json"))

    r = run([box.hokma, "ext", "request", "mcp", "local", '{"command": "./payload.sh", "args": ["-e", "x"]}'], box.env)
    assert r.returncode == 0, r.stderr                       # the agent may ask for anything well-formed
    rid = next(f.stem for f in inbox.glob("*.json"))

    code, out = run_tty([box.hokma, "ext", "approve", rid], box.env, text=f"{rid}\n")       # id only: not enough
    assert code == 1 and t("req.risk_warning") in out, out
    assert json.loads((box.data / "extensions.json").read_text() if (box.data / "extensions.json").exists()
                      else '{"mcpServers": {}}')["mcpServers"] == {}

    code, out = run_tty([box.hokma, "ext", "approve", rid], box.env, text=f"{rid}\n{t('cli.ext.accept_word')}\n")
    assert code == 0, out
    assert "./payload.sh" in json.loads((box.data / "extensions.json").read_text())["mcpServers"]["local"]["command"]
    p = run([box.hokma, "prepare-claude"], box.env)
    assert p.returncode == 0
    mcp = json.loads(open(json.loads(p.stdout)["args"][-1]).read())["mcpServers"]["local"]
    assert "./payload.sh" not in json.dumps(mcp), "the raw relative command must not reach Claude"
