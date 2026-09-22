from __future__ import annotations

# ruff: noqa: E402

import argparse
import copy
from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stream_state_router.host import (
    SoundVolumeViewAudioRouter,
    WindowsHDRController,
)
from stream_state_router.importers import (
    AdvancedSceneSwitcherImporter,
    SceneCollectionImporter,
    neutralize_referenced_test_layout_profiles,
    wire_windows_hdr_capture_profiles,
)
from stream_state_router.obs.client import OBSClientManager
from stream_state_router.services.config import (
    build_obs_config,
    load_config,
    validate_config,
)
from stream_state_router.services.single_instance import SingleInstanceGuard


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validation réelle SSR 2.2 : import de collection OBS, filtres, "
            "HDR Windows et routage audio SoundVolumeView."
        )
    )
    parser.add_argument(
        "--asc-path",
        type=Path,
        help=(
            "Fichier JSON Advanced Scene Switcher explicite. "
            "Sinon le fichier de la collection OBS est détecté automatiquement."
        ),
    )
    parser.add_argument(
        "--test-filter",
        action="store_true",
        help="Réécrire à l'identique un filtre OBS existant et vérifier le readback.",
    )
    parser.add_argument(
        "--filter-source",
        help="Source exacte du filtre à tester.",
    )
    parser.add_argument(
        "--filter",
        dest="filter_name",
        help="Nom exact du filtre à tester.",
    )
    parser.add_argument(
        "--test-hdr",
        action="store_true",
        help="Basculer temporairement HDR de l'écran principal puis restaurer.",
    )
    parser.add_argument(
        "--soundvolumeview-path",
        help="Chemin explicite vers SoundVolumeView.exe (prioritaire sur la config SSR).",
    )
    parser.add_argument(
        "--audio-process",
        help="Processus à router via SoundVolumeView, ex. Dofus.exe.",
    )
    parser.add_argument(
        "--audio-device",
        help="Périphérique SoundVolumeView, ex. Game.",
    )
    parser.add_argument(
        "--audio-roles",
        default="all",
        choices=("0", "1", "2", "all"),
        help="Rôles Windows SoundVolumeView (défaut: all).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Ne pas demander de confirmation pour HDR/audio.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Rapport JSON de sortie.",
    )
    parser.add_argument(
        "--export-migration-preview",
        type=Path,
        help=(
            "Écrit une copie de config avec migration logique ASC, "
            "sans modifier la configuration SSR active."
        ),
    )
    parser.add_argument(
        "--migration-include-layouts",
        action="store_true",
        help=(
            "Inclut les LayoutProfiles exhaustifs dans l'export de migration. "
            "Désactivé par défaut."
        ),
    )
    parser.add_argument(
        "--wire-hdr-profiles",
        action="store_true",
        help=(
            "Ajoute Windows HDR ON à CaptureProfile HDR et OFF à SDR "
            "si aucune action contradictoire n'existe."
        ),
    )
    parser.add_argument(
        "--enable-converted-rules",
        action="store_true",
        help="Active explicitement les nouvelles règles ASC créées par la migration.",
    )
    parser.add_argument(
        "--neutralize-test-layouts",
        action="store_true",
        help=(
            "Transforme en no-op les LayoutProfiles référencés dont la scène "
            "pointe encore vers [Module] TEST SSR."
        ),
    )
    return parser.parse_args()


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _select_filter(snapshot, source: str | None, name: str | None):
    filters = list(snapshot.filters)
    if source and name:
        matches = [
            item
            for item in filters
            if item.source.casefold() == source.casefold()
            and item.name.casefold() == name.casefold()
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"Filtre introuvable/ambigu : {source}/{name} "
                f"({len(matches)} correspondance(s))."
            )
        return matches[0]

    if not filters:
        raise RuntimeError("Aucun filtre OBS disponible pour le test.")

    print("\nFiltres OBS disponibles :")
    for index, item in enumerate(filters, start=1):
        print(
            f"  {index:>2}. {item.source} / {item.name} "
            f"[{item.kind}]"
        )
    while True:
        raw = input("Choisir le filtre à réécrire à l'identique : ").strip()
        try:
            index = int(raw)
        except ValueError:
            index = 0
        if 1 <= index <= len(filters):
            return filters[index - 1]
        print("Choix invalide.")


def _confirm(prompt: str, *, yes: bool) -> bool:
    if yes:
        return True
    answer = input(f"{prompt} [o/N] : ").strip().casefold()
    return answer in {"o", "oui", "y", "yes"}


