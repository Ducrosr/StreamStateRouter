from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..importers.scene_collection import ImportedFilter, ImportedInput, ImportedSceneItem
from ..obs.catalog import OBSResourceCatalog, OBSResourceCatalogReader
from ..obs.client import OBSClientManager
from .config import (
    build_host_controller,
    build_obs_config,
    validate_config,
)
from .config_insights import (
    CapabilityReport,
    build_capability_report,
    configured_action_types,
    scan_obs_reference_repairs,
)


@dataclass(frozen=True, slots=True)
class SystemCheckReport:
    config_errors: tuple[str, ...]
    capabilities: CapabilityReport
    obs_requests: Mapping[str, object] = field(default_factory=dict)
    references: Mapping[str, object] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.config_errors or self.capabilities.status == "error":
            return "error"
        if self.capabilities.status == "warning":
            return "warning"
        return "ready"

    @property
    def ok(self) -> bool:
        return self.status != "error"

    def as_mapping(self) -> dict[str, object]:
        return {
            "status": self.status,
            "ok": self.ok,
            "config": {
                "valid": not self.config_errors,
                "errors": list(self.config_errors),
            },
            "obs_requests": dict(self.obs_requests),
            "references": dict(self.references),
            "capabilities": {
                "status": self.capabilities.status,
                "summary": self.capabilities.summary,
                "items": [
                    {
                        "key": item.key,
                        "label": item.label,
                        "status": item.status,
                        "status_label": item.status_label,
                        "detail": item.detail,
                        "action": item.action,
                    }
                    for item in self.capabilities.items
                ],
                "findings": [
                    {
                        "severity": finding.severity,
                        "title": finding.title,
                        "detail": finding.detail,
                        "action": finding.action,
                    }
                    for finding in self.capabilities.findings
                ],
            },
        }


