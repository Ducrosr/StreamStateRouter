from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any, Mapping

from ..host import HostControlController
from ..planning import (
    DesiredAssignment,
    DesiredOwnershipConflict,
    DesiredState,
    DesiredStateConflict,
    PropertyKey,
    desired_state_from_action_sets,
)
from ..router.engine import StateChange
from ..router.models import DEFAULT_PROFILE_NAMES, StreamState
from .client import OBSClientManager
from .layouts import OBSLayoutManager, resolve_layout_profile
from .models import OBSAction, OBSProfile


ACTION_PROFILE_DOMAINS = ("game", "overlay", "capture", "audio")
PROFILE_DOMAINS = ACTION_PROFILE_DOMAINS
STATE_DOMAINS = ACTION_PROFILE_DOMAINS + ("layout",)
@dataclass(frozen=True, slots=True)
class DomainDispatchStatus:
    domain: str
    desired_profile: str
    applied_profile: str
    status: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class DispatchResult:
    executed: int
    skipped: int
    changed_domains: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    domain_statuses: tuple[DomainDispatchStatus, ...] = ()


class OBSDispatcher:
    """Translate logical stream states into explicit obs-websocket actions.

    It owns no game-detection logic. Logical profiles can inherit from a base
    profile and may declare optional OBS-side conditions.
    """

    def __init__(
        self,
        client: OBSClientManager,
        profiles: Mapping[str, Mapping[str, OBSProfile]] | None = None,
        layout_profiles: Mapping[str, Mapping[str, object]] | None = None,
        host_controller: HostControlController | None = None,
    ):
        self.client = client
        self.host_controller = host_controller
        self._profiles = {domain: dict(values) for domain, values in (profiles or {}).items()}
        self._layout_profiles = {
            str(name): dict(value) for name, value in (layout_profiles or {}).items()
        }
        self._layout_manager = OBSLayoutManager(client)
        self._last_state: StreamState | None = None
        self._desired_state: StreamState | None = None
        self._applied_profiles: dict[str, str] = {}
        # An explicit layout.apply may intentionally diverge OBS geometry from
        # the layout selected by the current routing state. Keep that manual
        # layout in place while routing continues to request the same baseline
        # LayoutProfile; a genuinely different routed layout releases the hold.
        self._manual_layout_hold_active = False
        self._manual_layout_routing_baseline = ""
        self._context_cache: tuple[float, dict[str, Any]] | None = None
        self._cooperative_yield = None

    @property
    def layout_manager(self) -> OBSLayoutManager:
        return self._layout_manager

    def set_cooperative_yield(self, callback) -> None:
        """Install the runtime shutdown checkpoint for long OBS batches."""
        self._cooperative_yield = callback
        self._layout_manager.set_cooperative_yield(callback)

    def _yield_runtime(self) -> None:
        callback = self._cooperative_yield
        if callback is not None:
            callback()

    def configure_profiles(
        self,
        profiles: Mapping[str, Mapping[str, OBSProfile]],
    ) -> None:
        self._profiles = {domain: dict(values) for domain, values in profiles.items()}
        self._last_state = None
        self._desired_state = None
        self._applied_profiles.clear()
        self._layout_manager.reset_cache()
        self._context_cache = None

    def configure_layouts(self, profiles: Mapping[str, Mapping[str, object]]) -> None:
        self._layout_profiles = {str(name): dict(value) for name, value in profiles.items()}
        self._layout_manager.reset_cache()
        self._last_state = None
        self._desired_state = None
        self._applied_profiles.clear()
        self._manual_layout_hold_active = False
        self._manual_layout_routing_baseline = ""
        self._context_cache = None

    def reset(self) -> None:
        self._last_state = None
        self._desired_state = None
        self._applied_profiles.clear()
        self._manual_layout_hold_active = False
        self._manual_layout_routing_baseline = ""
        self._layout_manager.reset_cache()
        self._context_cache = None

    def invalidate_applied_state(self) -> None:
        """Forget OBS-side convergence knowledge after reconnect/session changes."""
        self._applied_profiles.clear()
        self._context_cache = None

    @property
    def desired_state(self) -> StreamState | None:
        return self._desired_state

    def applied_profiles(self) -> dict[str, str]:
        return dict(self._applied_profiles)

    def set_manual_layout_hold(self, routed_profile: str = "") -> None:
        """Keep an explicit layout until routing genuinely changes layout target.

        routed_profile may be unknown during a runtime restart. In that case the
        first subsequent routing decision adopts its LayoutProfile as the
        baseline without overwriting the manual layout.
        """
        self._manual_layout_hold_active = True
        self._manual_layout_routing_baseline = str(routed_profile or "").strip()

    def clear_manual_layout_hold(self) -> None:
        self._manual_layout_hold_active = False
        self._manual_layout_routing_baseline = ""

    def _layout_is_manually_held_for(self, state: StreamState) -> bool:
        if not self._manual_layout_hold_active:
            return False
        baseline = self._manual_layout_routing_baseline
        if not baseline:
            # During restart the routed baseline can be temporarily unknown.
            # Hold every layout until the first genuine StateChange adopts one.
            return True
        # Once known, only the unchanged routed baseline is suppressed. A
        # different routed LayoutProfile must remain eligible so dispatch_change
        # can release the manual hold and apply the new automatic target.
        return state.profile_name("layout") == baseline

    def _is_unmanaged_default(self, domain: str, profile_name: str) -> bool:
        """Return whether the domain intentionally has no OBS profile owner."""

        if str(profile_name) != DEFAULT_PROFILE_NAMES.get(domain, ""):
            return False
        if domain == "layout":
            return not self._layout_profiles
        if domain in ACTION_PROFILE_DOMAINS:
            return not self._profiles.get(domain, {})
        return False

    def pending_domains(self, state: StreamState | None = None) -> tuple[str, ...]:
        wanted = state or self._desired_state
        if wanted is None:
            return ()
        return tuple(
            domain
            for domain in STATE_DOMAINS
            if not (domain == "layout" and self._layout_is_manually_held_for(wanted))
            and not self._is_unmanaged_default(domain, wanted.profile_name(domain))
            and self._applied_profiles.get(domain) != wanted.profile_name(domain)
        )

    def obs_context(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Return a small current OBS context for conditional rules/profiles.

        Context is cached briefly so a foreground polling loop never turns into
        a high-frequency obs-websocket polling loop. Explicit planner commands
        may bypass that cache so profile conditions are resolved against the
        same live OBS session as the catalog/observation pass.
        """
        now = time.monotonic()
        if (
            not force_refresh
            and self._context_cache is not None
            and now - self._context_cache[0] < 0.5
        ):
            return dict(self._context_cache[1])
        if not self.client.config.enabled:
            return {
                "obs_enabled": False,
                "streaming": False,
                "recording": False,
                "program_scene": "",
            }
        context: dict[str, Any] = {"obs_enabled": True}
        self._yield_runtime()
        try:
            stream = self.client.send("GetStreamStatus")
            context["streaming"] = bool(stream.get("outputActive", False))
        except Exception:
            context["streaming"] = None
        self._yield_runtime()
        try:
            record = self.client.send("GetRecordStatus")
            context["recording"] = bool(record.get("outputActive", False))
        except Exception:
            context["recording"] = None
        self._yield_runtime()
        try:
            scene = self.client.send("GetCurrentProgramScene")
            context["program_scene"] = str(
                scene.get("sceneName")
                or scene.get("currentProgramSceneName")
                or ""
            )
        except Exception:
            context["program_scene"] = ""
        self._context_cache = (now, dict(context))
        return context

    def cached_obs_context(self) -> dict[str, Any]:
        """Return the last OBS context snapshot without issuing any request."""
        if self._context_cache is not None:
            return dict(self._context_cache[1])
        enabled = bool(getattr(getattr(self.client, "config", None), "enabled", False))
        return {
            "obs_enabled": enabled,
            "streaming": None if enabled else False,
            "recording": None if enabled else False,
            "program_scene": "",
            "snapshot_available": False,
        }

    @staticmethod
    def conditions_match_context(
        conditions: Mapping[str, Any] | None,
        context: Mapping[str, Any],
    ) -> bool:
        if not conditions:
            return True
        if "streaming" in conditions:
            wanted = bool(conditions.get("streaming"))
            if context.get("streaming") is None or bool(context.get("streaming")) != wanted:
                return False
        if "recording" in conditions:
            wanted = bool(conditions.get("recording"))
            if context.get("recording") is None or bool(context.get("recording")) != wanted:
                return False
        if str(conditions.get("program_scene") or "").strip():
            if str(context.get("program_scene") or "") != str(conditions.get("program_scene") or ""):
                return False
        if bool(conditions.get("obs_enabled", False)) and not bool(context.get("obs_enabled")):
            return False
        return True

    def conditions_match(self, conditions: Mapping[str, Any] | None) -> bool:
        if not conditions:
            return True
        return self.conditions_match_context(conditions, self.obs_context())

    @staticmethod
    def _describe_action(action: OBSAction) -> dict[str, object]:
        params = dict(action.params)
        kind = action.type.strip().casefold()
        description: dict[str, object] = {
            "type": action.type,
            "enabled": bool(action.enabled),
        }
        if kind == "set_program_scene":
            description["target"] = str(params.get("scene") or "")
        elif kind == "scene_item_enabled":
            description["target"] = (
                f"{params.get('scene', '')}/{params.get('source', '')}"
            )
            description["value"] = bool(params.get("enabled", True))
        elif kind == "source_filter_enabled":
            description["target"] = (
                f"{params.get('source', '')}/{params.get('filter', '')}"
            )
            description["value"] = bool(params.get("enabled", True))
        elif kind == "source_filter_settings":
            description["target"] = (
                f"{params.get('source', '')}/{params.get('filter', '')}"
            )
            settings = params.get("settings")
            description["setting_keys"] = (
                sorted(str(key) for key in settings)
                if isinstance(settings, Mapping)
                else []
            )
        elif kind == "app_audio_output":
            description["target"] = str(params.get("process") or "")
            description["device"] = str(params.get("device") or "")
            description["roles"] = str(params.get("roles") or "all")
        elif kind == "windows_hdr":
            description["target"] = str(params.get("display") or "primary")
            description["value"] = bool(params.get("enabled", True))
        elif kind == "input_mute":
            description["target"] = str(params.get("input") or "")
            description["value"] = bool(params.get("muted", True))
        elif kind == "input_volume_db":
            description["target"] = str(params.get("input") or "")
            description["value"] = params.get("volume_db")
        elif kind == "set_input_settings":
            # Do not expose arbitrary settings content in diagnostics.
            description["target"] = str(params.get("input") or "")
            settings = params.get("settings")
            description["setting_keys"] = sorted(str(key) for key in settings) if isinstance(settings, Mapping) else []
        return description

    def _layout_visibility_owners(
        self,
        profile: Mapping[str, Any],
        *,
        owner: str,
    ) -> dict[PropertyKey, str]:
        """Expose specialized visibility ownership without duplicating layout rules.

        Geometry remains opaque and delegated to OBSLayoutManager. Runtime-owned
        visibility is reserved here as a conflict claim; LayoutProfile owns only
        the remaining visibility that its existing manager would actually apply.
        """

        claims: dict[PropertyKey, str] = {}
        scene = str(profile.get("scene") or "").strip()
        modules = profile.get("modules")
        if isinstance(modules, Mapping):
            for raw_module in modules.values():
                if (
                    not isinstance(raw_module, Mapping)
                    or not bool(raw_module.get("managed", True))
                    or bool(raw_module.get("locked", False))
                ):
                    continue
                elements = raw_module.get("elements")
                if not isinstance(elements, list):
                    continue
                for element in elements:
                    if (
                        not isinstance(element, Mapping)
                        or not bool(element.get("included", True))
                        or bool(element.get("locked", False))
                    ):
                        continue
                    source = str(element.get("source") or "").strip()
                    container = str(
                        element.get("container")
                        or raw_module.get("container")
                        or scene
                    ).strip()
                    if not source or not container:
                        continue
                    key = PropertyKey.scene_item_visibility(
                        collection="",
                        container=container,
                        source=source,
                    )
                    if self._layout_manager.runtime_visibility_owned(
                        container,
                        source,
                        element,
                    ):
                        claims[key] = "runtime:visibility"
                        continue
                    if bool(element.get("follow_visibility", True)):
                        claims[key] = owner

        support = profile.get("support_items")
        if isinstance(support, list):
            for item in support:
                if not isinstance(item, Mapping):
                    continue
                source = str(item.get("source") or "").strip()
                container = str(item.get("container") or "").strip()
                if not source or not container:
                    continue
                key = PropertyKey.scene_item_visibility(
                    collection="",
                    container=container,
                    source=source,
                )
                if self._layout_manager.runtime_visibility_owned(
                    container,
                    source,
                    item,
                ):
                    claims[key] = "runtime:visibility"
                else:
                    claims[key] = owner
        return claims

    def _resolve_state_plan(
        self,
        state: StreamState,
        *,
        context: Mapping[str, Any],
    ) -> tuple[
        list[dict[str, object]],
        DesiredState | None,
        DesiredStateConflict | DesiredOwnershipConflict | None,
        list[dict[str, str]],
    ]:
        """Resolve legacy profiles and declarative intent in one read-only pass."""

        domains: list[dict[str, object]] = []
        action_sets: list[tuple[str, tuple[OBSAction, ...]]] = []
        extra_assignments: list[DesiredAssignment] = []
        reserved_owners: dict[PropertyKey, str] = {
            PropertyKey.scene_item_visibility(
                collection="",
                container=container,
                source=source,
            ): "runtime:visibility"
            for container, source in self._layout_manager.runtime_visibility_claims()
        }
        declarative_blocks: list[dict[str, str]] = []

        for domain in STATE_DOMAINS:
            desired = state.profile_name(domain)
            applied = self._applied_profiles.get(domain, "")
            held = domain == "layout" and self._layout_is_manually_held_for(state)
            needs_apply = (
                not held
                and not self._is_unmanaged_default(domain, desired)
                and applied != desired
            )
            row: dict[str, object] = {
                "domain": domain,
                "desired_profile": desired,
                "applied_profile": applied,
                "needs_apply": needs_apply,
                "status": "held" if held else ("noop" if applied == desired else "planned"),
                "operations": [],
            }

            if domain == "layout":
                if held:
                    declarative_blocks.append(
                        {
                            "provenance": f"{domain}:{desired}",
                            "reason": "LayoutProfile maintenu manuellement",
                        }
                    )
                if desired not in self._layout_profiles:
                    if self._is_unmanaged_default(domain, desired):
                        row.update(
                            status="unmanaged",
                            message="Aucun LayoutProfile configuré pour ce domaine",
                        )
                    else:
                        row.update(status="missing", message="LayoutProfile introuvable")
                        declarative_blocks.append(
                            {
                                "provenance": f"{domain}:{desired}",
                                "reason": "LayoutProfile introuvable",
                            }
                        )
                    domains.append(row)
                    continue
                try:
                    profile = resolve_layout_profile(desired, self._layout_profiles)
                except Exception as exc:
                    row.update(status="failed", message=str(exc))
                    declarative_blocks.append(
                        {
                            "provenance": f"{domain}:{desired}",
                            "reason": f"Résolution LayoutProfile impossible : {exc}",
                        }
                    )
                    domains.append(row)
                    continue
                conditions = profile.get("conditions")
                condition_match = (
                    not isinstance(conditions, Mapping)
                    or self.conditions_match_context(conditions, context)
                )
                row["condition_match"] = condition_match
                if not condition_match:
                    row.update(status="blocked", message="conditions OBS non satisfaites")
                    declarative_blocks.append(
                        {
                            "provenance": f"{domain}:{desired}",
                            "reason": "conditions OBS non satisfaites",
                        }
                    )
                modules = profile.get("modules")
                support = profile.get("support_items")
                transition = profile.get("transition")
                scene = str(profile.get("scene") or "")
                row["operations"] = [
                    {
                        "type": "layout",
                        "scene": scene,
                        "modules": len(modules) if isinstance(modules, Mapping) else 0,
                        "support_items": len(support) if isinstance(support, list) else 0,
                        "transition": (
                            str(transition.get("mode") or "instant")
                            if isinstance(transition, Mapping)
                            else "instant"
                        ),
                    }
                ]
                row["extends"] = str(profile.get("extends") or "")
                reserved_owners.update(
                    self._layout_visibility_owners(
                        profile,
                        owner=f"{domain}:{desired}",
                    )
                )
                extra_assignments.append(
                    DesiredAssignment.create(
                        PropertyKey.layout_profile(
                            collection="",
                            scene=scene,
                        ),
                        desired,
                        provenance=f"{domain}:{desired}",
                    )
                )
                domains.append(row)
                continue

            try:
                profile = self._resolve_action_profile(domain, desired)
            except Exception as exc:
                row.update(status="failed", message=str(exc))
                declarative_blocks.append(
                    {
                        "provenance": f"{domain}:{desired}",
                        "reason": f"Résolution profil impossible : {exc}",
                    }
                )
                domains.append(row)
                continue
            if profile is None:
                if self._is_unmanaged_default(domain, desired):
                    row.update(
                        status="unmanaged",
                        message="Aucun profil OBS configuré pour ce domaine",
                    )
                else:
                    row.update(status="missing", message="Profil OBS introuvable")
                    declarative_blocks.append(
                        {
                            "provenance": f"{domain}:{desired}",
                            "reason": "Profil OBS introuvable",
                        }
                    )
                domains.append(row)
                continue

            condition_match = self.conditions_match_context(profile.conditions, context)
            row["condition_match"] = condition_match
            if not condition_match:
                row.update(status="blocked", message="conditions OBS non satisfaites")
                declarative_blocks.append(
                    {
                        "provenance": f"{domain}:{desired}",
                        "reason": "conditions OBS non satisfaites",
                    }
                )
            row["operations"] = [self._describe_action(action) for action in profile.actions]
            row["extends"] = profile.extends
            action_sets.append((f"{domain}:{desired}", profile.actions))
            domains.append(row)

        try:
            declarative = desired_state_from_action_sets(
                action_sets,
                extra_assignments=extra_assignments,
                reserved_owners=reserved_owners,
            )
        except (DesiredStateConflict, DesiredOwnershipConflict) as exc:
            return domains, None, exc, declarative_blocks
        return domains, declarative, None, declarative_blocks

    def resolve_desired_state(
        self,
        state: StreamState,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> DesiredState:
        """Resolve the selected profiles into one declarative target without I/O."""

        frozen_context = dict(context) if context is not None else self.cached_obs_context()
        _domains, desired, conflict, _blocks = self._resolve_state_plan(
            state,
            context=frozen_context,
        )
        if conflict is not None:
            raise conflict
        assert desired is not None
        return desired

    def plan_state(
        self,
        state: StreamState,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, object]:
        """Describe routing intent without mutating OBS or dispatcher state.

        The legacy domain view remains for compatibility, while the declarative
        target is resolved by the same pass that future dry-run/execution will
        consume.  No OBS read is performed here.
        """

        frozen_context = dict(context) if context is not None else self.cached_obs_context()
        domains, desired, conflict, declarative_blocks = self._resolve_state_plan(
            state,
            context=frozen_context,
        )
        result: dict[str, object] = {
            "state": state.as_variables(),
            "context": frozen_context,
            "domains": domains,
            "declarative_desired": (
                desired.as_mapping(diagnostic=True)
                if desired is not None
                else {"properties": []}
            ),
            "declarative_blocks": declarative_blocks,
        }
        if conflict is not None:
            result["declarative_error"] = {
                "code": (
                    "property_ownership_conflict"
                    if isinstance(conflict, DesiredOwnershipConflict)
                    else "property_conflict"
                ),
                "message": conflict.diagnostic_message(),
                "property": conflict.key.as_mapping(),
            }
        return result

    def dispatch_change(self, change: StateChange) -> DispatchResult:
        # change.previous is the router's previous *decision*, not necessarily
        # what OBS actually acknowledged. Always diff against applied state.
        if self._manual_layout_hold_active:
            desired_layout = change.current.profile_name("layout")
            baseline = self._manual_layout_routing_baseline
            if not baseline:
                # The restart happened before any routed state was known. Adopt
                # the first real routing target as the baseline, but keep the
                # explicit manual layout currently visible in OBS.
                self._manual_layout_routing_baseline = desired_layout
            elif desired_layout != baseline:
                # A genuine routing transition to another LayoutProfile releases
                # the explicit manual divergence.
                self.clear_manual_layout_hold()
        return self.dispatch_state(change.current)

    def dispatch_state(
        self,
        state: StreamState,
        *,
        previous: StreamState | None = None,
        force: bool = False,
    ) -> DispatchResult:
        del previous  # compatibility only: router history is not OBS applied state
        self._desired_state = state
        self._last_state = state
        if force:
            # An explicit force-reapply means automatic routing deliberately
            # takes ownership back from any prior manual layout divergence.
            self.clear_manual_layout_hold()
        changed = [
            domain
            for domain in STATE_DOMAINS
            if not (domain == "layout" and self._layout_is_manually_held_for(state))
            and (
                force
                or (
                    not self._is_unmanaged_default(
                        domain,
                        state.profile_name(domain),
                    )
                    and self._applied_profiles.get(domain)
                    != state.profile_name(domain)
                )
            )
        ]

        executed = 0
        skipped = 0
        warnings: list[str] = []
        statuses: list[DomainDispatchStatus] = []

        def status(domain: str, desired: str, value: str, message: str = "") -> None:
            statuses.append(
                DomainDispatchStatus(
                    domain=domain,
                    desired_profile=desired,
                    applied_profile=self._applied_profiles.get(domain, ""),
                    status=value,
                    message=message,
                )
            )

        for domain in changed:
            self._yield_runtime()
            profile_name = state.profile_name(domain)
            if self._is_unmanaged_default(domain, profile_name):
                skipped += 1
                status(
                    domain,
                    profile_name,
                    "unmanaged",
                    (
                        "Aucun LayoutProfile configuré pour ce domaine"
                        if domain == "layout"
                        else "Aucun profil OBS configuré pour ce domaine"
                    ),
                )
                continue
            if domain == "layout":
                if profile_name not in self._layout_profiles:
                    skipped += 1
                    status(domain, profile_name, "missing", "LayoutProfile introuvable")
                    continue
                try:
                    layout = resolve_layout_profile(profile_name, self._layout_profiles)
                except Exception as exc:
                    skipped += 1
                    warnings.append(str(exc))
                    status(domain, profile_name, "failed", str(exc))
                    continue
                conditions = layout.get("conditions")
                if isinstance(conditions, Mapping) and not self.conditions_match(conditions):
                    skipped += 1
                    status(domain, profile_name, "blocked", "conditions OBS non satisfaites")
                    continue
                try:
                    result = self._layout_manager.apply_profile(layout)
                except Exception as exc:
                    skipped += 1
                    warnings.append(str(exc))
                    status(domain, profile_name, "failed", str(exc))
                    continue
                executed += result.elements_applied
                skipped += result.elements_skipped
                warnings.extend(result.warnings)
                if result.missing_sources or result.warnings:
                    message = "; ".join(
                        [
                            *(f"source manquante: {name}" for name in result.missing_sources),
                            *result.warnings,
                        ]
                    )
                    status(domain, profile_name, "partial", message)
                    continue
                self._applied_profiles[domain] = profile_name
                status(domain, profile_name, "applied")
                continue

            try:
                profile = self._resolve_action_profile(domain, profile_name)
            except Exception as exc:
                skipped += 1
                warnings.append(str(exc))
                status(domain, profile_name, "failed", str(exc))
                continue
            if profile is None:
                skipped += 1
                status(domain, profile_name, "missing", "Profil OBS introuvable")
                continue
            if not self.conditions_match(profile.conditions):
                skipped += len(profile.actions) or 1
                status(domain, profile_name, "blocked", "conditions OBS non satisfaites")
                continue

            domain_executed = 0
            domain_skipped = 0
            failed = ""
            for action in profile.actions:
                self._yield_runtime()
                if not action.enabled:
                    skipped += 1
                    domain_skipped += 1
                    continue
                try:
                    self.execute_action(action)
                except Exception as exc:
                    failed = str(exc)
                    warnings.append(f"{domain}/{profile_name}: {exc}")
                    break
                executed += 1
                domain_executed += 1
            if failed:
                status(domain, profile_name, "failed", failed)
                continue
            self._applied_profiles[domain] = profile_name
            status(domain, profile_name, "applied")

        return DispatchResult(
            executed,
            skipped,
            tuple(changed),
            tuple(warnings),
            tuple(statuses),
        )

    def execute_profile(self, domain: str, profile_name: str) -> DispatchResult:
        if domain not in ACTION_PROFILE_DOMAINS:
            raise ValueError(f"Domaine inconnu : {domain}")
        profile = self._resolve_action_profile(domain, profile_name)
        if profile is None:
            raise ValueError(f"Profil introuvable : {domain}/{profile_name}")
        if not self.conditions_match(profile.conditions):
            return DispatchResult(0, len(profile.actions) or 1, (domain,))
        executed = 0
        skipped = 0
        for action in profile.actions:
            self._yield_runtime()
            if not action.enabled:
                skipped += 1
                continue
            self.execute_action(action)
            executed += 1
        return DispatchResult(executed, skipped, (domain,))

    def execute_layout_profile(self, profile_name: str, *, preview: bool = False) -> DispatchResult:
        self._yield_runtime()
        if profile_name not in self._layout_profiles:
            raise ValueError(f"Layout introuvable : {profile_name}")
        profile = resolve_layout_profile(profile_name, self._layout_profiles)
        if isinstance(profile.get("conditions"), Mapping) and not self.conditions_match(profile["conditions"]):
            return DispatchResult(0, 1, ("layout",))
        baseline = ""
        if not preview:
            if self._desired_state is not None:
                baseline = self._desired_state.profile_name("layout")
            elif self._applied_profiles.get("layout"):
                baseline = self._applied_profiles["layout"]
        result = (
            self._layout_manager.preview_profile(profile)
            if preview
            else self._layout_manager.apply_profile(profile)
        )
        if not preview:
            if baseline and profile_name == baseline:
                self.clear_manual_layout_hold()
            else:
                self.set_manual_layout_hold(baseline)
        return DispatchResult(
            result.elements_applied,
            result.elements_skipped,
            ("layout",),
            result.warnings,
        )

    def _resolve_action_profile(self, domain: str, name: str) -> OBSProfile | None:
        domain_profiles = self._profiles.get(domain, {})
        if name not in domain_profiles:
            return None
        return self._resolve_action_profile_inner(domain_profiles, name, ())

    def _resolve_action_profile_inner(
        self,
        profiles: Mapping[str, OBSProfile],
        name: str,
        stack: tuple[str, ...],
    ) -> OBSProfile:
        if name in stack:
            raise ValueError("Héritage circulaire de profil OBS : " + " -> ".join((*stack, name)))
        profile = profiles[name]
        if not profile.extends:
            return profile
        if profile.extends not in profiles:
            raise ValueError(f"Profil parent introuvable : {profile.extends}")
        parent = self._resolve_action_profile_inner(profiles, profile.extends, (*stack, name))
        conditions = dict(parent.conditions)
        conditions.update(dict(profile.conditions))
        return OBSProfile(
            name=profile.name,
            actions=(*parent.actions, *profile.actions),
            extends=profile.extends,
            conditions=conditions,
        )

    def execute_action(self, action: OBSAction) -> None:
        self._yield_runtime()
        kind = action.type.strip().casefold()
        p = dict(action.params)
        if kind == "set_program_scene":
            self.client.send(
                "SetCurrentProgramScene",
                {"sceneName": self._need(p, "scene")},
            )
            return
        if kind == "scene_item_enabled":
            scene = self._need(p, "scene")
            source = self._need(p, "source")
            item_id = self._scene_item_id(scene, source)
            self.client.send(
                "SetSceneItemEnabled",
                {
                    "sceneName": scene,
                    "sceneItemId": item_id,
                    "sceneItemEnabled": bool(p.get("enabled", True)),
                },
            )
            return
        if kind == "source_filter_enabled":
            self.client.send(
                "SetSourceFilterEnabled",
                {
                    "sourceName": self._need(p, "source"),
                    "filterName": self._need(p, "filter"),
                    "filterEnabled": bool(p.get("enabled", True)),
                },
            )
            return
        if kind == "source_filter_settings":
            settings = p.get("settings")
            if not isinstance(settings, Mapping):
                raise ValueError("source_filter_settings requiert params.settings")
            self.client.send(
                "SetSourceFilterSettings",
                {
                    "sourceName": self._need(p, "source"),
                    "filterName": self._need(p, "filter"),
                    "filterSettings": dict(settings),
                    "overlay": bool(p.get("overlay", True)),
                },
            )
            return
        if kind in {"app_audio_output", "windows_hdr"}:
            if self.host_controller is None:
                raise RuntimeError(
                    "Contrôle Windows indisponible pour cette action."
                )
            if not self.host_controller.execute(kind, p):
                raise ValueError(f"Action Windows inconnue : {action.type}")
            return
        if kind == "input_mute":
            self.client.send(
                "SetInputMute",
                {
                    "inputName": self._need(p, "input"),
                    "inputMuted": bool(p.get("muted", True)),
                },
            )
            return
        if kind == "input_volume_db":
            if "volume_db" not in p:
                raise ValueError("Paramètre OBS manquant : volume_db")
            raw_volume = p.get("volume_db")
            if (
                isinstance(raw_volume, bool)
                or not isinstance(raw_volume, (int, float))
                or not math.isfinite(float(raw_volume))
            ):
                raise ValueError("Paramètre OBS invalide : volume_db")
            volume_db = float(raw_volume)
            if not -100.0 <= volume_db <= 26.0:
                raise ValueError(
                    "Paramètre OBS hors plage : volume_db doit être entre -100 et 26"
                )
            self.client.send(
                "SetInputVolume",
                {
                    "inputName": self._need(p, "input"),
                    "inputVolumeDb": volume_db,
                },
            )
            return
        if kind == "set_input_settings":
            settings = p.get("settings")
            if not isinstance(settings, Mapping):
                raise ValueError("set_input_settings requiert params.settings")
            self.client.send(
                "SetInputSettings",
                {
                    "inputName": self._need(p, "input"),
                    "inputSettings": dict(settings),
                    "overlay": bool(p.get("overlay", True)),
                },
            )
            return
        raise ValueError(f"Type d'action inconnu : {action.type}")

    def _scene_item_id(self, scene: str, source: str) -> int:
        # sceneItemId is ephemeral OBS state. Resolve the logical
        # (scene, source) identity for every visibility operation so an ID
        # reused after an OBS scene edit can never target another item.
        response = self.client.send(
            "GetSceneItemId",
            {"sceneName": scene, "sourceName": source},
        )
        item_id = int(response.get("sceneItemId") or 0)
        if not item_id:
            raise RuntimeError(f"Source '{source}' introuvable dans la scène '{scene}'")
        return item_id

    @staticmethod
    def _need(params: Mapping[str, object], key: str) -> str:
        value = str(params.get(key) or "").strip()
        if not value:
            raise ValueError(f"Paramètre OBS manquant : {key}")
        return value


def profile_map_from_raw(
    raw: Mapping[str, Mapping[str, Mapping[str, object]]],
) -> dict[str, dict[str, OBSProfile]]:
    result: dict[str, dict[str, OBSProfile]] = {domain: {} for domain in ACTION_PROFILE_DOMAINS}
    for domain, profiles in raw.items():
        if domain not in ACTION_PROFILE_DOMAINS or not isinstance(profiles, Mapping):
            continue
        for name, profile_raw in profiles.items():
            if not isinstance(profile_raw, Mapping):
                continue
            actions_raw = profile_raw.get("actions", [])
            actions = tuple(
                OBSAction.from_mapping(item)
                for item in actions_raw
                if isinstance(item, Mapping)
            )
            conditions = profile_raw.get("conditions")
            result[domain][str(name)] = OBSProfile(
                str(name),
                actions,
                str(profile_raw.get("extends") or ""),
                dict(conditions) if isinstance(conditions, Mapping) else {},
            )
    return result
