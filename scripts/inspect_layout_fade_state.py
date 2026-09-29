from __future__ import annotations

# ruff: noqa: E402

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stream_state_router.obs.catalog import OBSResourceCatalogReader
from stream_state_router.obs.client import OBSClientManager
from stream_state_router.obs.layouts import SSR_FADE_FILTER
from stream_state_router.services.config import build_obs_config, load_config
from stream_state_router.services.paths import APP_NAME


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inspecte sans mutation les helpers de fade SSR visibles dans OBS "
            "et un résumé expurgé des obligations runtime.json."
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
            "Inclut l'inventaire de tous les filtres découverts, sans exporter "
            "leurs réglages arbitraires."
        ),
    )
    return parser.parse_args()


def _safe_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _user_data_dir_read_only() -> Path:
    """Return SSR's standard user-data path without creating any directory."""
    if os.name == "nt":
        base = Path(
            os.environ.get("APPDATA")
            or Path.home() / "AppData" / "Roaming"
        )
    else:
        base = Path(
            os.environ.get("XDG_CONFIG_HOME")
            or Path.home() / ".config"
        )
    return base / APP_NAME


def _existing_config_path() -> Path:
    path = _user_data_dir_read_only() / "config.json"
    if not path.is_file():
        raise FileNotFoundError(
            "Configuration SSR existante introuvable; "
            "l'inspecteur read-only refuse d'en créer une."
        )
    return path


def _safe_error(exc: BaseException) -> str:
    # Exception text can contain paths, URLs or plugin-provided data. The
    # shareable diagnostic keeps only the exception class.
    return type(exc).__name__


