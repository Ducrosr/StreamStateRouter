from __future__ import annotations

import copy
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .client import OBSClientManager, OBSResourceNotFoundError


# [Type] Module name and optional enriched form [Type:flag,flag] Module name.
MODULE_SOURCE_RE = re.compile(r"^\[([^\]:]+)(?::([^\]]+))?\]\s+(.+?)\s*$")

_ALIGN_LEFT = 1
_ALIGN_RIGHT = 2
_ALIGN_TOP = 4
_ALIGN_BOTTOM = 8
SSR_FADE_FILTER = "[SSR] Layout Fade"
SSR_FADE_FILTER_KIND = "color_filter_v2"
GROUP_RESIZE_SETTLE_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class ModuleSourceName:
    module: str
    element: str
    flags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class LayoutApplyResult:
    elements_applied: int = 0
    elements_skipped: int = 0
    missing_sources: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class PendingFadeCleanup:
    source: str
    collection: str
    created_at: float
    attempts: int = 0
    last_error: str = ""


@dataclass(frozen=True, slots=True)
class LayoutDiffItem:
    module: str
    source: str
    changes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LayoutCaptureResult:
    profile: dict[str, Any]
    complete: bool
    warnings: tuple[str, ...] = ()
    captured_modules: int = 0


@dataclass(frozen=True, slots=True)
class LayoutValidationIssue:
    level: str
    message: str
    module: str = ""
    source: str = ""


@dataclass(frozen=True, slots=True)
class CatalogElement:
    scene: str
    container: str
    path: tuple[str, ...]
    container_kind: str
    module: str
    element: str
    source: str
    enabled: bool
    transform: Mapping[str, Any]
    source_type: str = "input"
    flags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class SceneTopologyItem:
    scene: str
    container: str
    path: tuple[str, ...]
    container_kind: str
    source: str
    source_type: str
    flags: frozenset[str] = frozenset()


@dataclass(slots=True)
class LayoutSnapshot:
    profile: dict[str, Any]
    label: str = ""
    created_at: float = field(default_factory=time.time)
    collection: str = ""
    generation: int = 0
    complete: bool = True
    warnings: tuple[str, ...] = ()


def parse_module_source(source_name: str) -> ModuleSourceName | None:
    match = MODULE_SOURCE_RE.match(str(source_name or "").strip())
    if not match:
        return None
    module = match.group(1).strip()
    flags_raw = str(match.group(2) or "")
    element = match.group(3).strip()
    if not module or not element:
        return None
    flags = frozenset(
        token.casefold().strip()
        for token in re.split(r"[,;+\s]+", flags_raw)
        if token.strip()
    )
    return ModuleSourceName(module, element, flags)


def split_module_source(source_name: str) -> tuple[str, str] | None:
    parsed = parse_module_source(source_name)
    if parsed is None:
        return None
    return parsed.module, parsed.element


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _position_anchor_offsets(width: float, height: float, alignment: int) -> tuple[float, float]:
    if alignment & _ALIGN_LEFT:
        offset_x = 0.0
    elif alignment & _ALIGN_RIGHT:
        offset_x = width
    else:
        offset_x = width / 2.0

    if alignment & _ALIGN_TOP:
        offset_y = 0.0
    elif alignment & _ALIGN_BOTTOM:
        offset_y = height
    else:
        offset_y = height / 2.0
    return offset_x, offset_y


def anchor_factors(anchor: str) -> tuple[float, float]:
    mapping = {
        "top_left": (0.0, 0.0),
        "top_center": (0.5, 0.0),
        "top_right": (1.0, 0.0),
        "center_left": (0.0, 0.5),
        "center": (0.5, 0.5),
        "center_right": (1.0, 0.5),
        "bottom_left": (0.0, 1.0),
        "bottom_center": (0.5, 1.0),
        "bottom_right": (1.0, 1.0),
    }
    return mapping.get(str(anchor or "top_left"), (0.0, 0.0))


def transform_bbox(transform: Mapping[str, Any]) -> tuple[float, float, float, float]:
    width = abs(_float(transform.get("width")))
    height = abs(_float(transform.get("height")))
    x = _float(transform.get("positionX"))
    y = _float(transform.get("positionY"))
    rotation = math.radians(_float(transform.get("rotation")))
    alignment = _int(transform.get("alignment", 0))

    offset_x, offset_y = _position_anchor_offsets(width, height, alignment)
    corners = [
        (-offset_x, -offset_y),
        (width - offset_x, -offset_y),
        (width - offset_x, height - offset_y),
        (-offset_x, height - offset_y),
    ]
    cos_r = math.cos(rotation)
    sin_r = math.sin(rotation)
    points = [
        (x + cx * cos_r - cy * sin_r, y + cx * sin_r + cy * cos_r)
        for cx, cy in corners
    ]
    left = min(px for px, _ in points)
    top = min(py for _, py in points)
    right = max(px for px, _ in points)
    bottom = max(py for _, py in points)
    return left, top, max(0.0, right - left), max(0.0, bottom - top)


def _union_bounds(elements: Iterable[CatalogElement]) -> dict[str, float]:
    bounds = [transform_bbox(element.transform) for element in elements]
    if not bounds:
        return {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}
    left = min(item[0] for item in bounds)
    top = min(item[1] for item in bounds)
    right = max(item[0] + item[2] for item in bounds)
    bottom = max(item[1] + item[3] for item in bounds)
    return {
        "x": left,
        "y": top,
        "width": max(1.0, right - left),
        "height": max(1.0, bottom - top),
    }


