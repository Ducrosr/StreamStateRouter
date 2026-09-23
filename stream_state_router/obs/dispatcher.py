from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import time
from typing import Any, Mapping, Sequence

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
from ..router.models import DEFAULT_PROFILE_NAMES, ForegroundApp, StreamState
from .client import OBSClientManager
from .layouts import OBSLayoutManager, resolve_layout_profile
from .models import OBSAction, OBSProfile


ACTION_PROFILE_DOMAINS = ("game", "overlay", "capture", "audio")
PROFILE_DOMAINS = ACTION_PROFILE_DOMAINS
STATE_DOMAINS = ACTION_PROFILE_DOMAINS + ("layout",)
_TEMPLATE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


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


@dataclass(frozen=True, slots=True)
class LauncherPreparedAction:
    domain: str
    source_rule: str
    action: OBSAction
    target_key: str = ""


@dataclass(frozen=True, slots=True)
class LauncherPreparationPlan:
    signature: str
    actions: tuple[LauncherPreparedAction, ...]
    domains: tuple[str, ...]
    override_keys: tuple[str, ...]
    sources: tuple[str, ...]
    conflicts: tuple[str, ...] = ()


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
        self._control_variables: dict[str, str] = {}
        self._foreground_windows: dict[str, str] = {}
        self._launcher_override_keys: set[str] = set()

    def set_control_variables(
        self,
        values: Mapping[str, object] | None,
    ) -> None:
        self._control_variables = {
            str(key): str(value)
            for key, value in (values or {}).items()
        }

    def control_variables(self) -> dict[str, str]:
        return dict(self._control_variables)

    @staticmethod
    def _foreground_window_selector(app: ForegroundApp | None) -> str:
        if app is None:
            return ""
        title = str(app.window_title or "").strip()
        window_class = str(app.window_class or "").strip()
        exe = str(app.exe_name or "").strip()
        if not title or not window_class or not exe:
            return ""
        return f"{title}:{window_class}:{exe}"

    def update_foreground(self, app: ForegroundApp | None) -> bool:
        """Remember the latest OBS-compatible window selector per process."""
        if app is None or not str(app.exe_name or "").strip():
            return False
        selector = self._foreground_window_selector(app)
        if not selector:
            return False
        key = str(app.exe_name).strip().casefold()
        changed = self._foreground_windows.get(key) != selector
        self._foreground_windows[key] = selector
        return changed

    def _execution_variables(
        self,
        state: StreamState | None = None,
    ) -> dict[str, str]:
        values = dict(self._control_variables)
        active = state or self._desired_state or self._last_state
        if active is not None:
            values.update(active.as_variables())
        return values

    @classmethod
    def _render_value(
        cls,
        value: Any,
        variables: Mapping[str, str],
    ) -> Any:
        if isinstance(value, str):
            def replace(match: re.Match[str]) -> str:
                key = match.group(1)
                if key not in variables:
                    raise ValueError(
                        f"Variable SSR non définie dans le template : {key}"
                    )
                return str(variables[key])
            return _TEMPLATE_RE.sub(replace, value)
        if isinstance(value, Mapping):
            return {
                key: cls._render_value(item, variables)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._render_value(item, variables) for item in value]
        if isinstance(value, tuple):
            return tuple(cls._render_value(item, variables) for item in value)
        return value

    @classmethod
    def _contains_template(cls, value: Any) -> bool:
        if isinstance(value, str):
            return bool(_TEMPLATE_RE.search(value))
        if isinstance(value, Mapping):
            return any(cls._contains_template(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(cls._contains_template(item) for item in value)
        return False

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
        self._launcher_override_keys.clear()
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
        self._launcher_override_keys.clear()
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
        wanted_process = str(conditions.get("process_running") or "").strip()
        if wanted_process:
            running = context.get("running_processes")
            if running is None or isinstance(running, (str, bytes)):
                return False
            try:
                names = {
                    str(item).strip().casefold()
                    for item in running
                    if str(item).strip()
                }
            except TypeError:
                return False
            if wanted_process.casefold() not in names:
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
        elif kind == "wait_ms":
            description["value"] = params.get("duration_ms")
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
            provenance = f"{domain}:{desired}"
            if any(
                action.enabled
                and action.type.strip().casefold() == "wait_ms"
                for action in profile.actions
            ):
                declarative_blocks.append(
                    {
                        "provenance": provenance,
                        "reason": (
                            "Profil avec séquence temporelle wait_ms : "
                            "exécution classic-only"
                        ),
                    }
                )
            if any(
                action.enabled
                and self._contains_template(action.params)
                for action in profile.actions
            ):
                declarative_blocks.append(
                    {
                        "provenance": provenance,
                        "reason": (
                            "Profil avec paramètres dynamiques ${...} : "
                            "exécution classic-only"
                        ),
                    }
                )
            action_sets.append((provenance, profile.actions))
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

    @staticmethod
    def _launcher_target_key(kind: str, params: Mapping[str, Any]) -> str:
        kind = str(kind or "").strip().casefold()
        if kind == "set_program_scene":
            return "program_scene"
        if kind == "scene_item_enabled":
            return (
                "scene_item_enabled:"
                f"{str(params.get('scene') or '').casefold()}:"
                f"{str(params.get('source') or '').casefold()}"
            )
        if kind in {"source_filter_enabled", "source_filter_settings"}:
            return (
                f"{kind}:"
                f"{str(params.get('source') or '').casefold()}:"
                f"{str(params.get('filter') or '').casefold()}"
            )
        if kind in {"input_mute", "input_volume_db", "set_input_settings"}:
            return f"{kind}:{str(params.get('input') or '').casefold()}"
        if kind == "app_audio_output":
            return (
                "app_audio_output:"
                f"{str(params.get('process') or '').casefold()}"
            )
        if kind == "windows_hdr":
            return "windows_hdr"
        return ""

    @staticmethod
    def _launcher_effect_signature(kind: str, params: Mapping[str, Any]) -> str:
        return json.dumps(
            {
                "type": str(kind or "").strip().casefold(),
                "params": dict(params),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def build_launcher_preparation(
        self,
        candidates: Sequence[tuple[str, StreamState]],
        *,
        context: Mapping[str, Any] | None = None,
    ) -> LauncherPreparationPlan:
        """Merge explicitly marked pre-launch actions from launcher candidates."""
        frozen_context = (
            dict(context)
            if context is not None
            else self.cached_obs_context()
        )
        prepared: list[LauncherPreparedAction] = []
        seen_targets: dict[str, tuple[str, str]] = {}
        seen_free: set[str] = set()
        conflicts: list[str] = []
        domains: list[str] = []
        sources: list[str] = []

        for source_rule, state in candidates:
            source_rule = str(source_rule or "").strip() or "<règle>"
            if source_rule not in sources:
                sources.append(source_rule)
            variables = self._execution_variables(state)
            for domain in ACTION_PROFILE_DOMAINS:
                profile_name = state.profile_name(domain)
                try:
                    profile = self._resolve_action_profile(domain, profile_name)
                except Exception as exc:
                    conflicts.append(
                        f"{source_rule}: {domain}/{profile_name}: {exc}"
                    )
                    continue
                if profile is None:
                    continue
                if not self.conditions_match_context(
                    profile.conditions,
                    frozen_context,
                ):
                    continue
                for action in profile.actions:
                    if (
                        not action.enabled
                        or not action.preapply_on_launcher
                    ):
                        continue
                    rendered_params = self._render_value(
                        dict(action.params),
                        variables,
                    )
                    rendered = OBSAction(
                        type=action.type,
                        params=rendered_params,
                        enabled=True,
                        name=action.name,
                        preapply_on_launcher=False,
                    )
                    target_key = self._launcher_target_key(
                        rendered.type,
                        rendered.params,
                    )
                    effect = self._launcher_effect_signature(
                        rendered.type,
                        rendered.params,
                    )
                    if target_key:
                        previous = seen_targets.get(target_key)
                        if previous is not None:
                            previous_effect, previous_rule = previous
                            if previous_effect != effect:
                                conflicts.append(
                                    (
                                        f"{target_key}: préparation incompatible "
                                        f"entre « {previous_rule} » et "
                                        f"« {source_rule} »"
                                    )
                                )
                            continue
                        seen_targets[target_key] = (effect, source_rule)
                    else:
                        if effect in seen_free:
                            continue
                        seen_free.add(effect)
                    prepared.append(
                        LauncherPreparedAction(
                            domain=domain,
                            source_rule=source_rule,
                            action=rendered,
                            target_key=target_key,
                        )
                    )
                    if domain not in domains:
                        domains.append(domain)

        if conflicts:
            signature = hashlib.sha256(
                json.dumps(
                    {"sources": sources, "conflicts": conflicts},
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            return LauncherPreparationPlan(
                signature=signature,
                actions=(),
                domains=(),
                override_keys=(),
                sources=tuple(sources),
                conflicts=tuple(conflicts),
            )

        signature_payload = [
            {
                "domain": item.domain,
                "target": item.target_key,
                "effect": self._launcher_effect_signature(
                    item.action.type,
                    item.action.params,
                ),
            }
            for item in prepared
        ]
        signature = hashlib.sha256(
            json.dumps(
                signature_payload,
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return LauncherPreparationPlan(
            signature=signature,
            actions=tuple(prepared),
            domains=tuple(domains),
            override_keys=tuple(
                item.target_key
                for item in prepared
                if item.target_key
            ),
            sources=tuple(sources),
        )

    def activate_launcher_preparation(
        self,
        plan: LauncherPreparationPlan,
    ) -> DispatchResult:
        if plan.conflicts:
            return DispatchResult(0, 0, (), tuple(plan.conflicts))

        executed = 0
        skipped = 0
        warnings: list[str] = []
        domains: list[str] = []
        successful_keys: set[str] = set()
        self._launcher_override_keys.clear()

        for prepared in plan.actions:
            self._yield_runtime()
            try:
                self.execute_action(prepared.action)
            except Exception as exc:
                skipped += 1
                warnings.append(
                    f"{prepared.source_rule}/{prepared.domain}: {exc}"
                )
                continue
            executed += 1
            if prepared.domain not in domains:
                domains.append(prepared.domain)
            if prepared.target_key:
                successful_keys.add(prepared.target_key)

        self._launcher_override_keys = successful_keys
        return DispatchResult(
            executed,
            skipped,
            tuple(domains),
            tuple(warnings),
        )

    def clear_launcher_preparation_overrides(self) -> None:
        self._launcher_override_keys.clear()

    def launcher_override_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._launcher_override_keys))

    def invalidate_applied_domains(self, domains: Sequence[str]) -> None:
        for domain in domains:
            self._applied_profiles.pop(str(domain), None)

    def _action_overridden_by_launcher(
        self,
        action: OBSAction,
        variables: Mapping[str, str],
    ) -> bool:
        if not self._launcher_override_keys:
            return False
        rendered_params = self._render_value(
            dict(action.params),
            variables,
        )
        key = self._launcher_target_key(
            action.type,
            rendered_params,
        )
        return bool(key and key in self._launcher_override_keys)

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
            variables = self._execution_variables(state)
            for action in profile.actions:
                self._yield_runtime()
                if not action.enabled:
                    skipped += 1
                    domain_skipped += 1
                    continue
                if self._action_overridden_by_launcher(
                    action,
                    variables,
                ):
                    skipped += 1
                    domain_skipped += 1
                    continue
                try:
                    self.execute_action(action, variables=variables)
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

    def refresh_foreground_actions(
        self,
        state: StreamState,
        app: ForegroundApp | None,
    ) -> DispatchResult:
        """Refresh only active actions explicitly following this foreground process."""
        if app is None:
            return DispatchResult(0, 0, ())
        exe_key = str(app.exe_name or "").strip().casefold()
        if not exe_key:
            return DispatchResult(0, 0, ())
        self.update_foreground(app)

        executed = 0
        skipped = 0
        warnings: list[str] = []
        changed_domains: list[str] = []
        statuses: list[DomainDispatchStatus] = []
        variables = self._execution_variables(state)

        for domain in ACTION_PROFILE_DOMAINS:
            profile_name = state.profile_name(domain)
            try:
                profile = self._resolve_action_profile(domain, profile_name)
            except Exception as exc:
                warnings.append(str(exc))
                continue
            if profile is None or not self.conditions_match(profile.conditions):
                continue

            domain_executed = 0
            domain_failed = ""
            for action in profile.actions:
                if not action.enabled:
                    continue
                if action.type.strip().casefold() != "set_input_settings":
                    continue
                follow = str(
                    action.params.get("follow_foreground_process") or ""
                ).strip()
                if not follow or follow.casefold() != exe_key:
                    continue
                try:
                    self.execute_action(action, variables=variables)
                except Exception as exc:
                    domain_failed = str(exc)
                    warnings.append(
                        f"{domain}/{profile_name}: {exc}"
                    )
                    break
                executed += 1
                domain_executed += 1

            if domain_executed:
                changed_domains.append(domain)
                statuses.append(
                    DomainDispatchStatus(
                        domain=domain,
                        desired_profile=profile_name,
                        applied_profile=self._applied_profiles.get(
                            domain,
                            profile_name,
                        ),
                        status="applied" if not domain_failed else "failed",
                        message=domain_failed,
                    )
                )

        return DispatchResult(
            executed,
            skipped,
            tuple(changed_domains),
            tuple(warnings),
            tuple(statuses),
        )

    def has_action_profile(self, domain: str, profile_name: str) -> bool:
        return (
            domain in ACTION_PROFILE_DOMAINS
            and str(profile_name) in self._profiles.get(domain, {})
        )

    def execute_profile(
        self,
        domain: str,
        profile_name: str,
        *,
        state: StreamState | None = None,
    ) -> DispatchResult:
        if domain not in ACTION_PROFILE_DOMAINS:
            raise ValueError(f"Domaine inconnu : {domain}")
        profile = self._resolve_action_profile(domain, profile_name)
        if profile is None:
            raise ValueError(f"Profil introuvable : {domain}/{profile_name}")
        if not self.conditions_match(profile.conditions):
            return DispatchResult(0, len(profile.actions) or 1, (domain,))
        executed = 0
        skipped = 0
        variables = self._execution_variables(state)
        for action in profile.actions:
            self._yield_runtime()
            if not action.enabled:
                skipped += 1
                continue
            self.execute_action(action, variables=variables)
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

    def execute_action(
        self,
        action: OBSAction,
        *,
        variables: Mapping[str, str] | None = None,
    ) -> None:
        self._yield_runtime()
        kind = action.type.strip().casefold()
        p = self._render_value(
            dict(action.params),
            variables or self._execution_variables(),
        )
        if kind == "wait_ms":
            raw = p.get("duration_ms")
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ValueError("wait_ms requiert params.duration_ms numérique")
            duration_ms = float(raw)
            if (
                not math.isfinite(duration_ms)
                or not 0.0 <= duration_ms <= 10000.0
            ):
                raise ValueError(
                    "wait_ms params.duration_ms doit être compris entre 0 et 10000"
                )
            deadline = time.monotonic() + duration_ms / 1000.0
            while True:
                self._yield_runtime()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.05, remaining))
            return
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
            settings = dict(settings)
            follow = str(
                p.get("follow_foreground_process") or ""
            ).strip()
            if follow:
                selector = self._foreground_windows.get(
                    follow.casefold()
                )
                if selector:
                    settings["window"] = selector
            self.client.send(
                "SetInputSettings",
                {
                    "inputName": self._need(p, "input"),
                    "inputSettings": settings,
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
