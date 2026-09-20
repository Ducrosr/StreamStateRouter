from __future__ import annotations

from typing import Iterable, Mapping

from ..obs.models import OBSAction
from .models import DesiredAssignment, DesiredState, PropertyKey


class UnsupportedIntentAction(ValueError):
    pass


def desired_assignments_from_actions(
    actions: Iterable[OBSAction],
    *,
    collection: str = "",
    provenance: str,
) -> tuple[DesiredAssignment, ...]:
    """Translate stable OBS assignments into declarative managed properties.

    This is intentionally not a macro interpreter.  Only idempotent final-value
    actions already supported by SSR are translated.  Temporal effects belong to
    future recipes, not this adapter.
    """
    result: list[DesiredAssignment] = []
    for action in actions:
        if not action.enabled:
            continue
        kind = action.type.strip().casefold()
        params = dict(action.params)

        if kind == "set_program_scene":
            scene = str(params.get("scene") or "").strip()
            if not scene:
                raise ValueError("set_program_scene requires params.scene")
            result.append(
                DesiredAssignment.create(
                    PropertyKey.program_scene(collection=collection),
                    scene,
                    provenance=provenance,
                )
            )
            continue

        if kind == "scene_item_enabled":
            scene = str(params.get("scene") or "").strip()
            source = str(params.get("source") or "").strip()
            if not scene or not source:
                raise ValueError(
                    "scene_item_enabled requires params.scene and params.source"
                )
            result.append(
                DesiredAssignment.create(
                    PropertyKey.scene_item_visibility(
                        collection=collection,
                        container=scene,
                        source=source,
                    ),
                    bool(params.get("enabled", True)),
                    provenance=provenance,
                )
            )
            continue

        if kind == "source_filter_enabled":
            source = str(params.get("source") or "").strip()
            filter_name = str(params.get("filter") or "").strip()
            if not source or not filter_name:
                raise ValueError(
                    "source_filter_enabled requires params.source and params.filter"
                )
            result.append(
                DesiredAssignment.create(
                    PropertyKey.filter_enabled(
                        collection=collection,
                        source=source,
                        filter_name=filter_name,
                    ),
                    bool(params.get("enabled", True)),
                    provenance=provenance,
                )
            )
            continue

        if kind == "input_mute":
            input_name = str(params.get("input") or "").strip()
            if not input_name:
                raise ValueError("input_mute requires params.input")
            result.append(
                DesiredAssignment.create(
                    PropertyKey.input_mute(
                        collection=collection,
                        input_name=input_name,
                    ),
                    bool(params.get("muted", True)),
                    provenance=provenance,
                )
            )
            continue

        if kind == "input_volume_db":
            input_name = str(params.get("input") or "").strip()
            if not input_name:
                raise ValueError("input_volume_db requires params.input")
            result.append(
                DesiredAssignment.create(
                    PropertyKey.input_volume_db(
                        collection=collection,
                        input_name=input_name,
                    ),
                    float(params.get("volume_db", 0.0)),
                    provenance=provenance,
                )
            )
            continue

        if kind == "set_input_settings":
            input_name = str(params.get("input") or "").strip()
            settings = params.get("settings")
            if not input_name:
                raise ValueError("set_input_settings requires params.input")
            if not isinstance(settings, Mapping):
                raise ValueError("set_input_settings requires params.settings")
            for setting in sorted(settings, key=str):
                result.append(
                    DesiredAssignment.create(
                        PropertyKey.input_setting(
                            collection=collection,
                            input_name=input_name,
                            setting=str(setting),
                        ),
                        settings[setting],
                        provenance=provenance,
                    )
                )
            continue

        raise UnsupportedIntentAction(
            f"OBS action cannot be represented as a stable desired property: {action.type}"
        )

    return tuple(result)


def desired_state_from_action_sets(
    action_sets: Iterable[tuple[str, Iterable[OBSAction]]],
    *,
    collection: str = "",
    extra_assignments: Iterable[DesiredAssignment] = (),
) -> DesiredState:
    assignments: list[DesiredAssignment] = list(extra_assignments)
    for provenance, actions in action_sets:
        assignments.extend(
            desired_assignments_from_actions(
                actions,
                collection=collection,
                provenance=provenance,
            )
        )
    return DesiredState.build(assignments)
