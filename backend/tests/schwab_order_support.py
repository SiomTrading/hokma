"""A fake Schwab that can also answer with HEADERS (the `Location` of a 201, REF 3.2), for the order tests. It is
`FakeSchwab` with one more form of answer: `(status, body, headers)`. Everything else (routes, calls, token, wiring of
`hokma.schwab.http`) is the one of schwab_support.py."""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from schwab_support import Call, FakeSchwab

from hokma.schwab import http


class OrderFake(FakeSchwab):
    def __enter__(self) -> "OrderFake":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _serve(self) -> None:
                parts = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                call = Call(self.command, parts.path, dict(urllib.parse.parse_qsl(parts.query)),
                            {k.lower(): v for k, v in self.headers.items()}, body)
                fake.calls.append(call)
                answer = fake._routes.get((self.command, parts.path))
                if callable(answer):
                    answer = answer(call)
                extra: dict = {}
                if answer is None:
                    status, payload = 404, {"errors": [{"status": "404", "title": "Not Found"}]}
                elif isinstance(answer, tuple) and len(answer) == 3:
                    status, payload, extra = answer
                elif isinstance(answer, tuple):
                    status, payload = answer
                else:
                    status, payload = 200, answer
                raw = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Schwab-Client-CorrelId", "test-correl-id")
                for key, value in extra.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        http.set_base(f"http://127.0.0.1:{self._server.server_address[1]}")
        http.set_token_provider(lambda: self.token, lambda: self.token)
        http.reset_limits()
        return self
