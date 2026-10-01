from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.request import urlopen

from stream_state_router.widgets import (
    WidgetEventHub,
    WidgetRuntime,
    WidgetRuntimeConfig,
    import_html_module,
)


class WidgetRuntimeTests(unittest.TestCase):
    def test_event_hub_state_and_publish(self) -> None:
        hub = WidgetEventHub()
        queue = hub.subscribe("radio")
        hub.set_state("radio", {"title": "Track A"})
        event = queue.get(timeout=1.0)

        self.assertEqual(hub.snapshot("radio"), {"title": "Track A"})
        self.assertEqual(event.event_type, "state")
        self.assertEqual(event.payload, {"title": "Track A"})

        update = hub.publish(
            "radio",
            "track",
            {"title": "Track B"},
            update_state=True,
        )
        self.assertEqual(update.sequence, event.sequence + 1)
        self.assertEqual(hub.snapshot("radio"), {"title": "Track B"})

    def test_http_runtime_serves_package_health_state_and_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            library = root / "library"
            source.mkdir()
            (source / "index.html").write_text(
                '<script src="/runtime/client.js"></script><h1>SSR</h1>',
                encoding="utf-8",
            )
            package = import_html_module(
                source / "index.html",
                name="Test Widget",
                target_root=library,
            )
            runtime = WidgetRuntime(
                WidgetRuntimeConfig(
                    enabled=True,
                    host="127.0.0.1",
                    port=0,
                ),
                library_root=library,
            )
            runtime.start()
            try:
                runtime.set_state(
                    "test",
                    {"message": "hello"},
                )
                with urlopen(
                    runtime.base_url + "/health",
                    timeout=2.0,
                ) as response:
                    health = json.loads(
                        response.read().decode("utf-8")
                    )
                self.assertTrue(health["ok"])

                with urlopen(
                    runtime.widget_url(package.package_id),
                    timeout=2.0,
                ) as response:
                    html = response.read().decode("utf-8")
                self.assertIn("<h1>SSR</h1>", html)

                with urlopen(
                    runtime.base_url + "/runtime/client.js",
                    timeout=2.0,
                ) as response:
                    client = response.read().decode("utf-8")
                self.assertIn("window.SSRWidget", client)

                with urlopen(
                    runtime.base_url + "/api/state/test",
                    timeout=2.0,
                ) as response:
                    state = json.loads(
                        response.read().decode("utf-8")
                    )
                self.assertEqual(
                    state["state"],
                    {"message": "hello"},
                )
            finally:
                runtime.stop()

    def test_http_runtime_sse_delivers_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = WidgetRuntime(
                WidgetRuntimeConfig(
                    enabled=True,
                    host="127.0.0.1",
                    port=0,
                ),
                library_root=Path(tmp),
            )
            runtime.start()
            received: list[str] = []
            ready = threading.Event()

            def reader() -> None:
                with urlopen(
                    runtime.base_url + "/events/chat",
                    timeout=3.0,
                ) as response:
                    ready.set()
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline:
                        raw = response.readline().decode(
                            "utf-8",
                            errors="replace",
                        )
                        if raw.startswith("data: "):
                            received.append(raw[6:].strip())
                            if len(received) >= 2:
                                return

            thread = threading.Thread(
                target=reader,
                daemon=True,
            )
            thread.start()
            self.assertTrue(ready.wait(timeout=2.0))
            runtime.publish(
                "chat",
                "message",
                {"user": "Cloud", "text": "Salut"},
            )
            thread.join(timeout=3.0)
            runtime.stop()

            self.assertGreaterEqual(len(received), 2)
            event = json.loads(received[-1])
            self.assertEqual(event["type"], "message")
            self.assertEqual(event["payload"]["user"], "Cloud")

    def test_runtime_blocks_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package_root = root / "widget"
            package_root.mkdir()
            (package_root / "manifest.json").write_text(
                json.dumps(
                    {
                        "package_id": "widget",
                        "name": "Widget",
                        "entry": "index.html",
                    }
                ),
                encoding="utf-8",
            )
            (package_root / "index.html").write_text(
                "<html></html>",
                encoding="utf-8",
            )
            (root / "outside.txt").write_text(
                "secret",
                encoding="utf-8",
            )
            runtime = WidgetRuntime(
                WidgetRuntimeConfig(
                    enabled=True,
                    host="127.0.0.1",
                    port=0,
                ),
                library_root=root,
            )
            runtime.start()
            try:
                with self.assertRaises(Exception):
                    urlopen(
                        runtime.base_url
                        + "/widgets/widget/../outside.txt",
                        timeout=2.0,
                    )
            finally:
                runtime.stop()

    def test_runtime_rejects_non_local_bind(self) -> None:
        config = WidgetRuntimeConfig(
            enabled=True,
            host="0.0.0.0",
            port=17861,
        )
        with self.assertRaisesRegex(
            ValueError,
            "doit rester local",
        ):
            config.validate()


if __name__ == "__main__":
    unittest.main()
