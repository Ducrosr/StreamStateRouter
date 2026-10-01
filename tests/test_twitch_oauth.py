from __future__ import annotations

import unittest
from unittest.mock import patch

from stream_state_router.platforms import (
    DEFAULT_TWITCH_SCOPES,
    TwitchDeviceAuthorization,
    TwitchOAuthError,
    begin_device_authorization,
    poll_device_tokens,
    refresh_user_tokens,
)


class TwitchOAuthTests(unittest.TestCase):
    def test_begin_device_authorization_uses_minimal_scopes(self) -> None:
        with patch(
            "stream_state_router.platforms.twitch_oauth._form_request",
            return_value={
                "device_code": "device",
                "user_code": "ABCD",
                "verification_uri": "https://www.twitch.tv/activate",
                "expires_in": 1800,
                "interval": 5,
            },
        ) as request:
            authorization = begin_device_authorization("client")

        self.assertEqual(authorization.user_code, "ABCD")
        values = request.call_args.args[1]
        self.assertEqual(
            set(values["scopes"].split()),
            set(DEFAULT_TWITCH_SCOPES),
        )

    def test_pending_device_authorization_returns_none(self) -> None:
        authorization = TwitchDeviceAuthorization(
            device_code="device",
            user_code="ABCD",
            verification_uri="https://www.twitch.tv/activate",
            expires_in=1800,
            interval=5,
        )
        error = TwitchOAuthError(
            "pending",
            status=400,
            oauth_message="authorization_pending",
        )

        with patch(
            "stream_state_router.platforms.twitch_oauth._form_request",
            side_effect=error,
        ):
            self.assertIsNone(
                poll_device_tokens("client", authorization)
            )

    def test_device_token_response_is_normalized(self) -> None:
        authorization = TwitchDeviceAuthorization(
            device_code="device",
            user_code="ABCD",
            verification_uri="https://www.twitch.tv/activate",
            expires_in=1800,
            interval=5,
        )
        with patch(
            "stream_state_router.platforms.twitch_oauth._form_request",
            return_value={
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 14400,
                "scope": ["user:read:chat"],
                "token_type": "bearer",
            },
        ):
            tokens = poll_device_tokens("client", authorization)

        assert tokens is not None
        self.assertEqual(tokens.access_token, "access")
        self.assertEqual(tokens.refresh_token, "refresh")
        self.assertEqual(tokens.scopes, ("user:read:chat",))

    def test_refresh_rotates_both_tokens(self) -> None:
        with patch(
            "stream_state_router.platforms.twitch_oauth._form_request",
            return_value={
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "expires_in": 14400,
                "scope": ["user:read:chat", "bits:read"],
                "token_type": "bearer",
            },
        ) as request:
            tokens = refresh_user_tokens(
                "client",
                "refresh-1",
            )

        self.assertEqual(tokens.access_token, "access-2")
        self.assertEqual(tokens.refresh_token, "refresh-2")
        values = request.call_args.args[1]
        self.assertEqual(values["grant_type"], "refresh_token")
        self.assertEqual(values["refresh_token"], "refresh-1")


if __name__ == "__main__":
    unittest.main()
