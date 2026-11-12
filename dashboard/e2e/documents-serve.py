"""Server for the Documents E2E: the real app on a free port, plus a small local web server that plays a site with
papers. Nothing here reaches the internet: the host name `docs.example.test` resolves to a documentation address
(public for the SSRF guard) and the connector sends every request to the local server instead.

    python e2e/documents-serve.py <dist-folder> <port>

The site: /papers (HTML with three PDF links), /a.pdf (slow, so the "downloading" state can be seen), /b.pdf, and
/locked.pdf (always 403, the case that sends the student to the browser).
Prints one JSON line with the session token once it is listening, then serves until it is killed.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import uvicorn

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "backend" / "tests"))

dist, port = Path(sys.argv[1]), int(sys.argv[2])

import hokma.core.app as app_module  # noqa: E402
from docs_support import make_pdf  # noqa: E402
from hokma.core import session  # noqa: E402
from hokma.core.config import hokma_home  # noqa: E402
from hokma.db import migrate, open_database  # noqa: E402
from hokma.docs import web  # noqa: E402
from hokma.rpc.ws import SESSION_TOKEN  # noqa: E402

HOST = "docs.example.test"
PAGE = (
    "<html><head><title>Test papers</title></head><body>"
    '<a href="/a.pdf">Paper Alpha</a> <a href="/b.pdf">Paper Beta</a> <a href="/locked.pdf">Paper Locked</a>'
    "</body></html>"
).encode()
FILES = {
    "/a.pdf": make_pdf(["Alpha paper about volatility regimes."]),
    "/b.pdf": make_pdf(["Beta paper about order flow."]),
}
SLOW = {"/a.pdf": 1.5}


class Site(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, body, kind):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/papers":
            return self.reply(200, PAGE, "text/html; charset=utf-8")
        if self.path in FILES:
            time.sleep(SLOW.get(self.path, 0))
            return self.reply(200, FILES[self.path], "application/pdf")
        if self.path == "/locked.pdf":
            return self.reply(403, b"forbidden", "text/plain")
        self.reply(404, b"missing", "text/plain")


site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
SITE_PORT = site.server_address[1]
threading.Thread(target=site.serve_forever, daemon=True).start()

web.system_resolver = lambda host, _port: ["93.184.216.34"]                      # a public documentation address
web.default_connector = lambda _scheme, host, _address, _port: web._PinnedHTTP(host, "127.0.0.1", SITE_PORT)
web.HOST_PAUSE_SECONDS = 0.3

app_module.dashboard_dist = lambda: dist
ONCE = session.new_owner_secret()
session.arm_owner_secret_for_tests(ONCE)
migrate()
home = hokma_home()
home.mkdir(parents=True, exist_ok=True)
db = open_database()
with db.write() as u:
    u.profile.set_owner("E2E Owner", "E2E Fund")
    u.profile.complete_setup()
from hokma.projects import service  # noqa: E402

service.ensure_default(db)
db.close()

print(json.dumps({"token": SESSION_TOKEN, "once": ONCE, "home": str(home), "host": HOST}), flush=True)
uvicorn.run(app_module.create_app(port), host="127.0.0.1", port=port, log_level="warning")
