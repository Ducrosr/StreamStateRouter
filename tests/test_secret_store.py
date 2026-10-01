from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from stream_state_router.services.secrets import (
    SecretStoreUnavailable,
    WindowsDPAPISecretStore,
)


@unittest.skipUnless(os.name == "nt", "DPAPI Windows uniquement")
class WindowsDPAPISecretStoreTests(unittest.TestCase):
    def test_roundtrip_ciphertext_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = WindowsDPAPISecretStore(root=root)
            value = "access-token:very-secret-value"

            store.set("twitch.oauth", value)

            target = root / "twitch.oauth.dpapi"
            self.assertTrue(target.is_file())
            self.assertNotIn(
                value.encode("utf-8"),
                target.read_bytes(),
            )
            self.assertEqual(store.get("twitch.oauth"), value)

            store.delete("twitch.oauth")
            self.assertIsNone(store.get("twitch.oauth"))

    def test_invalid_secret_name_cannot_escape_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WindowsDPAPISecretStore(root=tmp)

            with self.assertRaises(ValueError):
                store.set("../escape", "secret")


class SecretStorePlatformTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "Non-Windows uniquement")
    def test_dpapi_reports_unavailable_off_windows(self) -> None:
        with self.assertRaises(SecretStoreUnavailable):
            WindowsDPAPISecretStore()


if __name__ == "__main__":
    unittest.main()
