from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import threading
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from ..events import EventBus
from ..presentation import PresentationStateStore
from ..services.paths import imported_widgets_dir
from .packages import WidgetPackage, list_widget_packages


@dataclass(frozen=True, slots=True)
class WidgetRuntimeConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8766


_BRIDGE_JS = r"""
(() => {
  const script = document.currentScript;
  const sourceUrl = new URL(script ? script.src : location.href, location.href);
  const requested =
    sourceUrl.searchParams.get("component") ||
    document.documentElement.dataset.ssrComponent ||
    "";
  const stateUrl = new URL("/runtime/state", sourceUrl.origin);
  if (requested) stateUrl.searchParams.set("component", requested);

  let lastRevision = -1;
  let managedTokenNames = new Set();
  const normalizeKey = (key) =>
    String(key).trim().replace(/[^A-Za-z0-9_-]+/g, "-").toLowerCase();

  const apply = (state) => {
    if (!state || state.revision === lastRevision) return;
    lastRevision = state.revision;
    const root = document.documentElement;
    root.dataset.ssrPresentation = state.profile || "";
    root.dataset.ssrAnimationIntensity = state.animation_intensity || "normal";

    const tokens = Object.assign(
      {},
      state.theme || {},
      (state.component_state && state.component_state.settings) || {}
    );
    const nextTokenNames = new Set();
    for (const [key, value] of Object.entries(tokens)) {
      if (
        typeof value === "string" ||
        typeof value === "number" ||
        typeof value === "boolean"
      ) {
        const cssName = "--ssr-" + normalizeKey(key);
        nextTokenNames.add(cssName);
        root.style.setProperty(cssName, String(value));
      }
    }
    for (const cssName of managedTokenNames) {
      if (!nextTokenNames.has(cssName)) {
        root.style.removeProperty(cssName);
      }
    }
    managedTokenNames = nextTokenNames;

    const component = state.component_state || {};
    root.dataset.ssrComponentMode = component.mode || "inherit";
    root.dataset.ssrComponentResource = component.resource || "";
    root.style.visibility =
      component.mode === "hidden" ? "hidden" : "visible";

    window.dispatchEvent(
      new CustomEvent("ssrstatechange", { detail: state })
    );
  };

  const refresh = async () => {
    try {
      const response = await fetch(stateUrl, { cache: "no-store" });
      if (response.ok) apply(await response.json());
    } catch (_) {
      // Keep the last state during brief SSR restarts.
    } finally {
      window.setTimeout(refresh, 250);
    }
  };
  refresh();
})();
""".strip()


