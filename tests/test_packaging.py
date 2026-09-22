from __future__ import annotations

from pathlib import Path
import unittest

from stream_state_router.services.single_instance import SINGLE_INSTANCE_MUTEX


class PackagingTests(unittest.TestCase):
    def test_installer_mutex_matches_runtime_single_instance_mutex(self):
        root = Path(__file__).resolve().parents[1]
        installer = (
            root / "installer" / "StreamStateRouter.iss"
        ).read_text(encoding="utf-8")

        expected = f"AppMutex={SINGLE_INSTANCE_MUTEX}"
        self.assertIn(expected, installer)


if __name__ == "__main__":
    unittest.main()
