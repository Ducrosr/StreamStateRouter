from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable, Mapping

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
    def success(self) -> bool:
        return (
            not self.config_errors
            and self.capabilities.status != "error"
        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "success": self.success,
            "config_errors": list(self.config_errors),
            "capabilities": {
                "status": self.capabilities.status,
                "summary": self.capabilities.summary,
                "items": [
                    asdict(item)
                    for item in self.capabilities.items
                ],
                "findings": [
                    asdict(item)
                    for item in self.capabilities.findings
                ],
            },
        }


def probe_host_capabilities(
    config: Mapping[str, object],
    *,
    controller_factory: Callable = build_host_controller,
) -> tuple[dict[str, object], dict[str, object]]:
    action_types = configured_action_types(config)
    audio_used = "app_audio_output" in action_types
    hdr_used = "windows_hdr" in action_types
    if not audio_used and not hdr_used:
        return {}, {}

    try:
        controller = controller_factory(config)
    except Exception as exc:
        detail = str(exc)
        return (
            (
                {
                    "status": "error",
                    "detail": detail,
                    "action": (
                        "Configurez SoundVolumeView dans "
                        "Paramètres > Contrôle Windows."
                    ),
                }
                if audio_used
                else {}
            ),
            (
                {
                    "status": "error",
                    "detail": detail,
                    "action": (
                        "Vérifiez la prise en charge HDR de Windows "
                        "et de l’écran ciblé."
                    ),
                }
                if hdr_used
                else {}
            ),
        )

    audio_probe: dict[str, object] = {}
    if audio_used:
        try:
            controller.audio_router.resolve_executable()
            audio_probe = {
                "status": "ready",
                "detail": "SoundVolumeView disponible.",
            }
        except Exception as exc:
            audio_probe = {
                "status": "error",
                "detail": str(exc),
                "action": (
                    "Configurez SoundVolumeView dans "
                    "Paramètres > Contrôle Windows."
                ),
            }

    hdr_probe: dict[str, object] = {}
    if hdr_used:
        try:
            rows = controller.hdr_controller.status(scope="primary")
            supported = [
                row
                for row in rows
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
                    "Vérifiez la prise en charge HDR de Windows "
                    "et de l’écran ciblé."
                ),
            }

    return audio_probe, hdr_probe


def run_system_check(
    config: Mapping[str, object],
    *,
    obs_client_factory: Callable = OBSClientManager,
    controller_factory: Callable = build_host_controller,
) -> SystemCheckReport:
    config_errors = tuple(validate_config(config))
    obs_config = build_obs_config(config)
    obs_connected = False
    obs_error = ""

    if obs_config.enabled:
        client = None
        try:
            client = obs_client_factory(obs_config)
            obs_connected, message = client.probe()
            if not obs_connected:
                obs_error = str(message or "")
        except Exception as exc:
            obs_error = str(exc)
        finally:
            if client is not None:
                close = getattr(client, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

    audio_probe, hdr_probe = probe_host_capabilities(
        config,
        controller_factory=controller_factory,
    )
    capabilities = build_capability_report(
        config,
        obs_enabled=bool(obs_config.enabled),
        obs_connected=obs_connected,
        obs_error=obs_error,
        audio_probe=audio_probe,
        hdr_probe=hdr_probe,
        include_catalog=False,
    )
    return SystemCheckReport(
        config_errors=config_errors,
        capabilities=capabilities,
    )


def render_system_check(report: SystemCheckReport) -> str:
    lines = [
        "Configuration : "
        + (
            "valide"
            if not report.config_errors
            else f"invalide ({len(report.config_errors)} erreur(s))"
        ),
        "Capacités :",
    ]
    for item in report.capabilities.items:
        lines.append(
            f"- {item.label}: {item.status_label}"
            + (f" — {item.detail}" if item.detail else "")
        )
        if item.action:
            lines.append(f"  Action : {item.action}")

    if report.capabilities.findings:
        lines.append("Santé de la configuration :")
        for finding in report.capabilities.findings:
            lines.append(
                f"- {finding.severity}: {finding.title}"
                + (f" — {finding.detail}" if finding.detail else "")
            )

    if report.config_errors:
        lines.append("Erreurs de configuration :")
        lines.extend(f"- {error}" for error in report.config_errors)

    lines.append(
        "Résultat : "
        + (
            "OK"
            if report.success
            else "ÉCHEC"
        )
        + f" — {report.capabilities.summary}"
    )
    return "\n".join(lines)
