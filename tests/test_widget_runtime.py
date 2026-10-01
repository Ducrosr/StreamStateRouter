from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

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
