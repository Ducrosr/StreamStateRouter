from __future__ import annotations

import json
from pathlib import Path
import unittest

from stream_state_router import __version__
from stream_state_router.services.single_instance import SINGLE_INSTANCE_MUTEX


class PackagingTests(unittest.TestCase):
    def test_release_metadata_versions_match(self):
        root = Path(__file__).resolve().parents[1]
        plugin_root = root / "streamdeck-plugin"

        package = json.loads(
            (plugin_root / "package.json").read_text(encoding="utf-8")
        )
        lock = json.loads(
            (plugin_root / "package-lock.json").read_text(encoding="utf-8")
        )
        manifest = json.loads(
            (
                plugin_root
                / "com.remyducros.streamstaterouter.sdPlugin"
                / "manifest.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(package["version"], __version__)
        self.assertEqual(lock["version"], __version__)
        self.assertEqual(lock["packages"][""]["version"], __version__)
        self.assertEqual(manifest["Version"], f"{__version__}.0")

    def test_installer_mutex_matches_runtime_single_instance_mutex(self):
        root = Path(__file__).resolve().parents[1]
        installer = (
            root / "installer" / "StreamStateRouter.iss"
        ).read_text(encoding="utf-8")

        expected = f"AppMutex={SINGLE_INSTANCE_MUTEX}"
        self.assertIn(expected, installer)


if __name__ == "__main__":
    unittest.main()
