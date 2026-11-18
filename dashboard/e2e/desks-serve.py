"""Server for the money desks E2E: the real app on a free port with the real kit installed in the working folder (the six
desk agents are seeded turned off), a migrated database, an owner, recorded market answers (no network) and a fake
`claude` first on PATH (all set by the runner).

    python e2e/desks-serve.py <dist-folder> <port>

Prints one JSON line with the session token once it is ready, then serves until it is killed.
"""
import json
import os
import sys
from pathlib import Path

import uvicorn

dist, port = Path(sys.argv[1]), int(sys.argv[2])
HERE = Path(__file__).resolve().parent
os.environ["HOKMA_MARKET_REPLAY_DIR"] = str(HERE / "market-replay")

import hokma.core.app as app_module  # noqa: E402
from hokma.core.config import hokma_home  # noqa: E402
from hokma.core import session  # noqa: E402
from hokma.db import migrate, open_database  # noqa: E402
from hokma.kit.sync import sync_kit  # noqa: E402
from hokma.rpc.ws import SESSION_TOKEN  # noqa: E402

app_module.dashboard_dist = lambda: dist
ONCE = session.new_owner_secret()                       # the one-time code of the start link, for the tab
session.arm_owner_secret_for_tests(ONCE)
template = HERE.parents[1] / "kit" / "template"
home = hokma_home()
home.mkdir(parents=True, exist_ok=True)
sync_kit(template, version="e2e", workdir=home, values={"owner_name": "E2E Owner", "fund_name": "E2E Fund"})
migrate()
db = open_database()
with db.write() as u:
    u.profile.set_owner("E2E Owner", "E2E Fund")
    u.profile.complete_setup()
db.close()

print(json.dumps({"token": SESSION_TOKEN, "once": ONCE, "home": str(home)}), flush=True)
uvicorn.run(app_module.create_app(port), host="127.0.0.1", port=port, log_level="warning")