_CHAT_HTML = r"""<!doctype html>
<html lang="fr" data-ssr-component="chat">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root {
    --ssr-accent: #63e6ff;
    --ssr-panel-opacity: .82;
    --ssr-glow: 10px;
    --ssr-font-size: 18px;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    padding: 0;
    width: 100%;
    height: 100%;
    overflow: hidden;
    background: transparent;
    color: white;
    font-family: Inter, "Segoe UI", sans-serif;
    font-size: var(--ssr-font-size);
  }
  #chat {
    display: flex;
    flex-direction: column;
    justify-content: flex-end;
    gap: 6px;
    width: 100%;
    height: 100%;
    padding: 8px;
  }
  .message {
    align-self: flex-start;
    max-width: 100%;
    padding: 7px 10px;
    border-left: 2px solid var(--ssr-accent);
    border-radius: 4px;
    background: rgba(8, 17, 24, var(--ssr-panel-opacity));
    box-shadow: 0 0 var(--ssr-glow) rgba(80, 220, 255, .18);
    overflow-wrap: anywhere;
    animation: ssr-enter 180ms ease-out both;
  }
  .author {
    font-weight: 700;
    margin-right: 7px;
    color: var(--ssr-accent);
  }
  .platform {
    margin-right: 6px;
    opacity: .65;
    font-size: .72em;
    text-transform: uppercase;
  }
  @keyframes ssr-enter {
    from { opacity: 0; transform: translateX(-8px); }
    to { opacity: 1; transform: translateX(0); }
  }
  html[data-ssr-animation-intensity="off"] .message {
    animation: none;
  }
  html[data-ssr-animation-intensity="low"] .message {
    animation-duration: 90ms;
  }
</style>
</head>
<body>
<div id="chat" aria-live="polite"></div>
<script src="/runtime/bridge.js?component=chat"></script>
<script>
(() => {
  const root = document.getElementById("chat");
  let after = 0;
  let streamId = "";
  const maxMessages = 80;

  const addMessage = (event) => {
    const payload = event.payload || {};
    const row = document.createElement("div");
    row.className = "message";
    row.dataset.sequence = String(event.sequence || "");

    const platform = document.createElement("span");
    platform.className = "platform";
    platform.textContent = event.platform || "";
    row.appendChild(platform);

    const author = document.createElement("span");
    author.className = "author";
    author.textContent =
      payload.display_name || payload.user_name || payload.author || "—";
    if (typeof payload.color === "string" && payload.color) {
      author.style.color = payload.color;
    }
    row.appendChild(author);

    const text = document.createElement("span");
    text.className = "text";
    text.textContent = payload.text || "";
    row.appendChild(text);

    root.appendChild(row);
    while (root.children.length > maxMessages) {
      root.removeChild(root.firstChild);
    }
  };

  const refresh = async () => {
    try {
      const response = await fetch(
        "/runtime/events?channel=chat&after=" + after + "&limit=50",
        { cache: "no-store" }
      );
      if (response.ok) {
        const snapshot = await response.json();
        const incomingStreamId = String(snapshot.stream_id || "");
        if (
          streamId &&
          incomingStreamId &&
          incomingStreamId !== streamId
        ) {
          streamId = incomingStreamId;
          after = 0;
          return;
        }
        if (incomingStreamId) streamId = incomingStreamId;
        for (const event of snapshot.events || []) {
          after = Math.max(after, Number(event.sequence) || 0);
          if (event.type === "message") addMessage(event);
        }
        after = Math.max(after, Number(snapshot.next_after) || after);
      }
    } catch (_) {
      // Keep current chat visible during short SSR restarts.
    } finally {
      window.setTimeout(refresh, 300);
    }
  };
  refresh();
})();
</script>
</body>
</html>
""".strip()


_EVENTS_HTML = r"""<!doctype html>
<html lang="fr" data-ssr-component="events">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root {
  --ssr-accent: #63e6ff;
  --ssr-panel-opacity: .82;
  --ssr-glow: 10px;
  --ssr-font-size: 17px;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; width: 100%; height: 100%; overflow: hidden;
  background: transparent; color: white;
  font-family: Inter, "Segoe UI", sans-serif;
  font-size: var(--ssr-font-size);
}
#events {
  display: flex; flex-direction: column-reverse; gap: 6px;
  width: 100%; height: 100%; padding: 8px;
}
.event {
  padding: 8px 10px;
  border-left: 2px solid var(--ssr-accent);
  border-radius: 4px;
  background: rgba(8,17,24,var(--ssr-panel-opacity));
  box-shadow: 0 0 var(--ssr-glow) rgba(80,220,255,.18);
  animation: enter 220ms ease-out both;
}
.kind { color: var(--ssr-accent); font-weight: 700; margin-right: 8px; }
.platform { opacity: .6; font-size: .72em; text-transform: uppercase; margin-right: 6px; }
@keyframes enter { from { opacity:0; transform:translateX(12px); } to { opacity:1; transform:none; } }
html[data-ssr-animation-intensity="off"] .event { animation:none; }
</style>
</head>
<body>
<div id="events" aria-live="polite"></div>
<script src="/runtime/bridge.js?component=events"></script>
<script>
(() => {
  const root = document.getElementById("events");
  let after = 0;
  let streamId = "";
  const maxRows = 30;
  const render = (event) => {
    const payload = event.payload || {};
    const row = document.createElement("div");
    row.className = "event";
    const platform = document.createElement("span");
    platform.className = "platform";
    platform.textContent = event.platform || "";
    row.appendChild(platform);
    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = payload.label || event.type || "event";
    row.appendChild(kind);
    const text = document.createElement("span");
    text.textContent =
      payload.text || payload.display_name || payload.user_name ||
      payload.title || "";
    row.appendChild(text);
    root.prepend(row);
    while (root.children.length > maxRows) root.removeChild(root.lastChild);
  };
  const refresh = async () => {
    try {
      const response = await fetch(
        "/runtime/events?channel=events&after=" + after + "&limit=50",
        { cache: "no-store" }
      );
      if (response.ok) {
        const snapshot = await response.json();
        const incomingStreamId = String(snapshot.stream_id || "");
        if (
          streamId &&
          incomingStreamId &&
          incomingStreamId !== streamId
        ) {
          streamId = incomingStreamId;
          after = 0;
          return;
        }
        if (incomingStreamId) streamId = incomingStreamId;
        for (const event of snapshot.events || []) {
          after = Math.max(after, Number(event.sequence) || 0);
          render(event);
        }
        after = Math.max(after, Number(snapshot.next_after) || after);
      }
    } catch (_) {}
    finally { window.setTimeout(refresh, 350); }
  };
  refresh();
})();
</script>
</body>
</html>""".strip()


