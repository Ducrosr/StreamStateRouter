from __future__ import annotations

from dataclasses import dataclass
import copy
import threading
from typing import Any, Mapping

from .models import PresentationComponent, ResolvedPresentationProfile


@dataclass(frozen=True, slots=True)
class PresentationStateSnapshot:
    revision: int
    profile: str
    lineage: tuple[str, ...]
    theme: Mapping[str, Any]
    components: Mapping[str, Mapping[str, Any]]
    animation_intensity: str
    widget_theme: str

    def as_mapping(
        self,
        *,
        component: str = "",
    ) -> dict[str, Any]:
        wanted = str(component or "").strip()
        selected = (
            dict(self.components.get(wanted, {}))
            if wanted
            else {}
        )
        component_state = (
            copy.deepcopy(selected)
            if selected
            else {
                "mode": "inherit",
                "resource": "",
                "settings": {},
            }
        )
        return {
            "revision": self.revision,
            "profile": self.profile,
            "lineage": list(self.lineage),
            "theme": copy.deepcopy(dict(self.theme)),
            "animation_intensity": self.animation_intensity,
            "widget_theme": self.widget_theme,
            "component": wanted,
            "component_state": component_state,
            # A component-scoped request must never expose sibling component
            # settings to an imported renderer sharing the runtime transport.
            "components": (
                {wanted: copy.deepcopy(component_state)}
                if wanted
                else {
                    key: copy.deepcopy(dict(value))
                    for key, value in self.components.items()
                }
            ),
        }


def _component_mapping(
    component: PresentationComponent,
) -> Mapping[str, Any]:
    return {
        "mode": component.mode,
        "resource": component.resource,
        "settings": dict(component.settings),
    }


class PresentationStateStore:
    """Thread-safe read-only bridge from routing to browser widgets."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._revision = 0
        self._snapshot = PresentationStateSnapshot(
            revision=0,
            profile="",
            lineage=(),
            theme={},
            components={},
            animation_intensity="normal",
            widget_theme="",
        )

    def update(
        self,
        profile: ResolvedPresentationProfile,
    ) -> PresentationStateSnapshot:
        with self._lock:
            self._revision += 1
            self._snapshot = PresentationStateSnapshot(
                revision=self._revision,
                profile=profile.name,
                lineage=tuple(profile.lineage),
                theme=copy.deepcopy(dict(profile.theme)),
                components={
                    str(key): copy.deepcopy(
                        dict(_component_mapping(value))
                    )
                    for key, value in profile.components.items()
                },
                animation_intensity=profile.animation_intensity,
                widget_theme=profile.widget_theme,
            )
            return self._snapshot

    def clear(self) -> PresentationStateSnapshot:
        with self._lock:
            self._revision += 1
            self._snapshot = PresentationStateSnapshot(
                revision=self._revision,
                profile="",
                lineage=(),
                theme={},
                components={},
                animation_intensity="normal",
                widget_theme="",
            )
            return self._snapshot

    def snapshot(self) -> PresentationStateSnapshot:
        with self._lock:
            current = self._snapshot
            return PresentationStateSnapshot(
                revision=current.revision,
                profile=current.profile,
                lineage=tuple(current.lineage),
                theme=copy.deepcopy(dict(current.theme)),
                components={
                    key: copy.deepcopy(dict(value))
                    for key, value in current.components.items()
                },
                animation_intensity=current.animation_intensity,
                widget_theme=current.widget_theme,
            )
