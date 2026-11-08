"""Server for the permissions E2E: the real app on a free port, an installation that came from an earlier version
(setup done, mode "ask", notice not seen), one project with a file already in the Hokma trash and one restore point
of a project whose file disappeared afterwards. A fake `claude` first on PATH is set by the runner.

    python e2e/permissions-serve.py <dist-folder> <port>

Prints one JSON line with the session token once it is listening, then serves until it is killed.
"""
import json
import os
import sys
from pathlib import Path

import uvicorn

dist, port = Path(sys.argv[1]), int(sys.argv[2])

import hokma.core.app as app_module  # noqa: E402
from hokma.core.config import hokma_home  # noqa: E402
from hokma.db import migrate, open_database  # noqa: E402
from hokma.kit.settings import protected_paths  # noqa: E402
from hokma.permissions import checkpoints, trash  # noqa: E402
from hokma.projects import service  # noqa: E402
from hokma.core import session  # noqa: E402
from hokma.rpc.ws import SESSION_TOKEN  # noqa: E402

app_module.dashboard_dist = lambda: dist
ONCE = session.new_owner_secret()                       # the one-time code of the start link, for the tab
session.arm_owner_secret_for_tests(ONCE)
migrate()
home = hokma_home()
home.mkdir(parents=True, exist_ok=True)
db = open_database()
with db.write() as u:
    u.profile.set_owner("E2E Owner", "E2E Fund")
    u.profile.complete_setup()
service.ensure_default(db)
project = service.create_project("Strategy", "other", slug="strategy", db=db)
folder = Path(project["path"])
(folder / "notas.md").write_text("original notes\n", encoding="utf-8")
(folder / "informes").mkdir(exist_ok=True)
(folder / "informes" / "viejo.md").write_text("old report\n", encoding="utf-8")

# what an agent did instead of deleting: it moved notas.md to the trash
trash.move(["notas.md"], "the owner asked to clean up", db=db, roots=[str(folder)],
           protect=[str(p) for p in protected_paths()], cwd=str(folder), project_id=project["id"])
# a restore point, and a file that disappeared after it (a way the list of protections does not see)
checkpoints.take(folder)
os.remove(folder / "informes" / "viejo.md")
db.close()

print(json.dumps({"token": SESSION_TOKEN, "once": ONCE, "home": str(home), "project": str(folder), "project_id": project["id"]}), flush=True)
uvicorn.run(app_module.create_app(port), host="127.0.0.1", port=port, log_level="warning")
