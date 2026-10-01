from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ..events import EventBus


@dataclass(frozen=True, slots=True)
class TwitchSubscriptionSpec:
    type: str
    version: str
    condition: Mapping[str, str]
    scopes: tuple[str, ...] = ()

    def create_payload(self, session_id: str) -> dict[str, object]:
        wanted = str(session_id or "").strip()
        if not wanted:
            raise ValueError("session_id requis")
        return {
            "type": self.type,
            "version": self.version,
            "condition": dict(self.condition),
            "transport": {
                "method": "websocket",
                "session_id": wanted,
            },
        }


@dataclass(frozen=True, slots=True)
class TwitchEventSubResult:
    kind: str
    message_id: str = ""
    session_id: str = ""
    keepalive_timeout_seconds: int | None = None
    reconnect_url: str = ""
    subscription_type: str = ""
    subscription_status: str = ""
    duplicate: bool = False
    published: int = 0


def build_default_subscriptions(
    *,
    broadcaster_user_id: str,
    user_id: str,
    moderator_user_id: str | None = None,
) -> tuple[TwitchSubscriptionSpec, ...]:
    broadcaster = str(broadcaster_user_id or "").strip()
    user = str(user_id or "").strip()
    moderator = str(moderator_user_id or broadcaster).strip()
    if not broadcaster:
        raise ValueError("broadcaster_user_id requis")
    if not user:
        raise ValueError("user_id requis")
    if not moderator:
        raise ValueError("moderator_user_id requis")

    broadcaster_condition = MappingProxyType(
        {"broadcaster_user_id": broadcaster}
    )
    return (
        TwitchSubscriptionSpec(
            "channel.chat.message",
            "1",
            MappingProxyType(
                {
                    "broadcaster_user_id": broadcaster,
                    "user_id": user,
                }
            ),
            ("user:read:chat",),
        ),
        TwitchSubscriptionSpec(
            "channel.follow",
            "2",
            MappingProxyType(
                {
                    "broadcaster_user_id": broadcaster,
                    "moderator_user_id": moderator,
                }
            ),
            ("moderator:read:followers",),
        ),
        TwitchSubscriptionSpec(
            "channel.subscribe",
            "1",
            broadcaster_condition,
            ("channel:read:subscriptions",),
        ),
        TwitchSubscriptionSpec(
            "channel.subscription.gift",
            "1",
            broadcaster_condition,
            ("channel:read:subscriptions",),
        ),
        TwitchSubscriptionSpec(
            "channel.subscription.message",
            "1",
            broadcaster_condition,
            ("channel:read:subscriptions",),
        ),
        TwitchSubscriptionSpec(
            "channel.cheer",
            "1",
            broadcaster_condition,
            ("bits:read",),
        ),
        TwitchSubscriptionSpec(
            "channel.raid",
            "1",
            MappingProxyType(
                {"to_broadcaster_user_id": broadcaster}
            ),
            (),
        ),
        TwitchSubscriptionSpec(
            "channel.channel_points_custom_reward_redemption.add",
            "1",
            broadcaster_condition,
            ("channel:read:redemptions",),
        ),
    )


def required_scopes(
    subscriptions: Sequence[TwitchSubscriptionSpec],
) -> tuple[str, ...]:
    values = {
        scope
        for spec in subscriptions
        for scope in spec.scopes
        if str(scope).strip()
    }
    return tuple(sorted(values, key=str.casefold))