def _deep_merge(base: Mapping[str, Any], child: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(base))
    for key, value in child.items():
        if key in {"extends"}:
            continue
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def resolve_layout_profile(
    name: str,
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    _stack: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Resolve LayoutProfile inheritance into a concrete profile."""
    if name in _stack:
        raise ValueError("Héritage circulaire de LayoutProfile : " + " -> ".join((*_stack, name)))
    raw = profiles.get(name)
    if not isinstance(raw, Mapping):
        raise KeyError(name)
    parent = str(raw.get("extends") or "").strip()
    if not parent:
        result = copy.deepcopy(dict(raw))
        result.pop("extends", None)
        return result
    base = resolve_layout_profile(parent, profiles, _stack=(*_stack, name))
    result = _deep_merge(base, raw)
    if not str(raw.get("scene") or "").strip():
        result["scene"] = base.get("scene", "")
    if "canvas" not in raw and "canvas" in base:
        result["canvas"] = copy.deepcopy(base["canvas"])
    return result


def compact_layout_overrides(
    child: Mapping[str, Any], parent: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a child layout containing only modules that differ from its base.

    Metadata (scene/canvas/transition/conditions) remains on the child, while
    byte-for-byte equivalent modules are removed so inherited layouts stay easy
    to maintain.
    """
    result = copy.deepcopy(dict(child))
    child_modules = result.get("modules") if isinstance(result.get("modules"), Mapping) else {}
    parent_modules = parent.get("modules") if isinstance(parent.get("modules"), Mapping) else {}
    result["modules"] = {
        name: value
        for name, value in child_modules.items()
        if name not in parent_modules or value != parent_modules.get(name)
    }
    return result


def diff_layout_profiles(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> list[str]:
    """Human-readable structural differences between two resolved layouts."""
    result: list[str] = []
    left_modules = left.get("modules", {}) if isinstance(left.get("modules"), Mapping) else {}
    right_modules = right.get("modules", {}) if isinstance(right.get("modules"), Mapping) else {}
    for name in sorted(set(left_modules) | set(right_modules), key=str.casefold):
        a = left_modules.get(name)
        b = right_modules.get(name)
        if a is None:
            result.append(f"{name}: absent à gauche, présent à droite")
            continue
        if b is None:
            result.append(f"{name}: présent à gauche, absent à droite")
            continue
        if not isinstance(a, Mapping) or not isinstance(b, Mapping):
            continue
        for key in ("visible", "locked", "anchor", "anchor_mode"):
            if a.get(key) != b.get(key):
                result.append(f"{name}.{key}: {a.get(key)!r} → {b.get(key)!r}")
        ga = a.get("geometry", {}) if isinstance(a.get("geometry"), Mapping) else {}
        gb = b.get("geometry", {}) if isinstance(b.get("geometry"), Mapping) else {}
        for key in ("x", "y", "width", "height"):
            if abs(_float(ga.get(key)) - _float(gb.get(key))) > 0.01:
                result.append(f"{name}.{key}: {_float(ga.get(key)):.1f} → {_float(gb.get(key)):.1f}")
    return result


class OBSLayoutManager:
    """Discover, validate, capture, preview and restore OBS module layouts."""

    def __init__(self, client: OBSClientManager):
        self.client = client
        self._scene_item_cache: dict[tuple[str, str], int] = {}
        self._cooperative_yield = None
        self._runtime_visibility_owners: set[tuple[str, str]] = set()
        self._undo_stack: list[LayoutSnapshot] = []
        self._preview_snapshot: LayoutSnapshot | None = None
        self._snapshot_generation = 0
        self._last_discovery_warnings: list[str] = []
        self._pending_fade_cleanup: dict[tuple[str, str], PendingFadeCleanup] = {}
        self._last_scene_collection = ""

    def pending_fade_cleanup(self) -> tuple[str, ...]:
        """Compatibility view of pending fade sources."""
        return tuple(
            sorted(
                {item.source for item in self._pending_fade_cleanup.values()},
                key=str.casefold,
            )
        )

    def export_pending_fade_cleanup(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "kind": "layout_fade",
                "source": item.source,
                "collection": item.collection,
                "created_at": float(item.created_at),
                "attempts": int(item.attempts),
                "last_error": item.last_error,
            }
            for item in sorted(
                self._pending_fade_cleanup.values(),
                key=lambda value: (value.collection.casefold(), value.source.casefold()),
            )
        )

    def import_pending_fade_cleanup(self, raw_items) -> int:
        imported = 0
        for raw in raw_items or ():
            if not isinstance(raw, Mapping):
                continue
            if str(raw.get("kind") or "").strip().casefold() != "layout_fade":
                continue
            source = str(raw.get("source") or "").strip()
            collection = str(raw.get("collection") or "").strip()
            if not source or not collection:
                continue
            try:
                pending = PendingFadeCleanup(
                    source=source,
                    collection=collection,
                    created_at=float(raw.get("created_at", 0.0) or 0.0),
                    attempts=max(0, int(raw.get("attempts", 0) or 0)),
                    last_error=str(raw.get("last_error") or ""),
                )
            except (TypeError, ValueError, OverflowError):
                continue
            self._pending_fade_cleanup[(collection, source)] = pending
            imported += 1
        return imported

    def retry_pending_fade_cleanup(self) -> tuple[str, ...]:
        """Retry only obligations belonging to the active Scene Collection."""
        if not self._pending_fade_cleanup:
            return ()
        try:
            current = self._scene_collection_name()
        except Exception as exc:
            return (f"Scene Collection non lisible pour le cleanup fondu ({exc})",)

        warnings: list[str] = []
        for key, pending in tuple(self._pending_fade_cleanup.items()):
            if pending.collection != current:
                continue
            try:
                self._set_source_opacity(pending.source, 1.0)
            except Exception as exc:
                pending.attempts += 1
                pending.last_error = str(exc)
                warnings.append(
                    f"{pending.source}: neutralisation du fondu impossible ({exc})"
                )
                continue
            self._pending_fade_cleanup.pop(key, None)
        return tuple(warnings)

    def _fade_collection_context(self, *, probe: bool = False) -> str:
        if probe:
            try:
                current = self._scene_collection_name()
                if current:
                    return current
            except Exception:
                pass
        collection = str(self._last_scene_collection or "").strip()
        if collection:
            return collection
        try:
            return self._scene_collection_name()
        except Exception:
            # Unknown context is deliberately non-replayable. It is still
            # exported for diagnostics/recovery rather than being silently lost.
            return "<unknown>"

    def _ensure_pending_fade(self, source: str, collection: str) -> PendingFadeCleanup:
        key = (str(collection), str(source))
        pending = self._pending_fade_cleanup.get(key)
        if pending is None:
            pending = PendingFadeCleanup(
                source=str(source),
                collection=str(collection),
                created_at=time.monotonic(),
            )
            self._pending_fade_cleanup[key] = pending
        return pending

    def _neutralize_fade_sources(
        self,
        sources: Iterable[str],
        *,
        collection: str | None = None,
    ) -> tuple[str, ...]:
        warnings: list[str] = []
        collection = str(collection or self._fade_collection_context()).strip()
        wanted_sources = {str(item) for item in sources if str(item)}
        if not wanted_sources:
            return ()

        # Neutralization is a mutation and must obey the same Scene Collection
        # boundary as deferred retries. If the origin is unknown, or OBS is now
        # in another collection, keep the obligation but never guess/replay it.
        for source in wanted_sources:
            self._ensure_pending_fade(source, collection)
        if not collection or collection == "<unknown>":
            return tuple(
                f"{source}: neutralisation suspendue (Scene Collection d'origine inconnue)"
                for source in sorted(wanted_sources, key=str.casefold)
            )
        try:
            current = self._scene_collection_name()
        except Exception as exc:
            for source in wanted_sources:
                pending = self._pending_fade_cleanup[(collection, source)]
                pending.attempts += 1
                pending.last_error = str(exc)
            return tuple(
                f"{source}: neutralisation suspendue (collection OBS non lisible: {exc})"
                for source in sorted(wanted_sources, key=str.casefold)
            )
        if current != collection:
            return tuple(
                f"{source}: neutralisation suspendue ({collection} != {current})"
                for source in sorted(wanted_sources, key=str.casefold)
            )

        for source in wanted_sources:
            pending = self._pending_fade_cleanup[(collection, source)]
            try:
                self._set_source_opacity(source, 1.0)
            except Exception as exc:
                pending.attempts += 1
                pending.last_error = str(exc)
                warnings.append(f"{source}: opacité neutre non acquittée ({exc})")
            else:
                self._pending_fade_cleanup.pop((collection, source), None)
        return tuple(warnings)

    def set_cooperative_yield(self, callback) -> None:
        """Install a lightweight runtime checkpoint used during long transitions."""
        self._cooperative_yield = callback

    def _yield_runtime(self) -> None:
        callback = self._cooperative_yield
        if callback is not None:
            callback()

    def _cooperative_sleep(self, seconds: float) -> None:
        remaining = max(0.0, float(seconds))
        if remaining <= 0:
            self._yield_runtime()
            return
        if self._cooperative_yield is None:
            time.sleep(remaining)
            return
        deadline = time.monotonic() + remaining
        while True:
            self._yield_runtime()
            left = deadline - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(0.02, left))

    def invalidate_session(self) -> None:
        """Invalidate transient restore points after an OBS reconnect/session reset."""
        self._snapshot_generation += 1
        self.reset_cache()

    def _scene_collection_name(self) -> str:
        response = self.client.send("GetSceneCollectionList")
        collection = str(response.get("currentSceneCollectionName") or "").strip()
        if collection:
            self._last_scene_collection = collection
        return collection

    def _snapshot_target_count(self, profile: Mapping[str, Any]) -> int:
        return len(self._build_desired_elements(profile)) + len(self._build_support_desired(profile))

    @staticmethod
    def _captured_snapshot_count(profile: Mapping[str, Any]) -> int:
        count = 0
        modules = profile.get("modules")
        if isinstance(modules, Mapping):
            for module in modules.values():
                if not isinstance(module, Mapping):
                    continue
                elements = module.get("elements")
                if isinstance(elements, list):
                    count += sum(1 for item in elements if isinstance(item, Mapping))
        support = profile.get("support_items")
        if isinstance(support, list):
            count += sum(1 for item in support if isinstance(item, Mapping))
        return count

    def _capture_snapshot(self, profile: Mapping[str, Any], label: str) -> LayoutSnapshot:
        collection = ""
        warnings: list[str] = []
        try:
            collection = self._scene_collection_name()
        except Exception as exc:
            warnings.append(f"Scene Collection non lisible : {exc}")
        expected = self._snapshot_target_count(profile)
        captured = self.snapshot_profile(profile)
        actual = self._captured_snapshot_count(captured)
        complete = bool(collection) and actual >= expected
        if actual < expected:
            warnings.append(f"Snapshot incomplet : {actual}/{expected} élément(s) capturé(s)")
        return LayoutSnapshot(
            captured,
            label,
            collection=collection,
            generation=self._snapshot_generation,
            complete=complete,
            warnings=tuple(warnings),
        )

    def _snapshot_context_error(self, snapshot: LayoutSnapshot) -> str:
        if snapshot.generation != self._snapshot_generation:
            return "Snapshot issu d'une session OBS précédente ; restauration refusée."
        try:
            current = self._scene_collection_name()
        except Exception as exc:
            return f"Scene Collection non lisible ; restauration refusée : {exc}"
        if snapshot.collection and current != snapshot.collection:
            return (
                f"Snapshot lié à la Scene Collection '{snapshot.collection}', "
                f"collection active '{current}' ; restauration refusée."
            )
        if not snapshot.complete:
            return "Snapshot incomplet ; restauration sûre impossible."
        return ""

    def _restore_snapshot(self, snapshot: LayoutSnapshot) -> LayoutApplyResult:
        context_error = self._snapshot_context_error(snapshot)
        if context_error:
            return LayoutApplyResult(warnings=(context_error, *snapshot.warnings))
        return self.apply_profile(
            snapshot.profile,
            record_undo=False,
            transition_override={"mode": "instant", "duration_ms": 0},
        )

    def set_runtime_visibility_owners(
        self,
        items: Iterable[tuple[str, str]],
    ) -> None:
        """Declare scene items whose visibility belongs to a runtime subsystem.

        LayoutProfiles keep owning geometry for these items, but capture/apply/
        preview/undo must not read or write their temporary enabled state.
        """
        self._runtime_visibility_owners = {
            (str(container).strip(), str(source).strip())
            for container, source in items
            if str(container).strip() and str(source).strip()
        }

    def runtime_visibility_owned(
        self,
        container: str,
        source: str,
        raw: Mapping[str, Any] | None = None,
    ) -> bool:
        if (str(container).strip(), str(source).strip()) in self._runtime_visibility_owners:
            return True
        return bool(
            isinstance(raw, Mapping)
            and str(raw.get("visibility_owner") or "").casefold() == "runtime"
        )

    def runtime_visibility_owners(self) -> frozenset[tuple[str, str]]:
        """Return the current runtime-owned visibility claims as a snapshot."""

        return frozenset(self._runtime_visibility_owners)

    def reset_cache(self) -> None:
        self._scene_item_cache.clear()

    def set_item_enabled(
        self,
        container: str,
        source: str,
        enabled: bool,
        *,
        container_kind: str = "scene",
    ) -> None:
        """Set one scene/group item visibility through the normal cached path."""
        kind = str(container_kind or "scene").casefold()
        if kind not in {"scene", "group"}:
            raise ValueError(f"Type de conteneur OBS inconnu : {container_kind}")
        self._set_enabled(str(container), str(source), bool(enabled))

    def set_activation_item_enabled(
        self,
        container: str,
        source: str,
        enabled: bool,
        *,
        container_kind: str = "scene",
    ) -> None:
        """Mutate activation visibility after a fresh pair-local id resolution.

        Activation visibility must never trust a sceneItemId cached by layout
        discovery/geometry. OBS can legally reuse the old numeric id for another
        source after a structural edit while still accepting the mutation.
        """
        kind = str(container_kind or "scene").casefold()
        if kind not in {"scene", "group"}:
            raise ValueError(f"Type de conteneur OBS inconnu : {container_kind}")
        container_name = str(container).strip()
        source_name = str(source).strip()
        if not container_name or not source_name:
            raise OBSResourceNotFoundError(
                "GetSceneItemId",
                f"Source '{source_name}' introuvable dans '{container_name}'",
            )

        item_id = self._fresh_scene_item_id(container_name, source_name)
        payload = {
            "sceneName": container_name,
            "sceneItemId": item_id,
            "sceneItemEnabled": bool(enabled),
        }
        try:
            self.client.send("SetSceneItemEnabled", payload)
        except OBSResourceNotFoundError:
            # The graph may have changed between resolution and mutation.
            # Resolve the exact pair once more; confirmed absence then propagates.
            item_id = self._fresh_scene_item_id(container_name, source_name)
            payload["sceneItemId"] = item_id
            self.client.send("SetSceneItemEnabled", payload)

    def canvas_size(self) -> tuple[int, int] | None:
        try:
            response = self.client.send("GetVideoSettings")
            width = _int(response.get("baseWidth"))
            height = _int(response.get("baseHeight"))
            return (width, height) if width > 0 and height > 0 else None
        except Exception:
            return None

    def list_scenes(self) -> tuple[list[str], str]:
        response = self.client.send("GetSceneList")
        names = [
            str(scene.get("sceneName") or "")
            for scene in response.get("scenes", []) or []
            if isinstance(scene, Mapping) and str(scene.get("sceneName") or "").strip()
        ]
        current = str(response.get("currentProgramSceneName") or "")
        if not current:
            try:
                current_response = self.client.send("GetCurrentProgramScene")
                current = str(current_response.get("currentProgramSceneName") or "")
            except Exception:
                current = ""
        return names, current

    def scan_scene_topology(
        self,
        scene: str,
        *,
        recursive: bool = True,
    ) -> tuple[SceneTopologyItem, ...]:
        """Read scene/group membership without fetching any item transforms.

        This is intentionally lightweight and is used for runtime eligibility
        checks. Scene-item IDs are not retained after the scan.
        """
        scene = str(scene or "").strip()
        if not scene:
            return ()
        self._last_discovery_warnings = []
        out: list[SceneTopologyItem] = []
        self._scan_topology_container(
            root_scene=scene,
            container=scene,
            path=(scene,),
            out=out,
            recursive=recursive,
            depth=0,
            prefetched=None,
            container_kind="scene",
        )
        return tuple(out)

    def _scan_topology_container(
        self,
        *,
        root_scene: str,
        container: str,
        path: tuple[str, ...],
        out: list[SceneTopologyItem],
        recursive: bool,
        depth: int,
        prefetched: list[Mapping[str, Any]] | None,
        container_kind: str,
    ) -> None:
        if depth > 8:
            return
        self._yield_runtime()
        if prefetched is None:
            response = self.client.send("GetSceneItemList", {"sceneName": container})
            items = response.get("sceneItems", []) or []
        else:
            items = prefetched

        for raw in items:
            self._yield_runtime()
            if not isinstance(raw, Mapping):
                continue
            source = str(raw.get("sourceName") or "").strip()
            if not source:
                continue
            is_group = bool(raw.get("isGroup", False))
            source_type = str(raw.get("sourceType") or "")
            input_kind = str(raw.get("inputKind") or "")
            is_scene = (
                not is_group
                and (source_type == "OBS_SOURCE_TYPE_SCENE" or input_kind == "scene")
            )
            source_kind = "group" if is_group else ("scene" if is_scene else "input")
            parsed = parse_module_source(source)
            hard_locked = parsed is not None and "locked" in parsed.flags
            if parsed is not None and not hard_locked:
                out.append(
                    SceneTopologyItem(
                        scene=root_scene,
                        container=container,
                        path=path,
                        container_kind=container_kind,
                        source=source,
                        source_type=source_kind,
                        flags=parsed.flags,
                    )
                )
            if hard_locked or not recursive:
                continue
            if is_group:
                try:
                    group = self.client.send(
                        "GetGroupSceneItemList",
                        {"sceneName": source},
                    )
                    children = [
                        item
                        for item in group.get("sceneItems", []) or []
                        if isinstance(item, Mapping)
                    ]
                    self._scan_topology_container(
                        root_scene=root_scene,
                        container=source,
                        path=(*path, source),
                        out=out,
                        recursive=True,
                        depth=depth + 1,
                        prefetched=children,
                        container_kind="group",
                    )
                except Exception as exc:
                    self._last_discovery_warnings.append(
                        f"Groupe '{source}' non lisible depuis '{container}' : {exc}"
                    )
                continue
            if is_scene:
                try:
                    self._scan_topology_container(
                        root_scene=root_scene,
                        container=source,
                        path=(*path, source),
                        out=out,
                        recursive=True,
                        depth=depth + 1,
                        prefetched=None,
                        container_kind="scene",
                    )
                except Exception as exc:
                    self._last_discovery_warnings.append(
                        f"Scène imbriquée '{source}' non lisible depuis '{container}' : {exc}"
                    )

    def discover_scene(self, scene: str, *, recursive: bool = True) -> dict[str, list[CatalogElement]]:
        scene = str(scene or "").strip()
        if not scene:
            return {}
        self._last_discovery_warnings = []
        # Scene-item ids are ephemeral. A structural edit in OBS can invalidate
        # or even reuse ids without producing an error for the old id. Discovery
        # must therefore always rebuild the cache from the current scene graph.
        self.reset_cache()
        elements: list[CatalogElement] = []
        self._discover_container(
            root_scene=scene,
            container=scene,
            path=(scene,),
            out=elements,
            recursive=recursive,
            depth=0,
            prefetched=None,
            container_kind="scene",
        )

        # Naming convention: ``[Type] Module name``. The text between
        # brackets is a category/type, not the module identity. Each distinct
        # OBS scene item is therefore its own layout module. This prevents
        # unrelated sources such as ``[Global] Date`` and
        # ``[Global] Signature`` from being merged into one giant ``Global``
        # module. If the same source name exists in more than one nested
        # container, append the container name to keep the catalog key unique.
        grouped: dict[tuple[str, str], list[CatalogElement]] = {}
        source_containers: dict[str, set[str]] = {}
        for element in elements:
            grouped.setdefault((element.source, element.container), []).append(element)
            source_containers.setdefault(element.source, set()).add(element.container)

        modules: dict[str, list[CatalogElement]] = {}
        for (source, container), values in grouped.items():
            key = source
            if len(source_containers[source]) > 1:
                key = f"{source} @ {container}"
            values.sort(key=lambda item: item.element.casefold())
            modules[key] = values
        return dict(sorted(modules.items(), key=lambda item: item[0].casefold()))

    def _discover_container(
        self,
        *,
        root_scene: str,
        container: str,
        path: tuple[str, ...],
        out: list[CatalogElement],
        recursive: bool,
        depth: int,
        prefetched: list[Mapping[str, Any]] | None,
        container_kind: str,
    ) -> None:
        if depth > 8:
            return
        self._yield_runtime()
        if prefetched is None:
            response = self.client.send("GetSceneItemList", {"sceneName": container})
            items = response.get("sceneItems", []) or []
        else:
            items = prefetched

        for raw in items:
            self._yield_runtime()
            if not isinstance(raw, Mapping):
                continue
            source = str(raw.get("sourceName") or "")
            item_id = _int(raw.get("sceneItemId"))
            if not source or not item_id:
                continue
            self._scene_item_cache[(container, source)] = item_id
            is_group = bool(raw.get("isGroup", False))
            source_type = str(raw.get("sourceType") or "")
            input_kind = str(raw.get("inputKind") or "")
            is_scene = (
                not is_group
                and (source_type == "OBS_SOURCE_TYPE_SCENE" or input_kind == "scene")
            )
            source_kind = "group" if is_group else ("scene" if is_scene else "input")

            parsed = parse_module_source(source)
            hard_locked = parsed is not None and "locked" in parsed.flags
            if parsed is not None and not hard_locked:
                transform_response = self.client.send(
                    "GetSceneItemTransform",
                    {"sceneName": container, "sceneItemId": item_id},
                )
                transform = transform_response.get("sceneItemTransform") or {}
                if not isinstance(transform, Mapping):
                    transform = {}
                out.append(
                    CatalogElement(
                        scene=root_scene,
                        container=container,
                        path=path,
                        container_kind=container_kind,
                        module=parsed.module,
                        element=parsed.element,
                        source=source,
                        enabled=bool(raw.get("sceneItemEnabled", True)),
                        transform=dict(transform),
                        source_type=source_kind,
                        flags=parsed.flags,
                    )
                )

            # ``:locked`` is a hard OBS-side exclusion convention. The item and
            # its whole subtree stay outside LayoutProfiles, so a later
            # recapture cannot accidentally overwrite its geometry, visibility
            # or internal composition.
            if hard_locked:
                continue

            if not recursive:
                continue
            if is_group:
                try:
                    group = self.client.send("GetGroupSceneItemList", {"sceneName": source})
                    children = [item for item in group.get("sceneItems", []) or [] if isinstance(item, Mapping)]
                    self._discover_container(
                        root_scene=root_scene,
                        container=source,
                        path=(*path, source),
                        out=out,
                        recursive=True,
                        depth=depth + 1,
                        prefetched=children,
                        container_kind="group",
                    )
                except Exception as exc:
                    self._last_discovery_warnings.append(
                        f"Groupe '{source}' non lisible depuis '{container}' : {exc}"
                    )
                continue

            if is_scene:
                try:
                    self._discover_container(
                        root_scene=root_scene,
                        container=source,
                        path=(*path, source),
                        out=out,
                        recursive=True,
                        depth=depth + 1,
                        prefetched=None,
                        container_kind="scene",
                    )
                except Exception as exc:
                    self._last_discovery_warnings.append(
                        f"Scène imbriquée '{source}' non lisible depuis '{container}' : {exc}"
                    )

    def capture_profile_result(
        self,
        scene: str,
        *,
        selected_sources: Iterable[str] | None = None,
        extends: str = "",
        transition: Mapping[str, Any] | None = None,
    ) -> LayoutCaptureResult:
        profile = self.capture_profile(
            scene,
            selected_sources=selected_sources,
            extends=extends,
            transition=transition,
        )
        modules = profile.get("modules") if isinstance(profile.get("modules"), Mapping) else {}
        warnings = tuple(self._last_discovery_warnings)
        return LayoutCaptureResult(
            profile=profile,
            complete=not warnings,
            warnings=warnings,
            captured_modules=len(modules),
        )

    def capture_profile(
        self,
        scene: str,
        *,
        selected_sources: Iterable[str] | None = None,
        extends: str = "",
        transition: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected = None if selected_sources is None else {str(item) for item in selected_sources}
        catalog = self.discover_scene(scene)
        canvas = self.canvas_size()
        modules_raw: dict[str, Any] = {}
        for module_key, elements in catalog.items():
            included = [element for element in elements if selected is None or element.source in selected]
            if not included:
                continue
            base_bounds = _union_bounds(included)
            element_payload = []
            module_locked = False
            for element in elements:
                is_included = selected is None or element.source in selected
                flags = set(element.flags)
                if "locked" in flags:
                    module_locked = True
                runtime_visibility = self.runtime_visibility_owned(
                    element.container,
                    element.source,
                )
                element_payload.append(
                    {
                        "source": element.source,
                        "element": element.element,
                        "container": element.container,
                        "container_kind": element.container_kind,
                        "path": list(element.path),
                        # Never persist a temporary runtime-visible state as a
                        # LayoutProfile baseline. False is the neutral hidden
                        # baseline; follow_visibility below makes it non-owned.
                        "enabled": False if runtime_visibility else element.enabled,
                        "included": is_included,
                        "follow_position": "fixed" not in flags and "nomove" not in flags,
                        "follow_size": "fixed" not in flags and "noresize" not in flags,
                        "follow_visibility": (
                            not runtime_visibility and "novis" not in flags
                        ),
                        "visibility_owner": "runtime" if runtime_visibility else "",
                        "locked": "locked" in flags,
                        "flags": sorted(flags),
                        "transform": dict(element.transform),
                        "source_type": element.source_type,
                    }
                )
            # Every OBS *scene* uses the global base-canvas coordinate system,
            # including nested scenes. Group children, on the other hand, use
            # group-local coordinates. This distinction matters when the OBS
            # base canvas changes: nested scene items must follow the canvas,
            # while group children must keep their local geometry.
            coordinate_space = (
                "root_canvas" if elements[0].container_kind == "scene" else "container_local"
            )
            module = {
                "display_name": elements[0].element,
                "module_type": elements[0].module,
                "source_name": elements[0].source,
                "container": elements[0].container,
                "container_kind": elements[0].container_kind,
                "coordinate_space": coordinate_space,
                "visible": any(
                    item.enabled
                    for item in included
                    if not self.runtime_visibility_owned(item.container, item.source)
                )
                if any(
                    not self.runtime_visibility_owned(item.container, item.source)
                    for item in included
                )
                else True,
                "managed": True,
                "locked": module_locked,
                "lock_aspect": True,
                "anchor": "top_left",
                "anchor_mode": "relative",
                "base_bounds": dict(base_bounds),
                "geometry": dict(base_bounds),
                "elements": element_payload,
            }
            # Only modules that live directly in the selected/root scene use
            # the root OBS canvas as their coordinate system. Nested scenes and
            # groups have their own local coordinate space. Scaling those local
            # transforms again when the root canvas changes would compound the
            # parent scale (e.g. Webcam -> Avatar -> Avatar Dynamic) and break
            # the relative composition.
            if canvas and coordinate_space == "root_canvas":
                module["normalized_geometry"] = self._normalize_geometry(base_bounds, canvas)
                module["anchor_offsets"] = self._anchor_offsets(base_bounds, canvas, "top_left")
            modules_raw[module_key] = module

        # Capture the implementation details of managed scene modules as well.
        # A module scene can contain ordinary sources that do not use the
        # ``[Type] Name`` convention (for example Avatar Dynamic.png). Those
        # sources still need to follow a base-canvas change, otherwise a nested
        # scene may keep its outer frame aligned while its internal PNG drifts or
        # shrinks. Only descendants of managed scene modules are captured here;
        # unrelated sources in the root scene remain outside SSR ownership.
        scene_names = set(self.list_scenes()[0])
        managed_scene_roots: list[tuple[str, tuple[str, ...]]] = []
        for values in catalog.values():
            for element in values:
                if selected is not None and element.source not in selected:
                    continue
                if element.source in scene_names:
                    managed_scene_roots.append((element.source, (*element.path, element.source)))
        support_items = self._capture_managed_scene_support(managed_scene_roots, scene_names)

        profile: dict[str, Any] = {
            "scene": scene,
            "coordinate_mode": "normalized",
            "modules": modules_raw,
            "support_items": support_items,
            "transition": dict(transition or {"mode": "instant", "duration_ms": 0, "steps": 8}),
            "conditions": {},
        }
        if extends:
            profile["extends"] = extends
        if canvas:
            profile["canvas"] = {"width": canvas[0], "height": canvas[1]}
        return profile

    def _capture_managed_scene_support(
        self,
        roots: Iterable[tuple[str, tuple[str, ...]]],
        scene_names: set[str],
    ) -> list[dict[str, Any]]:
        """Capture non-module descendants of managed module scenes.

        OBS scenes all share the base-canvas coordinate system. A module scene
        can however contain ordinary implementation sources (PNG, browser,
        group...) whose names intentionally do not follow SSR's module naming
        convention. They are still part of that managed module's visual
        composition and must be restored when the canvas changes.
        """
        captured: list[dict[str, Any]] = []
        visited_scenes: set[str] = set()

        def walk_group(group: str, path: tuple[str, ...], depth: int) -> None:
            if depth > 10:
                return
            try:
                response = self.client.send("GetGroupSceneItemList", {"sceneName": group})
            except Exception as exc:
                self._last_discovery_warnings.append(
                    f"Groupe de support '{group}' non lisible : {exc}"
                )
                return
            for raw in response.get("sceneItems", []) or []:
                if not isinstance(raw, Mapping):
                    continue
                source = str(raw.get("sourceName") or "")
                item_id = _int(raw.get("sceneItemId"))
                if not source or not item_id:
                    continue
                self._scene_item_cache[(group, source)] = item_id
                parsed = parse_module_source(source)
                hard_locked = parsed is not None and "locked" in parsed.flags
                if hard_locked:
                    # A locked module owns its descendants as well: do not
                    # capture hidden implementation items through support_items.
                    continue
                is_group = bool(raw.get("isGroup", False))
                source_type = str(raw.get("sourceType") or "")
                input_kind = str(raw.get("inputKind") or "")
                # OBS groups are internally scene-like sources and can report
                # OBS_SOURCE_TYPE_SCENE. They are nevertheless *not* valid
                # GetSceneItemList targets; group children must be queried with
                # GetGroupSceneItemList. Always give isGroup precedence.
                is_scene = (
                    not is_group
                    and (
                        source in scene_names
                        or source_type == "OBS_SOURCE_TYPE_SCENE"
                        or input_kind == "scene"
                    )
                )
                source_kind = "group" if is_group else ("scene" if is_scene else "input")
                if parsed is None:
                    tr = self.client.send(
                        "GetSceneItemTransform",
                        {"sceneName": group, "sceneItemId": item_id},
                    ).get("sceneItemTransform") or {}
                    runtime_visibility = self.runtime_visibility_owned(group, source)
                    captured.append({
                        "container": group,
                        "container_kind": "group",
                        "path": list(path),
                        "source": source,
                        "enabled": (
                            False
                            if runtime_visibility
                            else bool(raw.get("sceneItemEnabled", True))
                        ),
                        "visibility_owner": "runtime" if runtime_visibility else "",
                        "source_type": source_kind,
                        "transform": dict(tr) if isinstance(tr, Mapping) else {},
                    })
                if is_group:
                    walk_group(source, (*path, source), depth + 1)
                    continue
                if is_scene:
                    walk_scene(source, (*path, source), depth + 1)

        def walk_scene(scene_name: str, path: tuple[str, ...], depth: int) -> None:
            if depth > 10 or scene_name in visited_scenes:
                return
            visited_scenes.add(scene_name)
            try:
                response = self.client.send("GetSceneItemList", {"sceneName": scene_name})
            except Exception as exc:
                self._last_discovery_warnings.append(
                    f"Scène de support '{scene_name}' non lisible : {exc}"
                )
                return
            for raw in response.get("sceneItems", []) or []:
                if not isinstance(raw, Mapping):
                    continue
                source = str(raw.get("sourceName") or "")
                item_id = _int(raw.get("sceneItemId"))
                if not source or not item_id:
                    continue
                self._scene_item_cache[(scene_name, source)] = item_id
                parsed = parse_module_source(source)
                hard_locked = parsed is not None and "locked" in parsed.flags
                if hard_locked:
                    # A locked module owns its descendants as well: do not
                    # capture hidden implementation items through support_items.
                    continue
                is_group = bool(raw.get("isGroup", False))
                source_type = str(raw.get("sourceType") or "")
                input_kind = str(raw.get("inputKind") or "")
                # A group may advertise a scene source type, but OBS rejects
                # GetSceneItemList for it with code 602. isGroup is authoritative.
                is_scene = (
                    not is_group
                    and (
                        source in scene_names
                        or source_type == "OBS_SOURCE_TYPE_SCENE"
                        or input_kind == "scene"
                    )
                )
                source_kind = "group" if is_group else ("scene" if is_scene else "input")
                if parsed is None:
                    tr = self.client.send(
                        "GetSceneItemTransform",
                        {"sceneName": scene_name, "sceneItemId": item_id},
                    ).get("sceneItemTransform") or {}
                    runtime_visibility = self.runtime_visibility_owned(scene_name, source)
                    captured.append({
                        "container": scene_name,
                        "container_kind": "scene",
                        "path": list(path),
                        "source": source,
                        "enabled": (
                            False
                            if runtime_visibility
                            else bool(raw.get("sceneItemEnabled", True))
                        ),
                        "visibility_owner": "runtime" if runtime_visibility else "",
                        "source_type": source_kind,
                        "transform": dict(tr) if isinstance(tr, Mapping) else {},
                    })
                if is_group:
                    walk_group(source, (*path, source), depth + 1)
                    continue
                if is_scene:
                    walk_scene(source, (*path, source), depth + 1)

        for scene_name, path in roots:
            walk_scene(scene_name, path, 0)
        return captured

    @staticmethod
    def _profile_group_sources(profile: Mapping[str, Any]) -> set[str]:
        """Infer OBS group source names from captured profile topology.

        SSR 2.0.5/2.0.6 could mislabel a group as ``scene`` because OBS groups
        can report ``OBS_SOURCE_TYPE_SCENE``. Existing LayoutProfiles can still
        be repaired at apply time: any container explicitly marked ``group`` is
        itself the source name of an OBS group in its parent container.
        """
        groups: set[str] = set()
        raw_items = profile.get("support_items")
        if isinstance(raw_items, list):
            for raw in raw_items:
                if not isinstance(raw, Mapping):
                    continue
                if str(raw.get("container_kind") or "").casefold() == "group":
                    container = str(raw.get("container") or "").strip()
                    if container:
                        groups.add(container)
                if str(raw.get("source_type") or "").casefold() == "group":
                    source = str(raw.get("source") or "").strip()
                    if source:
                        groups.add(source)
        modules = profile.get("modules")
        if isinstance(modules, Mapping):
            for raw_module in modules.values():
                if not isinstance(raw_module, Mapping):
                    continue
                elements = raw_module.get("elements")
                if not isinstance(elements, list):
                    continue
                for element in elements:
                    if not isinstance(element, Mapping):
                        continue
                    if str(element.get("container_kind") or "").casefold() == "group":
                        container = str(element.get("container") or "").strip()
                        if container:
                            groups.add(container)
                    if str(element.get("source_type") or "").casefold() == "group":
                        source = str(element.get("source") or "").strip()
                        if source:
                            groups.add(source)
        return groups

    def _build_support_desired(self, profile: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_items = profile.get("support_items")
        if not isinstance(raw_items, list):
            return []
        captured_canvas = profile.get("canvas") if isinstance(profile.get("canvas"), Mapping) else {}
        current_canvas = self.canvas_size()
        group_sources = self._profile_group_sources(profile)
        pw = _float(captured_canvas.get("width"))
        ph = _float(captured_canvas.get("height"))
        if current_canvas and pw > 0 and ph > 0:
            rx = current_canvas[0] / pw
            ry = current_canvas[1] / ph
        else:
            rx = ry = 1.0

        desired: list[dict[str, Any]] = []
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                continue
            source = str(raw.get("source") or "")
            container = str(raw.get("container") or "")
            transform = raw.get("transform")
            if not source or not container or not isinstance(transform, Mapping):
                continue
            scene_space = str(raw.get("container_kind") or "scene") == "scene"
            sx = rx if scene_space else 1.0
            sy = ry if scene_space else 1.0
            update: dict[str, Any] = {
                "positionX": _float(transform.get("positionX")) * sx,
                "positionY": _float(transform.get("positionY")) * sy,
            }
            bounds_type = str(transform.get("boundsType") or "OBS_BOUNDS_NONE")
            if bounds_type and bounds_type != "OBS_BOUNDS_NONE":
                if "boundsWidth" in transform:
                    update["boundsWidth"] = max(1.0, _float(transform.get("boundsWidth")) * sx)
                if "boundsHeight" in transform:
                    update["boundsHeight"] = max(1.0, _float(transform.get("boundsHeight")) * sy)
            else:
                captured_width = abs(_float(transform.get("width")))
                captured_height = abs(_float(transform.get("height")))
                captured_scale_x = _float(transform.get("scaleX"), 1.0)
                captured_scale_y = _float(transform.get("scaleY"), 1.0)
                if captured_width > 0.0001:
                    update["__ssr_visual_width"] = captured_width * sx
                    update["__ssr_scale_sign_x"] = -1.0 if captured_scale_x < 0 else 1.0
                    update["__ssr_fallback_scale_x"] = captured_scale_x * sx
                else:
                    update["scaleX"] = captured_scale_x * sx
                if captured_height > 0.0001:
                    update["__ssr_visual_height"] = captured_height * sy
                    update["__ssr_scale_sign_y"] = -1.0 if captured_scale_y < 0 else 1.0
                    update["__ssr_fallback_scale_y"] = captured_scale_y * sy
                else:
                    update["scaleY"] = captured_scale_y * sy
            runtime_visibility = self.runtime_visibility_owned(container, source, raw)
            desired.append({
                "module": "[interne]",
                "element": source,
                "source": source,
                "container": container,
                "container_kind": str(raw.get("container_kind") or "scene"),
                "source_type": (
                    "group"
                    if source in group_sources
                    else str(raw.get("source_type") or "input")
                ),
                "path": list(raw.get("path") or ()),
                "transform": update,
                "enabled": None if runtime_visibility else bool(raw.get("enabled", True)),
                "visibility_owner": "runtime" if runtime_visibility else "",
                "support": True,
            })
        return desired

    def validate_profile(self, profile: Mapping[str, Any]) -> list[LayoutValidationIssue]:
        issues: list[LayoutValidationIssue] = []
        scene = str(profile.get("scene") or "").strip()
        if not scene:
            issues.append(LayoutValidationIssue("error", "Aucune scène OBS n'est définie."))
            return issues
        current_canvas = self.canvas_size()
        captured_canvas = profile.get("canvas") if isinstance(profile.get("canvas"), Mapping) else {}
        if current_canvas and captured_canvas:
            cw, ch = current_canvas
            pw, ph = _int(captured_canvas.get("width")), _int(captured_canvas.get("height"))
            if pw and ph and (cw, ch) != (pw, ph):
                issues.append(
                    LayoutValidationIssue(
                        "info",
                        f"Canvas différent : profil {pw}×{ph}, OBS {cw}×{ch}. Les coordonnées normalisées seront utilisées.",
                    )
                )

        self.reset_cache()
        desired = self._build_desired_elements(profile)
        desired.extend(self._build_support_desired(profile))
        for item in desired:
            module = str(item.get("module") or "")
            source = str(item.get("source") or "")
            container = str(item.get("container") or "")
            try:
                current = self._get_current_item(container, source)
            except OBSResourceNotFoundError:
                issues.append(
                    LayoutValidationIssue(
                        "warning",
                        f"Source absente ou renommée dans le conteneur '{container}'.",
                        module,
                        source,
                    )
                )
                continue
            except Exception as exc:
                issues.append(
                    LayoutValidationIssue(
                        "error",
                        f"Lecture OBS impossible dans '{container}' : {exc}",
                        module,
                        source,
                    )
                )
                continue
            if item.get("enabled") is not None and current.get("enabled") is None:
                issues.append(
                    LayoutValidationIssue(
                        "warning",
                        f"Visibilité actuelle inconnue dans '{container}'.",
                        module,
                        source,
                    )
                )

        modules = profile.get("modules", {}) if isinstance(profile.get("modules"), Mapping) else {}
        for module_name, raw in modules.items():
            if isinstance(raw, Mapping) and bool(raw.get("locked", False)):
                issues.append(
                    LayoutValidationIssue(
                        "info",
                        "Module verrouillé dans SSR.",
                        str(module_name),
                    )
                )
        return issues

    @staticmethod
    def _diff_tolerance(key: str) -> float:
        if key in {"scaleX", "scaleY"}:
            return 0.005
        if key == "rotation":
            return 0.1
        return 0.5

    def diff_profile(self, profile: Mapping[str, Any]) -> list[LayoutDiffItem]:
        self.reset_cache()
        desired = self._build_desired_elements(profile)
        desired.extend(self._build_support_desired(profile))
        diffs: list[LayoutDiffItem] = []
        for item in desired:
            container = str(item["container"])
            source = str(item["source"])
            try:
                current = self._get_current_item(container, source)
            except OBSResourceNotFoundError:
                diffs.append(
                    LayoutDiffItem(
                        str(item.get("module") or ""),
                        source,
                        (f"source absente dans {container}",),
                    )
                )
                continue
            except Exception as exc:
                diffs.append(
                    LayoutDiffItem(
                        str(item.get("module") or ""),
                        source,
                        (f"lecture OBS impossible dans {container}: {exc}",),
                    )
                )
                continue
            changes: list[str] = []
            current_transform = current["transform"]
            desired_transform = self._resolve_runtime_transform(
                item["transform"], current_transform
            )
            for key in (
                "positionX", "positionY", "scaleX", "scaleY", "rotation",
                "boundsWidth", "boundsHeight",
            ):
                if key not in desired_transform:
                    continue
                before = _float(current_transform.get(key))
                after = _float(desired_transform.get(key))
                if abs(before - after) > self._diff_tolerance(key):
                    changes.append(f"{key} {before:.3f}→{after:.3f}")
            if item["enabled"] is not None:
                current_enabled = current.get("enabled")
                if current_enabled is None:
                    changes.append("visibilité actuelle inconnue")
                elif bool(current_enabled) != bool(item["enabled"]):
                    changes.append(
                        f"visible {bool(current_enabled)}→{bool(item['enabled'])}"
                    )
            if changes:
                diffs.append(
                    LayoutDiffItem(
                        str(item.get("module") or ""),
                        source,
                        tuple(changes),
                    )
                )
        return diffs

    def preview_profile(self, profile: Mapping[str, Any]) -> LayoutApplyResult:
        if self._preview_snapshot is not None:
            cancelled = self.cancel_preview()
            if cancelled.warnings or cancelled.missing_sources:
                return cancelled
        snapshot = self._capture_snapshot(profile, "preview")
        if not snapshot.complete:
            return LayoutApplyResult(warnings=(
                "Aperçu refusé : impossible de garantir une restauration complète.",
                *snapshot.warnings,
            ))
        result = self.apply_profile(profile, record_undo=False)
        if result.elements_applied or not result.warnings:
            self._preview_snapshot = snapshot
        return result

    def cancel_preview(self) -> LayoutApplyResult:
        if self._preview_snapshot is None:
            return LayoutApplyResult()
        snapshot = self._preview_snapshot
        result = self._restore_snapshot(snapshot)
        if not result.warnings and not result.missing_sources:
            self._preview_snapshot = None
        return result

    def commit_preview(self) -> None:
        if self._preview_snapshot is not None:
            snapshot = self._preview_snapshot
            if not self._snapshot_context_error(snapshot):
                self._undo_stack.append(snapshot)
                self._undo_stack = self._undo_stack[-20:]
            self._preview_snapshot = None

    def snapshot_profile(self, profile: Mapping[str, Any]) -> dict[str, Any]:
        # Scene-item ids are only valid for the current OBS scene graph.
        self.reset_cache()
        desired = self._build_desired_elements(profile)
        scene = str(profile.get("scene") or "")
        by_module: dict[str, list[CatalogElement]] = {}
        for item in desired:
            self._yield_runtime()
            try:
                current = self._get_current_item(item["container"], item["source"])
            except Exception:
                continue
            if current.get("enabled") is None and item.get("enabled") is not None:
                raise RuntimeError(
                    f"Visibilité inconnue pour {item['container']}/{item['source']}: "
                    f"{current.get('enabled_error') or 'lecture OBS impossible'}"
                )
            by_module.setdefault(item["module"], []).append(
                CatalogElement(
                    scene=scene,
                    container=item["container"],
                    path=tuple(item.get("path") or (scene,)),
                    container_kind=str(item.get("container_kind") or "scene"),
                    module=item["module"],
                    element=item.get("element", item["source"]),
                    source=item["source"],
                    enabled=bool(current["enabled"]),
                    transform=dict(current["transform"]),
                    source_type=str(item.get("source_type") or "input"),
                )
            )
        canvas = self.canvas_size()
        modules: dict[str, Any] = {}
        for name, elements in by_module.items():
            bounds = _union_bounds(elements)
            coordinate_space = (
                "root_canvas" if elements[0].container_kind == "scene" else "container_local"
            )
            modules[name] = {
                "display_name": elements[0].module,
                "container": elements[0].container,
                "container_kind": elements[0].container_kind,
                "coordinate_space": coordinate_space,
                "visible": any(e.enabled for e in elements),
                "managed": True,
                "locked": False,
                "lock_aspect": True,
                "anchor": "top_left",
                "anchor_mode": "relative",
                "base_bounds": bounds,
                "geometry": dict(bounds),
                "elements": [
                    {
                        "source": e.source,
                        "element": e.element,
                        "container": e.container,
                        "container_kind": e.container_kind,
                        "path": list(e.path),
                        "enabled": (
                            False
                            if self.runtime_visibility_owned(e.container, e.source)
                            else e.enabled
                        ),
                        "included": True,
                        "follow_position": True,
                        "follow_size": True,
                        "follow_visibility": not self.runtime_visibility_owned(
                            e.container,
                            e.source,
                        ),
                        "visibility_owner": (
                            "runtime"
                            if self.runtime_visibility_owned(e.container, e.source)
                            else ""
                        ),
                        "locked": False,
                        "flags": [],
                        "source_type": e.source_type,
                        "transform": dict(e.transform),
                    }
                    for e in elements
                ],
            }
            if canvas and coordinate_space == "root_canvas":
                modules[name]["normalized_geometry"] = self._normalize_geometry(bounds, canvas)
        support_snapshot: list[dict[str, Any]] = []
        for raw in profile.get("support_items", []) if isinstance(profile.get("support_items"), list) else []:
            if not isinstance(raw, Mapping):
                continue
            container = str(raw.get("container") or "")
            source = str(raw.get("source") or "")
            if not container or not source:
                continue
            try:
                current = self._get_current_item(container, source)
            except Exception:
                continue
            item = copy.deepcopy(dict(raw))
            item["transform"] = dict(current["transform"])
            runtime_visibility = self.runtime_visibility_owned(container, source, raw)
            if current.get("enabled") is None and not runtime_visibility:
                raise RuntimeError(
                    f"Visibilité inconnue pour {container}/{source}: "
                    f"{current.get('enabled_error') or 'lecture OBS impossible'}"
                )
            if runtime_visibility:
                item["enabled"] = False
                item["visibility_owner"] = "runtime"
            else:
                item["enabled"] = bool(current["enabled"])
            support_snapshot.append(item)

        snapshot: dict[str, Any] = {
            "scene": scene,
            "coordinate_mode": "absolute",
            "modules": modules,
            "support_items": support_snapshot,
            "transition": {"mode": "instant", "duration_ms": 0},
        }
        if canvas:
            snapshot["canvas"] = {"width": canvas[0], "height": canvas[1]}
        return snapshot

    def undo_last(self) -> LayoutApplyResult:
        if not self._undo_stack:
            return LayoutApplyResult(warnings=("Aucun état précédent à restaurer.",))
        snapshot = self._undo_stack[-1]
        result = self._restore_snapshot(snapshot)
        if not result.warnings and not result.missing_sources:
            self._undo_stack.pop()
        return result

    def _transform_needs_update(
        self,
        current: Mapping[str, Any],
        target: Mapping[str, Any],
    ) -> bool:
        for key, value in target.items():
            if str(key).startswith("__ssr_"):
                continue
            if isinstance(value, (int, float)):
                if abs(_float(current.get(key)) - _float(value)) > self._diff_tolerance(str(key)):
                    return True
            elif current.get(key) != value:
                return True
        return False

    def apply_profile(
        self,
        profile: Mapping[str, Any],
        *,
        record_undo: bool = True,
        transition_override: Mapping[str, Any] | None = None,
    ) -> LayoutApplyResult:
        scene = str(profile.get("scene") or "").strip()
        if not scene:
            return LayoutApplyResult()

        # IMPORTANT: never trust cached sceneItemId values across a structural
        # OBS edit (delete/add/reorder/rebuild). OBS may invalidate ids or reuse
        # an old numeric id for another source. Resolve every apply from a clean
        # cache by (container, source name).
        self.reset_cache()
        undo_snapshot: LayoutSnapshot | None = None
        if record_undo:
            try:
                candidate = self._capture_snapshot(profile, "apply")
                if candidate.complete:
                    undo_snapshot = candidate
            except Exception:
                undo_snapshot = None

        desired = self._build_desired_elements(profile)
        desired.extend(self._build_support_desired(profile))
        # Descendants first is important for OBS groups: changing children can
        # make OBS recompute a group's bounds.
        desired.sort(key=lambda item: len(item.get("path") or ()), reverse=True)
        group_items = [
            item for item in desired
            if str(item.get("source_type") or "").casefold() == "group"
        ]
        regular_items = [
            item for item in desired
            if str(item.get("source_type") or "").casefold() != "group"
        ]

        transition = dict(profile.get("transition") or {}) if isinstance(profile.get("transition"), Mapping) else {}
        if transition_override is not None:
            transition = dict(transition_override)
        mode = str(transition.get("mode") or "instant").casefold()
        duration_ms = max(0, _int(transition.get("duration_ms"), 0))
        steps = self._effective_transition_steps(
            duration_ms,
            _int(transition.get("steps"), 8),
        )

        applied = 0
        skipped = 0
        modules_for_skip = profile.get("modules") if isinstance(profile.get("modules"), Mapping) else {}
        for raw_module in modules_for_skip.values():
            if not isinstance(raw_module, Mapping):
                continue
            if not bool(raw_module.get("managed", True)) or bool(raw_module.get("locked", False)):
                elements = raw_module.get("elements", [])
                if isinstance(elements, list):
                    skipped += sum(1 for item in elements if isinstance(item, Mapping))
                continue
            elements = raw_module.get("elements", [])
            if isinstance(elements, list):
                skipped += sum(
                    1
                    for item in elements
                    if isinstance(item, Mapping)
                    and (not bool(item.get("included", True)) or bool(item.get("locked", False)))
                )

        missing: list[str] = []
        warnings: list[str] = []
        mutated_any = False

        def prepare_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
            nonlocal skipped
            self._yield_runtime()
            if item.get("skip"):
                skipped += 1
                return None
            container = str(item["container"])
            source = str(item["source"])
            try:
                current = self._get_current_item(container, source)
            except Exception:
                missing.append(source)
                skipped += 1
                return None
            target_transform = self._resolve_runtime_transform(
                item["transform"], current["transform"]
            )
            target_enabled = item["enabled"]
            current_enabled = current.get("enabled")
            return {
                "item": item,
                "container": container,
                "source": source,
                "current_transform": current["transform"],
                "target_transform": target_transform,
                "transform_changed": self._transform_needs_update(
                    current["transform"], target_transform
                ),
                "current_enabled": current_enabled,
                "target_enabled": target_enabled,
                "visibility_changed": (
                    target_enabled is not None
                    and (
                        current_enabled is None
                        or bool(target_enabled) != bool(current_enabled)
                    )
                ),
            }

        def apply_item_immediate(item: Mapping[str, Any]) -> None:
            nonlocal applied, mutated_any
            prepared = prepare_item(item)
            if prepared is None:
                return
            container = prepared["container"]
            source = prepared["source"]
            target_transform = prepared["target_transform"]
            target_enabled = prepared["target_enabled"]
            # A zero-duration fade is just an immediate visibility change.
            if target_transform and prepared["transform_changed"]:
                self._set_transform(container, source, target_transform)
                mutated_any = True
            if prepared["visibility_changed"]:
                self._set_enabled(container, source, bool(target_enabled))
                mutated_any = True
            applied += 1

        animated = mode in {"move", "fade", "move_fade"} and duration_ms > 0
        if animated:
            prepared_items: list[dict[str, Any]] = []
            for item in desired:
                prepared = prepare_item(item)
                if prepared is not None:
                    prepared_items.append(prepared)
            actionable = [
                prepared
                for prepared in prepared_items
                if prepared["transform_changed"] or prepared["visibility_changed"]
            ]
            if actionable:
                mutated_any = True
                self._animate_layout_transition(
                    actionable,
                    mode=mode,
                    duration_ms=duration_ms,
                    steps=steps,
                    warnings=warnings,
                )
            applied += len(prepared_items)
        else:
            for item in regular_items:
                apply_item_immediate(item)
            if group_items:
                self._wait_group_resize_settle()
                for item in sorted(
                    group_items, key=lambda value: len(value.get("path") or ()), reverse=True
                ):
                    apply_item_immediate(item)

        # Groups need a final correction after descendants have settled. This is
        # intentionally outside the animation timeline: the transition itself is
        # global and lasts duration_ms once, not duration_ms once per source.
        # Keep the fact that a mutation happened *before* the writes; re-reading
        # after convergence would incorrectly suppress group stabilization.
        if group_items and mutated_any:
            self._wait_group_resize_settle()
            for item in sorted(
                group_items, key=lambda value: len(value.get("path") or ()), reverse=True
            ):
                container = str(item["container"])
                source = str(item["source"])
                try:
                    current = self._get_current_item(container, source)
                except Exception:
                    continue
                target_transform = self._resolve_runtime_transform(
                    item["transform"], current["transform"]
                )
                if target_transform and self._transform_needs_update(
                    current["transform"], target_transform
                ):
                    self._set_transform(container, source, target_transform)

            # One more render frame prevents the group's automatic resize pass
            # from winning a race against the final WebSocket transform.
            self._wait_group_resize_settle()
            for item in sorted(
                group_items, key=lambda value: len(value.get("path") or ()), reverse=True
            ):
                container = str(item["container"])
                source = str(item["source"])
                try:
                    current = self._get_current_item(container, source)
                except Exception:
                    continue
                target_transform = self._resolve_runtime_transform(
                    item["transform"], current["transform"]
                )
                if target_transform:
                    self._set_transform(container, source, target_transform)

        result = LayoutApplyResult(applied, skipped, tuple(sorted(set(missing))), tuple(warnings))
        if undo_snapshot is not None and applied > 0:
            self._undo_stack.append(undo_snapshot)
            self._undo_stack = self._undo_stack[-20:]
        return result

    @staticmethod
    def _effective_transition_steps(duration_ms: int, configured_steps: int) -> int:
        """Return a smooth but bounded number of animation frames.

        Historical profiles store 8 steps, which is visibly jerky. Treat the
        stored value as a minimum quality hint and target the OBS render-friendly
        cadence of about 60 FPS. The 361-point hard cap bounds WebSocket work
        even for unusually long hand-edited transition durations.
        """
        configured = max(1, min(60, int(configured_steps or 1)))
        if duration_ms <= 0:
            return configured
        cadence = int(math.ceil((float(duration_ms) / 1000.0) * 60.0)) + 1
        return max(configured, min(361, cadence))

    def _transition_progress(self, duration_ms: int, steps: int) -> Iterable[float]:
        """Yield wall-clock driven animation progress while dropping stale frames.

        A synchronous OBS request can occasionally take longer than one render
        interval. Replaying every missed frame afterwards creates visible bursts
        and pauses. Instead, jump directly to the newest frame that should have
        been visible at the current time while always emitting the exact final
        state.
        """
        if duration_ms <= 0 or steps <= 1:
            yield 1.0
            return

        total_seconds = max(0.000001, duration_ms / 1000.0)
        intervals = max(1, steps - 1)
        frame_seconds = total_seconds / intervals
        started = time.monotonic()
        frame = 1
        last_progress = 0.0

        while frame <= intervals:
            deadline = started + frame_seconds * frame
            remaining = deadline - time.monotonic()
            if remaining > 0:
                self._cooperative_sleep(remaining)

            elapsed = max(0.0, time.monotonic() - started)
            due_frame = min(
                intervals,
                max(frame, int(elapsed / frame_seconds)),
            )
            scheduled_progress = due_frame / intervals
            elapsed_progress = min(1.0, elapsed / total_seconds)
            progress = min(1.0, max(last_progress, scheduled_progress, elapsed_progress))
            yield progress
            last_progress = progress
            frame = due_frame + 1

    def _animate_opacity_batch(
        self,
        states: Mapping[str, tuple[float, float]],
        *,
        duration_ms: int,
        steps: int,
    ) -> None:
        if not states:
            return
        if duration_ms <= 0 or steps <= 1:
            for source, (_start, end) in states.items():
                self._set_source_opacity(source, end)
            return

        # Emit the exact starting opacity before the first timed frame. The
        # wall-clock timeline intentionally starts at the first interval (> 0),
        # so without this write a fade-in after reposition would jump directly
        # from 0 to 1/60 instead of having an explicit transparent boundary.
        for source, (start, _end) in states.items():
            self._yield_runtime()
            self._set_source_opacity(source, start)

        for t in self._transition_progress(duration_ms, steps):
            for source, (start, end) in states.items():
                self._yield_runtime()
                self._set_source_opacity(source, start + (end - start) * t)

    def _animate_fade_reposition(
        self,
        prepared_items: list[dict[str, Any]],
        *,
        duration_ms: int,
        steps: int,
        warnings: list[str],
    ) -> None:
        """Fade visible layout-owned items out, reposition, then fade them in.

        duration_ms is the duration of each fade phase. Geometry mutations are
        applied only while affected visible items are fully transparent.
        """
        fade_collection = self._fade_collection_context(probe=True)
        fade_out: dict[str, tuple[float, float]] = {}
        fade_in: dict[str, tuple[float, float]] = {}
        touched: set[str] = set()
        fallback: list[dict[str, Any]] = []

        for prepared in prepared_items:
            self._yield_runtime()
            source = prepared["source"]
            target_enabled = prepared["target_enabled"]
            current_enabled = prepared["current_enabled"]

            # Runtime-owned visibility must not be driven through a temporary
            # opacity transition by the layout engine.
            if target_enabled is None:
                fallback.append(prepared)
                continue
            if current_enabled is None:
                warnings.append(
                    f"Visibilité actuelle inconnue pour {source}; bascule directe utilisée."
                )
                fallback.append(prepared)
                continue

            current_visible = bool(current_enabled)
            target_visible = bool(target_enabled)
            needs_geometry = bool(
                prepared["transform_changed"] and prepared["target_transform"]
            )
            needs_visibility = bool(prepared["visibility_changed"])
            if not needs_geometry and not needs_visibility:
                continue

            if current_visible:
                touched.add(source)
                self._ensure_pending_fade(source, fade_collection)
                self._ensure_fade_filter(source, 1.0)
                fade_out[source] = (1.0, 0.0)
            if target_visible:
                touched.add(source)
                self._ensure_pending_fade(source, fade_collection)
                fade_in[source] = (0.0, 1.0)

        try:
            self._animate_opacity_batch(
                fade_out, duration_ms=duration_ms, steps=steps
            )

            for prepared in prepared_items:
                self._yield_runtime()
                target = prepared["target_transform"]
                if prepared["transform_changed"] and target:
                    self._set_transform(
                        prepared["container"], prepared["source"], target
                    )

                target_enabled = prepared["target_enabled"]
                current_enabled = prepared["current_enabled"]
                if target_enabled is None or current_enabled is None:
                    continue
                if bool(target_enabled) == bool(current_enabled):
                    continue
                if bool(target_enabled):
                    try:
                        self._set_source_opacity(prepared["source"], 0.0)
                        self._set_enabled(prepared["container"], prepared["source"], True)
                    except Exception as exc:
                        # The opacity mutation may have reached OBS even when its
                        # response was lost. Keep the pre-armed neutralization
                        # obligation, skip this source's fade-in, and degrade to a
                        # warning rather than aborting the whole layout transition.
                        warnings.append(
                            f"Fondu d'apparition incertain pour {prepared['source']}: {exc}"
                        )
                        fade_in.pop(prepared["source"], None)
                else:
                    self._set_enabled(prepared["container"], prepared["source"], False)

            self._animate_opacity_batch(
                fade_in, duration_ms=duration_ms, steps=steps
            )

            for prepared in fallback:
                target_enabled = prepared["target_enabled"]
                current_enabled = prepared["current_enabled"]
                if (
                    target_enabled is not None
                    and current_enabled is not None
                    and bool(target_enabled) != bool(current_enabled)
                ):
                    self._set_enabled(
                        prepared["container"],
                        prepared["source"],
                        bool(target_enabled),
                    )
        except Exception as exc:
            cleanup_warnings = self._neutralize_fade_sources(
                touched, collection=fade_collection
            )
            warnings.extend(cleanup_warnings)
            detail = ""
            if cleanup_warnings:
                detail = " · nettoyage fondu incomplet: " + "; ".join(cleanup_warnings)
            raise RuntimeError(f"Transition layout interrompue: {exc}{detail}") from exc
        else:
            warnings.extend(
                self._neutralize_fade_sources(touched, collection=fade_collection)
            )

    @staticmethod
    def _move_fade_opacity(mode: str, progress: float) -> float:
        """Opacity curve for a fast 15% edge fade around an invisible move."""
        t = max(0.0, min(1.0, float(progress)))
        edge = 0.15
        reveal = 1.0 - edge
        if mode == "through":
            if t <= edge:
                return 1.0 - (t / edge)
            if t < reveal:
                return 0.0
            return (t - reveal) / edge
        if mode == "in":
            if t < reveal:
                return 0.0
            return (t - reveal) / edge
        if mode == "out":
            if t <= edge:
                return 1.0 - (t / edge)
            return 0.0
        return 1.0

    def _animate_move_fade(
        self,
        prepared_items: list[dict[str, Any]],
        *,
        duration_ms: int,
        steps: int,
        warnings: list[str],
    ) -> None:
        """Move continuously while fading according to visibility ownership.

        duration_ms is the total move duration. For items visible before and
        after the layout change, opacity fades 100% -> 0% during the first 15%,
        stays fully transparent through the middle 70%, then fades 0% -> 100%
        during the final 15%. Geometry keeps moving continuously at ~60 Hz.
        """
        fade_collection = self._fade_collection_context(probe=True)
        touched_fades: set[str] = set()
        fade_modes: dict[int, str] = {}
        fallback_visibility: set[int] = set()

        for index, prepared in enumerate(prepared_items):
            self._yield_runtime()
            source = prepared["source"]
            target_enabled = prepared["target_enabled"]
            current_enabled = prepared["current_enabled"]

            # Layout geometry may still move when visibility belongs to another
            # subsystem, but this transition must not take temporary ownership
            # of that source opacity.
            if target_enabled is None:
                continue
            if current_enabled is None:
                warnings.append(
                    f"Visibilité actuelle inconnue pour {source}; fondu ignoré."
                )
                fallback_visibility.add(index)
                if bool(target_enabled):
                    self._set_enabled(
                        prepared["container"], prepared["source"], True
                    )
                continue

            current_visible = bool(current_enabled)
            target_visible = bool(target_enabled)
            if not current_visible and not target_visible:
                continue

            touched_fades.add(source)
            self._ensure_pending_fade(source, fade_collection)
            try:
                if current_visible:
                    self._ensure_fade_filter(source, 1.0)
                else:
                    self._set_source_opacity(source, 0.0)
                    self._set_enabled(
                        prepared["container"], prepared["source"], True
                    )
            except Exception as exc:
                warnings.append(f"Fondu indisponible pour {source}: {exc}")
                fallback_visibility.add(index)
                touched_fades.discard(source)
                if target_visible:
                    self._set_enabled(
                        prepared["container"], prepared["source"], True
                    )
                continue

            if current_visible and target_visible:
                fade_modes[index] = "through"
            elif target_visible:
                fade_modes[index] = "in"
            else:
                fade_modes[index] = "out"

        total_duration_ms = max(1, duration_ms)
        move_steps = self._effective_transition_steps(total_duration_ms, steps)
        # The fade only changes during the first/last 15%. Using the same ~60 Hz
        # cadence as geometry gives a smooth short fade without doubling traffic
        # during the transparent middle section because unchanged opacity is not
        # re-sent.
        opacity_points = self._effective_transition_steps(total_duration_ms, steps)
        opacity_interval = 1.0 / max(1, opacity_points - 1)
        next_opacity_progress = opacity_interval
        last_opacity: dict[int, float] = {}

        try:
            for t in self._transition_progress(total_duration_ms, move_steps):
                for prepared in prepared_items:
                    self._yield_runtime()
                    if not prepared["transform_changed"]:
                        continue
                    target = prepared["target_transform"]
                    current = prepared["current_transform"]
                    if not target:
                        continue
                    keys = [
                        key
                        for key in target
                        if isinstance(target.get(key), (int, float))
                    ]
                    update = {
                        key: _float(current.get(key), _float(target.get(key)))
                        + (
                            _float(target.get(key))
                            - _float(current.get(key), _float(target.get(key)))
                        )
                        * t
                        for key in keys
                    }
                    if update:
                        self._set_transform(
                            prepared["container"], prepared["source"], update
                        )

                opacity_due = t + 1e-9 >= next_opacity_progress or t >= 1.0
                if opacity_due or abs(t - 0.5) <= 1e-9:
                    for index, mode in fade_modes.items():
                        opacity = self._move_fade_opacity(mode, t)
                        previous = last_opacity.get(index)
                        if previous is None or abs(previous - opacity) > 1e-6:
                            source = prepared_items[index]["source"]
                            self._set_source_opacity(source, opacity)
                            last_opacity[index] = opacity
                    while next_opacity_progress <= t + 1e-9:
                        next_opacity_progress += opacity_interval

            # Force exact final transforms even if stale-frame dropping skipped
            # the nominal final geometry update.
            for prepared in prepared_items:
                target = prepared["target_transform"]
                if prepared["transform_changed"] and target:
                    self._set_transform(
                        prepared["container"], prepared["source"], target
                    )

            for index, prepared in enumerate(prepared_items):
                target_enabled = prepared["target_enabled"]
                current_enabled = prepared["current_enabled"]
                if target_enabled is None or current_enabled is None:
                    continue
                if index in fade_modes:
                    if bool(target_enabled):
                        self._set_source_opacity(prepared["source"], 1.0)
                    else:
                        self._set_enabled(
                            prepared["container"], prepared["source"], False
                        )
                        self._set_source_opacity(prepared["source"], 1.0)
                elif index in fallback_visibility:
                    if not bool(target_enabled):
                        self._set_enabled(
                            prepared["container"], prepared["source"], False
                        )
                    elif bool(target_enabled) != bool(current_enabled):
                        self._set_enabled(
                            prepared["container"], prepared["source"], True
                        )
        except Exception as exc:
            cleanup_warnings = self._neutralize_fade_sources(
                touched_fades, collection=fade_collection
            )
            warnings.extend(cleanup_warnings)
            detail = ""
            if cleanup_warnings:
                detail = " · nettoyage fondu incomplet: " + "; ".join(cleanup_warnings)
            raise RuntimeError(f"Transition layout interrompue: {exc}{detail}") from exc
        else:
            warnings.extend(
                self._neutralize_fade_sources(
                    touched_fades, collection=fade_collection
                )
            )

    def _animate_layout_transition(
        self,
        prepared_items: list[dict[str, Any]],
        *,
        mode: str,
        duration_ms: int,
        steps: int,
        warnings: list[str],
    ) -> None:
        """Animate one layout on a single global timeline with bounded fade cleanup."""
        if mode == "fade":
            self._animate_fade_reposition(
                prepared_items,
                duration_ms=duration_ms,
                steps=steps,
                warnings=warnings,
            )
            return
        if mode == "move_fade":
            self._animate_move_fade(
                prepared_items,
                duration_ms=duration_ms,
                steps=steps,
                warnings=warnings,
            )
            return

        move = mode == "move"
        fade = False
        # Bind all temporary fade obligations in this transition to the Scene
        # Collection observed immediately before any fade mutation.
        fade_collection = self._fade_collection_context(probe=True) if fade else ""
        fade_state: dict[int, tuple[float, float]] = {}
        fallback_visibility: set[int] = set()
        touched_fades: set[str] = set()

        for index, prepared in enumerate(prepared_items):
            self._yield_runtime()
            target_enabled = prepared["target_enabled"]
            current_enabled = prepared["current_enabled"]
            source = prepared["source"]
            container = prepared["container"]
            if target_enabled is None or not prepared["visibility_changed"]:
                continue
            if current_enabled is None:
                warnings.append(
                    f"Visibilité actuelle inconnue pour {source}; bascule directe utilisée."
                )
                fallback_visibility.add(index)
                if bool(target_enabled):
                    self._set_enabled(container, source, True)
                continue

            if fade:
                # A filter mutation can succeed in OBS even when the response is
                # lost. Arm neutralization before the first fade I/O so every
                # uncertain temporary filter state has durable recovery work.
                touched_fades.add(source)
                self._ensure_pending_fade(source, fade_collection)
                try:
                    if bool(target_enabled):
                        self._set_source_opacity(source, 0.0)
                        self._set_enabled(container, source, True)
                        fade_state[index] = (0.0, 1.0)
                    else:
                        self._ensure_fade_filter(source, 1.0)
                        fade_state[index] = (1.0, 0.0)
                except Exception as exc:
                    warnings.append(f"Fondu indisponible pour {source}: {exc}")
                    fallback_visibility.add(index)
                    if bool(target_enabled):
                        self._set_enabled(container, source, True)
            elif move and bool(target_enabled):
                self._set_enabled(container, source, True)

        try:
            for t in self._transition_progress(duration_ms, steps):
                for index, prepared in enumerate(prepared_items):
                    self._yield_runtime()
                    if move and prepared["transform_changed"]:
                        target = prepared["target_transform"]
                        current = prepared["current_transform"]
                        if target:
                            keys = [
                                key
                                for key in target
                                if isinstance(target.get(key), (int, float))
                            ]
                            update = {
                                key: _float(current.get(key), _float(target.get(key)))
                                + (
                                    _float(target.get(key))
                                    - _float(current.get(key), _float(target.get(key)))
                                )
                                * t
                                for key in keys
                            }
                            if update:
                                self._set_transform(
                                    prepared["container"],
                                    prepared["source"],
                                    update,
                                )

                    opacity = fade_state.get(index)
                    if opacity is not None:
                        start_opacity, end_opacity = opacity
                        self._set_source_opacity(
                            prepared["source"],
                            start_opacity + (end_opacity - start_opacity) * t,
                        )

            for index, prepared in enumerate(prepared_items):
                target_enabled = prepared["target_enabled"]
                current_enabled = prepared["current_enabled"]
                if (
                    not move
                    and prepared["target_transform"]
                    and prepared["transform_changed"]
                ):
                    self._set_transform(
                        prepared["container"],
                        prepared["source"],
                        prepared["target_transform"],
                    )
                if target_enabled is None:
                    continue
                if current_enabled is not None and bool(target_enabled) == current_enabled:
                    continue

                if index in fade_state:
                    if not bool(target_enabled):
                        self._set_enabled(prepared["container"], prepared["source"], False)
                        self._set_source_opacity(prepared["source"], 1.0)
                elif index in fallback_visibility or move:
                    if not bool(target_enabled):
                        self._set_enabled(prepared["container"], prepared["source"], False)
                    elif index in fallback_visibility:
                        self._set_enabled(prepared["container"], prepared["source"], True)
        except Exception as exc:
            cleanup_warnings = self._neutralize_fade_sources(touched_fades, collection=fade_collection)
            warnings.extend(cleanup_warnings)
            detail = ""
            if cleanup_warnings:
                detail = " · nettoyage fondu incomplet: " + "; ".join(cleanup_warnings)
            raise RuntimeError(f"Transition layout interrompue: {exc}{detail}") from exc
        else:
            cleanup_warnings = self._neutralize_fade_sources(touched_fades, collection=fade_collection)
            warnings.extend(cleanup_warnings)

    def _wait_group_resize_settle(self) -> None:
        """Allow OBS to finish automatic group-bound recomputation.

        libobs exposes obs_sceneitem_defer_group_resize_begin/end for native
        callers. obs-websocket does not expose those calls, so SSR waits a very
        short interval between child updates and the final group transform.
        """
        self._cooperative_sleep(GROUP_RESIZE_SETTLE_SECONDS)

    def _build_desired_elements(self, profile: Mapping[str, Any]) -> list[dict[str, Any]]:
        scene = str(profile.get("scene") or "").strip()
        modules = profile.get("modules")
        if not scene or not isinstance(modules, Mapping):
            return []
        current_canvas = self.canvas_size()
        group_sources = self._profile_group_sources(profile)
        desired: list[dict[str, Any]] = []
        for module_name, raw_module in modules.items():
            if not isinstance(raw_module, Mapping) or not bool(raw_module.get("managed", True)):
                continue
            if bool(raw_module.get("locked", False)):
                continue
            base = raw_module.get("base_bounds")
            geometry = self._target_geometry(profile, raw_module, current_canvas)
            elements = raw_module.get("elements")
            if not isinstance(base, Mapping) or not isinstance(geometry, Mapping) or not isinstance(elements, list):
                continue
            base_x = _float(base.get("x"))
            base_y = _float(base.get("y"))
            base_w = max(0.0001, abs(_float(base.get("width"), 1.0)))
            base_h = max(0.0001, abs(_float(base.get("height"), 1.0)))
            target_x = _float(geometry.get("x"), base_x)
            target_y = _float(geometry.get("y"), base_y)
            target_w = max(0.0001, abs(_float(geometry.get("width"), base_w)))
            target_h = max(0.0001, abs(_float(geometry.get("height"), base_h)))
            scale_x = target_w / base_w
            scale_y = target_h / base_h
            visible = bool(raw_module.get("visible", True))

            for element in elements:
                if not isinstance(element, Mapping) or not bool(element.get("included", True)):
                    continue
                source = str(element.get("source") or "").strip()
                transform = element.get("transform")
                if not source or not isinstance(transform, Mapping):
                    continue
                if bool(element.get("locked", False)):
                    continue
                container = str(element.get("container") or raw_module.get("container") or scene)
                position_x = _float(transform.get("positionX"))
                position_y = _float(transform.get("positionY"))
                update: dict[str, Any] = {}
                if bool(element.get("follow_position", True)):
                    update["positionX"] = target_x + (position_x - base_x) * scale_x
                    update["positionY"] = target_y + (position_y - base_y) * scale_y
                if bool(element.get("follow_size", True)):
                    bounds_type = str(transform.get("boundsType") or "OBS_BOUNDS_NONE")
                    if bounds_type and bounds_type != "OBS_BOUNDS_NONE":
                        if "boundsWidth" in transform:
                            update["boundsWidth"] = max(1.0, _float(transform.get("boundsWidth")) * scale_x)
                        if "boundsHeight" in transform:
                            update["boundsHeight"] = max(1.0, _float(transform.get("boundsHeight")) * scale_y)
                    else:
                        # Store the desired *rendered* size, not only the captured
                        # OBS scale. Scene sources inherit the OBS base-canvas
                        # dimensions as their intrinsic source size. When the base
                        # canvas changes (e.g. 1080p -> 1440p), their sourceWidth /
                        # sourceHeight therefore change even though scaleX/scaleY
                        # may stay untouched. Reapplying the old raw scale would
                        # make nested scene sources grow and overflow their parent.
                        # The runtime resolver below compensates against the
                        # current intrinsic size so the requested visual size is
                        # preserved in the module's coordinate space.
                        captured_width = abs(_float(transform.get("width")))
                        captured_height = abs(_float(transform.get("height")))
                        captured_scale_x = _float(transform.get("scaleX"), 1.0)
                        captured_scale_y = _float(transform.get("scaleY"), 1.0)
                        if captured_width > 0.0001:
                            update["__ssr_visual_width"] = captured_width * scale_x
                            update["__ssr_scale_sign_x"] = -1.0 if captured_scale_x < 0 else 1.0
                            update["__ssr_fallback_scale_x"] = captured_scale_x * scale_x
                        else:
                            update["scaleX"] = captured_scale_x * scale_x
                        if captured_height > 0.0001:
                            update["__ssr_visual_height"] = captured_height * scale_y
                            update["__ssr_scale_sign_y"] = -1.0 if captured_scale_y < 0 else 1.0
                            update["__ssr_fallback_scale_y"] = captured_scale_y * scale_y
                        else:
                            update["scaleY"] = captured_scale_y * scale_y
                enabled: bool | None = None
                runtime_visibility = self.runtime_visibility_owned(container, source, element)
                if (
                    not runtime_visibility
                    and bool(element.get("follow_visibility", True))
                ):
                    enabled = visible and bool(element.get("enabled", True))
                desired.append(
                    {
                        "module": str(module_name),
                        "element": str(element.get("element") or source),
                        "source": source,
                        "container": container,
                        "container_kind": str(element.get("container_kind") or raw_module.get("coordinate_space") or "scene"),
                        "path": list(element.get("path") or [scene]),
                        "source_type": (
                            "group"
                            if source in group_sources
                            else str(element.get("source_type") or "input")
                        ),
                        "transform": update,
                        "enabled": enabled,
                    }
                )
        return desired

    @staticmethod
    def _resolve_runtime_transform(
        target: Mapping[str, Any], current: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Resolve visual-size targets against the source's current intrinsic size.

        OBS scene sources change their intrinsic dimensions when the base canvas
        changes. Using a captured raw scale in that situation changes the visible
        size of nested scenes. SSR stores the desired rendered width/height and
        derives a fresh scale from the current transform at apply time.
        """
        resolved = {
            key: value
            for key, value in target.items()
            if not str(key).startswith("__ssr_")
        }

        def axis(
            *,
            visual_key: str,
            sign_key: str,
            scale_key: str,
            size_key: str,
            source_size_key: str,
        ) -> None:
            desired_visual = abs(_float(target.get(visual_key)))
            if desired_visual <= 0.0001:
                return

            current_scale = abs(_float(current.get(scale_key), 0.0))
            current_visual = abs(_float(current.get(size_key), 0.0))
            intrinsic = 0.0
            if current_scale > 0.000001 and current_visual > 0.000001:
                intrinsic = current_visual / current_scale
            if intrinsic <= 0.000001:
                intrinsic = abs(_float(current.get(source_size_key), 0.0))
            if intrinsic <= 0.000001:
                # Last-resort fallback for unusual sources that do not expose
                # width/sourceWidth in GetSceneItemTransform.
                fallback_key = (
                    "__ssr_fallback_scale_x"
                    if scale_key == "scaleX"
                    else "__ssr_fallback_scale_y"
                )
                resolved[scale_key] = _float(target.get(fallback_key), 1.0)
                return

            sign = -1.0 if _float(target.get(sign_key), 1.0) < 0 else 1.0
            resolved[scale_key] = sign * desired_visual / intrinsic

        axis(
            visual_key="__ssr_visual_width",
            sign_key="__ssr_scale_sign_x",
            scale_key="scaleX",
            size_key="width",
            source_size_key="sourceWidth",
        )
        axis(
            visual_key="__ssr_visual_height",
            sign_key="__ssr_scale_sign_y",
            scale_key="scaleY",
            size_key="height",
            source_size_key="sourceHeight",
        )
        return resolved

    def _target_geometry(
        self,
        profile: Mapping[str, Any],
        module: Mapping[str, Any],
        current_canvas: tuple[int, int] | None,
    ) -> Mapping[str, Any]:
        geometry = module.get("geometry") if isinstance(module.get("geometry"), Mapping) else {}

        # A LayoutProfile can recursively discover modules inside groups/scenes,
        # but their transforms are local to that container. They must not be
        # normalized against the root scene canvas. The parent scene item is the
        # object that scales when the OBS canvas changes; keeping descendants in
        # their captured local geometry preserves all relative sizes.
        coordinate_space = str(module.get("coordinate_space") or "").strip()
        if not coordinate_space:
            # Backward-compatible inference for schema-v4 profiles captured by
            # SSR 2.0.2 and earlier.
            scene = str(profile.get("scene") or "").strip()
            container = str(module.get("container") or scene).strip()
            container_kind = str(module.get("container_kind") or "").strip()
            if container_kind == "scene":
                coordinate_space = "root_canvas"
            elif container_kind == "group":
                coordinate_space = "container_local"
            else:
                # Legacy profiles did not store the container kind. A nested
                # OBS scene still uses the global base-canvas coordinate system.
                try:
                    scene_names = set(self.list_scenes()[0])
                except Exception:
                    scene_names = {scene}
                coordinate_space = "root_canvas" if container in scene_names else "container_local"
        if coordinate_space != "root_canvas":
            return geometry

        if not current_canvas:
            return geometry
        cw, ch = current_canvas
        mode = str(profile.get("coordinate_mode") or "normalized")
        normalized = module.get("normalized_geometry")
        if mode == "normalized" and isinstance(normalized, Mapping):
            return {
                "x": _float(normalized.get("x")) * cw,
                "y": _float(normalized.get("y")) * ch,
                "width": max(1.0, _float(normalized.get("width"), 0.001) * cw),
                "height": max(1.0, _float(normalized.get("height"), 0.001) * ch),
            }
        if mode == "normalized":
            captured_canvas = profile.get("canvas") if isinstance(profile.get("canvas"), Mapping) else {}
            pw = _float(captured_canvas.get("width"))
            ph = _float(captured_canvas.get("height"))
            if pw > 0 and ph > 0:
                return {
                    "x": _float(geometry.get("x")) * cw / pw,
                    "y": _float(geometry.get("y")) * ch / ph,
                    "width": max(1.0, _float(geometry.get("width"), 1.0) * cw / pw),
                    "height": max(1.0, _float(geometry.get("height"), 1.0) * ch / ph),
                }
        if str(module.get("anchor_mode") or "relative") == "pixel_margin":
            offsets = module.get("anchor_offsets")
            if isinstance(offsets, Mapping):
                ax, ay = anchor_factors(str(module.get("anchor") or "top_left"))
                width = _float(geometry.get("width"), 1.0)
                height = _float(geometry.get("height"), 1.0)
                anchor_x = cw * ax + _float(offsets.get("x"))
                anchor_y = ch * ay + _float(offsets.get("y"))
                return {
                    "x": anchor_x - width * ax,
                    "y": anchor_y - height * ay,
                    "width": width,
                    "height": height,
                }
        return geometry

    @staticmethod
    def _normalize_geometry(geometry: Mapping[str, Any], canvas: tuple[int, int]) -> dict[str, float]:
        width, height = canvas
        return {
            "x": _float(geometry.get("x")) / max(1, width),
            "y": _float(geometry.get("y")) / max(1, height),
            "width": _float(geometry.get("width"), 1.0) / max(1, width),
            "height": _float(geometry.get("height"), 1.0) / max(1, height),
        }

    @staticmethod
    def _anchor_offsets(
        geometry: Mapping[str, Any], canvas: tuple[int, int], anchor: str
    ) -> dict[str, float]:
        ax, ay = anchor_factors(anchor)
        width, height = canvas
        module_anchor_x = _float(geometry.get("x")) + _float(geometry.get("width")) * ax
        module_anchor_y = _float(geometry.get("y")) + _float(geometry.get("height")) * ay
        return {"x": module_anchor_x - width * ax, "y": module_anchor_y - height * ay}

    def _invalidate_scene_item_id(self, container: str, source: str) -> None:
        self._scene_item_cache.pop((container, source), None)

    def _get_current_item(self, container: str, source: str) -> dict[str, Any]:
        self._yield_runtime()
        # Scene-item ids are not a durable identifier for a LayoutProfile. OBS can
        # rebuild/renumber items after structural scene edits (for example when a
        # source is removed). A cached id that was valid during discovery must
        # therefore be treated as an optimization only. If OBS rejects it, evict
        # the cache entry, resolve the item again by (container, source name), and
        # retry once. This prevents one deleted source from making every other
        # cached item look missing.
        item_id = self._scene_item_id(container, source)
        try:
            transform_response = self.client.send(
                "GetSceneItemTransform", {"sceneName": container, "sceneItemId": item_id}
            )
        except Exception:
            self._yield_runtime()
            self._invalidate_scene_item_id(container, source)
            item_id = self._scene_item_id(container, source)
            transform_response = self.client.send(
                "GetSceneItemTransform", {"sceneName": container, "sceneItemId": item_id}
            )
        enabled = None
        enabled_error = ""
        self._yield_runtime()
        try:
            enabled_response = self.client.send(
                "GetSceneItemEnabled", {"sceneName": container, "sceneItemId": item_id}
            )
            if "sceneItemEnabled" in enabled_response:
                enabled = bool(enabled_response.get("sceneItemEnabled"))
            else:
                enabled_error = "GetSceneItemEnabled n'a pas renvoyé sceneItemEnabled"
        except Exception as exc:
            enabled_error = str(exc)
        return {
            "id": item_id,
            "transform": dict(transform_response.get("sceneItemTransform") or {}),
            "enabled": enabled,
            "enabled_error": enabled_error,
        }

    def _set_transform(self, container: str, source: str, transform: Mapping[str, Any]) -> None:
        self._yield_runtime()
        if not transform:
            return
        item_id = self._scene_item_id(container, source)
        payload = {
            "sceneName": container,
            "sceneItemId": item_id,
            "sceneItemTransform": dict(transform),
        }
        try:
            self.client.send("SetSceneItemTransform", payload)
        except Exception:
            self._yield_runtime()
            self._invalidate_scene_item_id(container, source)
            payload["sceneItemId"] = self._scene_item_id(container, source)
            self.client.send("SetSceneItemTransform", payload)

    def _set_enabled(self, container: str, source: str, enabled: bool) -> None:
        self._yield_runtime()
        item_id = self._scene_item_id(container, source)
        payload = {
            "sceneName": container,
            "sceneItemId": item_id,
            "sceneItemEnabled": bool(enabled),
        }
        try:
            self.client.send("SetSceneItemEnabled", payload)
        except Exception:
            self._yield_runtime()
            self._invalidate_scene_item_id(container, source)
            payload["sceneItemId"] = self._scene_item_id(container, source)
            self.client.send("SetSceneItemEnabled", payload)

    def _animate_transform(
        self,
        container: str,
        source: str,
        current: Mapping[str, Any],
        target: Mapping[str, Any],
        duration_ms: int,
        steps: int,
    ) -> None:
        keys = [key for key in target if isinstance(target.get(key), (int, float))]
        if not keys or duration_ms <= 0:
            self._set_transform(container, source, target)
            return
        delay = duration_ms / 1000.0 / steps
        for index in range(1, steps + 1):
            t = index / steps
            update = {
                key: _float(current.get(key), _float(target.get(key)))
                + (_float(target.get(key)) - _float(current.get(key), _float(target.get(key)))) * t
                for key in keys
            }
            self._set_transform(container, source, update)
            if index != steps:
                self._cooperative_sleep(delay)

    def _ensure_fade_filter(self, source: str, opacity: float) -> None:
        """Ensure the helper filter exists without recursively setting it.

        Filter creation is a bounded recovery path. Transport/protocol errors
        are not interpreted as absence and therefore propagate unchanged.
        """
        response = self.client.send("GetSourceFilterList", {"sourceName": source})
        filters = response.get("filters", []) or []
        found = any(
            isinstance(item, Mapping) and str(item.get("filterName") or "") == SSR_FADE_FILTER
            for item in filters
        )
        if not found:
            self.client.send(
                "CreateSourceFilter",
                {
                    "sourceName": source,
                    "filterName": SSR_FADE_FILTER,
                    "filterKind": SSR_FADE_FILTER_KIND,
                    "filterSettings": {
                        "opacity": max(0.0, min(1.0, float(opacity)))
                    },
                },
            )
            return
        self.client.send(
            "SetSourceFilterEnabled",
            {"sourceName": source, "filterName": SSR_FADE_FILTER, "filterEnabled": True},
        )

    def _set_source_opacity(self, source: str, opacity: float) -> None:
        payload = {
            "sourceName": source,
            "filterName": SSR_FADE_FILTER,
            "filterSettings": {"opacity": max(0.0, min(1.0, float(opacity)))},
            "overlay": True,
        }
        try:
            self.client.send("SetSourceFilterSettings", payload)
            return
        except OBSResourceNotFoundError:
            # Confirmed absence is the only error that may create/re-enable the
            # helper filter. Retry the settings write once, never recursively.
            self._ensure_fade_filter(source, opacity)
        self.client.send("SetSourceFilterSettings", payload)

    def _animate_opacity(self, source: str, start: float, end: float, duration_ms: int, steps: int) -> None:
        self._ensure_fade_filter(source, start)
        if duration_ms <= 0:
            self._set_source_opacity(source, end)
            return
        delay = duration_ms / 1000.0 / steps
        for index in range(1, steps + 1):
            value = start + (end - start) * (index / steps)
            self._set_source_opacity(source, value)
            if index != steps:
                self._cooperative_sleep(delay)

    def _fresh_scene_item_id(self, container: str, source: str) -> int:
        self._yield_runtime()
        self._invalidate_scene_item_id(container, source)
        response = self.client.send(
            "GetSceneItemId",
            {"sceneName": container, "sourceName": source},
        )
        item_id = _int(response.get("sceneItemId"))
        if not item_id:
            raise OBSResourceNotFoundError(
                "GetSceneItemId",
                f"Source '{source}' introuvable dans '{container}'",
            )
        self._scene_item_cache[(container, source)] = item_id
        return item_id

    def _scene_item_id(self, container: str, source: str) -> int:
        self._yield_runtime()
        key = (container, source)
        cached = self._scene_item_cache.get(key)
        if cached:
            return cached
        response = self.client.send(
            "GetSceneItemId",
            {"sceneName": container, "sourceName": source},
        )
        item_id = _int(response.get("sceneItemId"))
        if not item_id:
            raise RuntimeError(f"Source '{source}' introuvable dans '{container}'")
        self._scene_item_cache[key] = item_id
        return item_id
