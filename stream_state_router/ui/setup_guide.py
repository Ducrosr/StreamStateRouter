from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..router.models import ForegroundApp
from ..widgets import HtmlModuleInspection, inspect_html_module


@dataclass(frozen=True, slots=True)
class SetupGuideResult:
    task: str
    html_entry: str = ""
    html_package_root: str = ""
    html_name: str = ""
    # (logical state key, mode, selected profile)
    # mode is inherit, capture or profile.
    app_customizations: tuple[tuple[str, str, str], ...] = ()


class SetupGuideDialog(QDialog):
    """Small branching guide that delegates mutations to MainWindow workflows."""

    def __init__(
        self,
        parent=None,
        *,
        foreground: ForegroundApp | None = None,
        obs_enabled: bool = False,
        obs_connected: bool = False,
        profile_choices: Mapping[str, Sequence[str]] | None = None,
        fallback_state: Mapping[str, object] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Guide pas à pas — Configurer SSR")
        self.resize(760, 620)
        self._foreground = foreground
        self._obs_enabled = bool(obs_enabled)
        self._obs_connected = bool(obs_connected)
        self._profile_choices = {
            str(domain): [
                str(name)
                for name in values
                if str(name).strip()
            ]
            for domain, values in (profile_choices or {}).items()
        }
        self._fallback_state = {
            str(key): str(value)
            for key, value in (fallback_state or {}).items()
            if str(value).strip()
        }
        self._inspection: HtmlModuleInspection | None = None

        root = QVBoxLayout(self)
        self.steps = QLabel("")
        self.steps.setObjectName("Muted")
        root.addWidget(self.steps)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_task_page())
        self.stack.addWidget(self._build_details_page())
        self.stack.addWidget(self._build_preview_page())
        root.addWidget(self.stack, 1)

        nav = QHBoxLayout()
        self.back_button = QPushButton("Retour")
        self.back_button.clicked.connect(self._back)
        nav.addWidget(self.back_button)
        nav.addStretch(1)
        cancel = QPushButton("Annuler")
        cancel.clicked.connect(self.reject)
        nav.addWidget(cancel)
        self.next_button = QPushButton("Suivant")
        self.next_button.setObjectName("Primary")
        self.next_button.clicked.connect(self._next)
        nav.addWidget(self.next_button)
        self.finish_button = QPushButton("Terminer")
        self.finish_button.setObjectName("Primary")
        self.finish_button.clicked.connect(self.accept)
        nav.addWidget(self.finish_button)
        root.addLayout(nav)

        self.task.currentIndexChanged.connect(self._sync_task_details)
        self.html_mode.currentIndexChanged.connect(self._sync_html_mode)
        self.stack.currentChanged.connect(self._sync_navigation)
        self._sync_task_details()
        self._sync_html_mode()
        self._sync_navigation()

    def _build_task_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)

        title = QLabel("1. Que voulez-vous faire ?")
        title.setStyleSheet("font-size: 16pt; font-weight: 700;")
        root.addWidget(title)

        text = QLabel(
            "Le guide choisit le bon parcours sans masquer les objets SSR. "
            "Tout ce qu’il crée reste ensuite éditable en mode Expert."
        )
        text.setWordWrap(True)
        text.setObjectName("Muted")
        root.addWidget(text)

        form = QFormLayout()
        self.task = QComboBox()
        self.task.addItem(
            "Configurer l’application actuellement au premier plan",
            "app",
        )
        self.task.addItem(
            "Importer un module HTML dans la bibliothèque SSR",
            "html",
        )
        self.task.addItem(
            "Analyser ou importer ma collection OBS",
            "collection",
        )
        self.task.addItem(
            "Réparer des références OBS devenues obsolètes",
            "repair",
        )
        form.addRow("Objectif", self.task)
        root.addLayout(form)

        self.task_explanation = QLabel("")
        self.task_explanation.setWordWrap(True)
        self.task_explanation.setObjectName("Muted")
        root.addWidget(self.task_explanation)
        root.addStretch(1)
        return page

    def _build_details_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)

        title = QLabel("2. Vérifier les informations")
        title.setStyleSheet("font-size: 16pt; font-weight: 700;")
        root.addWidget(title)

        self.context = QLabel("")
        self.context.setWordWrap(True)
        root.addWidget(self.context)

        self.app_panel = QWidget()
        app_form = QFormLayout(self.app_panel)
        self.app_customization_controls: dict[
            str,
            tuple[QCheckBox, QComboBox, str],
        ] = {}

        def add_app_option(
            state_key: str,
            label: str,
            domain: str,
            *,
            capture_allowed: bool = True,
        ) -> None:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)

            check = QCheckBox("Personnaliser")
            fallback = str(
                self._fallback_state.get(state_key) or ""
            ).strip()
            check.setToolTip(
                (
                    f"Décoché : hérite du preset global « {fallback} »."
                    if fallback
                    else "Décoché : hérite du preset global."
                )
            )
            row_layout.addWidget(check)

            choice = QComboBox()
            if capture_allowed:
                choice.addItem("Capturer l’état actuel", "capture")
            for profile_name in self._profile_choices.get(domain, []):
                choice.addItem(
                    f"Utiliser « {profile_name} »",
                    f"profile:{profile_name}",
                )
            if choice.count() == 0:
                choice.addItem("Aucun profil disponible", "")
            choice.setEnabled(False)
            check.toggled.connect(choice.setEnabled)
            row_layout.addWidget(choice, 1)

            fallback_hint = QLabel(
                f"Défaut : {fallback or '—'}"
            )
            fallback_hint.setObjectName("Muted")
            row_layout.addWidget(fallback_hint)
            app_form.addRow(label, row)
            self.app_customization_controls[state_key] = (
                check,
                choice,
                fallback,
            )

        add_app_option("Game", "Jeu / sources", "game")
        add_app_option(
            "OverlayProfile",
            "Overlay / visibilité",
            "overlay",
        )
        add_app_option(
            "CaptureProfile",
            "Capture / HDR-SDR",
            "capture",
        )
        add_app_option(
            "AudioProfile",
            "Audio / routage",
            "audio",
        )
        add_app_option(
            "LayoutProfile",
            "Disposition",
            "layout",
        )
        add_app_option(
            "PresentationProfile",
            "Présentation / widgets",
            "presentation",
            capture_allowed=False,
        )

        app_note = QLabel(
            "Une ligne décochée reste héritée du preset global. "
            "« Capturer » crée/actualise uniquement ce que vous choisissez. "
            "Un profil existant réutilise directement sa logique : par exemple "
            "CaptureProfile HDR/SDR ou AudioProfile de routage."
        )
        app_note.setWordWrap(True)
        app_note.setObjectName("Muted")
        app_form.addRow("", app_note)
        root.addWidget(self.app_panel)

        self.html_panel = QWidget()
        form = QFormLayout(self.html_panel)

        self.html_mode = QComboBox()
        self.html_mode.addItem(
            "HTML ciblé — copie le fichier et ses dépendances statiques",
            "html",
        )
        self.html_mode.addItem(
            "Dossier de module — conserve toute l’arborescence",
            "folder",
        )
        form.addRow("Mode d’import", self.html_mode)

        self.html_name = QLineEdit()
        self.html_name.setPlaceholderText(
            "ex. Loveless Chat, Shinra TV, Midgar Radio"
        )
        form.addRow("Nom du module", self.html_name)

        entry_row = QWidget()
        entry_lay = QHBoxLayout(entry_row)
        entry_lay.setContentsMargins(0, 0, 0, 0)
        self.html_entry = QLineEdit()
        self.html_entry.setPlaceholderText("Point d’entrée .html")
        entry_lay.addWidget(self.html_entry, 1)
        choose_entry = QPushButton("Choisir…")
        choose_entry.clicked.connect(self._choose_html_entry)
        entry_lay.addWidget(choose_entry)
        form.addRow("Fichier HTML", entry_row)

        self.folder_row = QWidget()
        folder_lay = QHBoxLayout(self.folder_row)
        folder_lay.setContentsMargins(0, 0, 0, 0)
        self.html_folder = QLineEdit()
        self.html_folder.setPlaceholderText("Dossier racine du module")
        folder_lay.addWidget(self.html_folder, 1)
        choose_folder = QPushButton("Choisir…")
        choose_folder.clicked.connect(self._choose_html_folder)
        folder_lay.addWidget(choose_folder)
        form.addRow("Dossier du module", self.folder_row)

        security = QLabel(
            "SSR n’exécute pas le HTML pendant l’import. Les URLs distantes "
            "sont seulement signalées ; aucun téléchargement réseau n’est "
            "effectué. Les références qui sortent du dossier choisi sont "
            "considérées non sûres."
        )
        security.setWordWrap(True)
        security.setObjectName("Muted")
        form.addRow("", security)

        root.addWidget(self.html_panel)
        root.addStretch(1)
        return page

    def _build_preview_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)

        title = QLabel("3. Aperçu avant de continuer")
        title.setStyleSheet("font-size: 16pt; font-weight: 700;")
        root.addWidget(title)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        root.addWidget(self.preview, 1)
        return page

    def _task_key(self) -> str:
        return str(self.task.currentData() or "")

    def _sync_task_details(self) -> None:
        task = self._task_key()
        explanations = {
            "app": (
                "SSR réutilisera l’assistant de capture de l’état courant. "
                "Il entrera en Mode édition avant toute modification du brouillon."
            ),
            "html": (
                "Le module sera copié dans une bibliothèque locale gérée par SSR. "
                "L’import ne crée pas encore de source OBS et ne lance aucun script."
            ),
            "collection": (
                "SSR commencera par une analyse en lecture seule de la collection OBS."
            ),
            "repair": (
                "SSR comparera les références de la configuration au catalogue OBS "
                "avant de proposer une réparation dans le brouillon."
            ),
        }
        self.task_explanation.setText(explanations.get(task, ""))
        self.app_panel.setVisible(task == "app")
        self.html_panel.setVisible(task == "html")
        self._update_context()

    def _sync_html_mode(self) -> None:
        self.folder_row.setVisible(
            str(self.html_mode.currentData()) == "folder"
        )

    def _update_context(self) -> None:
        task = self._task_key()
        obs_state = (
            "connecté"
            if self._obs_connected
            else "désactivé"
            if not self._obs_enabled
            else "déconnecté"
        )
        if task == "app":
            app = self._foreground
            if app is None:
                self.context.setText(
                    f"OBS : {obs_state}. Aucune application de premier plan "
                    "n’est actuellement connue de SSR."
                )
            else:
                detail = app.window_title or app.process_path or "fenêtre détectée"
                self.context.setText(
                    f"Application : {app.exe_name} — {detail}\nOBS : {obs_state}"
                )
        elif task in {"collection", "repair"}:
            self.context.setText(
                f"OBS : {obs_state}. Ce parcours nécessite une connexion OBS "
                "pour lire le catalogue."
            )
        else:
            self.context.setText(
                "Cet import est local et ne nécessite pas OBS."
            )

    def _choose_html_entry(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Choisir le point d’entrée HTML",
            self.html_entry.text().strip(),
            "HTML (*.html *.htm);;Tous les fichiers (*)",
        )
        if not selected:
            return
        self.html_entry.setText(selected)
        if not self.html_name.text().strip():
            self.html_name.setText(Path(selected).stem)
        if (
            str(self.html_mode.currentData()) == "folder"
            and not self.html_folder.text().strip()
        ):
            self.html_folder.setText(str(Path(selected).parent))

    def _choose_html_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choisir le dossier du module",
            self.html_folder.text().strip(),
        )
        if not selected:
            return
        self.html_folder.setText(selected)
        if not self.html_entry.text().strip():
            root = Path(selected)
            candidates = sorted(
                [
                    *root.glob("index.html"),
                    *root.glob("index.htm"),
                    *root.glob("*.html"),
                    *root.glob("*.htm"),
                ],
                key=lambda path: path.name.casefold(),
            )
            if candidates:
                self.html_entry.setText(str(candidates[0]))
                if not self.html_name.text().strip():
                    self.html_name.setText(root.name)

    def _app_customization_plan(
        self,
    ) -> tuple[tuple[str, str, str], ...]:
        result: list[tuple[str, str, str]] = []
        for state_key, (
            check,
            choice,
            _fallback,
        ) in self.app_customization_controls.items():
            if not check.isChecked():
                result.append((state_key, "inherit", ""))
                continue
            raw = str(choice.currentData() or "").strip()
            if raw == "capture":
                result.append((state_key, "capture", ""))
                continue
            if raw.startswith("profile:"):
                result.append(
                    (
                        state_key,
                        "profile",
                        raw.split(":", 1)[1],
                    )
                )
                continue
            result.append((state_key, "invalid", ""))
        return tuple(result)

    def _validate_details(self) -> bool:
        task = self._task_key()
        if task in {"app", "collection", "repair"}:
            if not self._obs_enabled:
                QMessageBox.warning(
                    self,
                    "Guide SSR",
                    "Activez d’abord « Piloter OBS » dans Paramètres.",
                )
                return False
            if not self._obs_connected:
                QMessageBox.warning(
                    self,
                    "Guide SSR",
                    "OBS doit être connecté pour ce parcours.",
                )
                return False
            if task == "app" and self._foreground is None:
                QMessageBox.warning(
                    self,
                    "Guide SSR",
                    "Placez l’application à configurer au premier plan puis recommencez.",
                )
                return False
            if task == "app":
                invalid = [
                    key
                    for key, mode, _value
                    in self._app_customization_plan()
                    if mode == "invalid"
                ]
                if invalid:
                    QMessageBox.warning(
                        self,
                        "Guide SSR",
                        (
                            "Aucun profil n’est disponible pour : "
                            + ", ".join(invalid)
                            + ". Décochez la ligne ou créez d’abord un profil."
                        ),
                    )
                    return False
            return True

        if task != "html":
            return True

        entry = self.html_entry.text().strip()
        if not entry:
            QMessageBox.warning(
                self,
                "Import HTML",
                "Choisissez un fichier HTML.",
            )
            return False
        package_root = None
        if str(self.html_mode.currentData()) == "folder":
            folder = self.html_folder.text().strip()
            if not folder:
                QMessageBox.warning(
                    self,
                    "Import HTML",
                    "Choisissez le dossier racine du module.",
                )
                return False
            package_root = folder
        try:
            self._inspection = inspect_html_module(
                entry,
                package_root=package_root,
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Import HTML",
                str(exc),
            )
            return False
        return True

    def _build_preview(self) -> None:
        task = self._task_key()
        if task == "html":
            inspection = self._inspection
            assert inspection is not None
            lines = [
                "IMPORT DE MODULE HTML",
                "",
                f"Nom : {self.html_name.text().strip() or inspection.entry.stem}",
                f"Point d’entrée : {inspection.entry.name}",
                f"Fichiers gérés : {len(inspection.local_files)}",
                f"Taille : {inspection.total_bytes / 1024:.1f} Kio",
                "",
            ]
            if inspection.warnings:
                lines.append("Avertissements :")
                lines.extend(f"• {item}" for item in inspection.warnings)
                lines.append("")
            if inspection.remote_references:
                lines.append("Références distantes conservées :")
                lines.extend(
                    f"• {item}"
                    for item in inspection.remote_references[:20]
                )
                lines.append("")
            if inspection.missing_references:
                lines.append("Références locales manquantes :")
                lines.extend(
                    f"• {item}"
                    for item in inspection.missing_references[:20]
                )
                lines.append("")
            if inspection.unsafe_references:
                lines.append("Références ignorées car non sûres :")
                lines.extend(
                    f"• {item}"
                    for item in inspection.unsafe_references[:20]
                )
                lines.append("")
            lines.append(
                "Terminer copiera uniquement les fichiers autorisés dans "
                "la bibliothèque SSR. Aucun HTML/JS ne sera exécuté."
            )
            self.preview.setPlainText("\n".join(lines))
            return

        previews = {
            "app": "",
            "collection": (
                "SSR va analyser la collection OBS en lecture seule.\n\n"
                "Aucune scène, source ou configuration ne sera modifiée "
                "pendant l’analyse."
            ),
            "repair": (
                "SSR va rechercher les références OBS devenues invalides.\n\n"
                "Les réparations proposées ne seront inscrites que dans "
                "le brouillon, après votre validation."
            ),
        }
        if task == "app":
            labels = {
                "Game": "Jeu / sources",
                "OverlayProfile": "Overlay / visibilité",
                "CaptureProfile": "Capture / HDR-SDR",
                "AudioProfile": "Audio / routage",
                "LayoutProfile": "Disposition",
                "PresentationProfile": "Présentation / widgets",
            }
            lines = [
                "PLAN DE PERSONNALISATION DE L’APPLICATION",
                "",
            ]
            for key, mode, value in self._app_customization_plan():
                fallback = str(
                    self._fallback_state.get(key) or "preset global"
                )
                if mode == "inherit":
                    description = f"Hériter de « {fallback} »"
                elif mode == "capture":
                    description = "Capturer l’état actuel"
                else:
                    description = f"Utiliser « {value} »"
                lines.append(
                    f"• {labels.get(key, key)} : {description}"
                )
            lines.extend(
                [
                    "",
                    "Le brouillon restera modifiable avant application.",
                    "Les lignes héritées ne dupliquent aucun réglage.",
                ]
            )
            self.preview.setPlainText("\n".join(lines))
            return
        self.preview.setPlainText(previews.get(task, ""))

    def _back(self) -> None:
        index = self.stack.currentIndex()
        if index > 0:
            self.stack.setCurrentIndex(index - 1)

    def _next(self) -> None:
        index = self.stack.currentIndex()
        if index == 0:
            self.stack.setCurrentIndex(1)
            return
        if index == 1:
            if not self._validate_details():
                return
            self._build_preview()
            self.stack.setCurrentIndex(2)

    def _sync_navigation(self) -> None:
        index = self.stack.currentIndex()
        self.steps.setText(f"Étape {index + 1} sur 3")
        self.back_button.setEnabled(index > 0)
        self.next_button.setVisible(index < 2)
        self.finish_button.setVisible(index == 2)

    def result_value(self) -> SetupGuideResult:
        task = self._task_key()
        if task == "app":
            return SetupGuideResult(
                task=task,
                app_customizations=self._app_customization_plan(),
            )
        if task != "html":
            return SetupGuideResult(task=task)
        return SetupGuideResult(
            task=task,
            html_entry=self.html_entry.text().strip(),
            html_package_root=(
                self.html_folder.text().strip()
                if str(self.html_mode.currentData()) == "folder"
                else ""
            ),
            html_name=self.html_name.text().strip(),
        )


