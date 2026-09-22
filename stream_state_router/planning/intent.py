from __future__ import annotations

import math
from typing import Iterable, Mapping

from ..obs.models import OBSAction
from .models import DesiredAssignment, DesiredState, PropertyKey


class UnsupportedIntentAction(ValueError):
    pass


class DesiredOwnershipConflict(ValueError):
    def __init__(
        self,
        key: PropertyKey,
        left_owner: str,
        right_owner: str,
    ):
        super().__init__(
            f"Managed property {key.kind} has multiple owners: "
            f"{left_owner} vs {right_owner}"
        )
        self.key = key
        self.left_owner = left_owner
        self.right_owner = right_owner

    def diagnostic_message(self) -> str:
        return str(self)


def _owner_from_provenance(provenance: str) -> str:
    """Return the concrete intent owner with full profile provenance."""

    text = str(provenance or "").strip()
    return text or "unknown"


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
    result: dict[PropertyKey, DesiredAssignment] = {}

    def assign(assignment: DesiredAssignment) -> None:
        # Action-profile inheritance is already resolved into one ordered action
        # list by OBSDispatcher. Preserve its historical final-value semantics:
        # within one owner, the later assignment overrides the earlier one.
        # Cross-owner collisions are still rejected by
        # desired_state_from_action_sets().
        result[assignment.key] = assignment
    for action in actions:
        if not action.enabled:
            continue
        kind = action.type.strip().casefold()
        params = dict(action.params)

        if kind == "set_program_scene":
            scene = str(params.get("scene") or "").strip()
            if not scene:
                raise ValueError("set_program_scene requires params.scene")
            assign(
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
            assign(
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
            assign(
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

        if kind == "source_filter_settings":
            source = str(params.get("source") or "").strip()
            filter_name = str(params.get("filter") or "").strip()
            settings = params.get("settings")
            if not source or not filter_name:
                raise ValueError(
                    "source_filter_settings requires params.source and params.filter"
                )
            if not isinstance(settings, Mapping):
                raise ValueError(
                    "source_filter_settings requires params.settings"
                )
            if params.get("overlay") is False:
                raise UnsupportedIntentAction(
                    "source_filter_settings with overlay=false cannot be represented "
                    "as independent stable setting assignments"
                )
            for setting in sorted(settings, key=str):
                assign(
                    DesiredAssignment.create(
                        PropertyKey.filter_setting(
                            collection=collection,
                            source=source,
                            filter_name=filter_name,
                            setting=str(setting),
                        ),
                        settings[setting],
                        provenance=provenance,
                    )
                )
            continue

        if kind in {"app_audio_output", "windows_hdr"}:
            # Host actions are intentionally executed by the classic guarded
            # profile path for now. Returning no managed property keeps them
            # visible as intent-only in migration coverage without pretending
            # that OBS observation can acknowledge Windows host state.
            continue

        if kind == "input_mute":
            input_name = str(params.get("input") or "").strip()
            if not input_name:
                raise ValueError("input_mute requires params.input")
            assign(
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
            if "volume_db" not in params:
                raise ValueError("input_volume_db requires params.volume_db")
            raw_volume = params.get("volume_db")
            if (
                isinstance(raw_volume, bool)
                or not isinstance(raw_volume, (int, float))
                or not math.isfinite(float(raw_volume))
            ):
                raise ValueError("input_volume_db requires a finite params.volume_db")
            volume_db = float(raw_volume)
            if not -100.0 <= volume_db <= 26.0:
                raise ValueError(
                    "input_volume_db params.volume_db must be between -100 and 26"
                )
            assign(
                DesiredAssignment.create(
                    PropertyKey.input_volume_db(
                        collection=collection,
                        input_name=input_name,
                    ),
                    volume_db,
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
            if params.get("overlay") is False:
                raise UnsupportedIntentAction(
                    "set_input_settings with overlay=false cannot be represented "
                    "as independent stable setting assignments"
                )
            for setting in sorted(settings, key=str):
                assign(
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

    return tuple(result.values())


def desired_state_from_action_sets(
    action_sets: Iterable[tuple[str, Iterable[OBSAction]]],
    *,
    collection: str = "",
    extra_assignments: Iterable[DesiredAssignment] = (),
    reserved_owners: Mapping[PropertyKey, str] | None = None,
) -> DesiredState:
    assignments: list[DesiredAssignment] = []
    owners: dict[PropertyKey, str] = {
        key: str(owner)
        for key, owner in (reserved_owners or {}).items()
    }

    def append_owned(
        assignment: DesiredAssignment,
        *,
        owner: str,
    ) -> None:
        previous_owner = owners.get(assignment.key)
        if previous_owner is not None and previous_owner != owner:
            raise DesiredOwnershipConflict(
                assignment.key,
                previous_owner,
                owner,
            )
        owners[assignment.key] = owner
        assignments.append(assignment)

    for assignment in extra_assignments:
        provenance = assignment.provenance[0] if assignment.provenance else ""
        append_owned(
            assignment,
            owner=_owner_from_provenance(provenance),
        )

    for provenance, actions in action_sets:
        owner = _owner_from_provenance(provenance)
        for assignment in desired_assignments_from_actions(
            actions,
            collection=collection,
            provenance=provenance,
        ):
            append_owned(assignment, owner=owner)

    return DesiredState.build(assignments)