def _safe_pending_cleanup(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    allowed = {
        "kind",
        "source",
        "collection",
        "helper_id",
        "source_uuid",
        "source_kind",
        "filter_name",
        "filter_kind",
        "cleanup_action",
        "legacy",
        "ambiguous",
        "attempts",
    }
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        row = {
            str(key): value
            for key, value in item.items()
            if str(key) in allowed
            and isinstance(value, (str, int, float, bool, type(None)))
        }
        row["has_error"] = bool(item.get("last_error"))
        rows.append(row)
    return rows


def _load_runtime_marker() -> dict[str, Any]:
    path = _user_data_dir_read_only() / "runtime.json"
    payload: dict[str, Any] = {
        "exists": path.is_file(),
        "readable": False,
        "cleanup_schema": None,
        "clean_shutdown": None,
        "cleanup_complete": None,
        "pending_cleanup": [],
        "error_type": "",
    }
    if not path.is_file():
        return payload
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        payload["error_type"] = _safe_error(exc)
        return payload
    if not isinstance(raw, Mapping):
        payload["error_type"] = "InvalidRootType"
        return payload

    payload.update(
        {
            "readable": True,
            "cleanup_schema": raw.get("cleanup_schema"),
            "clean_shutdown": raw.get("clean_shutdown"),
            "cleanup_complete": raw.get("cleanup_complete"),
            "pending_cleanup": _safe_pending_cleanup(
                raw.get("pending_cleanup")
            ),
        }
    )
    return payload


class _ReadOnlyOBSClient:
    """Reject every non-Get OBS request before it reaches the transport."""

    def __init__(self, client: OBSClientManager) -> None:
        self._client = client
        self.requests: list[str] = []

    def send(self, request: str, data=None, **kwargs):
        request_name = str(request or "")
        if not request_name.startswith("Get"):
            raise RuntimeError(
                f"Requête OBS interdite en mode read-only: {request_name}"
            )
        self.requests.append(request_name)
        return self._client.send(request_name, data, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._client, name)


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
        assign(
            _safe_text(item.source),
            _safe_text(item.source_kind) or "unknown",
        )
    return kinds


def _candidate_sources(catalog) -> tuple[str, ...]:
    values = {
        *(_safe_text(item.name) for item in catalog.inputs),
        *(_safe_text(item.name) for item in catalog.scenes),
        *(_safe_text(name) for name in catalog.groups),
        *(_safe_text(item.source) for item in catalog.scene_items),
    }
    return tuple(sorted((item for item in values if item), key=str.casefold))


def _is_fade_helper_name(value: object) -> bool:
    name = _safe_text(value)
    return bool(
        name == SSR_FADE_FILTER
        or name.startswith(f"{SSR_FADE_FILTER}::")
    )


def _filter_row(
    reader,
    source: str,
    ref,
    *,
    include_opacity: bool,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "source": source,
        "name": _safe_text(ref.name),
        "kind": _safe_text(ref.kind),
        "enabled": ref.enabled if isinstance(ref.enabled, bool) else None,
        "settings": {},
        "read_error_type": "",
    }
    if not include_opacity:
        return row
    try:
        details = reader.filter_details(source, ref.name)
    except Exception as exc:
        row["read_error_type"] = _safe_error(exc)
        return row

    settings = dict(details.settings)
    raw_opacity = settings.get("opacity")
    row["settings"] = {
        "opacity": (
            raw_opacity
            if isinstance(raw_opacity, (int, float))
            and not isinstance(raw_opacity, bool)
            else None
        )
    }
    row["enabled"] = (
        details.filter.enabled
        if isinstance(details.filter.enabled, bool)
        else None
    )
    row["kind"] = _safe_text(details.filter.kind)
    return row


def _unique_report_path(requested: Path | None, stamp: str) -> Path:
    candidate = requested or Path.cwd() / f"ssr-fade-inspection-{stamp}.json"
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix or ".json"
    for index in range(2, 10000):
        alternate = candidate.with_name(f"{stem}-{index}{suffix}")
        if not alternate.exists():
            return alternate
    raise RuntimeError("Impossible de choisir un nom de rapport unique.")


def _atomic_write_report(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                dict(payload),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except Exception:
            pass


def main() -> int:
    args = _parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = _unique_report_path(args.report, stamp)

    report: dict[str, Any] = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_shareable",
        "status": "starting",
        "coverage": {
            "sources_attempted": 0,
            "sources_read": 0,
            "filter_detail_failures": 0,
            "context_stable": False,
        },
        "runtime_marker": _load_runtime_marker(),
        "obs": {},
        "catalog": {},
        "filters": [],
        "fade_helpers": [],
        "warnings": [],
        "obs_requests": [],
    }

    client: OBSClientManager | None = None
    readonly: _ReadOnlyOBSClient | None = None
    try:
        # Explicit path prevents load_config() from calling ensure_user_config().
        config = load_config(_existing_config_path())
        obs_config = replace(build_obs_config(config), enabled=True)
        report["obs"] = {
            "host": obs_config.host,
            "port": obs_config.port,
        }

        client = OBSClientManager(obs_config)
        readonly = _ReadOnlyOBSClient(client)
        readonly.send("GetVersion")

        reader = OBSResourceCatalogReader(readonly)
        catalog = reader.sync()
        initial_collection = _safe_text(catalog.collection)
        initial_session = int(
            getattr(readonly, "session_generation", 0) or 0
        )
        kinds = _source_kind_map(catalog)

        report["catalog"] = catalog.summary()
        report["catalog"]["source_kinds"] = dict(
            sorted(kinds.items(), key=lambda item: item[0].casefold())
        )

        all_filters: list[dict[str, Any]] = []
        fade_helpers: list[dict[str, Any]] = []

        sources = _candidate_sources(catalog)
        report["coverage"]["sources_attempted"] = len(sources)
        for source in sources:
            try:
                refs = reader.filters_for_source(source)
            except Exception as exc:
                report["warnings"].append(
                    f"Filtres illisibles pour {source} ({_safe_error(exc)})"
                )
                continue
            report["coverage"]["sources_read"] += 1

            for ref in refs:
                is_ssr_named = _safe_text(ref.name).startswith("[SSR]")
                is_fade_helper = _is_fade_helper_name(ref.name)
                row = _filter_row(
                    reader,
                    source,
                    ref,
                    # Never export arbitrary plugin settings. Opacity is the
                    # only allowlisted setting needed for fade diagnosis.
                    include_opacity=is_fade_helper,
                )
                if row["read_error_type"]:
                    report["coverage"]["filter_detail_failures"] += 1
                row["source_kind"] = kinds.get(source, "unknown")
                row["ssr_name_prefix"] = is_ssr_named
                row["ownership_proven"] = False
                row["ownership_note"] = (
                    "Nom uniquement : aucune propriété n'est déduite du préfixe."
                    if is_ssr_named
                    else ""
                )

                if args.include_all_filters or is_ssr_named:
                    all_filters.append(row)
                if is_fade_helper:
                    fade_helpers.append(row)

        final_collection = _safe_text(reader.current_collection())
        final_session = int(
            getattr(readonly, "session_generation", 0) or 0
        )
        context_stable = bool(
            initial_collection
            and final_collection == initial_collection
            and final_session == initial_session
        )
        report["coverage"]["context_stable"] = context_stable
        if not context_stable:
            report["warnings"].append(
                "Contexte OBS modifié pendant l'inspection; "
                "le rapport n'est pas une photographie atomique."
            )

        report["filters"] = all_filters
        report["fade_helpers"] = fade_helpers
        report["obs_requests"] = list(readonly.requests)

        partial = bool(
            report["warnings"]
            or report["coverage"]["filter_detail_failures"]
            or report["coverage"]["sources_read"]
            != report["coverage"]["sources_attempted"]
            or not context_stable
        )
        report["status"] = "partial" if partial else "ok"
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_report(report_path, report)

        print(
            f"Collection : {initial_collection!r} · "
            f"{len(fade_helpers)} helper(s) de fade visible(s)"
        )
        for row in fade_helpers:
            opacity = row.get("settings", {}).get("opacity")
            print(
                "  - "
                f"{row['source']} [{row['source_kind']}] · "
                f"kind={row['kind'] or '?'} · enabled={row['enabled']} · "
                f"opacity={opacity}"
            )
        print(f"Statut : {report['status']}")
        print(f"Rapport : {report_path}")
        print("Le proxy read-only a refusé toute requête OBS non-Get*.")
        return 0 if report["status"] == "ok" else 2
    except Exception as exc:
        report["status"] = "error"
        report["error_type"] = _safe_error(exc)
        if readonly is not None:
            report["obs_requests"] = list(readonly.requests)
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_report(report_path, report)
        print(f"ERREUR : {_safe_error(exc)}")
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
