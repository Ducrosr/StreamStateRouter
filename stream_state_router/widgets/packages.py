from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
from typing import Iterable
from urllib.parse import unquote, urlsplit

from ..services.paths import imported_widgets_dir


_MAX_FILES = 1000
_MAX_BYTES = 100 * 1024 * 1024
_SKIPPED_DIRS = {".git", "node_modules", "__pycache__"}
_CSS_URL_RE = re.compile(
    r"url\(\s*(['\"]?)(?P<value>[^)'\"]+)\1\s*\)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class HtmlModuleInspection:
    entry: Path
    package_root: Path
    local_files: tuple[Path, ...]
    missing_references: tuple[str, ...]
    remote_references: tuple[str, ...]
    unsafe_references: tuple[str, ...]
    warnings: tuple[str, ...]
    total_bytes: int


@dataclass(frozen=True, slots=True)
class WidgetPackage:
    package_id: str
    name: str
    root: Path
    entry: Path
    manifest: Path
    file_count: int
    total_bytes: int
    warnings: tuple[str, ...]
    remote_references: tuple[str, ...]

    @property
    def entry_uri(self) -> str:
        return self.entry.resolve().as_uri()


class _ReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = {key.casefold(): value for key, value in attrs}
        tag_name = tag.casefold()
        if tag_name in {
            "script",
            "img",
            "source",
            "video",
            "audio",
            "iframe",
            "embed",
            "track",
            "input",
        }:
            value = values.get("src")
            if value:
                self.references.append(value)
        if tag_name in {"link", "a"}:
            value = values.get("href")
            if value:
                self.references.append(value)
        style = values.get("style")
        if style:
            self.references.extend(_css_references(style))


def _css_references(text: str) -> list[str]:
    return [
        match.group("value").strip()
        for match in _CSS_URL_RE.finditer(text)
        if match.group("value").strip()
    ]


def _slugify(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return text.casefold() or "module"


def _reference_kind(raw: str) -> tuple[str, str]:
    value = str(raw or "").strip()
    if not value or value.startswith("#"):
        return ("ignore", "")
    parts = urlsplit(value)
    scheme = parts.scheme.casefold()
    if scheme in {"http", "https", "ws", "wss"}:
        return ("remote", value)
    if scheme in {"data", "blob", "about"}:
        return ("ignore", value)
    if scheme in {"file", "javascript"}:
        return ("unsafe", value)
    if scheme:
        return ("remote", value)
    path = unquote(parts.path or "")
    if not path:
        return ("ignore", value)
    if path.startswith(("/", "\\")):
        return ("unsafe", value)
    return ("local", path)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _references_for_file(path: Path) -> list[str]:
    suffix = path.suffix.casefold()
    if suffix in {".html", ".htm"}:
        parser = _ReferenceParser()
        parser.feed(_read_text(path))
        return parser.references
    if suffix == ".css":
        return _css_references(_read_text(path))
    return []


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _walk_package_root(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if any(part in _SKIPPED_DIRS for part in path.parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        yield path


def inspect_html_module(
    entry: str | Path,
    *,
    package_root: str | Path | None = None,
) -> HtmlModuleInspection:
    entry_path = Path(entry).expanduser().resolve()
    if not entry_path.is_file():
        raise ValueError(f"Fichier HTML introuvable : {entry_path}")
    if entry_path.suffix.casefold() not in {".html", ".htm"}:
        raise ValueError("Le point d’entrée doit être un fichier .html ou .htm")

    root = (
        Path(package_root).expanduser().resolve()
        if package_root is not None
        else entry_path.parent
    )
    if not root.is_dir():
        raise ValueError(f"Dossier de module introuvable : {root}")
    if not _inside(root, entry_path):
        raise ValueError("Le fichier HTML doit se trouver dans le dossier du module")

    local_files: set[Path] = {entry_path}
    missing: set[str] = set()
    remote: set[str] = set()
    unsafe: set[str] = set()

    if package_root is not None:
        local_files.update(_walk_package_root(root))
    else:
        pending = [entry_path]
        visited: set[Path] = set()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            for raw in _references_for_file(current):
                kind, value = _reference_kind(raw)
                if kind == "remote":
                    remote.add(value)
                    continue
                if kind == "unsafe":
                    unsafe.add(value)
                    continue
                if kind != "local":
                    continue
                candidate = (current.parent / value).resolve()
                if not _inside(root, candidate):
                    unsafe.add(raw)
                    continue
                if not candidate.is_file():
                    missing.add(raw)
                    continue
                if candidate not in local_files:
                    local_files.add(candidate)
                    if candidate.suffix.casefold() in {
                        ".html",
                        ".htm",
                        ".css",
                    }:
                        pending.append(candidate)

    files = sorted(local_files)
    if len(files) > _MAX_FILES:
        raise ValueError(
            f"Le module contient plus de {_MAX_FILES} fichiers gérés"
        )
    total_bytes = sum(path.stat().st_size for path in files)
    if total_bytes > _MAX_BYTES:
        raise ValueError(
            "Le module dépasse la limite d’import de 100 Mio"
        )

    warnings: list[str] = []
    if missing:
        warnings.append(
            f"{len(missing)} référence(s) locale(s) introuvable(s)"
        )
    if remote:
        warnings.append(
            f"{len(remote)} dépendance(s) distante(s) resteront externes"
        )
    if unsafe:
        warnings.append(
            f"{len(unsafe)} référence(s) absolue(s) ou non sûre(s) ignorée(s)"
        )
    if package_root is None:
        warnings.append(
            "Import HTML ciblé : les imports JS dynamiques ne peuvent pas "
            "être découverts automatiquement. Utilisez l’import de dossier "
            "pour un module complexe."
        )

    return HtmlModuleInspection(
        entry=entry_path,
        package_root=root,
        local_files=tuple(files),
        missing_references=tuple(sorted(missing)),
        remote_references=tuple(sorted(remote)),
        unsafe_references=tuple(sorted(unsafe)),
        warnings=tuple(warnings),
        total_bytes=total_bytes,
    )


def _allocate_target(root: Path, package_id: str) -> tuple[str, Path]:
    candidate_id = package_id
    suffix = 2
    while (root / candidate_id).exists():
        candidate_id = f"{package_id}-{suffix}"
        suffix += 1
    return candidate_id, root / candidate_id


def import_html_module(
    entry: str | Path,
    *,
    name: str = "",
    package_root: str | Path | None = None,
    target_root: str | Path | None = None,
) -> WidgetPackage:
    inspection = inspect_html_module(
        entry,
        package_root=package_root,
    )
    library_root = (
        Path(target_root).expanduser().resolve()
        if target_root is not None
        else imported_widgets_dir()
    )
    library_root.mkdir(parents=True, exist_ok=True)

    display_name = str(name or inspection.entry.stem).strip()
    package_id, destination = _allocate_target(
        library_root,
        _slugify(display_name),
    )
    destination.mkdir(parents=True, exist_ok=False)

    copied = 0
    try:
        for source in inspection.local_files:
            relative = source.relative_to(inspection.package_root)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied += 1

        entry_relative = inspection.entry.relative_to(
            inspection.package_root
        )
        imported_entry = destination / entry_relative
        manifest = destination / "manifest.json"
        payload = {
            "schema_version": 1,
            "package_id": package_id,
            "name": display_name,
            "entry": entry_relative.as_posix(),
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "source_mode": (
                "folder" if package_root is not None else "html"
            ),
            "file_count": copied,
            "total_bytes": inspection.total_bytes,
            "warnings": list(inspection.warnings),
            "missing_references": list(
                inspection.missing_references
            ),
            "remote_references": list(
                inspection.remote_references
            ),
            "unsafe_references": list(
                inspection.unsafe_references
            ),
        }
        manifest.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise

    return WidgetPackage(
        package_id=package_id,
        name=display_name,
        root=destination,
        entry=imported_entry,
        manifest=manifest,
        file_count=copied,
        total_bytes=inspection.total_bytes,
        warnings=inspection.warnings,
        remote_references=inspection.remote_references,
    )


def list_widget_packages(
    *,
    root: str | Path | None = None,
) -> tuple[WidgetPackage, ...]:
    library_root = (
        Path(root).expanduser().resolve()
        if root is not None
        else imported_widgets_dir()
    )
    if not library_root.exists():
        return ()

    found: list[WidgetPackage] = []
    for manifest in sorted(library_root.glob("*/manifest.json")):
        try:
            payload = json.loads(
                manifest.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        package_root = manifest.parent
        entry = package_root / str(payload.get("entry") or "")
        if not entry.is_file() or not _inside(package_root, entry):
            continue
        found.append(
            WidgetPackage(
                package_id=str(payload.get("package_id") or package_root.name),
                name=str(payload.get("name") or package_root.name),
                root=package_root,
                entry=entry,
                manifest=manifest,
                file_count=int(payload.get("file_count") or 0),
                total_bytes=int(payload.get("total_bytes") or 0),
                warnings=tuple(
                    str(item)
                    for item in payload.get("warnings", [])
                    if str(item)
                ),
                remote_references=tuple(
                    str(item)
                    for item in payload.get("remote_references", [])
                    if str(item)
                ),
            )
        )
    return tuple(found)
