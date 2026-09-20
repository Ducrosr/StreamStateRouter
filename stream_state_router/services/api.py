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
    max_body_bytes: int = 65536


class LocalControlAPI:
    """Small localhost-only control API used by Stream Deck and local tools."""

    def __init__(
        self,
        config: APIConfig,
        *,
        status: Callable[[], Mapping[str, Any]],
        action: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None],
        request_status: Callable[[str], Mapping[str, Any] | None] | None = None,
    ) -> None:
        self.config = config
        self._status = status
        self._action = action
        self._request_status = request_status
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

            def _browser_request_allowed(self) -> bool:
                # SSR has no browser-facing CORS API. Reject Origin-bearing
                # requests explicitly instead of relying on browser defaults.
                return not bool(self.headers.get("Origin"))

            def _reply(self, status: int, payload: Mapping[str, Any]) -> None:
                data = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _preflight(self) -> bool:
                if not self._browser_request_allowed():
                    self._reply(403, {"ok": False, "error": "browser_origin_not_allowed"})
                    return False
                if not self._authorized():
                    self._reply(401, {"ok": False, "error": "unauthorized"})
                    return False
                return True

            def do_OPTIONS(self) -> None:  # noqa: N802
                self._reply(403, {"ok": False, "error": "browser_origin_not_allowed"})

            def do_GET(self) -> None:  # noqa: N802
                if not self._preflight():
                    return
                path = self.path.split("?", 1)[0].rstrip("/")
                if path == "/status":
                    try:
                        self._reply(200, {"ok": True, **dict(outer._status())})
                    except Exception as exc:
                        self._reply(500, {"ok": False, "error": str(exc)})
                    return
                if path.startswith("/requests/"):
                    request_id = path[len("/requests/") :].strip()
                    if not request_id or outer._request_status is None:
                        self._reply(404, {"ok": False, "error": "not_found"})
                        return
                    row = outer._request_status(request_id)
                    if row is None:
                        self._reply(404, {"ok": False, "error": "request_not_found"})
                        return
                    self._reply(200, {"ok": True, **dict(row)})
                    return
                self._reply(404, {"ok": False, "error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                if not self._preflight():
                    return
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
                if content_type != "application/json":
                    self._reply(415, {"ok": False, "error": "content_type_must_be_application_json"})
                    return
                try:
                    raw_length = self.headers.get("Content-Length")
                    if raw_length is None:
                        raise ValueError("missing_content_length")
                    length = int(raw_length)
                    if length < 0:
                        raise ValueError("invalid_content_length")
                except (TypeError, ValueError, OverflowError) as exc:
                    self._reply(400, {"ok": False, "error": str(exc) or "invalid_content_length"})
                    return
                if length > max(0, int(outer.config.max_body_bytes)):
                    self._reply(413, {"ok": False, "error": "request_body_too_large"})
                    return
                try:
                    raw = self.rfile.read(length) if length else b"{}"
                    payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    self._reply(400, {"ok": False, "error": "invalid_json"})
                    return
                if not isinstance(payload, dict):
                    self._reply(400, {"ok": False, "error": "json_object_required"})
                    return
                action_name = self.path.split("?", 1)[0].strip("/").replace("/", ".")
                try:
                    result = outer._action(action_name, payload) or {}
                    response = {"ok": True, **dict(result)}
                    code = 202 if response.get("status") == "accepted" else 200
                    self._reply(code, response)
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
