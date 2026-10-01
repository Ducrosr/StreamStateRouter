from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True, slots=True)
class CueAction:
    type: str
    params: Mapping[str, Any] = field(default_factory=dict)
    enabled: bool = True
    name: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CueAction":
        params = raw.get("params")
        return cls(
            type=str(raw.get("type") or "").strip(),
            params=_freeze_mapping(
                params if isinstance(params, Mapping) else {}
            ),
            enabled=bool(raw.get("enabled", True)),
            name=str(raw.get("name") or "").strip(),
        )


@dataclass(frozen=True, slots=True)
class CueFrame:
    at_ms: int
    actions: tuple[CueAction, ...]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CueFrame":
        actions_raw = raw.get("actions")
        actions = tuple(
            CueAction.from_mapping(item)
            for item in (
                actions_raw
                if isinstance(actions_raw, list)
                else []
            )
            if isinstance(item, Mapping)
        )
        return cls(
            at_ms=max(0, int(raw.get("at_ms", 0) or 0)),
            actions=actions,
        )


@dataclass(frozen=True, slots=True)
class Cue:
    name: str
    frames: tuple[CueFrame, ...]
    interrupt_policy: str = "replace"

    @classmethod
    def from_mapping(
        cls,
        name: str,
        raw: Mapping[str, Any],
    ) -> "Cue":
        frames_raw = raw.get("frames")
        frames = [
            CueFrame.from_mapping(item)
            for item in (
                frames_raw
                if isinstance(frames_raw, list)
                else []
            )
            if isinstance(item, Mapping)
        ]
        frames.sort(key=lambda frame: frame.at_ms)
        return cls(
            name=str(name),
            frames=tuple(frames),
            interrupt_policy=str(
                raw.get("interrupt_policy") or "replace"
            ).strip().casefold(),
        )

    @property
    def duration_ms(self) -> int:
        return self.frames[-1].at_ms if self.frames else 0

    @property
    def action_count(self) -> int:
        return sum(len(frame.actions) for frame in self.frames)


@dataclass(frozen=True, slots=True)
class PresentationComponent:
    mode: str = "inherit"
    resource: str = ""
    settings: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "PresentationComponent":
        settings = raw.get("settings")
        return cls(
            mode=str(raw.get("mode") or "inherit").strip().casefold(),
            resource=str(raw.get("resource") or "").strip(),
            settings=_freeze_mapping(
                settings if isinstance(settings, Mapping) else {}
            ),
        )


@dataclass(frozen=True, slots=True)
class ShaderFilterState:
    source: str
    filter_name: str
    enabled: bool | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)
    overlay: bool = True

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "ShaderFilterState":
        settings = raw.get("settings")
        enabled = raw.get("enabled")
        return cls(
            source=str(raw.get("source") or "").strip(),
            filter_name=str(
                raw.get("filter") or raw.get("filter_name") or ""
            ).strip(),
            enabled=(
                bool(enabled)
                if isinstance(enabled, bool)
                else None
            ),
            settings=_freeze_mapping(
                settings if isinstance(settings, Mapping) else {}
            ),
            overlay=bool(raw.get("overlay", True)),
        )


@dataclass(frozen=True, slots=True)
class ShaderSet:
    name: str
    filters: tuple[ShaderFilterState, ...]

    @classmethod
    def from_mapping(
        cls,
        name: str,
        raw: Mapping[str, Any],
    ) -> "ShaderSet":
        filters_raw = raw.get("filters")
        filters = tuple(
            ShaderFilterState.from_mapping(item)
            for item in (
                filters_raw
                if isinstance(filters_raw, list)
                else []
            )
            if isinstance(item, Mapping)
        )
        return cls(str(name), filters)


@dataclass(frozen=True, slots=True)
class TransitionProfile:
    name: str
    transition_name: str
    duration_ms: int | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)
    overlay: bool = True

    @classmethod
    def from_mapping(
        cls,
        name: str,
        raw: Mapping[str, Any],
    ) -> "TransitionProfile":
        duration_raw = raw.get("duration_ms")
        duration = (
            int(duration_raw)
            if duration_raw not in (None, "")
            else None
        )
        settings = raw.get("settings")
        return cls(
            name=str(name),
            transition_name=str(
                raw.get("transition_name")
                or raw.get("transition")
                or ""
            ).strip(),
            duration_ms=duration,
            settings=_freeze_mapping(
                settings if isinstance(settings, Mapping) else {}
            ),
            overlay=bool(raw.get("overlay", True)),
        )