class TwitchEventSubMessageProcessor:
    """Normalize Twitch EventSub WebSocket messages into the SSR EventBus.

    Transport and OAuth are deliberately outside this class. It is safe to feed
    recorded Twitch frames into this processor for deterministic tests.
    """

    def __init__(
        self,
        event_bus: EventBus,
        *,
        dedup_limit: int = 2048,
    ):
        self.event_bus = event_bus
        self._dedup_limit = max(32, int(dedup_limit))
        self._seen_order: deque[str] = deque()
        self._seen: set[str] = set()

    def _deduplicate(self, message_id: str) -> bool:
        value = str(message_id or "").strip()
        if not value:
            return False
        if value in self._seen:
            return True
        self._seen.add(value)
        self._seen_order.append(value)
        while len(self._seen_order) > self._dedup_limit:
            stale = self._seen_order.popleft()
            self._seen.discard(stale)
        return False

    @staticmethod
    def _mapping(value: object) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _text(value: object) -> str:
        return str(value or "").strip()

    def process(
        self,
        frame: Mapping[str, Any],
    ) -> TwitchEventSubResult:
        metadata = self._mapping(frame.get("metadata"))
        payload = self._mapping(frame.get("payload"))
        message_type = self._text(
            metadata.get("message_type")
        ).casefold()
        message_id = self._text(metadata.get("message_id"))

        duplicate = self._deduplicate(message_id)
        if duplicate:
            return TwitchEventSubResult(
                kind=message_type or "unknown",
                message_id=message_id,
                duplicate=True,
            )

        if message_type == "session_welcome":
            session = self._mapping(payload.get("session"))
            timeout = session.get("keepalive_timeout_seconds")
            return TwitchEventSubResult(
                kind="welcome",
                message_id=message_id,
                session_id=self._text(session.get("id")),
                keepalive_timeout_seconds=(
                    int(timeout)
                    if isinstance(timeout, int)
                    and not isinstance(timeout, bool)
                    else None
                ),
            )

        if message_type == "session_keepalive":
            return TwitchEventSubResult(
                kind="keepalive",
                message_id=message_id,
            )

        if message_type == "session_reconnect":
            session = self._mapping(payload.get("session"))
            return TwitchEventSubResult(
                kind="reconnect",
                message_id=message_id,
                session_id=self._text(session.get("id")),
                reconnect_url=self._text(
                    session.get("reconnect_url")
                ),
            )

        subscription = self._mapping(payload.get("subscription"))
        subscription_type = self._text(
            metadata.get("subscription_type")
            or subscription.get("type")
        )

        if message_type == "revocation":
            return TwitchEventSubResult(
                kind="revocation",
                message_id=message_id,
                subscription_type=subscription_type,
                subscription_status=self._text(
                    subscription.get("status")
                ),
            )

        if message_type != "notification":
            return TwitchEventSubResult(
                kind=message_type or "unknown",
                message_id=message_id,
            )

        event = self._mapping(payload.get("event"))
        published = self._publish_notification(
            subscription_type,
            event,
        )
        return TwitchEventSubResult(
            kind="notification",
            message_id=message_id,
            subscription_type=subscription_type,
            published=published,
        )

    def _publish(
        self,
        channels: tuple[str, ...],
        *,
        type: str,
        payload: Mapping[str, object],
    ) -> int:
        for channel in channels:
            self.event_bus.publish(
                channel=channel,
                type=type,
                platform="twitch",
                payload=payload,
            )
        return len(channels)

    def _publish_notification(
        self,
        subscription_type: str,
        event: Mapping[str, Any],
    ) -> int:
        kind = str(subscription_type or "").strip().casefold()
        user_name = self._text(
            event.get("user_name")
            or event.get("from_broadcaster_user_name")
        )
        user_login = self._text(
            event.get("user_login")
            or event.get("from_broadcaster_user_login")
        )
        base: dict[str, object] = {
            "display_name": user_name or user_login,
            "user_name": user_name,
            "user_login": user_login,
            "user_id": self._text(
                event.get("user_id")
                or event.get("from_broadcaster_user_id")
            ),
        }

        if kind == "channel.chat.message":
            message = self._mapping(event.get("message"))
            payload = {
                **base,
                "text": self._text(message.get("text")),
                "color": self._text(event.get("color")),
                "message_id": self._text(event.get("message_id")),
                "badges": list(event.get("badges") or []),
                "message_type": self._text(
                    event.get("message_type")
                ),
            }
            return self._publish(
                ("chat",),
                type="message",
                payload=payload,
            )

        if kind == "channel.follow":
            return self._publish(
                ("events", "alerts"),
                type="follow",
                payload={
                    **base,
                    "label": "Nouveau follower",
                    "title": "Nouveau follower",
                    "text": base["display_name"],
                },
            )

        if kind == "channel.subscribe":
            tier = self._text(event.get("tier"))
            return self._publish(
                ("events", "alerts"),
                type="subscription",
                payload={
                    **base,
                    "label": "Nouvel abonnement",
                    "title": "Nouvel abonnement",
                    "text": base["display_name"],
                    "tier": tier,
                    "is_gift": bool(event.get("is_gift", False)),
                },
            )

        if kind == "channel.subscription.gift":
            total = int(event.get("total") or 0)
            return self._publish(
                ("events", "alerts"),
                type="subscription_gift",
                payload={
                    **base,
                    "label": "Abonnements offerts",
                    "title": "Abonnements offerts",
                    "text": (
                        f"{base['display_name']} · {total}"
                        if base["display_name"]
                        else str(total)
                    ),
                    "total": total,
                    "tier": self._text(event.get("tier")),
                    "is_anonymous": bool(
                        event.get("is_anonymous", False)
                    ),
                },
            )

        if kind == "channel.subscription.message":
            message = self._mapping(event.get("message"))
            months = int(event.get("cumulative_months") or 0)
            return self._publish(
                ("events", "alerts"),
                type="resubscription",
                payload={
                    **base,
                    "label": "Réabonnement",
                    "title": "Réabonnement",
                    "text": self._text(message.get("text"))
                    or base["display_name"],
                    "months": months,
                    "tier": self._text(event.get("tier")),
                },
            )

        if kind == "channel.cheer":
            bits = int(event.get("bits") or 0)
            anonymous = bool(event.get("is_anonymous", False))
            display = (
                "Anonyme"
                if anonymous
                else str(base["display_name"] or "")
            )
            return self._publish(
                ("events", "alerts"),
                type="cheer",
                payload={
                    **base,
                    "display_name": display,
                    "label": "Bits",
                    "title": f"{bits} Bits",
                    "text": self._text(event.get("message"))
                    or display,
                    "bits": bits,
                    "is_anonymous": anonymous,
                },
            )

        if kind == "channel.raid":
            viewers = int(event.get("viewers") or 0)
            return self._publish(
                ("events", "alerts"),
                type="raid",
                payload={
                    **base,
                    "label": "Raid",
                    "title": f"Raid · {viewers} spectateurs",
                    "text": base["display_name"],
                    "viewers": viewers,
                },
            )

        if (
            kind
            == "channel.channel_points_custom_reward_redemption.add"
        ):
            reward = self._mapping(event.get("reward"))
            title = self._text(reward.get("title"))
            return self._publish(
                ("events", "alerts"),
                type="reward_redemption",
                payload={
                    **base,
                    "label": "Récompense",
                    "title": title or "Récompense de chaîne",
                    "text": self._text(event.get("user_input"))
                    or base["display_name"],
                    "reward_id": self._text(reward.get("id")),
                    "reward_title": title,
                    "reward_cost": int(reward.get("cost") or 0),
                    "redemption_id": self._text(event.get("id")),
                },
            )

        return 0