@dataclass(frozen=True, slots=True)
class WidgetObsInstallResult:
    input_name: str
    scene: str
    component: str
    width: int
    height: int
    shutdown_when_not_visible: bool
    restart_when_active: bool


class WidgetObsInstallDialog(QDialog):
    def __init__(
        self,
        parent=None,
        *,
        module_name: str,
        default_scene: str = "",
    ):
        super().__init__(parent)
        self.setWindowTitle("Créer le module dans OBS")
        self.resize(560, 390)

        root = QVBoxLayout(self)
        title = QLabel(f"Créer « {module_name} » comme Browser Source")
        title.setStyleSheet("font-size: 15pt; font-weight: 700;")
        root.addWidget(title)

        note = QLabel(
            "La création passe par le worker SSR. Laissez la scène vide "
            "pour utiliser la scène programme courante."
        )
        note.setWordWrap(True)
        note.setObjectName("Muted")
        root.addWidget(note)

        form = QFormLayout()
        self.input_name = QLineEdit(f"[SSR] {module_name}")
        form.addRow("Nom dans OBS", self.input_name)

        self.scene = QLineEdit(default_scene)
        self.scene.setPlaceholderText(
            "Vide = scène programme courante"
        )
        form.addRow("Scène OBS", self.scene)

        self.component = QLineEdit()
        self.component.setPlaceholderText(
            "Optionnel, ex. chat / events / radio"
        )
        form.addRow("Composant SSR", self.component)

        size_row = QWidget()
        size_lay = QHBoxLayout(size_row)
        size_lay.setContentsMargins(0, 0, 0, 0)
        self.width = QSpinBox()
        self.width.setRange(16, 8192)
        self.width.setValue(800)
        self.height = QSpinBox()
        self.height.setRange(16, 8192)
        self.height.setValue(600)
        size_lay.addWidget(QLabel("L"))
        size_lay.addWidget(self.width)
        size_lay.addWidget(QLabel("H"))
        size_lay.addWidget(self.height)
        size_lay.addStretch(1)
        form.addRow("Taille navigateur", size_row)

        self.shutdown = QCheckBox(
            "Fermer la page quand la source n’est pas visible"
        )
        form.addRow("", self.shutdown)

        self.restart = QCheckBox(
            "Recharger la page quand la source devient active"
        )
        form.addRow("", self.restart)

        root.addLayout(form)

        warning = QLabel(
            "Cette opération modifie OBS immédiatement et est protégée "
            "par Safe Live lorsqu’un stream ou un enregistrement est actif."
        )
        warning.setWordWrap(True)
        warning.setObjectName("Warn")
        root.addWidget(warning)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("Annuler")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        create = QPushButton("Créer dans OBS")
        create.setObjectName("LiveAction")
        create.clicked.connect(self._accept_checked)
        actions.addWidget(create)
        root.addLayout(actions)

    def _accept_checked(self) -> None:
        if not self.input_name.text().strip():
            QMessageBox.warning(
                self,
                "Créer le module dans OBS",
                "Le nom de la source OBS est requis.",
            )
            return
        self.accept()

    def result_value(self) -> WidgetObsInstallResult:
        return WidgetObsInstallResult(
            input_name=self.input_name.text().strip(),
            scene=self.scene.text().strip(),
            component=self.component.text().strip(),
            width=self.width.value(),
            height=self.height.value(),
            shutdown_when_not_visible=self.shutdown.isChecked(),
            restart_when_active=self.restart.isChecked(),
        )