@dataclass(frozen=True, slots=True)
class SoundAction:
    input_name: str
    action: str = "restart"

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
    ) -> "SoundAction":
        return cls(
            input_name=str(
                raw.get("input")
                or raw.get("input_name")
                or ""
            ).strip(),
            action=str(
                raw.get("action") or "restart"
            ).strip().casefold(),
        )


@dataclass(frozen=True, slots=True)
class SoundSet:
    name: str
    enter: tuple[SoundAction, ...] = ()
    exit: tuple[SoundAction, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        name: str,
        raw: Mapping[str, Any],
    ) -> "SoundSet":
        def actions(key: str) -> tuple[SoundAction, ...]:
            value = raw.get(key)
            return tuple(
                SoundAction.from_mapping(item)
                for item in (
                    value if isinstance(value, list) else []
                )
                if isinstance(item, Mapping)
            )

        return cls(
            name=str(name),
            enter=actions("enter"),
            exit=actions("exit"),
        )


@dataclass(frozen=True, slots=True)
class PresentationProfile:
    name: str
    extends: str = ""
    enter_cue: str = ""
    exit_cue: str = ""
    transition_profile: str = ""
    shader_set: str = ""
    sound_set: str = ""
    widget_theme: str = ""
    animation_intensity: str = "normal"
    theme: Mapping[str, Any] = field(default_factory=dict)
    components: Mapping[str, PresentationComponent] = field(
        default_factory=dict
    )

    @classmethod
    def from_mapping(
        cls,
        name: str,
        raw: Mapping[str, Any],
    ) -> "PresentationProfile":
        theme = raw.get("theme")
        components_raw = raw.get("components")
        components = {
            str(key): PresentationComponent.from_mapping(value)
            for key, value in (
                components_raw.items()
                if isinstance(components_raw, Mapping)
                else ()
            )
            if isinstance(value, Mapping)
        }
        return cls(
            name=str(name),
            extends=str(raw.get("extends") or "").strip(),
            enter_cue=str(raw.get("enter_cue") or "").strip(),
            exit_cue=str(raw.get("exit_cue") or "").strip(),
            transition_profile=str(
                raw.get("transition_profile") or ""
            ).strip(),
            shader_set=str(raw.get("shader_set") or "").strip(),
            sound_set=str(raw.get("sound_set") or "").strip(),
            widget_theme=str(raw.get("widget_theme") or "").strip(),
            animation_intensity=str(
                raw.get("animation_intensity") or "normal"
            ).strip().casefold(),
            theme=_freeze_mapping(
                theme if isinstance(theme, Mapping) else {}
            ),
            components=MappingProxyType(components),
        )


@dataclass(frozen=True, slots=True)
class ResolvedPresentationProfile:
    name: str
    lineage: tuple[str, ...]
    enter_cue: str = ""
    exit_cue: str = ""
    transition_profile: str = ""
    shader_set: str = ""
    sound_set: str = ""
    widget_theme: str = ""
    animation_intensity: str = "normal"
    theme: Mapping[str, Any] = field(default_factory=dict)
    components: Mapping[str, PresentationComponent] = field(
        default_factory=dict
    )


@dataclass(frozen=True, slots=True)
class PresentationRegistry:
    profiles: Mapping[str, PresentationProfile]
    cues: Mapping[str, Cue]
    transitions: Mapping[str, TransitionProfile]
    shader_sets: Mapping[str, ShaderSet]
    sound_sets: Mapping[str, SoundSet]

    def profile(self, name: str) -> ResolvedPresentationProfile | None:
        if str(name) not in self.profiles:
            return None
        return resolve_presentation_profile(str(name), self.profiles)

    def cue(self, name: str) -> Cue | None:
        return self.cues.get(str(name))

    def transition(
        self,
        name: str,
    ) -> TransitionProfile | None:
        return self.transitions.get(str(name))

    def shader_set(self, name: str) -> ShaderSet | None:
        return self.shader_sets.get(str(name))

    def sound_set(self, name: str) -> SoundSet | None:
        return self.sound_sets.get(str(name))