_ALERTS_HTML = r"""<!doctype html>
<html lang="fr" data-ssr-component="alerts">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root {
  --ssr-accent: #63e6ff;
  --ssr-panel-opacity: .88;
  --ssr-glow: 24px;
  --ssr-font-size: 30px;
}
* { box-sizing: border-box; }
html, body {
  margin:0; width:100%; height:100%; overflow:hidden;
  background:transparent; color:white; font-family:Inter,"Segoe UI",sans-serif;
}
#root {
  width:100%; height:100%; display:flex; align-items:center; justify-content:center;
  pointer-events:none;
}
.alert {
  min-width: 320px; max-width: 80%;
  padding: 22px 30px; text-align:center;
  border: 1px solid var(--ssr-accent);
  background: rgba(8,17,24,var(--ssr-panel-opacity));
  box-shadow: 0 0 var(--ssr-glow) rgba(80,220,255,.35);
  border-radius: 10px;
  animation: alert-in 320ms ease-out both;
}
.title { color:var(--ssr-accent); font-size:var(--ssr-font-size); font-weight:800; }
.text { margin-top:8px; font-size:.65em; }
@keyframes alert-in {
  from { opacity:0; transform:scale(.92) translateY(10px); }
  to { opacity:1; transform:none; }
}
html[data-ssr-animation-intensity="off"] .alert { animation:none; }
</style>
</head>
<body>
<div id="root"></div>
<script src="/runtime/bridge.js?component=alerts"></script>
<script>
(() => {
  const root = document.getElementById("root");
  let after = 0;
  let streamId = "";
  const queue = [];
  let active = false;
  const showNext = () => {
    if (active || !queue.length) return;
    active = true;
    const event = queue.shift();
    const payload = event.payload || {};
    const box = document.createElement("div");
    box.className = "alert";
    const title = document.createElement("div");
    title.className = "title";
    title.textContent = payload.title || payload.label || event.type || "Alerte";
    const text = document.createElement("div");
    text.className = "text";
    text.textContent =
      payload.text || payload.display_name || payload.user_name || "";
    box.appendChild(title);
    box.appendChild(text);
    root.replaceChildren(box);
    const duration = Math.max(1000, Math.min(20000, Number(payload.duration_ms) || 5000));
    window.setTimeout(() => {
      root.replaceChildren();
      active = false;
      showNext();
    }, duration);
  };
  const refresh = async () => {
    try {
      const response = await fetch(
        "/runtime/events?channel=alerts&after=" + after + "&limit=20",
        { cache: "no-store" }
      );
      if (response.ok) {
        const snapshot = await response.json();
        const incomingStreamId = String(snapshot.stream_id || "");
        if (
          streamId &&
          incomingStreamId &&
          incomingStreamId !== streamId
        ) {
          streamId = incomingStreamId;
          after = 0;
          return;
        }
        if (incomingStreamId) streamId = incomingStreamId;
        for (const event of snapshot.events || []) {
          after = Math.max(after, Number(event.sequence) || 0);
          queue.push(event);
        }
        after = Math.max(after, Number(snapshot.next_after) || after);
        showNext();
      }
    } catch (_) {}
    finally { window.setTimeout(refresh, 300); }
  };
  refresh();
})();
</script>
</body>
</html>""".strip()


