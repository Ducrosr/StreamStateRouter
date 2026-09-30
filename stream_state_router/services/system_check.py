from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

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
)


@dataclass(frozen=True, slots=True)
class SystemCheckReport:
    config_errors: tuple[str, ...]
    capabilities: CapabilityReport

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


def _sequence(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _probe_obs_catalog(client: OBSClientManager) -> dict[str, object]:
    """Read a compact OBS inventory without mutating OBS."""

    try:
        scenes_response = client.send("GetSceneList")
        inputs_response = client.send("GetInputList")
        scenes = _sequence(scenes_response.get("scenes"))
        inputs = _sequence(inputs_response.get("inputs"))

        scene_items = 0
        for scene in scenes:
            name = str(scene.get("sceneName") or "").strip()
            if not name:
                continue
            response = client.send(
                "GetSceneItemList",
                {"sceneName": name},
            )
            scene_items += len(_sequence(response.get("sceneItems")))

        return {
            "available": True,
            "stale": False,
            "scenes": len(scenes),
            "inputs": len(inputs),
            "scene_items": scene_items,
        }
    except Exception as exc:
        # A partial/failed inventory is diagnostic only. Preserve the OBS
        # connection result and expose the read failure separately.
        return {
            "available": True,
            "stale": True,
            "stale_reason": str(exc),
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

    client = None
    try:
        obs_config = build_obs_config(config)
        obs_enabled = bool(obs_config.enabled)
        if obs_enabled:
            client = obs_client_factory(obs_config)
            obs_connected, message = client.probe()
            if obs_connected:
                catalog_status = _probe_obs_catalog(client)
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
        audio_probe=audio_probe,
        hdr_probe=hdr_probe,
    )
    return SystemCheckReport(
        config_errors=config_errors,
        capabilities=capabilities,
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