def resolve_presentation_profile(
    name: str,
    profiles: Mapping[str, PresentationProfile],
    *,
    _stack: tuple[str, ...] = (),
) -> ResolvedPresentationProfile:
    wanted = str(name)
    if wanted in _stack:
        raise ValueError(
            "Héritage circulaire PresentationProfile : "
            + " -> ".join((*_stack, wanted))
        )
    profile = profiles.get(wanted)
    if profile is None:
        raise ValueError(
            f"PresentationProfile introuvable : {wanted}"
        )

    if not profile.extends:
        return ResolvedPresentationProfile(
            name=profile.name,
            lineage=(profile.name,),
            enter_cue=profile.enter_cue,
            exit_cue=profile.exit_cue,
            transition_profile=profile.transition_profile,
            shader_set=profile.shader_set,
            sound_set=profile.sound_set,
            widget_theme=profile.widget_theme,
            animation_intensity=profile.animation_intensity,
            theme=_freeze_mapping(profile.theme),
            components=MappingProxyType(dict(profile.components)),
        )

    parent = resolve_presentation_profile(
        profile.extends,
        profiles,
        _stack=(*_stack, wanted),
    )
    theme = dict(parent.theme)
    theme.update(dict(profile.theme))

    components = dict(parent.components)
    for key, child in profile.components.items():
        if child.mode == "inherit":
            continue
        if child.mode == "hidden":
            components[key] = child
            continue
        inherited = components.get(key)
        inherited_settings = (
            dict(inherited.settings)
            if inherited is not None
            and inherited.mode == "custom"
            else {}
        )
        inherited_settings.update(dict(child.settings))
        components[key] = PresentationComponent(
            mode="custom",
            resource=(
                child.resource
                or (
                    inherited.resource
                    if inherited is not None
                    and inherited.mode == "custom"
                    else ""
                )
            ),
            settings=_freeze_mapping(inherited_settings),
        )

    def inherit(child: str, parent_value: str) -> str:
        return child if child else parent_value

    return ResolvedPresentationProfile(
        name=profile.name,
        lineage=(*parent.lineage, profile.name),
        enter_cue=inherit(profile.enter_cue, parent.enter_cue),
        exit_cue=inherit(profile.exit_cue, parent.exit_cue),
        transition_profile=inherit(
            profile.transition_profile,
            parent.transition_profile,
        ),
        shader_set=inherit(profile.shader_set, parent.shader_set),
        sound_set=inherit(profile.sound_set, parent.sound_set),
        widget_theme=inherit(profile.widget_theme, parent.widget_theme),
        animation_intensity=(
            profile.animation_intensity
            if profile.animation_intensity != "normal"
            else parent.animation_intensity
        ),
        theme=_freeze_mapping(theme),
        components=MappingProxyType(components),
    )


def build_presentation_registry(
    *,
    profiles_raw: Mapping[str, Any] | None,
    cues_raw: Mapping[str, Any] | None,
    transitions_raw: Mapping[str, Any] | None = None,
    shader_sets_raw: Mapping[str, Any] | None = None,
    sound_sets_raw: Mapping[str, Any] | None = None,
) -> PresentationRegistry:
    profiles = {
        str(name): PresentationProfile.from_mapping(str(name), raw)
        for name, raw in (profiles_raw or {}).items()
        if isinstance(raw, Mapping)
    }
    cues = {
        str(name): Cue.from_mapping(str(name), raw)
        for name, raw in (cues_raw or {}).items()
        if isinstance(raw, Mapping)
    }
    transitions = {
        str(name): TransitionProfile.from_mapping(str(name), raw)
        for name, raw in (transitions_raw or {}).items()
        if isinstance(raw, Mapping)
    }
    shader_sets = {
        str(name): ShaderSet.from_mapping(str(name), raw)
        for name, raw in (shader_sets_raw or {}).items()
        if isinstance(raw, Mapping)
    }
    sound_sets = {
        str(name): SoundSet.from_mapping(str(name), raw)
        for name, raw in (sound_sets_raw or {}).items()
        if isinstance(raw, Mapping)
    }
    return PresentationRegistry(
        profiles=MappingProxyType(profiles),
        cues=MappingProxyType(cues),
        transitions=MappingProxyType(transitions),
        shader_sets=MappingProxyType(shader_sets),
        sound_sets=MappingProxyType(sound_sets),
    )
