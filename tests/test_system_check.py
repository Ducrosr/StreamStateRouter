from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from stream_state_router.services.system_check import (
    probe_host_capabilities,
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
        self.session_generation = 7
        self.requests: list[tuple[str, object]] = []
        self.__class__.instances.append(self)

    def probe(self):
        return True, "OBS WebSocket connecté — OBS 32.2.2"

    def send(self, request: str, data=None):
        self.requests.append((request, data))
        if request == "GetVersion":
            return {"availableRequests": [
                "GetVersion", "GetSceneCollectionList", "GetSceneList",
                "GetGroupList", "GetSceneItemList", "GetGroupSceneItemList",
                "GetInputList", "GetSceneTransitionList", "GetVideoSettings",
            ]}
        if request == "GetSceneCollectionList":
            return {"currentSceneCollectionName": "Streaming"}
        if request == "GetSceneList":
            return {
                "currentProgramSceneName": "In Game",
                "currentProgramSceneUuid": "scene-1",
                "scenes": [
                    {"sceneName": "In Game", "sceneUuid": "scene-1", "sceneIndex": 0},
                    {"sceneName": "Just Chatting", "sceneUuid": "scene-2", "sceneIndex": 1},
                ],
            }
        if request == "GetGroupList":
            return {"groups": ["Group A"]}
        if request == "GetSceneItemList":
            scene = str((data or {}).get("sceneName") or "")
            if scene == "In Game":
                return {"sceneItems": [
                    {"sceneItemId": 1, "sourceName": "Game Capture", "sourceUuid": "input-game", "inputKind": "game_capture", "sceneItemEnabled": True},
                    {"sceneItemId": 2, "sourceName": "Group A", "sourceUuid": "group-a", "isGroup": True, "sceneItemEnabled": True},
                ]}
            return {"sceneItems": [
                {"sceneItemId": 3, "sourceName": "Micro", "sourceUuid": "input-mic", "inputKind": "wasapi_input_capture", "sceneItemEnabled": True}
            ]}
        if request == "GetGroupSceneItemList":
            group = str((data or {}).get("sceneName") or "")
            if group == "Group A":
                return {"sceneItems": [
                    {"sceneItemId": 4, "sourceName": "Group B", "sourceUuid": "group-b", "isGroup": True, "sceneItemEnabled": True}
                ]}
            if group == "Group B":
                return {"sceneItems": [
                    {"sceneItemId": 5, "sourceName": "Overlay", "sourceUuid": "input-overlay", "inputKind": "browser_source", "sceneItemEnabled": True}
                ]}
            raise AssertionError(f"Unexpected group: {group}")
        if request == "GetInputList":
            return {"inputs": [
                {"inputName": "Game Capture", "inputKind": "game_capture", "inputUuid": "input-game"},
                {"inputName": "Micro", "inputKind": "wasapi_input_capture", "inputUuid": "input-mic"},
                {"inputName": "Overlay", "inputKind": "browser_source", "inputUuid": "input-overlay"},
            ]}
        if request == "GetSceneTransitionList":
            return {"transitions": [
                {"transitionName": "Fade", "transitionKind": "fade_transition", "transitionUuid": "transition-fade"}
            ]}
        if request == "GetVideoSettings":
            return {"baseWidth": 2560, "baseHeight": 1440}
        if request == "GetSourceFilterList":
            source = str((data or {}).get("sourceName") or "")
            if source == "Game Capture":
                return {"filters": [
                    {
                        "filterName": "HDR Tone Map",
                        "filterKind": "shader_filter",
                        "filterEnabled": True,
                    }
                ]}
            return {"filters": []}
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
        requests = [request for request, _data in client.requests]
        self.assertTrue(requests)
        self.assertTrue(all(request.startswith("Get") for request in requests))
        for required in (
            "GetVersion",
            "GetSceneCollectionList",
            "GetSceneList",
            "GetGroupList",
            "GetSceneItemList",
            "GetGroupSceneItemList",
            "GetInputList",
            "GetSceneTransitionList",
            "GetVideoSettings",
        ):
            self.assertIn(required, requests)
        self.assertEqual(requests.count("GetGroupSceneItemList"), 2)
        self.assertEqual(controller.audio_router.resolve_calls, 1)
        self.assertEqual(
            controller.hdr_controller.status_calls,
            ["primary"],
        )
        items = {item.key: item for item in report.capabilities.items}
        self.assertEqual(items["obs"].status, "ready")
        self.assertEqual(items["catalog"].status, "ready")
        self.assertEqual(items["obs_requests"].status, "unused")
        self.assertEqual(items["obs_references"].status, "ready")
        self.assertIn("2 scène(s)", items["catalog"].detail)
        self.assertIn("2 groupe(s)", items["catalog"].detail)
        self.assertIn("3 input(s)", items["catalog"].detail)
        self.assertIn("5 Scene Item(s)", items["catalog"].detail)
        self.assertEqual(items["audio"].status, "ready")
        self.assertEqual(items["hdr"].status, "ready")
        self.assertTrue(report.ok)

    def test_required_obs_request_missing_is_blocking(self) -> None:
        config = _config()
        config["obs"]["enabled"] = True
        config["profiles"]["audio"]["Default"]["actions"] = [
            {
                "type": "input_mute",
                "enabled": True,
                "params": {"input": "Micro", "muted": False},
            }
        ]

        report = run_system_check(
            config,
            obs_client_factory=_FakeOBSClient,
        )

        items = {item.key: item for item in report.capabilities.items}
        self.assertEqual(items["obs_requests"].status, "error")
        self.assertIn("SetInputMute", items["obs_requests"].detail)
        self.assertFalse(report.ok)
        payload = report.as_mapping()["obs_requests"]
        self.assertEqual(payload["missing"][0]["request"], "SetInputMute")

    def test_required_obs_request_matrix_reports_owners(self) -> None:
        config = _config()
        config["obs"]["enabled"] = True
        config["profiles"]["audio"]["Default"]["actions"] = [
            {
                "type": "input_mute",
                "enabled": True,
                "params": {"input": "Micro", "muted": False},
            }
        ]

        class CompatibleOBS(_FakeOBSClient):
            def send(self, request: str, data=None):
                response = super().send(request, data)
                if request == "GetVersion":
                    response = dict(response)
                    response["availableRequests"] = [
                        *response["availableRequests"],
                        "SetInputMute",
                    ]
                return response

        report = run_system_check(
            config,
            obs_client_factory=CompatibleOBS,
        )

        items = {item.key: item for item in report.capabilities.items}
        self.assertEqual(items["obs_requests"].status, "ready")
        required = {
            row["request"]: row
            for row in report.as_mapping()["obs_requests"]["required"]
        }
        self.assertIn("SetInputMute", required)
        self.assertTrue(
            any(
                "audio/Default" in owner
                for owner in required["SetInputMute"]["owners"]
            )
        )

    def test_reference_lint_reports_missing_scene_and_candidate(self) -> None:
        config = _config()
        config["obs"]["enabled"] = True
        config["layout_profiles"]["Vanilla"]["scene"] = "In Gmae"

        report = run_system_check(
            config,
            obs_client_factory=_FakeOBSClient,
        )

        items = {item.key: item for item in report.capabilities.items}
        self.assertEqual(items["obs_references"].status, "error")
        payload = report.as_mapping()["references"]
        issue = next(
            item
            for item in payload["issues"]
            if item["current"] == "In Gmae"
        )
        self.assertEqual(issue["candidate"], "In Game")
        self.assertFalse(report.ok)

    def test_filter_reference_lint_reads_only_configured_source(self) -> None:
        config = _config()
        config["obs"]["enabled"] = True
        config["profiles"]["overlay"]["Vanilla"]["actions"] = [
            {
                "type": "source_filter_enabled",
                "enabled": True,
                "params": {
                    "source": "Game Capture",
                    "filter": "HDR Tone Mapp",
                    "enabled": True,
                },
            }
        ]

        class CompatibleOBS(_FakeOBSClient):
            def send(self, request: str, data=None):
                response = super().send(request, data)
                if request == "GetVersion":
                    response = dict(response)
                    response["availableRequests"] = [
                        *response["availableRequests"],
                        "SetSourceFilterEnabled",
                    ]
                return response

        report = run_system_check(
            config,
            obs_client_factory=CompatibleOBS,
        )

        client = _FakeOBSClient.instances[-1]
        filter_reads = [
            data
            for request, data in client.requests
            if request == "GetSourceFilterList"
        ]
        self.assertEqual(
            filter_reads,
            [{"sourceName": "Game Capture"}],
        )
        payload = report.as_mapping()["references"]
        issue = next(
            item
            for item in payload["issues"]
            if item["current"] == "HDR Tone Mapp"
        )
        self.assertEqual(issue["candidate"], "HDR Tone Map")

    def test_reference_lint_remains_read_only(self) -> None:
        config = _config()
        config["obs"]["enabled"] = True
        config["profiles"]["overlay"]["Vanilla"]["actions"] = [
            {
                "type": "source_filter_enabled",
                "enabled": True,
                "params": {
                    "source": "Game Capture",
                    "filter": "HDR Tone Map",
                    "enabled": True,
                },
            }
        ]

        class CompatibleOBS(_FakeOBSClient):
            def send(self, request: str, data=None):
                response = super().send(request, data)
                if request == "GetVersion":
                    response = dict(response)
                    response["availableRequests"] = [
                        *response["availableRequests"],
                        "SetSourceFilterEnabled",
                    ]
                return response

        report = run_system_check(
            config,
            obs_client_factory=CompatibleOBS,
        )

        client = _FakeOBSClient.instances[-1]
        self.assertTrue(
            all(
                request.startswith("Get")
                for request, _data in client.requests
            )
        )
        items = {item.key: item for item in report.capabilities.items}
        self.assertEqual(items["obs_references"].status, "ready")

    def test_host_probe_is_shared_and_read_only(self) -> None:
        config = _config()
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
                    "enabled": False,
                    "display": "primary",
                },
            }
        ]
        controller = _FakeHostController()

        audio, hdr = probe_host_capabilities(
            config,
            controller_factory=lambda _config: controller,
        )

        self.assertEqual(audio["status"], "ready")
        self.assertEqual(hdr["status"], "ready")
        self.assertEqual(controller.audio_router.resolve_calls, 1)
        self.assertEqual(controller.hdr_controller.status_calls, ["primary"])

    def test_hdr_probe_uses_all_scope_when_any_action_targets_all(self) -> None:
        config = _config()
        config["profiles"]["capture"]["Default"]["actions"] = [
            {
                "type": "windows_hdr",
                "params": {
                    "enabled": True,
                    "display": "all",
                },
            }
        ]
        controller = _FakeHostController()

        _audio, hdr = probe_host_capabilities(
            config,
            controller_factory=lambda _config: controller,
        )

        self.assertEqual(hdr["status"], "ready")
        self.assertEqual(controller.hdr_controller.status_calls, ["all"])

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

    def test_catalog_probe_fails_closed_if_reader_attempts_mutation(self) -> None:
        config = _config()
        config["obs"]["enabled"] = True

        class MutatingReader:
            def __init__(self, client) -> None:
                self.client = client

            def sync(self):
                self.client.send(
                    "SetInputMute",
                    {"inputName": "Micro", "inputMuted": True},
                )
                raise AssertionError("mutation should have been rejected")

        with patch(
            "stream_state_router.services.system_check.OBSResourceCatalogReader",
            MutatingReader,
        ):
            report = run_system_check(
                config,
                obs_client_factory=_FakeOBSClient,
            )

        client = _FakeOBSClient.instances[-1]
        self.assertFalse(
            any(request.startswith("Set") for request, _data in client.requests)
        )
        items = {item.key: item for item in report.capabilities.items}
        self.assertEqual(items["obs"].status, "ready")
        self.assertEqual(items["catalog"].status, "warning")
        self.assertIn("read-only", items["catalog"].detail)

    def test_catalog_failure_is_warning_not_mutation(self) -> None:
        config = _config()
        config["obs"]["enabled"] = True

        class PartialOBS(_FakeOBSClient):
            def send(self, request: str, data=None):
                if request == "GetSceneItemList":
                    self.requests.append((request, data))
                    raise RuntimeError("catalog read failed")
                return super().send(request, data)

        report = run_system_check(
            config,
            obs_client_factory=PartialOBS,
        )

        items = {item.key: item for item in report.capabilities.items}
        self.assertEqual(items["obs"].status, "ready")
        self.assertEqual(items["catalog"].status, "warning")
        self.assertIn("catalog read failed", items["catalog"].detail)

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
