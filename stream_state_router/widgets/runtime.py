from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import queue
import threading
import time
from typing import Any, Mapping
from urllib.parse import quote, unquote, urlsplit

from ..services.paths import imported_widgets_dir


_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True, slots=True)
class WidgetRuntimeConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 17861

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any] | None,
    ) -> "WidgetRuntimeConfig":
        data = raw if isinstance(raw, Mapping) else {}
        return cls(
            enabled=bool(data.get("enabled", True)),
            host=str(data.get("host") or "127.0.0.1").strip(),
            port=int(data.get("port", 17861)),
        )

    def validate(self, *, allow_ephemeral_port: bool = False) -> None:
        if self.host.casefold() not in _ALLOWED_HOSTS:
            raise ValueError("Widget Runtime doit rester local")
        minimum = 0 if allow_ephemeral_port else 1
        if not minimum <= int(self.port) <= 65535:
            raise ValueError("Port Widget Runtime invalide")


@dataclass(frozen=True, slots=True)
class WidgetEvent:
    sequence: int
    channel: str
    event_type: str
    payload: Mapping[str, Any]
    timestamp: float

    def as_mapping(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "channel": self.channel,
            "type": self.event_type,
            "payload": dict(self.payload),
            "timestamp": self.timestamp,
        }


class WidgetEventHub:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sequence = 0
        self._state: dict[str, dict[str, Any]] = {}
        self._subscribers: dict[
            str,
            set[queue.Queue[WidgetEvent]],
        ] = {}

    @staticmethod
    def _channel(value: str) -> str:
        channel = str(value or "").strip()
        if not channel:
            raise ValueError("Canal widget requis")
        if len(channel) > 128:
            raise ValueError("Canal widget trop long")
        return channel

    def snapshot(self, channel: str) -> dict[str, Any]:
        key = self._channel(channel)
        with self._lock:
            return dict(self._state.get(key, {}))

    def set_state(
        self,
        channel: str,
        state: Mapping[str, Any],
        *,
        publish: bool = True,
    ) -> WidgetEvent | None:
        key = self._channel(channel)
        payload = dict(state)
        with self._lock:
            self._state[key] = payload
        if not publish:
            return None
        return self.publish(
            key,
            "state",
            payload,
            update_state=False,
        )

    def publish(
        self,
        channel: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        update_state: bool = False,
    ) -> WidgetEvent:
        key = self._channel(channel)
        kind = str(event_type or "message").strip() or "message"
        body = dict(payload or {})
        with self._lock:
            if update_state:
                current = dict(self._state.get(key, {}))
                current.update(body)
                self._state[key] = current
            self._sequence += 1
            event = WidgetEvent(
                sequence=self._sequence,
                channel=key,
                event_type=kind,
                payload=body,
                timestamp=time.time(),
            )
            targets = tuple(self._subscribers.get(key, ()))
        for target in targets:
            try:
                target.put_nowait(event)
            except queue.Full:
                try:
                    target.get_nowait()
                except queue.Empty:
                    pass
                try:
                    target.put_nowait(event)
                except queue.Full:
                    pass
        return event

    def subscribe(
        self,
        channel: str,
        *,
        max_events: int = 128,
    ) -> queue.Queue[WidgetEvent]:
        key = self._channel(channel)
        target: queue.Queue[WidgetEvent] = queue.Queue(
            maxsize=max(1, int(max_events))
        )
        with self._lock:
            self._subscribers.setdefault(key, set()).add(target)
        return target

    def unsubscribe(
        self,
        channel: str,
        target: queue.Queue[WidgetEvent],
    ) -> None:
        key = self._channel(channel)
        with self._lock:
            listeners = self._subscribers.get(key)
            if not listeners:
                return
            listeners.discard(target)
            if not listeners:
                self._subscribers.pop(key, None)


_CLIENT_JS = r"""
(() => {
  const api = {
    async state(channel) {
      const response = await fetch(
        "/api/state/" + encodeURIComponent(channel),
        { cache: "no-store" }
      );
      if (!response.ok) throw new Error("SSR state " + response.status);
      return await response.json();
    },
    subscribe(channel, callback, options = {}) {
      const source = new EventSource(
        "/events/" + encodeURIComponent(channel)
      );
      source.onmessage = event => {
        try {
          callback(JSON.parse(event.data));
        } catch (error) {
          console.error("SSR widget event", error);
        }
      };
      if (options.onOpen) source.onopen = options.onOpen;
      if (options.onError) source.onerror = options.onError;
      return source;
    }
  };
  window.SSRWidget = Object.freeze(api);
})();
""".strip()


class _WidgetHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        handler,
        *,
        runtime: "WidgetRuntime",
    ):
        self.runtime = runtime
        super().__init__(address, handler)


