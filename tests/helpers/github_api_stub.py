"""A local stand-in for the GitHub REST endpoints the agent scripts call.

``scripts/agents/post_comment.py`` speaks plain REST over urllib, so tests
point ``GITHUB_API_URL`` at this server instead of stubbing a ``gh`` binary.
It serves the PR-head read (``GET .../pulls/{n}``), the issue-comment list
and single-comment body reads the poster makes, and records every write
(PATCH/POST) as ``"METHOD /path"`` in ``.writes``.

``heads`` is the sequence of SHAs the pulls endpoint returns, one per read:
while more than one remains the next read pops the front, and the last value
repeats - so ``[HEAD, OTHER_HEAD]`` models a head that moved between the two
reads the poster makes.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


class GitHubAPIStub:
    def __init__(self, *, heads: list[str], comments: list[dict] | None = None):
        assert heads, "GitHubAPIStub needs at least one head SHA to serve"
        self._heads = list(heads)
        self.comments = comments or []
        self.writes: list[str] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # keep the test output clean
                pass

            def _json(self, code: int, obj) -> None:
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if "/pulls/" in path:
                    sha = (
                        stub._heads.pop(0)
                        if len(stub._heads) > 1
                        else stub._heads[0]
                    )
                    self._json(200, {"head": {"sha": sha}})
                elif "/issues/comments/" in path:
                    cid = path.rsplit("/", 1)[-1]
                    body = next(
                        (c["body"] for c in stub.comments if str(c["id"]) == cid), ""
                    )
                    self._json(200, {"body": body})
                elif path.endswith("/comments"):
                    self._json(200, stub.comments)
                else:
                    self._json(404, {"message": "not found"})

            def _write(self, code: int) -> None:
                stub.writes.append(f"{self.command} {urlparse(self.path).path}")
                self._json(code, {})

            def do_PATCH(self) -> None:
                self._write(200)

            def do_POST(self) -> None:
                self._write(201)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"