class _WidgetServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class WidgetRuntime:
    """Loopback-only, read-only HTTP surface for SSR browser widgets."""

    def __init__(
        self,
        config: WidgetRuntimeConfig,
        state_store: PresentationStateStore,
        *,
        event_bus: EventBus | None = None,
        library_root: str | Path | None = None,
    ):
        self.config = config
        self.state_store = state_store
        self.event_bus = event_bus if event_bus is not None else EventBus()
        self.library_root = (
            Path(library_root).expanduser().resolve()
            if library_root is not None
            else imported_widgets_dir().resolve()
        )
        self._lock = threading.RLock()
        self._packages: dict[str, WidgetPackage] = {}
        self._server: _WidgetServer | None = None
        self._thread: threading.Thread | None = None
        self.refresh_packages()

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int:
        server = self._server
        if server is not None:
            return int(server.server_address[1])
        return int(self.config.port)

    @property
    def base_url(self) -> str:
        host = (
            "127.0.0.1"
            if self.config.host in {"localhost", "::1"}
            else self.config.host
        )
        return f"http://{host}:{self.port}"

    def refresh_packages(self) -> None:
        packages = list_widget_packages(root=self.library_root)
        with self._lock:
            self._packages = {
                package.package_id: package
                for package in packages
            }

    def package_url(
        self,
        package_id: str,
        *,
        component: str = "",
    ) -> str:
        package = self._package(package_id)
        if package is None:
            raise KeyError(f"Widget inconnu : {package_id}")
        suffix = (
            f"?component={component}"
            if str(component).strip()
            else ""
        )
        return (
            f"{self.base_url}/widgets/{package.package_id}/{suffix}"
        )

    def _package(self, package_id: str) -> WidgetPackage | None:
        with self._lock:
            return self._packages.get(str(package_id))

    def _state_payload(self, component: str) -> dict[str, Any]:
        return self.state_store.snapshot().as_mapping(
            component=component,
        )

    @staticmethod
    def _inside(root: Path, candidate: Path) -> bool:
        try:
            candidate.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False

    def _static_file(
        self,
        package_id: str,
        relative: str,
    ) -> Path | None:
        package = self._package(package_id)
        if package is None:
            return None
        root = package.root.resolve()
        if not relative:
            candidate = package.entry.resolve()
        else:
            decoded = unquote(relative).replace("\\", "/")
            if decoded.startswith("/") or ".." in Path(decoded).parts:
                return None
            candidate = (root / decoded).resolve()
        if (
            candidate.name.casefold() == "manifest.json"
            or not self._inside(root, candidate)
            or not candidate.is_file()
        ):
            return None
        return candidate

    @staticmethod
    def _component_host_html(component: str) -> bytes:
        wanted = str(component or "").strip().casefold()
        if not wanted:
            raise ValueError("component requis")
        component_json = json.dumps(wanted, ensure_ascii=False)
        fallback = (
            f"builtin:{wanted}"
            if wanted in {"chat", "events", "alerts"}
            else ""
        )
        fallback_json = json.dumps(fallback)
        html = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body,#host,iframe{margin:0;width:100%;height:100%;border:0;background:transparent;overflow:hidden}
