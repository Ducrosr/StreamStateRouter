from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from stream_state_router.events import EventBus
from stream_state_router.platforms import (
    TwitchAudiencePoller,
    TwitchAudienceState,
    TwitchEventSubConfig,
    TwitchHelixClient,
)


class TwitchHelixTests(unittest.TestCase):
    def _config(self) -> TwitchEventSubConfig:
        return TwitchEventSubConfig(
            enabled=True,
            client_id="client",
            user_access_token="token",
            broadcaster_user_id="100",
        )

    def test_validate_token_maps_identity_and_scopes(self) -> None:
        payload = {
            "client_id": "client",
            "user_id": "100",
            "login": "remy",
            "scopes": ["user:read:chat"],
            "expires_in": 3600,
        }

        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self):
                return json.dumps(payload).encode("utf-8")

        with patch(
            "stream_state_router.platforms.twitch_helix.urlopen",
            return_value=Response(),
        ):
            info = TwitchHelixClient(self._config()).validate_token()

        self.assertEqual(info.client_id, "client")
        self.assertEqual(info.user_id, "100")
        self.assertEqual(info.login, "remy")
        self.assertEqual(info.scopes, ("user:read:chat",))

    def test_audience_maps_live_stream(self) -> None:
        client = TwitchHelixClient(self._config())
        client._helix = lambda *_args, **_kwargs: {
            "data": [
                {
                    "viewer_count": 42,
                    "title": "Midgar Live",
                    "game_id": "123",
                    "game_name": "Final Fantasy VII",
                    "started_at": "2026-10-01T00:00:00Z",
                }
            ]
        }

        state = client.audience()

        self.assertEqual(
            state,
            TwitchAudienceState(
                live=True,
                viewer_count=42,
                title="Midgar Live",
                game_id="123",
                game_name="Final Fantasy VII",
                started_at="2026-10-01T00:00:00Z",
            ),
        )

    def test_audience_returns_offline_when_stream_missing(self) -> None:
        client = TwitchHelixClient(self._config())
        client._helix = lambda *_args, **_kwargs: {"data": []}

        self.assertEqual(client.audience(), TwitchAudienceState())

    def test_poller_only_publishes_when_state_changes(self) -> None:
        bus = EventBus()

        class Client:
            def __init__(self):
                self.calls = 0
            def audience(self):
                self.calls += 1
                return TwitchAudienceState(
                    live=True,
                    viewer_count=10 if self.calls < 3 else 11,
                )

        client = Client()
        poller = TwitchAudiencePoller(
            client,
            bus,
            interval_seconds=30,
        )

        poller.poll_once()
        poller.poll_once()
        poller.poll_once()

        events = bus.events("audience")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].payload["viewer_count"], 10)
        self.assertEqual(events[1].payload["viewer_count"], 11)


if __name__ == "__main__":
    unittest.main()
