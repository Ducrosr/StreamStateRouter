from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import (
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


class SetupGuideDialog(QDialog):
    """Small branching guide that delegates mutations to MainWindow workflows."""

    def __init__(
        self,
        parent=None,
        *,
        foreground: ForegroundApp | None = None,
        obs_enabled: bool = False,
        obs_connected: bool = False,
    ):
        super().__init__(parent)
        self.setWindowTitle("Guide pas à pas — Configurer SSR")
        self.resize(760, 620)
        self._foreground = foreground
        self._obs_enabled = bool(obs_enabled)
        self._obs_connected = bool(obs_connected)
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
            "app": (
                "SSR va ouvrir l’assistant « Configurer l’application courante ».\n\n"
                "1. Lecture de l’état OBS\n"
                "2. Choix des domaines à gérer\n"
                "3. Création du brouillon\n"
                "4. Revue avant Enregistrer et appliquer"
            ),
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