class _ReadOnlyOBSClient:
    """Fail closed if a diagnostic reader ever attempts an OBS mutation."""

    def __init__(self, client: OBSClientManager):
        self._client = client

    @property
    def session_generation(self) -> int:
        return int(getattr(self._client, "session_generation", 0) or 0)

    def send(
        self,
        request: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        name = str(request or "").strip()
        if not name.startswith("Get"):
            raise RuntimeError(
                f"Contrôle système read-only : requête OBS refusée ({name or '<vide>'})"
            )
        return self._client.send(name, data)


@dataclass(frozen=True, slots=True)
class _ReferenceSnapshot:
    scenes: tuple[str, ...]
    groups: tuple[str, ...]
    inputs: tuple[ImportedInput, ...]
    filters: tuple[ImportedFilter, ...]
    scene_items: tuple[ImportedSceneItem, ...]
    unreadable_filter_sources: frozenset[str] = frozenset()


_OBS_ACTION_REQUESTS: dict[str, tuple[str, ...]] = {
    "set_program_scene": ("SetCurrentProgramScene",),
    "scene_item_enabled": ("GetSceneItemId", "SetSceneItemEnabled"),
    "source_filter_enabled": ("SetSourceFilterEnabled",),
    "source_filter_settings": ("SetSourceFilterSettings",),
    "input_mute": ("SetInputMute",),
    "input_volume_db": ("SetInputVolume",),
    "set_input_settings": ("SetInputSettings",),
}


def _probe_obs_catalog(
    client: OBSClientManager,
) -> tuple[dict[str, object], OBSResourceCatalog | None]:
    """Read the lightweight OBS catalog through a read-only fail-closed guard."""

    try:
        catalog = OBSResourceCatalogReader(_ReadOnlyOBSClient(client)).sync()
        partial_reasons = list(catalog.warnings)
        if catalog.unreadable_containers:
            partial_reasons.append(
                "conteneur(s) illisible(s) : "
                + ", ".join(sorted(catalog.unreadable_containers, key=str.casefold))
            )
        return (
            {
                "available": True,
                "stale": False,
                "partial": bool(partial_reasons),
                "partial_reason": "; ".join(partial_reasons),
                "collection": catalog.collection,
                "session_generation": catalog.session_generation,
                "scenes": len(catalog.scenes),
                "groups": len(catalog.groups),
                "inputs": len(catalog.inputs),
                "scene_items": len(catalog.scene_items),
                "transitions": len(catalog.transitions),
                "available_requests": len(catalog.available_requests),
            },
            catalog,
        )
    except Exception as exc:
        return (
            {
                "available": True,
                "stale": True,
                "stale_reason": str(exc),
            },
            None,
        )


def _add_request_owner(
    owners: dict[str, set[str]],
    request: str,
    owner: str,
) -> None:
    owners.setdefault(str(request), set()).add(str(owner))


def _configured_obs_request_requirements(
    config: Mapping[str, Any],
) -> dict[str, tuple[str, ...]]:
    """Map configured actions and OBS-dependent conditions to requests."""

    owners: dict[str, set[str]] = {}
    profiles = config.get("profiles")
    if isinstance(profiles, Mapping):
        for domain, domain_profiles in profiles.items():
            if not isinstance(domain_profiles, Mapping):
                continue
            for profile_name, profile in domain_profiles.items():
                if not isinstance(profile, Mapping):
                    continue
                actions = profile.get("actions")
                if isinstance(actions, list):
                    for action in actions:
                        if (
                            not isinstance(action, Mapping)
                            or not bool(action.get("enabled", True))
                        ):
                            continue
                        kind = str(action.get("type") or "").strip().casefold()
                        for request in _OBS_ACTION_REQUESTS.get(kind, ()):
                            _add_request_owner(
                                owners,
                                request,
                                f"{domain}/{profile_name} · {kind}",
                            )
                conditions = profile.get("conditions")
                if isinstance(conditions, Mapping):
                    if "streaming" in conditions:
                        _add_request_owner(
                            owners,
                            "GetStreamStatus",
                            f"{domain}/{profile_name} · condition streaming",
                        )
                    if "recording" in conditions:
                        _add_request_owner(
                            owners,
                            "GetRecordStatus",
                            f"{domain}/{profile_name} · condition recording",
                        )
                    if str(conditions.get("program_scene") or "").strip():
                        _add_request_owner(
                            owners,
                            "GetCurrentProgramScene",
                            f"{domain}/{profile_name} · condition program_scene",
                        )

    rules = config.get("rules")
    if isinstance(rules, list):
        for rule in rules:
            if (
                not isinstance(rule, Mapping)
                or not bool(rule.get("enabled", True))
            ):
                continue
            conditions = rule.get("conditions")
            if not isinstance(conditions, Mapping):
                continue
            label = str(rule.get("name") or "Règle")
            if "streaming" in conditions:
                _add_request_owner(
                    owners,
                    "GetStreamStatus",
                    f"règle {label} · streaming",
                )
            if "recording" in conditions:
                _add_request_owner(
                    owners,
                    "GetRecordStatus",
                    f"règle {label} · recording",
                )
            if str(conditions.get("program_scene") or "").strip():
                _add_request_owner(
                    owners,
                    "GetCurrentProgramScene",
                    f"règle {label} · program_scene",
                )

    layouts = config.get("layout_profiles")
    if isinstance(layouts, Mapping):
        for name, profile in layouts.items():
            if not isinstance(profile, Mapping):
                continue
            conditions = profile.get("conditions")
            if not isinstance(conditions, Mapping):
                continue
            if "streaming" in conditions:
                _add_request_owner(
                    owners,
                    "GetStreamStatus",
                    f"layout {name} · condition streaming",
                )
            if "recording" in conditions:
                _add_request_owner(
                    owners,
                    "GetRecordStatus",
                    f"layout {name} · condition recording",
                )
            if str(conditions.get("program_scene") or "").strip():
                _add_request_owner(
                    owners,
                    "GetCurrentProgramScene",
                    f"layout {name} · condition program_scene",
                )

    return {
        request: tuple(sorted(values, key=str.casefold))
        for request, values in sorted(owners.items())
    }


def probe_obs_request_capabilities(
    config: Mapping[str, Any],
    catalog: OBSResourceCatalog | None,
) -> dict[str, object]:
    requirements = _configured_obs_request_requirements(config)
    rows = [
        {"request": request, "owners": list(owners)}
        for request, owners in requirements.items()
    ]
    if not requirements:
        return {
            "status": "unused",
            "detail": (
                "Aucune action/condition configurée ne requiert "
                "d’appel OBS supplémentaire."
            ),
            "required": rows,
            "missing": [],
        }
    if catalog is None:
        return {
            "status": "warning",
            "detail": (
                "Compatibilité des requêtes non vérifiable "
                "sans catalogue OBS fiable."
            ),
            "required": rows,
            "missing": [],
        }

    available = set(catalog.available_requests)
    if not available:
        return {
            "status": "warning",
            "detail": (
                "OBS n’a pas fourni availableRequests ; "
                "la compatibilité d’exécution ne peut pas être prouvée."
            ),
            "required": rows,
            "missing": [],
        }

    missing = [
        {"request": request, "owners": list(requirements[request])}
        for request in requirements
        if request not in available
    ]
    if missing:
        names = ", ".join(str(item["request"]) for item in missing)
        return {
            "status": "error",
            "detail": (
                f"{len(missing)} requête(s) requise(s) absente(s) : {names}."
            ),
            "action": (
                "Vérifiez la version d’OBS/obs-websocket ou retirez "
                "la fonctionnalité qui dépend de ces requêtes."
            ),
            "required": rows,
            "missing": missing,
        }
    return {
        "status": "ready",
        "detail": (
            f"{len(requirements)} requête(s) d’exécution configurée(s), "
            "toutes annoncées par OBS."
        ),
        "required": rows,
        "missing": [],
    }

def _configured_filter_sources(
    config: Mapping[str, Any],
) -> tuple[str, ...]:
    sources: set[str] = set()
    profiles = config.get("profiles")
    if not isinstance(profiles, Mapping):
        return ()
    for domain_profiles in profiles.values():
        if not isinstance(domain_profiles, Mapping):
            continue
        for profile in domain_profiles.values():
            if not isinstance(profile, Mapping):
                continue
            actions = profile.get("actions")
            if not isinstance(actions, list):
                continue
            for action in actions:
                if (
                    not isinstance(action, Mapping)
                    or not bool(action.get("enabled", True))
                ):
                    continue
                kind = str(action.get("type") or "").strip().casefold()
                if kind not in {
                    "source_filter_enabled",
                    "source_filter_settings",
                }:
                    continue
                params = action.get("params")
                if not isinstance(params, Mapping):
                    continue
                source = str(params.get("source") or "").strip()
                if source and not source.startswith("$"):
                    sources.add(source)
    return tuple(sorted(sources, key=str.casefold))


def _reference_snapshot(
    config: Mapping[str, Any],
    client: OBSClientManager,
    catalog: OBSResourceCatalog,
) -> _ReferenceSnapshot:
    reader = OBSResourceCatalogReader(_ReadOnlyOBSClient(client))
    filters: list[ImportedFilter] = []
    unreadable: set[str] = set()
    for source in _configured_filter_sources(config):
        try:
            refs = reader.filters_for_source(source)
        except Exception:
            unreadable.add(source)
            continue
        filters.extend(
            ImportedFilter(
                source=source,
                name=ref.name,
                kind=ref.kind,
                enabled=ref.enabled,
                settings={},
            )
            for ref in refs
        )

    return _ReferenceSnapshot(
        scenes=tuple(scene.name for scene in catalog.scenes),
        groups=tuple(catalog.groups),
        inputs=tuple(
            ImportedInput(
                name=item.name,
                kind=item.kind,
                uuid=item.uuid,
                settings={},
                muted=None,
                volume_db=None,
            )
            for item in catalog.inputs
        ),
        filters=tuple(filters),
        scene_items=tuple(
            ImportedSceneItem(
                scene=item.container,
                source=item.source,
                occurrence=item.occurrence,
                enabled=item.enabled,
            )
            for item in catalog.scene_items
        ),
        unreadable_filter_sources=frozenset(unreadable),
    )


def probe_obs_references(
    config: Mapping[str, Any],
    client: OBSClientManager,
    catalog: OBSResourceCatalog | None,
) -> dict[str, object]:
    if catalog is None:
        return {
            "status": "warning",
            "detail": "Références OBS non vérifiables sans catalogue fiable.",
            "issues": [],
        }

    snapshot = _reference_snapshot(config, client, catalog)
    issues = scan_obs_reference_repairs(config, snapshot)
    rows = [
        {
            "kind": issue.kind,
            "location": issue.location,
            "current": issue.current,
            "candidate": issue.candidate,
            "confidence": issue.confidence,
            "reason": issue.reason,
        }
        for issue in issues
    ]
    incomplete = bool(
        catalog.warnings
        or catalog.unreadable_containers
        or snapshot.unreadable_filter_sources
    )

    if issues:
        examples = "; ".join(
            (
                f"{issue.kind} {issue.location}: {issue.current!r}"
                + (
                    f" → {issue.candidate!r}"
                    if issue.candidate
                    else ""
                )
            )
            for issue in issues[:3]
        )
        return {
            "status": "warning" if incomplete else "error",
            "detail": (
                f"{len(issues)} référence(s) OBS introuvable(s). {examples}"
                + (" · inventaire partiel" if incomplete else "")
            ),
            "action": (
                "Corrigez ou revalidez ces références dans la collection OBS "
                "avant d’exécuter les profils concernés."
            ),
            "issues": rows,
            "incomplete": incomplete,
            "unreadable_filter_sources": sorted(
                snapshot.unreadable_filter_sources,
                key=str.casefold,
            ),
        }

    if incomplete:
        details: list[str] = []
        if snapshot.unreadable_filter_sources:
            details.append(
                "filtres non lisibles pour "
                + ", ".join(
                    sorted(
                        snapshot.unreadable_filter_sources,
                        key=str.casefold,
                    )
                )
            )
        if catalog.unreadable_containers:
            details.append(
                "conteneurs non lisibles : "
                + ", ".join(
                    sorted(catalog.unreadable_containers, key=str.casefold)
                )
            )
        suffix = ": " + "; ".join(details) if details else "."
        return {
            "status": "warning",
            "detail": (
                "Aucune référence cassée prouvée, mais le lint est incomplet"
                + suffix
            ),
            "issues": [],
            "incomplete": True,
            "unreadable_filter_sources": sorted(
                snapshot.unreadable_filter_sources,
                key=str.casefold,
            ),
        }

    return {
        "status": "ready",
        "detail": "Les références OBS statiques configurées sont présentes.",
        "issues": [],
        "incomplete": False,
        "unreadable_filter_sources": [],
    }


def _configured_hdr_scope(config: Mapping[str, Any]) -> str:
    """Return the broadest HDR scope requested by configured HDR actions."""

    profiles = config.get("profiles")
    if not isinstance(profiles, Mapping):
        return "primary"
    for domain_profiles in profiles.values():
        if not isinstance(domain_profiles, Mapping):
            continue
        for profile in domain_profiles.values():
            if not isinstance(profile, Mapping):
                continue
            actions = profile.get("actions")
            if not isinstance(actions, list):
                continue
            for action in actions:
                if not isinstance(action, Mapping):
                    continue
                if (
                    str(action.get("type") or "").strip().casefold()
                    != "windows_hdr"
                ):
                    continue
                params = action.get("params")
                if (
                    isinstance(params, Mapping)
                    and str(params.get("display") or "primary")
                    .strip()
                    .casefold()
                    == "all"
                ):
                    return "all"
    return "primary"


def probe_host_capabilities(
    config: Mapping[str, Any],
    *,
    controller_factory: Callable[..., object] = build_host_controller,
) -> tuple[dict[str, object], dict[str, object]]:
    """Probe only host capabilities that are actually referenced by config.

    The probes are deliberately read-only:
    - audio resolves the SoundVolumeView executable but does not run it;
    - HDR reads DisplayConfig status but never changes Advanced Color/HDR.
    """

    action_types = configured_action_types(config)
    audio_used = "app_audio_output" in action_types
    hdr_used = "windows_hdr" in action_types

    if not audio_used and not hdr_used:
        return {}, {}

    try:
        controller = controller_factory(config)
    except Exception as exc:
        detail = str(exc)
        audio_probe = (
            {
                "status": "error",
                "detail": detail,
                "action": (
                    "Configurez SoundVolumeView dans Paramètres > "
                    "Contrôle Windows."
                ),
            }
            if audio_used
            else {}
        )
        hdr_probe = (
            {
                "status": "error",
                "detail": detail,
                "action": (
                    "Vérifiez la prise en charge HDR de Windows et de "
                    "l’écran ciblé."
                ),
            }
            if hdr_used
            else {}
        )
        return audio_probe, hdr_probe

    audio_probe: dict[str, object] = {}
    if audio_used:
        try:
            executable = controller.audio_router.resolve_executable()
            audio_probe = {
                "status": "ready",
                "detail": f"SoundVolumeView disponible : {executable}",
            }
        except Exception as exc:
            audio_probe = {
                "status": "error",
                "detail": str(exc),
                "action": (
                    "Configurez SoundVolumeView dans Paramètres > "
                    "Contrôle Windows."
                ),
            }

    hdr_probe: dict[str, object] = {}
    if hdr_used:
        try:
            scope = _configured_hdr_scope(config)
            rows = controller.hdr_controller.status(scope=scope)
            supported = [
                row for row in rows
                if bool(row.get("supported", False))
            ]
            if supported:
                enabled = any(
                    bool(row.get("enabled", False))
                    for row in supported
                )
                hdr_probe = {
                    "status": "ready",
                    "detail": (
                        "HDR pris en charge sur l’écran principal · "
                        + (
                            "actuellement activé"
                            if enabled
                            else "actuellement désactivé"
                        )
                    ),
                }
            else:
                hdr_probe = {
                    "status": "error",
                    "detail": (
                        "Aucun écran principal compatible HDR n’a été "
                        "détecté par l’API Windows."
                    ),
                }
        except Exception as exc:
            hdr_probe = {
                "status": "error",
                "detail": str(exc),
                "action": (
                    "Vérifiez la prise en charge HDR de Windows et de "
                    "l’écran ciblé."
                ),
            }

    return audio_probe, hdr_probe


def run_system_check(
    config: Mapping[str, Any],
    *,
    obs_client_factory: Callable[..., OBSClientManager] = OBSClientManager,
    host_controller_factory: Callable[..., object] = build_host_controller,
) -> SystemCheckReport:
    """Run the read-only configuration/OBS/Windows readiness check."""

    config_errors = tuple(validate_config(config))
    action_types = configured_action_types(config)

    obs_enabled = False
    obs_connected = False
    obs_error = ""
    catalog_status: dict[str, object] = {
        "available": False,
        "stale": False,
    }
    catalog: OBSResourceCatalog | None = None
    obs_request_probe: dict[str, object] = {}
    reference_probe: dict[str, object] = {}

    client = None
    try:
        obs_config = build_obs_config(config)
        obs_enabled = bool(obs_config.enabled)
        if obs_enabled:
            client = obs_client_factory(obs_config)
            obs_connected, message = client.probe()
            if obs_connected:
                catalog_status, catalog = _probe_obs_catalog(client)
                obs_request_probe = probe_obs_request_capabilities(
                    config,
                    catalog,
                )
                reference_probe = probe_obs_references(
                    config,
                    client,
                    catalog,
                )
            else:
                obs_error = str(message or "Connexion OBS indisponible.")
    except Exception as exc:
        obs_error = str(exc)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    if (
        "app_audio_output" in action_types
        or "windows_hdr" in action_types
    ):
        audio_probe, hdr_probe = probe_host_capabilities(
            config,
            controller_factory=host_controller_factory,
        )
    else:
        audio_probe, hdr_probe = {}, {}

    capabilities = build_capability_report(
        config,
        obs_enabled=obs_enabled,
        obs_connected=obs_connected,
        obs_error=obs_error,
        catalog_status=catalog_status,
        obs_request_probe=obs_request_probe,
        reference_probe=reference_probe,
        audio_probe=audio_probe,
        hdr_probe=hdr_probe,
    )
    return SystemCheckReport(
        config_errors=config_errors,
        capabilities=capabilities,
        obs_requests=obs_request_probe,
        references=reference_probe,
    )


def render_system_check_report(report: SystemCheckReport) -> str:
    lines = [
        "Stream State Router — contrôle système (lecture seule)",
        "",
    ]

    if report.config_errors:
        lines.append(
            f"[ERREUR] Configuration · {len(report.config_errors)} erreur(s)"
        )
        lines.extend(f"  - {error}" for error in report.config_errors)
    else:
        lines.append("[OK] Configuration · valide")

    labels = {
        "ready": "OK",
        "warning": "ATTENTION",
        "error": "ERREUR",
        "disabled": "DÉSACTIVÉ",
        "unused": "NON UTILISÉ",
        "unknown": "INCONNU",
    }

    for item in report.capabilities.items:
        label = labels.get(item.status, item.status.upper())
        detail = f" · {item.detail}" if item.detail else ""
        lines.append(f"[{label}] {item.label}{detail}")
        if item.action:
            lines.append(f"  Action : {item.action}")

    if report.capabilities.findings:
        lines.append("")
        lines.append("Santé de la configuration :")
        for finding in report.capabilities.findings:
            severity = labels.get(
                finding.severity,
                finding.severity.upper(),
            )
            detail = f" · {finding.detail}" if finding.detail else ""
            lines.append(
                f"[{severity}] {finding.title}{detail}"
            )
            if finding.action:
                lines.append(f"  Action : {finding.action}")

    lines.extend(
        [
            "",
            (
                "Résultat : prêt"
                if report.status == "ready"
                else (
                    "Résultat : utilisable avec points à vérifier"
                    if report.status == "warning"
                    else "Résultat : erreur(s) à corriger"
                )
            ),
        ]
    )
    return "\n".join(lines)
