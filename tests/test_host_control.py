from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from stream_state_router.host import (
    HostControlConfig,
    HostControlController,
    SoundVolumeViewAudioRouter,
    WindowsHDRController,
)


class FakeRunner:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), dict(kwargs)))
        return SimpleNamespace(
            returncode=self.returncode,
            stdout="",
            stderr="failed" if self.returncode else "",
        )


class FakeHDRAPI:
    def __init__(self):
        self.primary = object()
        self.secondary = object()
        self.states = {
            self.primary: False,
            self.secondary: False,
        }
        self.supported = {
            self.primary: True,
            self.secondary: True,
        }
        self.set_calls: list[tuple[object, bool]] = []

    def active_paths(self):
        return (self.primary, self.secondary)

    def primary_device_name(self):
        return r"\\.\DISPLAY1"

    def source_name(self, path):
        return (
            r"\\.\DISPLAY1"
            if path is self.primary
            else r"\\.\DISPLAY2"
        )

    def advanced_color_info(self, path):
        return SimpleNamespace(
            supported=self.supported[path],
            enabled=self.states[path],
        )

    def set_advanced_color(self, path, enabled):
        self.set_calls.append((path, bool(enabled)))
        self.states[path] = bool(enabled)


class FakeAudioRouter:
    def __init__(self):
        self.calls = []

    def set_app_default(self, **kwargs):
        self.calls.append(dict(kwargs))


class FakeHDRController:
    def __init__(self):
        self.calls = []

    def set_enabled(self, enabled, *, scope):
        self.calls.append((bool(enabled), scope))
        return 1


class HostControlTests(unittest.TestCase):
    def test_soundvolumeview_builds_exact_set_app_default_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "SoundVolumeView.exe"
            exe.write_bytes(b"stub")
            runner = FakeRunner()
            router = SoundVolumeViewAudioRouter(
                str(exe),
                timeout_seconds=3.5,
                runner=runner,
            )

            router.set_app_default(
                device="Game",
                process="Dofus.exe",
                roles="all",
            )

            self.assertEqual(len(runner.calls), 1)
            command, kwargs = runner.calls[0]
            self.assertEqual(
                command,
                [
                    str(exe),
                    "/SetAppDefault",
                    "Game",
                    "all",
                    "Dofus.exe",
                ],
            )
            self.assertEqual(kwargs["timeout"], 3.5)
            self.assertFalse(kwargs["check"])
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["text"])

    def test_soundvolumeview_rejects_invalid_role_before_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "SoundVolumeView.exe"
            exe.write_bytes(b"stub")
            runner = FakeRunner()
            router = SoundVolumeViewAudioRouter(str(exe), runner=runner)

            with self.assertRaisesRegex(ValueError, "roles"):
                router.set_app_default(
                    device="Game",
                    process="Overwatch.exe",
                    roles="gaming",
                )

            self.assertEqual(runner.calls, [])

    def test_hdr_primary_changes_only_primary_display_and_acknowledges(self):
        api = FakeHDRAPI()
        controller = WindowsHDRController(api=api)

        changed = controller.set_enabled(True, scope="primary")

        self.assertEqual(changed, 1)
        self.assertEqual(api.set_calls, [(api.primary, True)])
        self.assertTrue(api.states[api.primary])
        self.assertFalse(api.states[api.secondary])

    def test_hdr_all_is_noop_for_already_matching_displays(self):
        api = FakeHDRAPI()
        api.states[api.primary] = True
        api.states[api.secondary] = True
        controller = WindowsHDRController(api=api)

        changed = controller.set_enabled(True, scope="all")

        self.assertEqual(changed, 0)
        self.assertEqual(api.set_calls, [])

    def test_host_controller_routes_supported_actions(self):
        audio = FakeAudioRouter()
        hdr = FakeHDRController()
        controller = HostControlController(
            HostControlConfig(),
            audio_router=audio,
            hdr_controller=hdr,
        )

        self.assertTrue(
            controller.execute(
                "app_audio_output",
                {
                    "device": "Game",
                    "process": "Overwatch.exe",
                    "roles": "all",
                },
            )
        )
        self.assertTrue(
            controller.execute(
                "windows_hdr",
                {"enabled": True, "display": "primary"},
            )
        )
        self.assertFalse(controller.execute("other", {}))
        self.assertEqual(
            audio.calls,
            [
                {
                    "device": "Game",
                    "process": "Overwatch.exe",
                    "roles": "all",
                }
            ],
        )
        self.assertEqual(hdr.calls, [(True, "primary")])


if __name__ == "__main__":
    unittest.main()
