from __future__ import annotations

import unittest

from stream_state_router.platforms import (
    TwitchCredentialBundle,
    TwitchCredentialStore,
)
from stream_state_router.platforms.twitch_session import (
    TwitchOAuthTokens,
    TwitchTokenValidation,
)


class _MemorySecretStore:
    def __init__(self):
        self.values = {}

    def set(self, name, value):
        self.values[name] = (
            value.decode("utf-8")
            if isinstance(value, bytes)
            else str(value)
        )

    def get(self, name):
        return self.values.get(name)

    def delete(self, name):
        self.values.pop(name, None)


class TwitchCredentialStoreTests(unittest.TestCase):
    def test_bundle_roundtrip_validation_and_refresh_rotation(self) -> None:
        backend = _MemorySecretStore()
        store = TwitchCredentialStore(backend)
        bundle = TwitchCredentialBundle(
            client_id="client",
            access_token="access-1",
            refresh_token="refresh-1",
        )
        validated = bundle.with_validation(
            TwitchTokenValidation(
                client_id="client",
                user_id="123",
                login="remy",
                scopes=("user:read:chat", "bits:read"),
                expires_in=3600,
            )
        )
        store.save(validated)

        loaded = store.load()
        self.assertEqual(loaded, validated)
        self.assertNotIn(
            "twitch",
            vars(store),
        )

        rotated = loaded.rotate(
            TwitchOAuthTokens(
                access_token="access-2",
                refresh_token="refresh-2",
                expires_in=14400,
                scopes=("user:read:chat", "bits:read"),
            )
        )
        store.save(rotated)

        latest = store.load()
        self.assertEqual(latest.access_token, "access-2")
        self.assertEqual(latest.refresh_token, "refresh-2")
        self.assertEqual(latest.user_id, "123")
        self.assertEqual(latest.login, "remy")

        store.delete()
        self.assertIsNone(store.load())

    def test_store_rejects_incomplete_secret(self) -> None:
        backend = _MemorySecretStore()
        store = TwitchCredentialStore(backend)

        with self.assertRaises(ValueError):
            store.save(
                TwitchCredentialBundle(
                    client_id="client",
                    access_token="",
                    refresh_token="refresh",
                )
            )


if __name__ == "__main__":
    unittest.main()
