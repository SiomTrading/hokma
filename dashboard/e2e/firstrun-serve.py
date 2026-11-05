"""Server for the first-run E2E: the real app on a free port with an empty working folder, no profile and no setup
done, exactly what a person gets the first time. A fake `claude` first on PATH is set by the runner.

    python e2e/firstrun-serve.py <dist-folder> <port>

Prints one JSON line with the session token once it is ready, then serves until it is killed.
"""
import json
import sys
from pathlib import Path

import uvicorn

dist, port = Path(sys.argv[1]), int(sys.argv[2])

import hokma.core.app as app_module  # noqa: E402
from hokma.core.config import hokma_home  # noqa: E402
from hokma.rpc.ws import SESSION_TOKEN  # noqa: E402

app_module.dashboard_dist = lambda: dist
home = hokma_home()
home.mkdir(parents=True, exist_ok=True)

print(json.dumps({"token": SESSION_TOKEN, "home": str(home)}), flush=True)
uvicorn.run(app_module.create_app(port), host="127.0.0.1", port=port, log_level="warning")
