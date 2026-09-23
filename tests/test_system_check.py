from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from stream_state_router.services.system_check import (
    render_system_check_report,
    run_system_check,
)


def _config() -> dict:
    return json.loads(
        Path("config/default.json").read_text(encoding="utf-8")
    )


class _FakeOBSClient:
    instances: list["_FakeOBSClient"] = []

    def __init__(self, config) -> None:
        self.config = config
        self.closed = False
        self.requests: list[tuple[str, object]] = []
        self.__class__.instances.append(self)

    def probe(self):
        return True, "OBS WebSocket connecté — OBS 32.2.2"

    def send(self, request: str, data=None):
        self.requests.append((request, data))
        if request == "GetSceneList":
            return {
                "scenes": [
                    {"sceneName": "In Game"},
                    {"sceneName": "Just Chatting"},
                ]
            }
        if request == "GetInputList":
            return {
                "inputs": [
                    {"inputName": "Game Capture"},
                    {"inputName": "Micro"},
                ]
            }
        if request == "GetSceneItemList":
            scene = str((data or {}).get("sceneName") or "")
            return {
                "sceneItems": (
                    [{"sceneItemId": 1}, {"sceneItemId": 2}]
                    if scene == "In Game"
                    else [{"sceneItemId": 3}]
                )
            }
        raise AssertionError(f"Unexpected request: {request}")

    def close(self) -> None:
        self.closed = True


class _FakeAudioRouter:
    def __init__(self) -> None:
        self.resolve_calls = 0

    def resolve_executable(self) -> str:
        self.resolve_calls += 1
        return r"C:\Tools\SoundVolumeView.exe"


class _FakeHDRController:
    def __init__(self) -> None:
        self.status_calls: list[str] = []

    def status(self, *, scope: str = "primary"):
        self.status_calls.append(scope)
        return (
            {
                "source": r"\\.\DISPLAY1",
                "supported": True,
                "enabled": True,
                "primary": True,
            },
        )


class _FakeHostController:
    def __init__(self) -> None:
        self.audio_router = _FakeAudioRouter()
        self.hdr_controller = _FakeHDRController()


class SystemCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeOBSClient.instances.clear()

    def test_disabled_unused_capabilities_need_no_external_probe(self) -> None:
        config = _config()
        host_calls = []

        def host_factory(_config):
            host_calls.append(True)
            raise AssertionError("host controller should not be created")

        report = run_system_check(
            config,
            obs_client_factory=_FakeOBSClient,
            host_controller_factory=host_factory,
        )

        self.assertEqual(report.config_errors, ())
        self.assertEqual(_FakeOBSClient.instances, [])
        self.assertEqual(host_calls, [])
        items = {item.key: item for item in report.capabilities.items}
        self.assertEqual(items["obs"].status, "disabled")
        self.assertEqual(items["audio"].status, "unused")
        self.assertEqual(items["hdr"].status, "unused")

    def test_read_only_check_probes_obs_catalog_audio_and_hdr(self) -> None:
        config = _config()
        config["obs"]["enabled"] = True
        config["profiles"]["audio"]["Default"]["actions"] = [
            {
                "type": "app_audio_output",
                "params": {
                    "device": "Game",
                    "process": "Overwatch.exe",
                    "roles": "all",
                },
            }
        ]
        config["profiles"]["capture"]["Default"]["actions"] = [
            {
                "type": "windows_hdr",
                "params": {
                    "enabled": True,
                    "display": "primary",
                },
            }
        ]
        controller = _FakeHostController()

        report = run_system_check(
            config,
            obs_client_factory=_FakeOBSClient,
            host_controller_factory=lambda _config: controller,
        )

        self.assertEqual(report.config_errors, ())
        self.assertEqual(len(_FakeOBSClient.instances), 1)
        client = _FakeOBSClient.instances[0]
        self.assertTrue(client.closed)
        self.assertEqual(
            [request for request, _data in client.requests],
            [
                "GetSceneList",
                "GetInputList",
                "GetSceneItemList",
                "GetSceneItemList",
            ],
        )
        self.assertEqual(controller.audio_router.resolve_calls, 1)
        self.assertEqual(
            controller.hdr_controller.status_calls,
            ["primary"],
        )
        items = {item.key: item for item in report.capabilities.items}
        self.assertEqual(items["obs"].status, "ready")
        self.assertEqual(items["catalog"].status, "ready")
        self.assertIn("2 scène(s)", items["catalog"].detail)
        self.assertIn("3 Scene Item(s)", items["catalog"].detail)
        self.assertEqual(items["audio"].status, "ready")
        self.assertEqual(items["hdr"].status, "ready")
        self.assertTrue(report.ok)

    def test_probe_failure_is_reported_without_throwing(self) -> None:
        config = _config()
        config["obs"]["enabled"] = True

        class FailingOBS(_FakeOBSClient):
            def probe(self):
                return False, "OBS WebSocket : timed out"

        report = run_system_check(
            config,
            obs_client_factory=FailingOBS,
        )

        items = {item.key: item for item in report.capabilities.items}
        self.assertEqual(items["obs"].status, "error")
        self.assertIn("timed out", items["obs"].detail)
        self.assertFalse(report.ok)
        self.assertTrue(FailingOBS.instances[-1].closed)

    def test_invalid_config_forces_error_status(self) -> None:
        config = _config()
        config["profiles"]["audio"]["Default"]["actions"] = [
            {
                "type": "app_audio_output",
                "params": {
                    "device": "",
                    "process": "",
                    "roles": "gaming",
                },
            }
        ]

        report = run_system_check(config)

        self.assertTrue(report.config_errors)
        self.assertEqual(report.status, "error")
        self.assertFalse(report.ok)

    def test_json_mapping_and_text_never_include_obs_password(self) -> None:
        config = _config()
        config["obs"]["password"] = "do-not-print-me"

        report = run_system_check(config)
        serialized = json.dumps(
            report.as_mapping(),
            ensure_ascii=False,
        )
        rendered = render_system_check_report(report)

        self.assertNotIn("do-not-print-me", serialized)
        self.assertNotIn("do-not-print-me", rendered)
        self.assertIn("Configuration", rendered)
        self.assertIn("Résultat", rendered)


if __name__ == "__main__":
    unittest.main()
