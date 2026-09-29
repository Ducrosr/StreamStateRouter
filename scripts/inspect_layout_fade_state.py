from __future__ import annotations

# ruff: noqa: E402

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stream_state_router.obs.catalog import OBSResourceCatalogReader
from stream_state_router.obs.client import OBSClientManager
from stream_state_router.obs.layouts import SSR_FADE_FILTER
from stream_state_router.services.config import build_obs_config, load_config
from stream_state_router.services.paths import user_data_dir


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inspecte sans mutation les helpers de fade SSR visibles dans OBS "
            "et les obligations runtime.json."
        )
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="Chemin du rapport JSON. Par défaut, écrit dans le dossier courant.",
    )
    parser.add_argument(
        "--include-all-filters",
        action="store_true",
        help=(
            "Inclut la liste de tous les filtres découverts. "
            "Par défaut, seuls les filtres [SSR] sont détaillés."
        ),
    )
    return parser.parse_args()


def _safe_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _load_runtime_marker() -> dict[str, Any]:
    path = user_data_dir() / "runtime.json"
    payload: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "readable": False,
        "cleanup_schema": None,
        "clean_shutdown": None,
        "cleanup_complete": None,
        "pending_cleanup": [],
        "error": "",
    }
    if not path.exists():
        return payload
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        payload["error"] = str(exc)
        return payload
    if not isinstance(raw, Mapping):
        payload["error"] = "runtime.json n'est pas un objet JSON."
        return payload

    pending = raw.get("pending_cleanup")
    payload.update(
        {
            "readable": True,
            "cleanup_schema": raw.get("cleanup_schema"),
            "clean_shutdown": raw.get("clean_shutdown"),
            "cleanup_complete": raw.get("cleanup_complete"),
            "pending_cleanup": [
                dict(item)
                for item in pending
                if isinstance(item, Mapping)
            ]
            if isinstance(pending, list)
            else [],
        }
    )
    return payload


def _source_kind_map(catalog) -> dict[str, str]:
    kinds: dict[str, str] = {}

    def assign(name: str, kind: str) -> None:
        if not name:
            return
        previous = kinds.get(name)
        if previous is None:
            kinds[name] = kind
        elif previous != kind:
            kinds[name] = "ambiguous"

    for item in catalog.inputs:
        assign(_safe_text(item.name), "input")
    for item in catalog.scenes:
        assign(_safe_text(item.name), "scene")
    for name in catalog.groups:
        assign(_safe_text(name), "group")
    for item in catalog.scene_items:
        assign(_safe_text(item.source), _safe_text(item.source_kind) or "unknown")
    return kinds


def _candidate_sources(catalog) -> tuple[str, ...]:
    values = {
        *(_safe_text(item.name) for item in catalog.inputs),
        *(_safe_text(item.name) for item in catalog.scenes),
        *(_safe_text(name) for name in catalog.groups),
        *(_safe_text(item.source) for item in catalog.scene_items),
    }
    return tuple(sorted((item for item in values if item), key=str.casefold))


def _filter_row(reader, source: str, ref, *, include_settings: bool) -> dict[str, Any]:
    row: dict[str, Any] = {
        "source": source,
        "name": ref.name,
        "kind": ref.kind,
        "enabled": ref.enabled,
        "settings": {},
        "read_error": "",
    }
    if not include_settings:
        return row
    try:
        details = reader.filter_details(source, ref.name)
    except Exception as exc:
        row["read_error"] = str(exc)
        return row

    settings = dict(details.settings)
    if ref.name == SSR_FADE_FILTER:
        # Keep the diagnostic narrowly scoped. Opacity is the only setting
        # currently needed to diagnose the temporary fade helper.
        raw_opacity = settings.get("opacity")
        row["settings"] = {
            "opacity": raw_opacity
            if isinstance(raw_opacity, (int, float)) and not isinstance(raw_opacity, bool)
            else None
        }
    else:
        row["settings"] = settings
    row["enabled"] = details.filter.enabled
    row["kind"] = details.filter.kind
    return row


def main() -> int:
    args = _parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = args.report or Path.cwd() / f"ssr-fade-inspection-{stamp}.json"

    report: dict[str, Any] = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only",
        "mutating_requests": [],
        "status": "starting",
        "runtime_marker": _load_runtime_marker(),
        "obs": {},
        "catalog": {},
        "filters": [],
        "fade_helpers": [],
        "warnings": [],
    }

    client: OBSClientManager | None = None
    try:
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

        reader = OBSResourceCatalogReader(client)
        catalog = reader.sync()
        kinds = _source_kind_map(catalog)

        report["catalog"] = catalog.summary()
        report["catalog"]["source_kinds"] = dict(
            sorted(kinds.items(), key=lambda item: item[0].casefold())
        )

        all_filters: list[dict[str, Any]] = []
        fade_helpers: list[dict[str, Any]] = []

        for source in _candidate_sources(catalog):
            try:
                refs = reader.filters_for_source(source)
            except Exception as exc:
                report["warnings"].append(
                    f"Filtres illisibles pour {source}: {exc}"
                )
                continue

            for ref in refs:
                is_ssr_named = ref.name.startswith("[SSR]")
                should_detail = (
                    args.include_all_filters
                    or is_ssr_named
                    or ref.name == SSR_FADE_FILTER
                )
                row = _filter_row(
                    reader,
                    source,
                    ref,
                    include_settings=should_detail,
                )
                row["source_kind"] = kinds.get(source, "unknown")
                row["ssr_name_prefix"] = is_ssr_named
                row["ownership_proven"] = False
                row["ownership_note"] = (
                    "Nom uniquement : A1 n'est pas encore disponible, "
                    "donc aucune propriété SSR n'est considérée prouvée."
                    if is_ssr_named
                    else ""
                )

                if args.include_all_filters or is_ssr_named:
                    all_filters.append(row)
                if ref.name == SSR_FADE_FILTER:
                    fade_helpers.append(row)

        report["filters"] = all_filters
        report["fade_helpers"] = fade_helpers
        report["status"] = "ok"
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()

        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        print(
            f"Collection : {catalog.collection!r} · "
            f"{len(fade_helpers)} filtre(s) nommé(s) {SSR_FADE_FILTER!r}"
        )
        for row in fade_helpers:
            opacity = row.get("settings", {}).get("opacity")
            print(
                "  - "
                f"{row['source']} [{row['source_kind']}] · "
                f"kind={row['kind'] or '?'} · enabled={row['enabled']} · "
                f"opacity={opacity}"
            )
        print(f"Rapport : {report_path}")
        print("Aucune requête de mutation OBS n'a été émise par ce script.")
        return 0
    except Exception as exc:
        report["status"] = "error"
        report["error"] = str(exc)
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(f"ERREUR : {exc}")
        print(f"Rapport partiel : {report_path}")
        return 1
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
