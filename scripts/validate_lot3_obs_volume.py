from __future__ import annotations

# ruff: noqa: E402

import argparse
import getpass
import json
import math
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stream_state_router.obs.client import OBSClientManager
from stream_state_router.obs.dispatcher import OBSDispatcher, profile_map_from_raw
from stream_state_router.obs.models import OBSConnectionConfig
from stream_state_router.planning.planner import INPUT_VOLUME_DB_ABS_TOLERANCE
from stream_state_router.router.engine import StateRouterEngine
from stream_state_router.router.models import StreamState
from stream_state_router.router.rules import RuleSet
from stream_state_router.services.paths import config_path
from stream_state_router.services.runtime import RoutingService
from stream_state_router.services.single_instance import SingleInstanceGuard

LAB_PROFILE = "__Lot3VolumeLab__"


class _NullForegroundProvider:
    def get(self):
        return None


class _CommandCollector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._results: dict[str, Any] = {}
        self._events: dict[str, threading.Event] = {}

    def callback(self, event) -> None:
        if getattr(event, "kind", "") != "obs_command_result":
            return
        request_id = str(getattr(event, "request_id", "") or "")
        if not request_id:
            return
        with self._lock:
            self._results[request_id] = getattr(event, "payload", None)
            waiter = self._events.setdefault(request_id, threading.Event())
            waiter.set()

    def wait(self, request_id: str, *, timeout: float) -> Any:
        with self._lock:
            if request_id in self._results:
                return self._results[request_id]
            waiter = self._events.setdefault(request_id, threading.Event())
        if not waiter.wait(timeout):
            raise TimeoutError(f"Commande SSR sans résultat après {timeout:.1f} s: {request_id}")
        with self._lock:
            return self._results[request_id]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validation réelle OBS du Lot 3 input_volume_db. "
            "Le test applique temporairement une variation de volume, vérifie "
            "le chemin déclaratif SSR complet, puis restaure le multiplicateur "
            "OBS initial."
        )
    )
    parser.add_argument("--host", help="Hôte OBS WebSocket (défaut: config SSR ou 127.0.0.1)")
    parser.add_argument("--port", type=int, help="Port OBS WebSocket (défaut: config SSR ou 4455)")
    parser.add_argument(
        "--input",
        dest="input_selector",
        help="Nom exact ou UUID de l'input OBS. Sans option, choix interactif.",
    )
    parser.add_argument(
        "--target-db",
        type=float,
        help=(
            "Cible de test en dB, entre -100 et +26. "
            "Sans option, le script choisit automatiquement une variation <= 1 dB."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Timeout par commande SSR en secondes (défaut: 15).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Ne pas demander de confirmation avant la mutation temporaire.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Chemin du rapport JSON. Par défaut: lot3-obs-validation-<timestamp>.json",
    )
    return parser.parse_args()


def _read_existing_obs_settings() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    obs = payload.get("obs")
    return dict(obs) if isinstance(obs, dict) else {}


def _connection_config(args: argparse.Namespace) -> OBSConnectionConfig:
    existing = _read_existing_obs_settings()
    host = str(args.host or existing.get("host") or "127.0.0.1")
    port = int(args.port or existing.get("port") or 4455)

    env_password = os.environ.get("OBS_WEBSOCKET_PASSWORD")
    if env_password is not None:
        password = env_password
    elif "password" in existing:
        password = str(existing.get("password") or "")
    else:
        password = getpass.getpass(
            "Mot de passe OBS WebSocket (Entrée si aucun mot de passe) : "
        )

    return OBSConnectionConfig(
        enabled=True,
        host=host,
        port=port,
        password=password,
        timeout_seconds=3.0,
        reconnect_seconds=0.5,
    )


def _select_input(
    client: OBSClientManager,
    selector: str | None,
) -> dict[str, str]:
    response = client.send("GetInputList")
    candidates = [
        {
            "name": str(row.get("inputName") or "").strip(),
            "uuid": str(row.get("inputUuid") or "").strip(),
            "kind": str(row.get("inputKind") or "").strip(),
        }
        for row in response.get("inputs", []) or []
        if isinstance(row, dict)
        and str(row.get("inputName") or "").strip()
        and str(row.get("inputUuid") or "").strip()
    ]

    rows: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for row in candidates:
        try:
            client.send(
                "GetInputVolume",
                {"inputUuid": row["uuid"]},
            )
        except Exception as exc:
            skipped.append(
                {
                    **row,
                    "reason": str(exc),
                }
            )
            continue
        rows.append(row)

    if skipped:
        print("\nInputs ignorés car ils ne supportent pas le volume audio :")
        for row in skipped:
            print(f"  - {row['name']}  [{row['kind']}]")

    if not rows:
        raise RuntimeError(
            "OBS ne retourne aucun input audio avec UUID compatible GetInputVolume."
        )

    if selector:
        wanted = selector.strip().casefold()
        matches = [
            row
            for row in rows
            if row["name"].casefold() == wanted or row["uuid"].casefold() == wanted
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"Input introuvable ou ambigu pour {selector!r}; "
                f"{len(matches)} correspondance(s)."
            )
        return matches[0]

    print("\nInputs OBS disponibles :")
    for index, row in enumerate(rows, start=1):
        print(f"  {index:>2}. {row['name']}  [{row['kind']}]  {row['uuid']}")
    while True:
        raw = input("Choisir le numéro de l'input à tester : ").strip()
        try:
            index = int(raw)
        except ValueError:
            index = 0
        if 1 <= index <= len(rows):
            return rows[index - 1]
        print("Choix invalide.")


def _choose_target(original_db: float, requested: float | None) -> float:
    if requested is not None:
        if not math.isfinite(requested) or not -100.0 <= requested <= 26.0:
            raise ValueError("--target-db doit être un nombre fini entre -100 et +26 dB.")
        target = float(requested)
    elif original_db > 27.0 or original_db < -101.0:
        raise RuntimeError(
            "Le volume physique actuel est à plus de 1 dB de la plage "
            "écrivable [-100,+26]. Choisir un autre input ou fournir "
            "explicitement --target-db après vérification manuelle."
        )
    elif original_db > 26.0:
        target = 26.0
    elif original_db < -100.0:
        target = -100.0
    elif original_db - 1.0 >= -100.0:
        target = max(-100.0, min(26.0, original_db - 1.0))
    else:
        target = max(-100.0, min(26.0, original_db + 1.0))

    if math.isclose(
        target,
        original_db,
        rel_tol=0.0,
        abs_tol=max(INPUT_VOLUME_DB_ABS_TOLERANCE * 10.0, 0.001),
    ):
        alternative = original_db + 1.0 if original_db + 1.0 <= 26.0 else original_db - 1.0
        target = max(-100.0, min(26.0, alternative))

    if math.isclose(
        target,
        original_db,
        rel_tol=0.0,
        abs_tol=max(INPUT_VOLUME_DB_ABS_TOLERANCE * 10.0, 0.001),
    ):
        raise RuntimeError("Impossible de choisir une cible de test distincte et sûre.")

    return float(target)


def _result_mapping(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {"success": False, "error": "Résultat SSR vide", "result": None}
    return {
        "success": bool(getattr(payload, "success", False)),
        "error": str(getattr(payload, "error", "") or ""),
        "result": getattr(payload, "result", None),
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def main() -> int:
    args = _parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = args.report or Path.cwd() / f"lot3-obs-validation-{stamp}.json"
    report: dict[str, Any] = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "starting",
        "lot3_head": "cc54631f50c7b1d473799b8c9075c007e0432fdd",
        "tolerance_db": INPUT_VOLUME_DB_ABS_TOLERANCE,
    }

    guard = SingleInstanceGuard()
    client: OBSClientManager | None = None
    service: RoutingService | None = None
    original_mul: float | None = None
    original_db: float | None = None
    input_ref: dict[str, str] | None = None
    mutation_submitted = False
    mutated = False

    try:
        if guard.already_running:
            raise RuntimeError(
                "Une instance SSR est déjà active. Fermer SSR avant le test réel Lot 3."
            )

        obs_config = _connection_config(args)
        report["obs"] = {"host": obs_config.host, "port": obs_config.port}
        client = OBSClientManager(obs_config)

        ok, diagnostic = client.probe()
        print(diagnostic)
        if not ok:
            raise RuntimeError(diagnostic)

        version = client.send("GetVersion")
        available = {
            str(item).strip()
            for item in version.get("availableRequests", []) or []
            if str(item).strip()
        }
        required = {"GetInputList", "GetInputVolume", "SetInputVolume"}
        missing = sorted(required - available)
        if missing:
            raise RuntimeError(
                "OBS n'annonce pas les requêtes nécessaires : " + ", ".join(missing)
            )

        input_ref = _select_input(client, args.input_selector)
        volume = client.send(
            "GetInputVolume",
            {"inputUuid": input_ref["uuid"]},
        )
        original_db = float(volume.get("inputVolumeDb"))
        original_mul = float(volume.get("inputVolumeMul"))

        if not math.isfinite(original_db):
            raise RuntimeError(
                "Le volume dB actuel n'est pas fini. Choisir un input avec un volume non nul."
            )
        if not math.isfinite(original_mul) or not 0.0 <= original_mul <= 20.0:
            raise RuntimeError("Le multiplicateur de volume OBS initial est invalide.")

        target_db = _choose_target(original_db, args.target_db)
        report["input"] = dict(input_ref)
        report["original"] = {
            "input_volume_db": original_db,
            "input_volume_mul": original_mul,
        }
        report["target_db"] = target_db

        print(
            f"\nInput : {input_ref['name']}\n"
            f"UUID  : {input_ref['uuid']}\n"
            f"Volume initial : {original_db:.6f} dB (mul={original_mul:.9f})\n"
            f"Cible temporaire : {target_db:.6f} dB\n"
            "Le volume initial sera restauré avec inputVolumeMul dans le bloc finally."
        )
        if not args.yes:
            answer = input("Exécuter le test réel Lot 3 ? [o/N] : ").strip().casefold()
            if answer not in {"o", "oui", "y", "yes"}:
                report["status"] = "cancelled"
                print("Test annulé sans mutation.")
                return 0

        profiles = profile_map_from_raw(
            {
                "audio": {
                    LAB_PROFILE: {
                        "actions": [
                            {
                                "type": "input_volume_db",
                                "params": {
                                    "input": input_ref["name"],
                                    "volume_db": target_db,
                                },
                            }
                        ]
                    }
                }
            }
        )
        dispatcher = OBSDispatcher(client, profiles)
        engine = StateRouterEngine(RuleSet([]), debounce_ms=0)
        engine.set_manual_override(StreamState(audio_profile=LAB_PROFILE))

        service = RoutingService(
            engine,
            dispatcher,
            poll_ms=20,
            provider=_NullForegroundProvider(),
            obs_probe_seconds=60.0,
            config_revision="lot3-real-obs-lab",
            declarative_execution_enabled=True,
        )
        collector = _CommandCollector()
        service.on_event = collector.callback
        service.pause(True)
        service.start()

        prepare_id = service.request_prepare_declarative_execution()
        prepared_payload = collector.wait(prepare_id, timeout=max(1.0, args.timeout))
        prepared = _result_mapping(prepared_payload)
        report["prepare"] = prepared
        if not prepared["success"]:
            raise RuntimeError(f"Préparation déclarative échouée : {prepared['error']}")
        prepared_result = prepared["result"]
        if not isinstance(prepared_result, dict):
            raise RuntimeError("La préparation SSR n'a pas retourné de résultat structuré.")
        plan_id = str(prepared_result.get("plan_id") or "").strip()
        if not plan_id:
            raise RuntimeError("La préparation SSR n'a pas retourné de plan_id.")

        mutation_submitted = True
        execute_id = service.request_execute_declarative_plan(plan_id)
        executed_payload = collector.wait(execute_id, timeout=max(1.0, args.timeout))
        executed = _result_mapping(executed_payload)
        report["execute"] = executed
        if not executed["success"]:
            raise RuntimeError(f"Exécution déclarative échouée : {executed['error']}")

        execution_result = executed["result"]
        if not isinstance(execution_result, dict):
            raise RuntimeError("L'exécution SSR n'a pas retourné de résultat structuré.")
        if not bool(execution_result.get("converged")):
            raise RuntimeError(
                "Le plan Lot 3 n'a pas convergé : "
                + json.dumps(execution_result, ensure_ascii=False)
            )

        mutated = True
        after = client.send(
            "GetInputVolume",
            {"inputUuid": input_ref["uuid"]},
        )
        after_db = float(after.get("inputVolumeDb"))
        after_mul = float(after.get("inputVolumeMul"))
        report["after_execute"] = {
            "input_volume_db": after_db,
            "input_volume_mul": after_mul,
        }

        if not math.isclose(
            after_db,
            target_db,
            rel_tol=1e-9,
            abs_tol=INPUT_VOLUME_DB_ABS_TOLERANCE,
        ):
            raise RuntimeError(
                f"Readback OBS hors tolérance après exécution : {after_db} dB "
                f"pour une cible {target_db} dB."
            )

        report["status"] = "passed"
        print(
            f"\nPASS — SSR a convergé à {after_db:.6f} dB "
            f"(tolérance {INPUT_VOLUME_DB_ABS_TOLERANCE:g} dB)."
        )
        return 0

    except KeyboardInterrupt:
        report["status"] = "interrupted"
        report["error"] = "Interruption utilisateur"
        print("\nTest interrompu; restauration du volume initial...")
        return 130
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        print(f"\nFAIL — {exc}", file=sys.stderr)
        return 1
    finally:
        if service is not None:
            try:
                shutdown = service.stop()
                report["runtime_stop"] = {
                    "ok": bool(shutdown),
                    "diagnostic": (
                        shutdown.diagnostic_summary()
                        if hasattr(shutdown, "diagnostic_summary")
                        else str(shutdown)
                    ),
                }
            except Exception as exc:
                report["runtime_stop"] = {"ok": False, "error": str(exc)}

        if (
            mutation_submitted
            and client is not None
            and input_ref is not None
            and original_mul is not None
        ):
            try:
                client.send(
                    "SetInputVolume",
                    {
                        "inputUuid": input_ref["uuid"],
                        "inputVolumeMul": original_mul,
                    },
                )
                restored = client.send(
                    "GetInputVolume",
                    {"inputUuid": input_ref["uuid"]},
                )
                restored_db = float(restored.get("inputVolumeDb"))
                restored_mul = float(restored.get("inputVolumeMul"))
                report["restore"] = {
                    "attempted": True,
                    "input_volume_db": restored_db,
                    "input_volume_mul": restored_mul,
                    "mul_matches_original": math.isclose(
                        restored_mul,
                        original_mul,
                        rel_tol=1e-6,
                        abs_tol=1e-7,
                    ),
                }
                if mutated:
                    print(
                        f"Volume restauré : {restored_db:.6f} dB "
                        f"(mul={restored_mul:.9f})."
                    )
            except Exception as exc:
                report["restore"] = {
                    "attempted": True,
                    "success": False,
                    "error": str(exc),
                }
                print(
                    "ATTENTION — restauration automatique du volume impossible : "
                    f"{exc}",
                    file=sys.stderr,
                )

        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        guard.close()

        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        try:
            _write_report(report_path, report)
            print(f"Rapport : {report_path}")
        except Exception as exc:
            print(f"Impossible d'écrire le rapport JSON : {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