def main() -> int:
    args = _parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = args.report or Path.cwd() / f"ssr-22-validation-{stamp}.json"
    report: dict[str, Any] = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "starting",
        "checks": {},
    }

    guard = SingleInstanceGuard()
    client: OBSClientManager | None = None
    hdr_restore: bool | None = None

    try:
        if guard.already_running:
            raise RuntimeError(
                "Une instance SSR est déjà active. Fermer SSR avant ce labo."
            )

        config = load_config()
        obs_config = replace(build_obs_config(config), enabled=True)
        report["obs"] = {
            "host": obs_config.host,
            "port": obs_config.port,
        }
        client = OBSClientManager(obs_config)
        ok, diagnostic = client.probe()
        print(diagnostic)
        if not ok:
            raise RuntimeError(diagnostic)

        importer = SceneCollectionImporter(client)
        # obsws-python logs every rejected request with logger.exception().
        # GetInputMute/GetInputVolume legitimately return 604 for non-audio
        # inputs during exhaustive discovery; SSR handles those responses as
        # capability probes. Keep the lab readable while preserving the actual
        # SSR warnings/failures and restore the SDK logger immediately after.
        sdk_logger = logging.getLogger("obsws_python.reqs")
        previous_sdk_level = sdk_logger.level
        sdk_logger.setLevel(logging.CRITICAL)
        try:
            snapshot = importer.snapshot()
        finally:
            sdk_logger.setLevel(previous_sdk_level)
        report["checks"]["collection_snapshot"] = {
            "ok": True,
            "collection": snapshot.collection,
            "current_program_scene": snapshot.current_program_scene,
            "scenes": len(snapshot.scenes),
            "inputs": len(snapshot.inputs),
            "filters": len(snapshot.filters),
            "scene_items": len(snapshot.scene_items),
            "warnings": list(snapshot.warnings),
        }
        print(
            "\nSnapshot collection : "
            f"{snapshot.collection!r} · {len(snapshot.scenes)} scène(s) · "
            f"{len(snapshot.inputs)} input(s) · {len(snapshot.filters)} filtre(s) · "
            f"{len(snapshot.scene_items)} Scene Item(s)"
        )

        preview_layout_config = copy.deepcopy(config)
        layouts, layout_skipped = importer.capture_layout_profiles(
            snapshot=snapshot,
        )
        layout_report = SceneCollectionImporter.apply_layout_profiles(
            preview_layout_config,
            collection=snapshot.collection,
            profiles=layouts,
            skipped=layout_skipped,
        )
        layout_errors = validate_config(preview_layout_config)
        report["checks"]["collection_layout_preview"] = {
            "ok": not layout_errors,
            "profiles": len(layouts),
            "added": layout_report.added,
            "refreshed": layout_report.refreshed,
            "skipped": list(layout_report.skipped),
            "validation_errors": layout_errors,
        }
        print(
            "Layouts importables : "
            f"{len(layouts)} profil(s), "
            f"{len(layout_report.skipped)} avertissement(s)."
        )
        if layout_errors:
            raise RuntimeError(
                "La capture exhaustive des layouts produit une configuration "
                "invalide : " + " | ".join(layout_errors)
            )

        migration_preview_config: dict[str, Any] | None = None
        detected = (
            args.asc_path
            if args.asc_path is not None
            else AdvancedSceneSwitcherImporter.find_scene_collection_file(
                snapshot.collection
            )
        )
        if detected is not None:
            preview_config = copy.deepcopy(config)
            asc_data = AdvancedSceneSwitcherImporter.load(detected)
            asc_report = AdvancedSceneSwitcherImporter.apply_to_config(
                asc_data,
                preview_config,
                snapshot=snapshot,
            )
            errors = validate_config(preview_config)
            imported_controls = preview_config.get(
                "control_variables",
                {},
            )
            if not isinstance(imported_controls, dict):
                imported_controls = {}
            streamdeck_rebinds = [
                item
                for item in asc_report.skipped
                if "Stream Deck SSR" in item
            ]
            report["checks"]["advanced_scene_switcher_preview"] = {
                "ok": not errors,
                "path": str(detected),
                "macros_total": asc_report.macros_total,
                "macros_converted": asc_report.macros_converted,
                "actions_converted": asc_report.actions_converted,
                "rules_created": asc_report.rules_created,
                "profiles_created": asc_report.profiles_created,
                "attached_to_existing_rules": asc_report.attached_to_existing_rules,
                "control_variables": dict(imported_controls),
                "streamdeck_rebinds_required": len(streamdeck_rebinds),
                "skipped": list(asc_report.skipped),
                "rejected_raw_count": len(asc_report.rejected_raw),
                "rejected_raw": [
                    item.as_mapping() for item in asc_report.rejected_raw
                ],
                "validation_errors": errors,
            }
            print(
                "ASC détecté : "
                f"{asc_report.macros_converted}/{asc_report.macros_total} macro(s) "
                "convertible(s) sur une copie de la config."
            )
            if imported_controls:
                controls_text = ", ".join(
                    f"{key}={value}"
                    for key, value in sorted(imported_controls.items())
                )
                print(f"Variables de contrôle importées : {controls_text}")
            if streamdeck_rebinds:
                print(
                    "Boutons Stream Deck à reconfigurer dans le plugin SSR : "
                    f"{len(streamdeck_rebinds)}."
                )
            if asc_report.skipped:
                print("Éléments ASC non automatisés :")
                for item in asc_report.skipped:
                    print(f"  - {item}")
            if errors:
                raise RuntimeError(
                    "La prévisualisation ASC produit une configuration invalide : "
                    + " | ".join(errors)
                )

            if args.export_migration_preview is not None:
                migration_preview_config = copy.deepcopy(config)
                if args.migration_include_layouts:
                    SceneCollectionImporter.apply_layout_profiles(
                        migration_preview_config,
                        collection=snapshot.collection,
                        profiles=layouts,
                        skipped=layout_skipped,
                    )
                AdvancedSceneSwitcherImporter.apply_to_config(
                    asc_data,
                    migration_preview_config,
                    snapshot=snapshot,
                    enable_created_rules=bool(
                        args.enable_converted_rules
                    ),
                )
                hdr_profiles_changed: tuple[str, ...] = ()
                test_layouts_neutralized: tuple[str, ...] = ()
                if args.wire_hdr_profiles:
                    hdr_profiles_changed = wire_windows_hdr_capture_profiles(
                        migration_preview_config
                    )
                if args.neutralize_test_layouts:
                    test_layouts_neutralized = (
                        neutralize_referenced_test_layout_profiles(
                            migration_preview_config
                        )
                    )
                migration_errors = validate_config(migration_preview_config)
                if migration_errors:
                    raise RuntimeError(
                        "La migration logique exportée serait invalide : "
                        + " | ".join(migration_errors)
                    )
                args.export_migration_preview.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )
                args.export_migration_preview.write_text(
                    json.dumps(
                        migration_preview_config,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                report["checks"]["migration_preview_export"] = {
                    "ok": True,
                    "path": str(args.export_migration_preview),
                    "mode": "logic_migration",
                    "includes_layouts": bool(
                        args.migration_include_layouts
                    ),
                    "includes_global_snapshot_merge": False,
                    "wire_hdr_profiles": bool(args.wire_hdr_profiles),
                    "hdr_profiles_changed": list(hdr_profiles_changed),
                    "enable_converted_rules": bool(
                        args.enable_converted_rules
                    ),
                    "neutralize_test_layouts": bool(
                        args.neutralize_test_layouts
                    ),
                    "test_layouts_neutralized": list(
                        test_layouts_neutralized
                    ),
                }
                if args.wire_hdr_profiles:
                    if hdr_profiles_changed:
                        print(
                            "CaptureProfiles HDR câblés : "
                            + ", ".join(hdr_profiles_changed)
                        )
                    else:
                        print(
                            "CaptureProfiles HDR/SDR déjà correctement câblés."
                        )
                if args.enable_converted_rules:
                    print("Nouvelles règles ASC converties : activées.")
                if args.neutralize_test_layouts:
                    print(
                        "LayoutProfiles de test neutralisés : "
                        + (
                            ", ".join(test_layouts_neutralized)
                            if test_layouts_neutralized
                            else "aucun"
                        )
                    )
                print(
                    "Prévisualisation migration logique écrite : "
                    f"{args.export_migration_preview}"
                )
        else:
            report["checks"]["advanced_scene_switcher_preview"] = {
                "ok": True,
                "detected": False,
            }
            print("Aucun bloc Advanced Scene Switcher détecté dans la collection.")
            if args.export_migration_preview is not None:
                raise RuntimeError(
                    "Impossible d'exporter la migration logique sans bloc ASC."
                )

        if args.test_filter:
            selected = _select_filter(
                snapshot,
                args.filter_source,
                args.filter_name,
            )
            before = importer.reader.filter_details(
                selected.source,
                selected.name,
            )
            original = dict(before.settings)
            client.send(
                "SetSourceFilterSettings",
                {
                    "sourceName": selected.source,
                    "filterName": selected.name,
                    "filterSettings": original,
                    "overlay": True,
                },
            )
            after = importer.reader.filter_details(
                selected.source,
                selected.name,
            )
            changed_keys = {
                key: {
                    "before": original.get(key),
                    "after": after.settings.get(key),
                }
                for key in original
                if after.settings.get(key) != original.get(key)
            }
            if changed_keys:
                raise RuntimeError(
                    "Le readback filtre diffère après réécriture no-op : "
                    + json.dumps(changed_keys, ensure_ascii=False)
                )
            report["checks"]["filter_noop_write"] = {
                "ok": True,
                "source": selected.source,
                "filter": selected.name,
                "setting_keys": sorted(original),
            }
            print(
                f"PASS filtre — {selected.source}/{selected.name} "
                "réécrit à l'identique et acquitté."
            )

        if args.test_hdr:
            hdr = WindowsHDRController()
            rows = hdr.status(scope="primary")
            supported = [row for row in rows if bool(row.get("supported"))]
            if len(supported) != 1:
                raise RuntimeError(
                    "Impossible d'identifier exactement un écran principal HDR compatible."
                )
            initial = bool(supported[0]["enabled"])
            hdr_restore = initial
            print(
                f"\nHDR écran principal : {'ON' if initial else 'OFF'}."
            )
            if not _confirm(
                "Basculer HDR temporairement puis restaurer l'état initial ?",
                yes=args.yes,
            ):
                report["checks"]["hdr_toggle"] = {
                    "ok": True,
                    "skipped": True,
                    "initial": initial,
                }
            else:
                hdr.set_enabled(not initial, scope="primary")
                changed = hdr.status(scope="primary")
                changed_supported = [
                    row for row in changed if bool(row.get("supported"))
                ]
                if (
                    len(changed_supported) != 1
                    or bool(changed_supported[0]["enabled"]) == initial
                ):
                    raise RuntimeError("Windows n'a pas acquitté la bascule HDR.")
                hdr.set_enabled(initial, scope="primary")
                hdr_restore = None
                restored = hdr.status(scope="primary")
                restored_supported = [
                    row for row in restored if bool(row.get("supported"))
                ]
                if (
                    len(restored_supported) != 1
                    or bool(restored_supported[0]["enabled"]) != initial
                ):
                    raise RuntimeError("La restauration HDR n'est pas acquittée.")
                report["checks"]["hdr_toggle"] = {
                    "ok": True,
                    "initial": initial,
                    "restored": initial,
                }
                print("PASS HDR — bascule acquittée puis état initial restauré.")

        if bool(args.audio_process) != bool(args.audio_device):
            raise ValueError(
                "--audio-process et --audio-device doivent être fournis ensemble."
            )
        if args.audio_process and args.audio_device:
            host = config.get("host_control")
            host_settings = host if isinstance(host, dict) else {}
            path = str(
                args.soundvolumeview_path
                or host_settings.get("soundvolumeview_path")
                or ""
            )
            print(
                "\nRoutage audio persistant demandé : "
                f"{args.audio_process} -> {args.audio_device} ({args.audio_roles})."
            )
            if not _confirm(
                "Appliquer cette préférence Windows ?",
                yes=args.yes,
            ):
                report["checks"]["audio_route"] = {
                    "ok": True,
                    "skipped": True,
                }
            else:
                router = SoundVolumeViewAudioRouter(
                    path,
                    timeout_seconds=float(
                        host_settings.get("audio_timeout_seconds", 5.0)
                    ),
                )
                router.set_app_default(
                    device=args.audio_device,
                    process=args.audio_process,
                    roles=args.audio_roles,
                )
                report["checks"]["audio_route"] = {
                    "ok": True,
                    "process": args.audio_process,
                    "device": args.audio_device,
                    "roles": args.audio_roles,
                }
                print("PASS audio — SoundVolumeView a accepté /SetAppDefault.")

        report["status"] = "passed"
        return 0

    except KeyboardInterrupt:
        report["status"] = "interrupted"
        report["error"] = "Interruption utilisateur"
        return 130
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        print(f"\nFAIL — {exc}", file=sys.stderr)
        return 1
    finally:
        if hdr_restore is not None:
            try:
                WindowsHDRController().set_enabled(
                    hdr_restore,
                    scope="primary",
                )
                report["hdr_emergency_restore"] = {
                    "attempted": True,
                    "ok": True,
                    "restored": hdr_restore,
                }
                print("HDR restauré dans le bloc finally.")
            except Exception as exc:
                report["hdr_emergency_restore"] = {
                    "attempted": True,
                    "ok": False,
                    "error": str(exc),
                }
                print(
                    f"ATTENTION — restauration HDR impossible : {exc}",
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
            print(
                f"Impossible d'écrire le rapport : {exc}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    raise SystemExit(main())
