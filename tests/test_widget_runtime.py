from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from stream_state_router.media import MediaState, MediaStateStore
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
                        }
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
            self.assertIn(
                '"/runtime/state?component="',
                html,
            )
            self.assertIn(
                'value.startsWith("widget:")',
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

    def test_now_playing_endpoint_and_builtin_widget_are_token_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime, _package = self._runtime(root)
            runtime.stop()

            media_store = MediaStateStore()
            media_store.update(
                MediaState(
                    provider="jellyfin",
                    player="Streaming PC",
                    track_id="track-1",
                    title="Mako Reactor",
                    artists=("Shinra Radio",),
                    album="Midgar",
                    duration_seconds=240.0,
                    position_seconds=60.0,
                    playback="playing",
                    artwork_key="track-1",
                )
            )
            calls: list[str] = []
            runtime = WidgetRuntime(
                WidgetRuntimeConfig(
                    enabled=True,
                    host="127.0.0.1",
                    port=0,
                ),
                PresentationStateStore(),
                media_store=media_store,
                media_artwork=lambda key: (
                    calls.append(key) or (b"cover", "image/jpeg")
                ),
                library_root=root / "library",
            )
            runtime.start()
            self.addCleanup(runtime.stop)

            _status, body, content_type = self._get(
                runtime.base_url + "/runtime/media"
            )
            payload = json.loads(body)
            self.assertIn("application/json", content_type)
            self.assertEqual(payload["title"], "Mako Reactor")
            self.assertEqual(payload["artists"], ["Shinra Radio"])
            self.assertNotIn("token", payload)

            _status, artwork, content_type = self._get(
                runtime.base_url
                + "/runtime/media/artwork?key=track-1"
            )
            self.assertEqual(artwork, b"cover")
            self.assertEqual(content_type, "image/jpeg")
            self.assertEqual(calls, ["track-1"])

            _status, html, content_type = self._get(
                runtime.base_url + "/builtin/now-playing"
            )
            self.assertIn("text/html", content_type)
            self.assertIn(b"/runtime/media", html)
            self.assertIn(b"/runtime/media/artwork", html)
            self.assertNotIn(b"X-Emby-Token", html)

    def test_component_host_supports_builtin_now_playing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _package = self._runtime(Path(tmp))

            _status, body, _content_type = self._get(
                runtime.base_url + "/component/now-playing"
            )
            html = body.decode("utf-8")

            self.assertIn(
                'const fallback = "builtin:now-playing"',
                html,
            )
            self.assertIn(
                '["chat","events","alerts","now-playing"]',
                html,
            )

    def test_builtin_clock_and_countdown_are_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _package = self._runtime(Path(tmp))

            _status, clock, content_type = self._get(
                runtime.base_url + "/builtin/clock"
            )
            self.assertIn("text/html", content_type)
            self.assertIn(b"Intl.DateTimeFormat", clock)
            self.assertIn(
                b"/runtime/bridge.js?component=clock",
                clock,
            )

            runtime.event_bus.publish(
                channel="countdown",
                type="set",
                platform="ssr",
                payload={
                    "label": "J’ARRIVE",
                    "target_epoch": 9999999999,
                    "hide_at_zero": True,
                },
            )
            _status, countdown, content_type = self._get(
                runtime.base_url + "/builtin/countdown"
            )
            self.assertIn("text/html", content_type)
            self.assertIn(
                b"/runtime/events?channel=countdown",
                countdown,
            )
            self.assertIn(b"hide_at_zero", countdown)

            _status, host, _content_type = self._get(
                runtime.base_url + "/component/countdown"
            )
            html = host.decode("utf-8")
            self.assertIn(
                'const fallback = "builtin:countdown"',
                html,
            )

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
