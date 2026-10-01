from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from stream_state_router.media import MediaArtworkStore, MediaState, MediaStateStore, media_artwork_identity
from stream_state_router.presentation import (
    PresentationStateStore,
    build_presentation_registry,
)
from stream_state_router.widgets import (
    WidgetRuntime,
    WidgetRuntimeConfig,
    import_html_module,
)


class WidgetRuntimeTests(unittest.TestCase):
    def _runtime(self, root: Path):
        source = root / "source"
        library = root / "library"
        source.mkdir()
        (source / "index.html").write_text(
            (
                "<html data-ssr-component=\"chat\">"
                "<body>Loveless</body></html>"
            ),
            encoding="utf-8",
        )
        package = import_html_module(
            source / "index.html",
            name="Loveless Chat",
            target_root=library,
        )

        registry = build_presentation_registry(
            profiles_raw={
                "Midgar": {
                    "animation_intensity": "low",
                    "widget_theme": "Midgar",
                    "theme": {
                        "accent": "#00ffff",
                        "panel_opacity": 0.82,
                    },
                    "components": {
                        "chat": {
                            "mode": "hidden",
                            "resource": "chat/midgar",
                            "settings": {
                                "glow": "14px",
                            },
                        },
                        "events": {
                            "mode": "custom",
                            "resource": "events/midgar",
                            "settings": {
                                "private_marker": "must-not-leak",
                            },
                        },
                    },
                }
            },
            cues_raw={},
        )
        profile = registry.profile("Midgar")
        assert profile is not None

        store = PresentationStateStore()
        store.update(profile)
        runtime = WidgetRuntime(
            WidgetRuntimeConfig(
                enabled=True,
                host="127.0.0.1",
                port=0,
            ),
            store,
            library_root=library,
        )
        runtime.start()
        self.addCleanup(runtime.stop)
        return runtime, package

    def test_ipv6_loopback_base_url_uses_bracketed_literal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = WidgetRuntime(
                WidgetRuntimeConfig(
                    enabled=True,
                    host="::1",
                    port=8766,
                ),
                PresentationStateStore(),
                library_root=Path(tmp),
            )

            self.assertEqual(
                runtime.base_url,
                "http://[::1]:8766",
            )

    def test_running_requires_live_server_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = WidgetRuntime(
                WidgetRuntimeConfig(
                    enabled=True,
                    host="127.0.0.1",
                    port=8766,
                ),
                PresentationStateStore(),
                library_root=Path(tmp),
            )
            runtime._server = object()
            runtime._thread = threading.Thread(target=lambda: None)

            self.assertFalse(runtime.running)

    @staticmethod
    def _get(url: str) -> tuple[int, bytes, str]:
        with urlopen(url, timeout=2.0) as response:
            return (
                response.status,
                response.read(),
                response.headers.get("Content-Type", ""),
            )

    def test_state_endpoint_exposes_only_resolved_widget_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _package = self._runtime(Path(tmp))

            status, body, content_type = self._get(
                runtime.base_url + "/runtime/state?component=chat"
            )
            payload = json.loads(body)

            self.assertEqual(status, 200)
            self.assertIn("application/json", content_type)
            self.assertEqual(payload["profile"], "Midgar")
            self.assertEqual(payload["theme"]["accent"], "#00ffff")
            self.assertEqual(
                payload["component_state"]["mode"],
                "hidden",
            )
            self.assertEqual(
                payload["component_state"]["settings"]["glow"],
                "14px",
            )
            self.assertNotIn("obs", payload)
            self.assertNotIn("token", payload)
            self.assertEqual(set(payload["components"]), {"chat"})
            self.assertNotIn(
                "must-not-leak",
                json.dumps(payload, ensure_ascii=False),
            )

    def test_media_endpoint_exposes_normalized_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _package = self._runtime(Path(tmp))
            media = MediaStateStore("vlc")
            media.update(
                MediaState(
                    provider="vlc",
                    connected=True,
                    playback_state="playing",
                    title="Mako Reactor",
                    artist="Suno",
                    album="Midgar Radio",
                    duration_seconds=180,
                    position_seconds=45,
                    volume_percent=75,
                    track_id="42",
                )
            )
            runtime.media_state_store = media

            status, body, content_type = self._get(
                runtime.base_url + "/runtime/media"
            )
            payload = json.loads(body)

            self.assertEqual(status, 200)
            self.assertIn("application/json", content_type)
            self.assertEqual(payload["provider"], "vlc")
            self.assertEqual(payload["playback_state"], "playing")
            self.assertEqual(payload["title"], "Mako Reactor")
            self.assertEqual(payload["position_seconds"], 45)
            self.assertEqual(payload["revision"], 1)
            self.assertNotIn("password", payload)
            self.assertNotIn("uri", payload)
            self.assertFalse(payload["artwork_available"])
            self.assertEqual(payload["artwork_url"], "")
            self.assertNotIn("error", payload)

    def test_media_stale_can_change_without_media_revision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _package = self._runtime(Path(tmp))
            now = [10.0]
            media = MediaStateStore("vlc", clock=lambda: now[0])
            media.update(
                MediaState(
                    provider="vlc",
                    connected=True,
                    playback_state="playing",
                    title="Mako",
                )
            )
            runtime.media_state_store = media

            _status, body, _content_type = self._get(
                runtime.base_url + "/runtime/media"
            )
            fresh = json.loads(body)
            self.assertEqual(fresh["revision"], 1)
            self.assertFalse(fresh["stale"])

            now[0] = 20.0
            _status, body, _content_type = self._get(
                runtime.base_url + "/runtime/media"
            )
            stale = json.loads(body)
            self.assertEqual(stale["revision"], 1)
            self.assertTrue(stale["stale"])

    def test_media_artwork_is_served_from_safe_runtime_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _package = self._runtime(Path(tmp))
            media = MediaStateStore("vlc")
            state = MediaState(
                provider="vlc",
                connected=True,
                playback_state="playing",
                title="Mako",
                uri="file:///C:/Music/mako.flac",
                artwork_url="file:///C:/Music/cover.jpg",
                track_id="42",
            )
            media.update(state)
            runtime.media_state_store = media

            artwork = MediaArtworkStore()
            identity = media_artwork_identity(
                provider=state.provider,
                track_id=state.track_id,
                uri=state.uri,
                title=state.title,
                artwork_url=state.artwork_url,
            )
            valid_jpeg = b"\xff\xd8\xff\xe0SSR-JPEG"
            artwork.update(
                valid_jpeg,
                content_type="image/jpeg",
                identity=identity,
            )
            runtime.media_artwork_store = artwork

            _status, body, _content_type = self._get(
                runtime.base_url + "/runtime/media"
            )
            payload = json.loads(body)
            self.assertTrue(payload["artwork_available"])
            self.assertEqual(payload["artwork_revision"], 1)
            self.assertEqual(
                payload["artwork_url"],
                "/runtime/media/artwork?revision=1",
            )

            status, cover, content_type = self._get(
                runtime.base_url + payload["artwork_url"]
            )
            self.assertEqual(status, 200)
            self.assertEqual(cover, valid_jpeg)
            self.assertIn("image/jpeg", content_type)

            with self.assertRaises(HTTPError) as expired:
                self._get(
                    runtime.base_url
                    + "/runtime/media/artwork?revision=0"
                )
            self.assertEqual(expired.exception.code, 404)

    def test_media_payload_hides_artwork_from_previous_track(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _package = self._runtime(Path(tmp))
            media = MediaStateStore("vlc")
            current = MediaState(
                provider="vlc",
                connected=True,
                playback_state="playing",
                title="Track B",
                uri="file:///C:/Music/b.flac",
                track_id="2",
            )
            media.update(current)
            runtime.media_state_store = media

            artwork = MediaArtworkStore()
            artwork.update(
                b"\x89PNG\r\n\x1a\nSSR-PNG",
                content_type="image/png",
                identity=media_artwork_identity(
                    provider="vlc",
                    track_id="1",
                    uri="file:///C:/Music/a.flac",
                    title="Track A",
                ),
            )
            runtime.media_artwork_store = artwork

            _status, body, _content_type = self._get(
                runtime.base_url + "/runtime/media"
            )
            payload = json.loads(body)

            self.assertFalse(payload["artwork_available"])
            self.assertEqual(payload["artwork_url"], "")

    def test_builtin_radio_consumes_media_endpoint_without_html_injection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _package = self._runtime(Path(tmp))

            status, body, content_type = self._get(
                runtime.base_url + "/builtin/radio"
            )

            self.assertEqual(status, 200)
            self.assertIn("text/html", content_type)
            self.assertIn(b"/runtime/media", body)
            self.assertIn(b'id="cover"', body)
            self.assertIn(b"state.artwork_url", body)
            self.assertIn(b"state.artwork_revision", body)
            self.assertIn(b"state.stale", body)
            self.assertIn(b"runtimeUnavailable", body)
            self.assertIn(b"performance.now()", body)
            self.assertNotIn(b"state.revision === lastRevision", body)
            self.assertIn("État obsolète".encode("utf-8"), body)
            self.assertIn("SSR indisponible".encode("utf-8"), body)
            self.assertIn(b"textContent", body)
            self.assertNotIn(b"innerHTML", body)
            self.assertIn(
                b"/runtime/bridge.js?component=radio",
                body,
            )

    def test_runtime_serves_widget_entry_and_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, package = self._runtime(Path(tmp))

            status, body, content_type = self._get(
                runtime.package_url(
                    package.package_id,
                    component="chat",
                )
            )
            self.assertEqual(status, 200)
            self.assertIn("text/html", content_type)
            self.assertIn(b"Loveless", body)

            status, bridge, content_type = self._get(
                runtime.base_url + "/runtime/bridge.js?component=chat"
            )
            self.assertEqual(status, 200)
            self.assertIn("javascript", content_type)
            self.assertIn(b"ssrstatechange", bridge)
            self.assertIn(b"--ssr-", bridge)
            self.assertIn(b'component.mode === "hidden"', bridge)
            self.assertIn(b"removeProperty", bridge)
            self.assertIn(b"ssr.widget.state", bridge)
            self.assertIn(b"ssr.media.state", bridge)
            self.assertIn(b"ssrmediastatechange", bridge)
            self.assertIn(b'requested === "radio"', bridge)
            self.assertIn(b"window.parent !== window", bridge)

    def test_imported_html_can_receive_presentation_bridge_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime, package = self._runtime(root)
            source_entry = root / "source" / "index.html"
            source_before = source_entry.read_text(encoding="utf-8")

            _status, body, _content_type = self._get(
                runtime.package_url(
                    package.package_id,
                    component="chat",
                )
            )
            rendered = body.decode("utf-8")

            self.assertIn(
                '/runtime/bridge.js?component=chat',
                rendered,
            )
            self.assertEqual(
                source_entry.read_text(encoding="utf-8"),
                source_before,
            )

            _status, plain_body, _content_type = self._get(
                runtime.package_url(package.package_id)
            )
            self.assertNotIn(
                b"/runtime/bridge.js",
                plain_body,
            )

    def test_events_endpoint_and_builtin_chat_are_platform_agnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _package = self._runtime(Path(tmp))
            runtime.event_bus.publish(
                channel="chat",
                type="message",
                platform="twitch",
                payload={
                    "display_name": "Cloud",
                    "text": "<b>not html</b>",
                    "color": "#44ccff",
                },
            )

            status, body, _content_type = self._get(
                runtime.base_url
                + "/runtime/events?channel=chat&after=0&limit=20"
            )
            snapshot = json.loads(body)
            self.assertEqual(status, 200)
            self.assertTrue(snapshot["stream_id"])
            self.assertEqual(snapshot["next_after"], 1)
            self.assertEqual(len(snapshot["events"]), 1)
            self.assertEqual(
                snapshot["events"][0]["payload"]["text"],
                "<b>not html</b>",
            )

            _status, chat, content_type = self._get(
                runtime.base_url + "/builtin/chat"
            )
            self.assertIn("text/html", content_type)
            self.assertIn(b'text.textContent = payload.text', chat)
            self.assertNotIn(b"innerHTML", chat)
            self.assertIn(b"/runtime/events?channel=chat", chat)

            after = snapshot["events"][0]["sequence"]
            _status, body, _content_type = self._get(
                runtime.base_url
                + f"/runtime/events?channel=chat&after={after}"
            )
            self.assertEqual(json.loads(body)["events"], [])

    def test_component_host_defaults_to_builtin_and_can_switch_resource(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, package = self._runtime(Path(tmp))

            _status, body, content_type = self._get(
                runtime.base_url + "/component/chat"
            )
            html = body.decode("utf-8")
            self.assertIn("text/html", content_type)
            self.assertIn('const component = "chat"', html)
            self.assertIn('const fallback = "builtin:chat"', html)
            self.assertIn('"radio"', html)
            self.assertIn(
                '"/runtime/state?component="',
                html,
            )
            self.assertIn(
                'value.startsWith("widget:")',
                html,
            )
            self.assertIn('frame.setAttribute("sandbox", "allow-scripts")', html)
            self.assertIn('if (c.mode === "hidden")', html)
            self.assertIn("host.replaceChildren()", html)
            self.assertIn("postMessage", html)
            self.assertIn("ssr.media.state", html)
            self.assertIn("/runtime/media", html)
            self.assertIn(
                'currentComponent.mode === "hidden"',
                html,
            )

            registry = build_presentation_registry(
                profiles_raw={
                    "Custom": {
                        "components": {
                            "chat": {
                                "mode": "custom",
                                "resource": (
                                    f"widget:{package.package_id}"
                                ),
                                "settings": {},
                            }
                        }
                    }
                },
                cues_raw={},
            )
            profile = registry.profile("Custom")
            assert profile is not None
            runtime.state_store.update(profile)

            _status, state_body, _content_type = self._get(
                runtime.base_url
                + "/runtime/state?component=chat"
            )
            payload = json.loads(state_body)
            self.assertEqual(
                payload["component_state"]["resource"],
                f"widget:{package.package_id}",
            )

    def test_builtin_events_and_alerts_use_normalized_event_channels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _package = self._runtime(Path(tmp))

            runtime.event_bus.publish(
                channel="events",
                type="follow",
                platform="demo",
                payload={
                    "label": "Nouveau follower",
                    "display_name": "Cloud",
                },
            )
            runtime.event_bus.publish(
                channel="alerts",
                type="subscription",
                platform="demo",
                payload={
                    "title": "NOUVEAU SOLDAT",
                    "text": "Cloud rejoint le programme",
                    "duration_ms": 4000,
                },
            )

            _status, events_html, content_type = self._get(
                runtime.base_url + "/builtin/events"
            )
            self.assertIn("text/html", content_type)
            self.assertIn(
                b"/runtime/events?channel=events",
                events_html,
            )
            self.assertIn(b"textContent", events_html)
            self.assertNotIn(b"innerHTML", events_html)

            _status, alerts_html, content_type = self._get(
                runtime.base_url + "/builtin/alerts"
            )
            self.assertIn("text/html", content_type)
            self.assertIn(
                b"/runtime/events?channel=alerts",
                alerts_html,
            )
            self.assertIn(b"queue.push(event)", alerts_html)
            self.assertIn(b"maxQueuedAlerts = 100", alerts_html)
            self.assertIn(
                b"if (queue.length >= maxQueuedAlerts) queue.shift()",
                alerts_html,
            )
            self.assertIn(b"queue.length = 0", alerts_html)
            self.assertNotIn(b"innerHTML", alerts_html)

            _status, events_body, _content_type = self._get(
                runtime.base_url
                + "/runtime/events?channel=events&after=0"
            )
            _status, alerts_body, _content_type = self._get(
                runtime.base_url
                + "/runtime/events?channel=alerts&after=0"
            )
            self.assertEqual(
                json.loads(events_body)["events"][0]["type"],
                "follow",
            )
            self.assertEqual(
                json.loads(alerts_body)["events"][0]["type"],
                "subscription",
            )

    def test_runtime_rejects_published_file_modified_after_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, package = self._runtime(Path(tmp))
            package.entry.write_text(
                "<html><body>tampered</body></html>",
                encoding="utf-8",
            )

            with self.assertRaises(HTTPError) as error:
                self._get(
                    runtime.package_url(
                        package.package_id,
                        component="chat",
                    )
                )
            self.assertEqual(error.exception.code, 404)

    def test_runtime_rejects_unpublished_files_added_after_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, package = self._runtime(Path(tmp))
            secret = package.root / "secret.txt"
            secret.write_text("not published", encoding="utf-8")

            with self.assertRaises(HTTPError) as error:
                self._get(
                    runtime.base_url
                    + f"/widgets/{package.package_id}/secret.txt"
                )
            self.assertEqual(error.exception.code, 404)

    def test_runtime_rejects_untrusted_host_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _package = self._runtime(Path(tmp))
            request = Request(
                runtime.base_url + "/health",
                headers={"Host": "attacker.example"},
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=2.0)
            self.assertEqual(error.exception.code, 403)

    def test_runtime_rejects_opaque_and_foreign_origins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _package = self._runtime(Path(tmp))
            for origin in ("null", "http://127.0.0.1:9"):
                request = Request(
                    runtime.base_url + "/runtime/state?component=chat",
                    headers={"Origin": origin},
                )
                with self.assertRaises(HTTPError) as error:
                    urlopen(request, timeout=2.0)
                self.assertEqual(error.exception.code, 403)

            request = Request(
                runtime.base_url + "/runtime/state?component=chat",
                headers={"Origin": runtime.base_url},
            )
            with urlopen(request, timeout=2.0) as response:
                self.assertEqual(response.status, 200)

    def test_imported_package_response_has_restrictive_csp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, package = self._runtime(Path(tmp))
            with urlopen(
                runtime.package_url(
                    package.package_id,
                    component="chat",
                ),
                timeout=2.0,
            ) as response:
                csp = response.headers.get(
                    "Content-Security-Policy",
                    "",
                )
            self.assertIn("connect-src 'none'", csp)
            self.assertIn("frame-src 'none'", csp)
            self.assertIn("object-src 'none'", csp)

    def test_runtime_is_read_only_and_blocks_package_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, package = self._runtime(Path(tmp))

            request = Request(
                runtime.base_url + "/runtime/state",
                data=b"{}",
                method="POST",
            )
            with self.assertRaises(HTTPError) as post_error:
                urlopen(request, timeout=2.0)
            self.assertEqual(post_error.exception.code, 405)

            escape = (
                runtime.base_url
                + f"/widgets/{package.package_id}/%2e%2e/manifest.json"
            )
            with self.assertRaises(HTTPError) as escape_error:
                urlopen(escape, timeout=2.0)
            self.assertEqual(escape_error.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