class _WidgetHandler(BaseHTTPRequestHandler):
    server_version = "SSRWidgetRuntime/1"

    @property
    def runtime(self) -> "WidgetRuntime":
        return self.server.runtime  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args) -> None:
        return

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        *,
        content_type: str,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(
        self,
        status: int,
        payload: Mapping[str, Any],
    ) -> None:
        body = json.dumps(
            dict(payload),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._send_bytes(
            status,
            body,
            content_type="application/json; charset=utf-8",
        )

    def _package_file(
        self,
        package_id: str,
        relative: str,
    ) -> Path | None:
        safe_id = unquote(package_id).strip()
        if (
            not safe_id
            or safe_id in {".", ".."}
            or "/" in safe_id
            or "\\" in safe_id
        ):
            return None
        root = (
            self.runtime.library_root / safe_id
        ).resolve()
        manifest = root / "manifest.json"
        if not manifest.is_file() or root.is_symlink():
            return None

        wanted = unquote(relative or "").lstrip("/")
        if not wanted:
            try:
                raw = json.loads(
                    manifest.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                return None
            wanted = str(
                raw.get("entry") if isinstance(raw, Mapping) else ""
            ).strip()
        if not wanted:
            return None

        candidate = (root / wanted).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        if (
            not candidate.is_file()
            or candidate.is_symlink()
        ):
            return None
        return candidate

    def _serve_package(
        self,
        package_id: str,
        relative: str,
    ) -> None:
        path = self._package_file(package_id, relative)
        if path is None:
            self._json(
                HTTPStatus.NOT_FOUND,
                {"error": "widget asset introuvable"},
            )
            return
        try:
            body = path.read_bytes()
        except OSError:
            self._json(
                HTTPStatus.NOT_FOUND,
                {"error": "widget asset indisponible"},
            )
            return
        mime, _encoding = mimetypes.guess_type(path.name)
        content_type = mime or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
            "image/svg+xml",
        }:
            content_type += "; charset=utf-8"
        cache = (
            "no-store"
            if path.suffix.casefold() in {".html", ".htm"}
            else "public, max-age=60"
        )
        self._send_bytes(
            HTTPStatus.OK,
            body,
            content_type=content_type,
            cache_control=cache,
        )

    def _serve_events(self, channel: str) -> None:
        try:
            target = self.runtime.hub.subscribe(channel)
        except ValueError as exc:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"error": str(exc)},
            )
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            snapshot = self.runtime.hub.snapshot(channel)
            initial = {
                "sequence": 0,
                "channel": channel,
                "type": "state",
                "payload": snapshot,
                "timestamp": time.time(),
            }
            self.wfile.write(
                (
                    "event: message\n"
                    + "data: "
                    + json.dumps(
                        initial,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n\n"
                ).encode("utf-8")
            )
            self.wfile.flush()
            while not self.runtime.stopping.is_set():
                try:
                    event = target.get(timeout=10.0)
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps(
                    event.as_mapping(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                self.wfile.write(
                    (
                        f"id: {event.sequence}\n"
                        "event: message\n"
                        f"data: {payload}\n\n"
                    ).encode("utf-8")
                )
                self.wfile.flush()
        except (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
        ):
            pass
        finally:
            self.runtime.hub.unsubscribe(channel, target)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path

        if path == "/health":
            self._json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "StreamStateRouter Widget Runtime",
                    "port": self.runtime.port,
                },
            )
            return

        if path == "/runtime/client.js":
            self._send_bytes(
                HTTPStatus.OK,
                _CLIENT_JS.encode("utf-8"),
                content_type="application/javascript; charset=utf-8",
            )
            return

        if path.startswith("/api/state/"):
            channel = unquote(path[len("/api/state/"):])
            try:
                state = self.runtime.hub.snapshot(channel)
            except ValueError as exc:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": str(exc)},
                )
                return
            self._json(
                HTTPStatus.OK,
                {
                    "channel": channel,
                    "state": state,
                },
            )
            return

        if path.startswith("/events/"):
            channel = unquote(path[len("/events/"):])
            self._serve_events(channel)
            return

        if path.startswith("/widgets/"):
            remainder = path[len("/widgets/"):]
            package_id, _, relative = remainder.partition("/")
            self._serve_package(package_id, relative)
            return

        self._json(
            HTTPStatus.NOT_FOUND,
            {"error": "route introuvable"},
        )


class WidgetRuntime:
    def __init__(
        self,
        config: WidgetRuntimeConfig,
        *,
        library_root: str | Path | None = None,
    ):
        self.config = config
        self.library_root = (
            Path(library_root).expanduser().resolve()
            if library_root is not None
            else imported_widgets_dir().resolve()
        )
        self.library_root.mkdir(parents=True, exist_ok=True)
        self.hub = WidgetEventHub()
        self.stopping = threading.Event()
        self._server: _WidgetHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(
            self._server is not None
            and thread is not None
            and thread.is_alive()
        )

    @property
    def port(self) -> int:
        server = self._server
        if server is None:
            return int(self.config.port)
        return int(server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def widget_url(self, package_id: str) -> str:
        wanted = str(package_id or "").strip()
        if not wanted:
            raise ValueError("package_id requis")
        return (
            f"{self.base_url}/widgets/"
            f"{quote(wanted, safe='')}/"
        )

    def start(self) -> None:
        if not self.config.enabled or self.running:
            return
        self.config.validate(allow_ephemeral_port=True)
        self.stopping.clear()
        server = _WidgetHTTPServer(
            (self.config.host, int(self.config.port)),
            _WidgetHandler,
            runtime=self,
        )
        self._server = server
        thread = threading.Thread(
            target=server.serve_forever,
            name="SSR-WidgetRuntime",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        self.stopping.set()
        server = self._server
        thread = self._thread
        if server is not None:
            server.shutdown()
            server.server_close()
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)
        self._server = None
        self._thread = None

    def set_state(
        self,
        channel: str,
        state: Mapping[str, Any],
    ) -> WidgetEvent | None:
        return self.hub.set_state(channel, state)

    def publish(
        self,
        channel: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        update_state: bool = False,
    ) -> WidgetEvent:
        return self.hub.publish(
            channel,
            event_type,
            payload,
            update_state=update_state,
        )
