from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping


@dataclass(frozen=True, slots=True)
class APIConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    token: str = ""


class LocalControlAPI:
    """Small localhost-only control API used by Stream Deck and local tools."""

    def __init__(
        self,
        config: APIConfig,
        *,
        status: Callable[[], Mapping[str, Any]],
        action: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None],
    ) -> None:
        self.config = config
        self._status = status
        self._action = action
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def bound_port(self) -> int:
        return int(self._server.server_address[1]) if self._server is not None else int(self.config.port)

    def start(self) -> None:
        if not self.config.enabled or self._server is not None:
            return
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "StreamStateRouter/2.0"

            def log_message(self, _format: str, *_args) -> None:
                return

            def _authorized(self) -> bool:
                if not outer.config.token:
                    return True
                auth = self.headers.get("Authorization", "")
                return auth == f"Bearer {outer.config.token}"

            def _reply(self, status: int, payload: Mapping[str, Any]) -> None:
                data = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                if not self._authorized():
                    self._reply(401, {"ok": False, "error": "unauthorized"})
                    return
                if self.path.rstrip("/") == "/status":
                    try:
                        self._reply(200, {"ok": True, **dict(outer._status())})
                    except Exception as exc:
                        self._reply(500, {"ok": False, "error": str(exc)})
                    return
                self._reply(404, {"ok": False, "error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    self._reply(401, {"ok": False, "error": "unauthorized"})
                    return
                length = int(self.headers.get("Content-Length", "0") or 0)
                try:
                    raw = self.rfile.read(length) if length else b"{}"
                    payload = json.loads(raw.decode("utf-8"))
                    if not isinstance(payload, dict):
                        payload = {}
                except Exception:
                    self._reply(400, {"ok": False, "error": "invalid_json"})
                    return
                action_name = self.path.strip("/").replace("/", ".")
                try:
                    result = outer._action(action_name, payload) or {}
                    self._reply(200, {"ok": True, **dict(result)})
                except ValueError as exc:
                    self._reply(400, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    self._reply(500, {"ok": False, "error": str(exc)})

        self._server = ThreadingHTTPServer((self.config.host, self.config.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="SSR-LocalAPI",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        server = self._server
        self._server = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        thread = self._thread
        self._thread = None
        if thread and thread.is_alive():
            thread.join(timeout=1.5)