iframe{display:block}
</style>
</head>
<body>
<div id="host"></div>
<script>
(() => {
  const component = __COMPONENT__;
  const fallback = __FALLBACK__;
  const host = document.getElementById("host");
  let current = "";
  let frame = null;

  const routeFor = (resource) => {
    const value = String(resource || fallback || "").trim();
    if (value.startsWith("builtin:")) {
      const name = value.slice("builtin:".length);
      if (["chat","events","alerts"].includes(name)) {
        return "/builtin/" + encodeURIComponent(name);
      }
      return "";
    }
    if (value.startsWith("widget:") || value.startsWith("package:")) {
      const id = value.slice(value.indexOf(":") + 1).trim();
      if (!id) return "";
      return "/widgets/" + encodeURIComponent(id) + "/?component=" + encodeURIComponent(component);
    }
    return "";
  };

  const apply = (state) => {
    const c = (state && state.component_state) || {};
    if (c.mode === "hidden") {
      host.style.visibility = "hidden";
      return;
    }
    host.style.visibility = "visible";
    const route = routeFor(c.resource);
    if (route === current) return;
    current = route;
    host.replaceChildren();
    frame = null;
    if (!route) return;
    frame = document.createElement("iframe");
    frame.src = route;
    frame.setAttribute("allowtransparency", "true");
    frame.setAttribute("scrolling", "no");
    host.appendChild(frame);
  };

  const refresh = async () => {
    try {
      const response = await fetch(
        "/runtime/state?component=" + encodeURIComponent(component),
        { cache: "no-store" }
      );
      if (response.ok) apply(await response.json());
    } catch (_) {}
    finally { window.setTimeout(refresh, 250); }
  };
  refresh();
})();
</script>
</body>
</html>"""
        html = html.replace("__COMPONENT__", component_json)
        html = html.replace("__FALLBACK__", fallback_json)
        return html.encode("utf-8")

    @staticmethod
    def _inject_bridge(
        body: bytes,
        *,
        component: str,
    ) -> bytes:
        wanted = str(component or "").strip()
        if not wanted or b"/runtime/bridge.js" in body:
            return body
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return body
        script = (
            '<script src="/runtime/bridge.js?component='
            + quote(wanted, safe="")
            + '"></script>'
        )
        lowered = text.casefold()
        head_index = lowered.rfind("</head>")
        if head_index >= 0:
            text = text[:head_index] + script + text[head_index:]
        else:
            body_index = lowered.rfind("</body>")
            if body_index >= 0:
                text = (
                    text[:body_index]
                    + script
                    + text[body_index:]
                )
            else:
                text += script
        return text.encode("utf-8")

    def _handler(self):
        runtime = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "SSRWidgetRuntime/1"

            def log_message(self, _format: str, *_args) -> None:
                return

            def _common_headers(
                self,
                *,
                content_type: str,
                length: int,
                cache: str = "no-store",
            ) -> None:
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", cache)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")

            def _send_bytes(
                self,
                body: bytes,
                *,
                content_type: str,
                status: int = HTTPStatus.OK,
                head_only: bool = False,
                cache: str = "no-store",
            ) -> None:
                self.send_response(int(status))
                self._common_headers(
                    content_type=content_type,
                    length=len(body),
                    cache=cache,
                )
                self.end_headers()
                if not head_only:
                    self.wfile.write(body)

            def _send_json(
                self,
                payload: object,
                *,
                status: int = HTTPStatus.OK,
                head_only: bool = False,
            ) -> None:
                body = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                self._send_bytes(
                    body,
                    content_type="application/json; charset=utf-8",
                    status=status,
                    head_only=head_only,
                )

            def _serve(self, *, head_only: bool) -> None:
                parsed = urlsplit(self.path)
                path = parsed.path or "/"
                if path == "/health":
                    self._send_json(
                        {
                            "ok": True,
                            "profile": (
                                runtime.state_store.snapshot().profile
                            ),
                        },
                        head_only=head_only,
                    )
                    return

                if path == "/runtime/state":
                    component = str(
                        parse_qs(parsed.query).get(
                            "component",
                            [""],
                        )[0]
                    ).strip()
                    self._send_json(
                        runtime._state_payload(component),
                        head_only=head_only,
                    )
                    return

                if path == "/runtime/events":
                    query = parse_qs(parsed.query)
                    channel = str(
                        query.get("channel", [""])[0]
                    ).strip()
                    try:
                        after = max(
                            0,
                            int(query.get("after", ["0"])[0]),
                        )
                        limit = max(
                            1,
                            min(
                                500,
                                int(query.get("limit", ["100"])[0]),
                            ),
                        )
                    except (TypeError, ValueError):
                        self._send_json(
                            {"error": "invalid_cursor"},
                            status=HTTPStatus.BAD_REQUEST,
                            head_only=head_only,
                        )
                        return
                    if not channel:
                        self._send_json(
                            {"error": "channel_required"},
                            status=HTTPStatus.BAD_REQUEST,
                            head_only=head_only,
                        )
                        return
                    self._send_json(
                        runtime.event_bus.snapshot(
                            channel,
                            after=after,
                            limit=limit,
                        ),
                        head_only=head_only,
                    )
                    return

                component_prefix = "/component/"
                if path.startswith(component_prefix):
                    component = unquote(
                        path[len(component_prefix):]
                    ).strip().strip("/")
                    if (
                        not component
                        or "/" in component
                        or "\\" in component
                    ):
                        self._send_json(
                            {"error": "component_not_found"},
                            status=HTTPStatus.NOT_FOUND,
                            head_only=head_only,
                        )
                        return
                    try:
                        body = runtime._component_host_html(component)
                    except ValueError:
                        self._send_json(
                            {"error": "component_not_found"},
                            status=HTTPStatus.NOT_FOUND,
                            head_only=head_only,
                        )
                        return
                    self._send_bytes(
                        body,
                        content_type="text/html; charset=utf-8",
                        head_only=head_only,
                        cache="no-cache",
                    )
                    return

                if path == "/runtime/bridge.js":
                    body = _BRIDGE_JS.encode("utf-8")
                    self._send_bytes(
                        body,
                        content_type=(
                            "application/javascript; charset=utf-8"
                        ),
                        head_only=head_only,
                    )
                    return

                if path in {"/builtin/chat", "/builtin/chat/"}:
                    self._send_bytes(
                        _CHAT_HTML.encode("utf-8"),
                        content_type="text/html; charset=utf-8",
                        head_only=head_only,
                        cache="no-cache",
                    )
                    return

                if path in {"/builtin/events", "/builtin/events/"}:
                    self._send_bytes(
                        _EVENTS_HTML.encode("utf-8"),
                        content_type="text/html; charset=utf-8",
                        head_only=head_only,
                        cache="no-cache",
                    )
                    return

                if path in {"/builtin/alerts", "/builtin/alerts/"}:
                    self._send_bytes(
                        _ALERTS_HTML.encode("utf-8"),
                        content_type="text/html; charset=utf-8",
                        head_only=head_only,
                        cache="no-cache",
                    )
                    return

                prefix = "/widgets/"
                if path.startswith(prefix):
                    rest = path[len(prefix):]
                    package_id, separator, relative = rest.partition("/")
                    if not package_id:
                        self._send_json(
                            {"error": "widget_not_found"},
                            status=HTTPStatus.NOT_FOUND,
                            head_only=head_only,
                        )
                        return
                    file_path = runtime._static_file(
                        unquote(package_id),
                        relative if separator else "",
                    )
                    if file_path is None:
                        self._send_json(
                            {"error": "widget_file_not_found"},
                            status=HTTPStatus.NOT_FOUND,
                            head_only=head_only,
                        )
                        return
                    try:
                        body = file_path.read_bytes()
                    except OSError:
                        self._send_json(
                            {"error": "widget_file_unreadable"},
                            status=HTTPStatus.NOT_FOUND,
                            head_only=head_only,
                        )
                        return
                    guessed, _encoding = mimetypes.guess_type(
                        file_path.name
                    )
                    content_type = guessed or "application/octet-stream"
                    if (
                        file_path.suffix.casefold() in {".html", ".htm"}
                    ):
                        component = str(
                            parse_qs(parsed.query).get(
                                "component",
                                [""],
                            )[0]
                        ).strip()
                        body = runtime._inject_bridge(
                            body,
                            component=component,
                        )
                    if content_type.startswith("text/"):
                        content_type += "; charset=utf-8"
                    self._send_bytes(
                        body,
                        content_type=content_type,
                        head_only=head_only,
                        cache="no-cache",
                    )
                    return

                self._send_json(
                    {"error": "not_found"},
                    status=HTTPStatus.NOT_FOUND,
                    head_only=head_only,
                )

            def do_GET(self) -> None:
                self._serve(head_only=False)

            def do_HEAD(self) -> None:
                self._serve(head_only=True)

            def do_POST(self) -> None:
                self._send_json(
                    {"error": "read_only"},
                    status=HTTPStatus.METHOD_NOT_ALLOWED,
                )

        return Handler

    def start(self) -> None:
        if not self.config.enabled or self.running:
            return
        if self.config.host not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError(
                "Widget Runtime doit rester lié à l’interface loopback"
            )
        server = _WidgetServer(
            (self.config.host, int(self.config.port)),
            self._handler(),
        )
        thread = threading.Thread(
            target=server.serve_forever,
            name="SSR-WidgetRuntime",
            daemon=True,
        )
        self._server = server
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=2.0)
