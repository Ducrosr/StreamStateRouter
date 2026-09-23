from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from stream_state_router.ui.main_window import MainWindow


class _FakeSceneSelector:
    def __init__(self, value: str) -> None:
        self.value = value

    def currentText(self) -> str:
        return self.value


class LayoutToolIsolationTests(unittest.TestCase):
    def test_auto_scan_only_queues_runtime_catalog_command(self) -> None:
        service = Mock()
        service.request_layout_catalog.return_value = "request-1"
        runtime_client = SimpleNamespace(
            config=SimpleNamespace(enabled=True),
            connected=True,
        )
        exploding_manager = SimpleNamespace(
            discover_scene=Mock(
                side_effect=AssertionError(
                    "Qt auto-scan must not call OBS directly"
                )
            )
        )
        window = SimpleNamespace(
            _service=service,
            _client=runtime_client,
            _pending_auto_layout_catalog_request="",
            _pending_auto_layout_catalog_scene="",
            _obs_module_catalog={"existing": [{"source": "Existing"}]},
            _layout_sync_manager=exploding_manager,
            layout_scene=_FakeSceneSelector("In Game"),
        )

        MainWindow._auto_scan_modules(window)

        service.request_layout_catalog.assert_called_once_with("In Game")
        exploding_manager.discover_scene.assert_not_called()
        self.assertEqual(
            window._pending_auto_layout_catalog_request,
            "request-1",
        )
        self.assertEqual(
            window._pending_auto_layout_catalog_scene,
            "In Game",
        )

    def test_dispose_layout_tool_manager_closes_only_independent_client(self) -> None:
        runtime_client = SimpleNamespace()
        tool_client = SimpleNamespace(close=Mock())
        manager = SimpleNamespace(client=tool_client)
        window = SimpleNamespace(
            _layout_sync_manager=manager,
            _client=runtime_client,
            _log=Mock(),
        )

        MainWindow._dispose_layout_sync_manager(window)

        self.assertIsNone(window._layout_sync_manager)
        tool_client.close.assert_called_once_with()

    def test_dispose_never_closes_runtime_owned_client(self) -> None:
        runtime_client = SimpleNamespace(close=Mock())
        manager = SimpleNamespace(client=runtime_client)
        window = SimpleNamespace(
            _layout_sync_manager=manager,
            _client=runtime_client,
            _log=Mock(),
        )

        MainWindow._dispose_layout_sync_manager(window)

        self.assertIsNone(window._layout_sync_manager)
        runtime_client.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
