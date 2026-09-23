from __future__ import annotations

import copy
import json
import os
import time
from typing import Mapping
from PySide6.QtCore import QObject, Qt, Signal, QTimer, QSettings
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QHeaderView,
    QMenu,
    QPlainTextEdit,
    QScrollArea,
)

from .. import __version__
from ..activation import TriggerTargetIdentity
from ..importers import (
    AdvancedSceneSwitcherImporter,
    CurrentStateCaptureOptions,
    SceneCollectionImporter,
    build_current_state_capture_draft,
    find_process_rules,
    suggest_capture_name,
    neutralize_referenced_test_layout_profiles,
    wire_windows_hdr_capture_profiles,
)
from ..obs.client import OBSClientManager
from ..obs.dispatcher import PROFILE_DOMAINS, STATE_DOMAINS, OBSDispatcher
from ..obs.layouts import OBSLayoutManager, anchor_factors, compact_layout_overrides, diff_layout_profiles, resolve_layout_profile
from ..router.engine import StateChange, StateRouterEngine
from ..router.models import ForegroundApp, StreamState
from ..services.config import (
    build_activation_policies,
    config_revision,
    build_obs_config,
    build_host_controller,
    build_profiles,
    build_layout_profiles,
    build_ruleset,
    export_config,
    import_config,
    latest_valid_backup,
    save_config,
    validate_config,
    push_layout_history,
    pop_layout_history,
    release_runtime_visibility_ownership,
)
from ..services.control_variables import ControlVariableStore
from ..services.runtime import RoutingService, RuntimeEvent
from ..services.api import APIConfig, LocalControlAPI
from ..services.startup import is_startup_enabled, set_startup_enabled
from .dialogs import (
    ActionDialog,
    CollectionImportDialog,
    CurrentStateCaptureDialog,
    CollectionLogicImportDialog,
    ModuleLayoutDialog,
    RuleDialog,
)
from .presentation import (
    UserActivityEntry,
    build_automation_rows,
    build_dashboard_snapshot,
    build_diagnostic_report,
    build_simulation_report,
    user_activity_from_runtime_event,
)


DOMAIN_LABELS = {
    "game": "Game",
    "overlay": "OverlayProfile",
    "capture": "CaptureProfile",
    "audio": "AudioProfile",
    "layout": "LayoutProfile",
}


class RuntimeBridge(QObject):
    foreground = Signal(object)
    state_change = Signal(object)
    dispatch = Signal(object)
    runtime_event = Signal(object)
    activation_result = Signal(object)


class MainWindow(QMainWindow):
    def __init__(
        self,
        config: dict,
        *,
        logger,
        start_minimized: bool = False,
        runtime_marker=None,
    ):
        super().__init__()
        self.setWindowTitle(f"Stream State Router {__version__}")
        self.setMinimumSize(760, 520)
        self.resize(1180, 760)
        self._window_settings = QSettings("Ducrosr", "StreamStateRouter")
        saved_geometry = self._window_settings.value("main_window/geometry")
        if saved_geometry is not None:
            self.restoreGeometry(saved_geometry)
        self._expert_mode = bool(
            self._window_settings.value("main_window/expert_mode", False, type=bool)
        )
        self.config = copy.deepcopy(config)
        self._saved_revision = config_revision(self.config)
        self._applied_revision = ""
        self._draft_dirty = False
        self.logger = logger
        self.start_minimized = start_minimized
        self._runtime_marker = runtime_marker
        self._pending_cleanup_transfer = tuple(
            getattr(runtime_marker, "previous_pending_cleanup", ()) or ()
        )
        self._service: RoutingService | None = None
        self._dispatcher: OBSDispatcher | None = None
        self._client: OBSClientManager | None = None
        self._obs_module_catalog: dict[str, list] = {}
        self._layout_sync_manager: OBSLayoutManager | None = None
        self._catalog_tree_guard = False
        self._quitting = False
        self._api: LocalControlAPI | None = None
        self._known_catalog_sources: set[str] = set()
        self._preview_active = False
        self._routing_incomplete = False
        self._runtime_restart_in_progress = False
        self._pending_collection_imports: dict[str, dict[str, object]] = {}
        self._user_activity_history: list[tuple[str, UserActivityEntry]] = []

        self.bridge = RuntimeBridge()
        self.bridge.foreground.connect(self._on_foreground)
        self.bridge.state_change.connect(self._on_state_change)
        self.bridge.dispatch.connect(self._on_dispatch)
        self.bridge.runtime_event.connect(self._on_runtime_event)

        self._build_ui()
        self._apply_ui_mode()
        self._build_menu()
        self._build_tray()
        self._load_config_into_ui()
        self._wire_dirty_signals()
        self._start_runtime()
        self._start_api()
        self._module_scan_timer = QTimer(self)
        self._module_scan_timer.timeout.connect(self._auto_scan_modules)
        self._configure_module_scan_timer()
        self._refresh_dashboard_summary()

        if start_minimized and self.tray.isVisible():
            self.hide()

    # ---------- UI construction ----------
    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(12)
        self.setCentralWidget(root)

        top = QHBoxLayout()
        title = QLabel("Stream State Router")
        title.setObjectName("Title")
        top.addWidget(title)
        top.addStretch(1)
        self.obs_status = QLabel("OBS : —")
        self.obs_status.setObjectName("Muted")
        top.addWidget(self.obs_status)
        self.mode_button = QPushButton()
        self.mode_button.clicked.connect(self._toggle_ui_mode)
        top.addWidget(self.mode_button)
        self.pause_button = QPushButton("Suspendre")
        self.pause_button.clicked.connect(self._toggle_pause)
        top.addWidget(self.pause_button)
        layout.addLayout(top)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.dashboard_tab_index = self.tabs.addTab(
            self._scrollable_tab(self._build_dashboard()), "Dashboard"
        )
        self.automations_tab_index = self.tabs.addTab(
            self._scrollable_tab(self._build_automations_tab()), "Automatisations"
        )
        self.rules_tab_index = self.tabs.addTab(
            self._scrollable_tab(self._build_rules_tab()), "Règles"
        )
        self.profiles_tab_index = self.tabs.addTab(
            self._scrollable_tab(self._build_profiles_tab()), "Profils"
        )
        self.layouts_tab_index = self.tabs.addTab(
            self._scrollable_tab(self._build_layouts_tab()), "Layouts"
        )
        self.settings_tab_index = self.tabs.addTab(
            self._scrollable_tab(self._build_settings_tab()), "Paramètres"
        )
        self.logs_tab_index = self.tabs.addTab(
            self._scrollable_tab(self._build_logs_tab()), "Journal"
        )

        footer = QHBoxLayout()
        self.unsaved = QLabel("")
        self.unsaved.setObjectName("Warn")
        footer.addWidget(self.unsaved)
        footer.addStretch(1)
        self.save_button = QPushButton("Enregistrer et appliquer")
        self.save_button.setObjectName("Primary")
        self.save_button.clicked.connect(self.save_and_apply)
        footer.addWidget(self.save_button)
        layout.addLayout(footer)

    def _scrollable_tab(self, page: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        scroll.setWidget(page)
        return scroll

    def _save_window_geometry(self) -> None:
        self._window_settings.setValue(
            "main_window/geometry",
            self.saveGeometry(),
        )
        self._window_settings.sync()

    def _toggle_ui_mode(self) -> None:
        self._expert_mode = not self._expert_mode
        self._window_settings.setValue(
            "main_window/expert_mode",
            self._expert_mode,
        )
        self._window_settings.sync()
        self._apply_ui_mode()

    def _apply_ui_mode(self) -> None:
        if not hasattr(self, "tabs"):
            return
        expert_only = (
            self.rules_tab_index,
            self.profiles_tab_index,
            self.layouts_tab_index,
            self.logs_tab_index,
        )
        for index in expert_only:
            self.tabs.setTabVisible(index, self._expert_mode)
        for widget_name in ("state_card", "override_card"):
            widget = getattr(self, widget_name, None)
            if widget is not None:
                widget.setVisible(self._expert_mode)
        self.tabs.setTabVisible(self.dashboard_tab_index, True)
        self.tabs.setTabVisible(self.automations_tab_index, True)
        self.tabs.setTabVisible(self.settings_tab_index, True)
        self.mode_button.setText(
            "Mode simple" if self._expert_mode else "Mode expert"
        )
        self.mode_button.setToolTip(
            "Masquer les écrans techniques."
            if self._expert_mode
            else "Afficher les règles, profils, layouts et le journal technique."
        )
        if not self._expert_mode and self.tabs.currentIndex() in expert_only:
            self.tabs.setCurrentIndex(self.dashboard_tab_index)
        if hasattr(self, "unsaved"):
            self._refresh_config_revision_status()

    def _refresh_dashboard_summary(self) -> None:
        if not hasattr(self, "dashboard_health"):
            return
        service = self._service
        client = self._client
        if service is None:
            self.dashboard_health.setText("Runtime indisponible")
            self.dashboard_health.setObjectName("Bad")
            self.dashboard_decision.setText("Décision : —")
            self.dashboard_reason.setText("Pourquoi : le moteur de routage n’est pas actif.")
            self.dashboard_diff.clear()
            return

        try:
            explanation = service.explain_decision(app)
            routing = (
                explanation.get("routing", {})
                if isinstance(explanation, Mapping)
                else {}
            )
            routing_kind = (
                str(routing.get("kind") or "").strip().casefold()
                if isinstance(routing, Mapping)
                else ""
            )
            routing_rule = (
                str(routing.get("rule_name") or "").strip()
                if isinstance(routing, Mapping)
                else ""
            )
            if routing_kind == "ignore":
                QMessageBox.warning(
                    self,
                    "Capture de l’état actuel",
                    (
                        "L’application courante est actuellement ignorée par "
                        f"la règle « {routing_rule or 'IGNORE'} ».\n\n"
                        "L’assistant Simple ne remplace jamais une règle IGNORE. "
                        "Modifiez d’abord cette règle en mode Expert."
                    ),
                )
                return

            exact_matches = find_process_rules(self.config, process)
            if routing_kind == "match" and routing_rule and not exact_matches:
                raw_rules = self.config.get("rules")
                configured_rules = (
                    raw_rules if isinstance(raw_rules, list) else []
                )
                active_rule = next(
                    (
                        rule
                        for rule in configured_rules
                        if isinstance(rule, Mapping)
                        and str(rule.get("name") or "").strip() == routing_rule
                    ),
                    None,
                )
                if isinstance(active_rule, Mapping):
                    QMessageBox.warning(
                        self,
                        "Capture de l’état actuel",
                        (
                            "Cette application est déjà pilotée par la règle "
                            f"« {routing_rule} », mais cette règle utilise des "
                            "sélecteurs avancés plutôt qu’un processus exact.\n\n"
                            "Pour éviter de créer une règle concurrente, "
                            "modifiez cette configuration en mode Expert."
                        ),
                    )
                    return

            logical_state = (
                dict(routing.get("effective_state") or {})
                if isinstance(routing, Mapping)
                and isinstance(routing.get("effective_state"), Mapping)
                else {}
            )
            request_id = service.request_collection_import_preview(
                include_layouts=True,
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Capture de l’état actuel",
                str(exc),
            )
            return

        self._pending_collection_imports[request_id] = {
            "mode": "guided_current_state_capture",
            "app": {
                "process": process,
                "path": str(app.process_path or ""),
                "title": str(app.window_title or ""),
            },
            "logical_state": logical_state,
        }
        self._record_user_activity(
            UserActivityEntry(
                "Muted",
                "Capture de l’état actuel démarrée",
                process,
            )
        )
        self.statusBar().showMessage(
            "Lecture de la scène OBS courante…",
            8000,
        )

    def _complete_guided_current_state_capture(
        self,
        *,
        snapshot,
        raw_result: Mapping[str, object],
        context: Mapping[str, object],
    ) -> None:
        raw_app = context.get("app")
        app = dict(raw_app) if isinstance(raw_app, Mapping) else {}
        process = str(app.get("process") or "").strip()
        process_path = str(app.get("path") or "").strip()
        window_title = str(app.get("title") or "").strip()
        if not process:
            QMessageBox.critical(
                self,
                "Capture de l’état actuel",
                "Le processus capturé n’est plus disponible.",
            )
            return

        matches = find_process_rules(self.config, process)
        base_name = os.path.splitext(os.path.basename(process))[0].strip()
        suggested_name = (
            str(matches[0].get("name") or "").strip()
            if matches
            else suggest_capture_name(self.config, base_name)
        )
        dialog = CurrentStateCaptureDialog(
            self,
            process=process,
            process_path=process_path,
            window_title=window_title,
            current_scene=str(snapshot.current_program_scene or ""),
            suggested_name=suggested_name,
            matching_rules=matches,
        )
        if dialog.exec() != QDialog.Accepted:
            self._record_user_activity(
                UserActivityEntry(
                    "Muted",
                    "Capture de l’état actuel annulée",
                    process,
                )
            )
            return

        raw_options = dialog.options()
        options = CurrentStateCaptureOptions(
            name=str(raw_options.get("name") or "").strip(),
            process=str(raw_options.get("process") or "").strip(),
            existing_rule_name=str(
                raw_options.get("existing_rule_name") or ""
            ).strip(),
            include_input_settings=bool(
                raw_options.get("include_input_settings", True)
            ),
            include_audio_state=bool(
                raw_options.get("include_audio_state", True)
            ),
            include_filters=bool(
                raw_options.get("include_filters", True)
            ),
            include_visibility=bool(
                raw_options.get("include_visibility", True)
            ),
            include_layout=bool(
                raw_options.get("include_layout", True)
            ),
        )
        raw_layouts = raw_result.get("layouts")
        layouts = raw_layouts if isinstance(raw_layouts, Mapping) else {}
        logical_raw = context.get("logical_state")
        logical_state = (
            dict(logical_raw)
            if isinstance(logical_raw, Mapping)
            else {}
        )

        try:
            draft = build_current_state_capture_draft(
                self.config,
                snapshot=snapshot,
                raw_layouts=layouts,
                logical_state=logical_state,
                options=options,
            )
            errors = validate_config(draft.config)
            if errors:
                raise ValueError(
                    "Le brouillon généré n’est pas valide :\n- "
                    + "\n- ".join(errors)
                )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Capture de l’état actuel",
                str(exc),
            )
            return

        if not self._confirm_current_state_capture_draft(draft):
            self._record_user_activity(
                UserActivityEntry(
                    "Muted",
                    "Brouillon de capture refusé",
                    process,
                )
            )
            return

        self.config = copy.deepcopy(draft.config)
        self._load_config_into_ui()
        self._mark_dirty()
        self._refresh_dashboard_summary()
        self._record_user_activity(
            UserActivityEntry(
                "Good",
                "Brouillon créé depuis l’état actuel",
                draft.report.rule_name,
            )
        )
        self.statusBar().showMessage(
            (
                "Brouillon prêt — vérifiez-le puis utilisez "
                "« Enregistrer et appliquer »"
            ),
            10000,
        )

    def _confirm_current_state_capture_draft(self, draft) -> bool:
        report = draft.report
        dialog = QDialog(self)
        dialog.setWindowTitle("Aperçu du brouillon")
        dialog.resize(760, 560)
        root = QVBoxLayout(dialog)

        title = QLabel(
            (
                f"Mettre à jour « {report.rule_name} »"
                if report.mode == "update"
                else f"Créer « {report.rule_name} »"
            )
        )
        title.setStyleSheet("font-size: 15pt; font-weight: 700;")
        root.addWidget(title)

        automation = next(
            (
                row
                for row in build_automation_rows(draft.config)
                if row.name == report.rule_name
            ),
            None,
        )
        if automation is not None:
            sentence = QLabel(
                f"{automation.trigger}\n→ {automation.result}"
            )
            sentence.setWordWrap(True)
            root.addWidget(sentence)

        summary = QTreeWidget()
        summary.setColumnCount(2)
        summary.setHeaderLabels(["Modification", "Valeur"])
        summary.setRootIsDecorated(False)
        summary.setAlternatingRowColors(True)
        rows = [
            ("Processus détecté", report.process),
            ("Scène OBS", report.scene or "—"),
            ("GameProfile", report.game_profile),
            (
                "Actions GameProfile",
                (
                    f"{report.added_actions} ajoutée(s), "
                    f"{report.replaced_actions} remplacée(s)"
                ),
            ),
            (
                "Sources de la scène",
                str(report.captured_inputs),
            ),
            (
                "Filtres de la scène",
                str(report.captured_filters),
            ),
            (
                "Scene Items",
                str(report.captured_scene_items),
            ),
            (
                "LayoutProfile",
                (
                    report.layout_profile
                    if report.layout_captured
                    else "inchangé"
                ),
            ),
        ]
        for label, value in rows:
            summary.addTopLevelItem(
                QTreeWidgetItem([str(label), str(value)])
            )
        summary.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        summary.header().setStretchLastSection(True)
        root.addWidget(summary, 1)

        if report.notes:
            notes = QLabel("\n".join(f"• {item}" for item in report.notes))
            notes.setWordWrap(True)
            notes.setObjectName("Muted")
            root.addWidget(notes)

        if report.warnings:
            warnings_title = QLabel(
                f"Avertissements ({len(report.warnings)})"
            )
            warnings_title.setObjectName("Warn")
            root.addWidget(warnings_title)
            warnings = QPlainTextEdit()
            warnings.setReadOnly(True)
            warnings.setMaximumHeight(120)
            warnings.setPlainText(
                "\n".join(f"• {item}" for item in report.warnings)
            )
            root.addWidget(warnings)

        note = QLabel(
            "« Créer le brouillon » modifie uniquement la configuration ouverte "
            "dans SSR. OBS et le runtime courant restent inchangés jusqu’à "
            "« Enregistrer et appliquer »."
        )
        note.setWordWrap(True)
        note.setObjectName("Muted")
        root.addWidget(note)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("Annuler")
        cancel.clicked.connect(dialog.reject)
        actions.addWidget(cancel)
        accept = QPushButton("Créer le brouillon")
        accept.setObjectName("Primary")
        accept.clicked.connect(dialog.accept)
        actions.addWidget(accept)
        root.addLayout(actions)

        return dialog.exec() == QDialog.Accepted

    def _guided_analyze_collection(self) -> None:
        if self._service is None:
            QMessageBox.warning(
                self,
                "Analyse collection OBS",
                "Le runtime SSR n’est pas disponible.",
            )
            return
        try:
            request_id = self._service.request_collection_import_preview(
                include_layouts=True,
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Analyse collection OBS",
                str(exc),
            )
            return

        self._pending_collection_imports[request_id] = {
            "mode": "guided_analysis",
            "domain": "",
            "profile_name": "",
            "options": {},
        }
        self._record_user_activity(
            UserActivityEntry("Muted", "Analyse de la collection OBS démarrée")
        )
        self.statusBar().showMessage(
            "Analyse de la collection OBS en cours…",
            8000,
        )

    def _show_guided_collection_analysis(
        self,
        snapshot,
        raw_result: Mapping[str, object],
    ) -> None:
        raw_layouts = raw_result.get("layouts")
        layout_count = len(raw_layouts) if isinstance(raw_layouts, Mapping) else 0
        raw_skipped = raw_result.get("layout_skipped")
        skipped_layouts = (
            len(raw_skipped)
            if isinstance(raw_skipped, list)
            else 0
        )

        dialog = QDialog(self)
        dialog.setWindowTitle("Analyse de la collection OBS")
        dialog.resize(760, 500)
        root = QVBoxLayout(dialog)

        title = QLabel(
            f"Collection : {snapshot.collection or '—'}"
        )
        title.setStyleSheet("font-size: 15pt; font-weight: 700;")
        root.addWidget(title)

        scene = QLabel(
            "Scène programme actuelle : "
            f"{snapshot.current_program_scene or '—'}"
        )
        scene.setObjectName("Muted")
        root.addWidget(scene)

        summary = QTreeWidget()
        summary.setColumnCount(2)
        summary.setHeaderLabels(["Élément détecté", "Quantité"])
        summary.setRootIsDecorated(False)
        summary.setAlternatingRowColors(True)
        rows = [
            ("Scènes OBS", len(snapshot.scenes)),
            ("Inputs / sources", len(snapshot.inputs)),
            ("Filtres", len(snapshot.filters)),
            ("Scene Items", len(snapshot.scene_items)),
            ("Layouts importables", layout_count),
            ("Layouts ignorés par sécurité", skipped_layouts),
            ("Avertissements", len(snapshot.warnings)),
        ]
        for label, value in rows:
            summary.addTopLevelItem(
                QTreeWidgetItem([label, str(value)])
            )
        summary.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        summary.header().setStretchLastSection(True)
        root.addWidget(summary, 1)

        note = QLabel(
            "Cette analyse n’a modifié ni OBS ni la configuration SSR. "
            "La migration automatique reste conservatrice : une macro ou un "
            "élément ambigu n’est jamais approximé."
        )
        note.setWordWrap(True)
        note.setObjectName("Muted")
        root.addWidget(note)

        if snapshot.warnings:
            warnings = QPlainTextEdit()
            warnings.setReadOnly(True)
            warnings.setMaximumHeight(110)
            warnings.setPlainText(
                "\n".join(f"• {item}" for item in snapshot.warnings)
            )
            root.addWidget(warnings)

        actions = QHBoxLayout()
        migrate = QPushButton("Migrer ce qui est sûr…")
        migrate.setObjectName("Primary")
        migrate.clicked.connect(dialog.accept)
        migrate.clicked.connect(self._migrate_collection_logic)
        actions.addWidget(migrate)

        advanced = QPushButton("Ouvrir les outils d’import avancés")
        advanced.clicked.connect(dialog.accept)
        advanced.clicked.connect(self._open_import_tools)
        actions.addWidget(advanced)
        actions.addStretch(1)

        close = QPushButton("Fermer")
        close.clicked.connect(dialog.accept)
        actions.addWidget(close)
        root.addLayout(actions)
        dialog.exec()

    def _open_import_tools(self) -> None:
        self._ensure_expert_mode()
        self.tabs.setCurrentIndex(self.profiles_tab_index)

    def _import_collection_to_profile(self) -> None:
        current = self._current_profile()
        if not current:
            QMessageBox.warning(
                self,
                "Import collection OBS",
                "Sélectionnez d'abord un profil cible.",
            )
            return
        if self._service is None:
            QMessageBox.warning(
                self,
                "Import collection OBS",
                "Le runtime SSR n'est pas disponible.",
            )
            return

        domain, profile_name, _profile = current
        dialog = CollectionImportDialog(
            self,
            target_domain=domain,
            target_profile=profile_name,
        )
        if dialog.exec() != QDialog.Accepted:
            return

        options = dialog.options()
        try:
            request_id = self._service.request_collection_import_preview(
                include_layouts=bool(options.get("include_layouts", False)),
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Import collection OBS",
                str(exc),
            )
            return

        self._pending_collection_imports[request_id] = {
            "mode": "snapshot_profile",
            "domain": domain,
            "profile_name": profile_name,
            "options": copy.deepcopy(options),
        }
        self._log(
            "Import collection OBS mis en file "
            f"({request_id[:8]}) vers {domain}/{profile_name}."
        )
        self.statusBar().showMessage(
            "Lecture de la collection OBS en cours…",
            8000,
        )

    def _migrate_collection_logic(self) -> None:
        if self._service is None:
            QMessageBox.warning(
                self,
                "Migration collection OBS",
                "Le runtime SSR n'est pas disponible.",
            )
            return

        dialog = CollectionLogicImportDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return

        options = dialog.options()
        try:
            request_id = self._service.request_collection_import_preview(
                include_layouts=bool(options.get("include_layouts", False)),
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Migration collection OBS",
                str(exc),
            )
            return

        self._pending_collection_imports[request_id] = {
            "mode": "logic_migration",
            "domain": "",
            "profile_name": "",
            "options": copy.deepcopy(options),
        }
        self._log(
            "Prévisualisation migration logique OBS/ASC mise en file "
            f"({request_id[:8]})."
        )
        self.statusBar().showMessage(
            "Lecture de la collection OBS pour migration logique…",
            8000,
        )

    def _complete_collection_import(
        self,
        payload,
        context: Mapping[str, object],
    ) -> None:
        if not bool(getattr(payload, "success", False)):
            QMessageBox.critical(
                self,
                "Import collection OBS",
                str(
                    getattr(payload, "error", "")
                    or "La lecture de la collection OBS a échoué."
                ),
            )
            return

        raw_result = getattr(payload, "result", None)
        if not isinstance(raw_result, Mapping):
            QMessageBox.critical(
                self,
                "Import collection OBS",
                "Le runtime a retourné un rapport d'import invalide.",
            )
            return
        raw_snapshot = raw_result.get("snapshot")
        if not isinstance(raw_snapshot, Mapping):
            QMessageBox.critical(
                self,
                "Import collection OBS",
                "Le snapshot de collection OBS est absent ou invalide.",
            )
            return

        mode = str(context.get("mode") or "snapshot_profile")
        domain = str(context.get("domain") or "")
        profile_name = str(context.get("profile_name") or "")
        raw_options = context.get("options")
        options = (
            dict(raw_options)
            if isinstance(raw_options, Mapping)
            else {}
        )
        snapshot = SceneCollectionImporter.snapshot_from_mapping(
            raw_snapshot
        )

        if mode == "guided_current_state_capture":
            self._complete_guided_current_state_capture(
                snapshot=snapshot,
                raw_result=raw_result,
                context=context,
            )
            return

        if mode == "guided_analysis":
            self._record_user_activity(
                UserActivityEntry(
                    "Good",
                    "Analyse de la collection OBS terminée",
                    (
                        f"{len(snapshot.scenes)} scène(s), "
                        f"{len(snapshot.inputs)} source(s), "
                        f"{len(snapshot.filters)} filtre(s)"
                    ),
                )
            )
            self._show_guided_collection_analysis(snapshot, raw_result)
            return

        previous = copy.deepcopy(self.config)
        asc_report = None
        asc_path = ""
        layout_report = None
        hdr_profiles_changed: tuple[str, ...] = ()
        test_layouts_neutralized: tuple[str, ...] = ()
        try:
            report = None
            if mode == "snapshot_profile":
                report = SceneCollectionImporter.merge_actions_into_profile(
                    self.config,
                    domain=domain,
                    profile_name=profile_name,
                    snapshot=snapshot,
                    include_input_settings=bool(
                        options.get("include_input_settings", True)
                    ),
                    include_audio_state=bool(
                        options.get("include_audio_state", True)
                    ),
                    include_filters=bool(
                        options.get("include_filters", True)
                    ),
                    include_visibility=bool(
                        options.get("include_visibility", False)
                    ),
                )

            if bool(options.get("include_layouts", False)):
                raw_layouts = raw_result.get("layouts")
                layouts = (
                    {
                        str(name): dict(profile)
                        for name, profile in raw_layouts.items()
                        if isinstance(profile, Mapping)
                    }
                    if isinstance(raw_layouts, Mapping)
                    else {}
                )
                raw_skipped = raw_result.get("layout_skipped")
                layout_skipped = tuple(
                    str(item)
                    for item in (
                        raw_skipped
                        if isinstance(raw_skipped, list)
                        else []
                    )
                )
                layout_report = (
                    SceneCollectionImporter.apply_layout_profiles(
                        self.config,
                        collection=snapshot.collection,
                        profiles=layouts,
                        skipped=layout_skipped,
                    )
                )

            asc_path = str(options.get("asc_path") or "").strip()
            if not asc_path:
                detected = (
                    AdvancedSceneSwitcherImporter.find_scene_collection_file(
                        snapshot.collection
                    )
                )
                asc_path = str(detected) if detected is not None else ""
            if asc_path:
                asc_data = AdvancedSceneSwitcherImporter.load(asc_path)
                asc_report = AdvancedSceneSwitcherImporter.apply_to_config(
                    asc_data,
                    self.config,
                    snapshot=snapshot,
                    enable_created_rules=bool(
                        options.get("enable_converted_rules", False)
                    ),
                )

            if (
                mode == "logic_migration"
                and bool(options.get("wire_hdr_profiles", False))
            ):
                hdr_profiles_changed = wire_windows_hdr_capture_profiles(
                    self.config
                )
            if (
                mode == "logic_migration"
                and bool(options.get("neutralize_test_layouts", False))
            ):
                test_layouts_neutralized = (
                    neutralize_referenced_test_layout_profiles(self.config)
                )

            errors = validate_config(self.config)
            if errors:
                raise ValueError(
                    "La configuration importée n'est pas valide :\n- "
                    + "\n- ".join(errors)
                )
        except Exception as exc:
            self.config = previous
            self._refresh_rules_table()
            self._refresh_profile_names()
            self._refresh_layout_profile_names()
            self._refresh_override_boxes()
            QMessageBox.critical(
                self,
                "Import collection OBS",
                str(exc),
            )
            return

        self._mark_dirty()
        self._refresh_rules_table()
        self._refresh_profile_names()
        self._refresh_layout_profile_names()
        if mode == "snapshot_profile":
            self.profile_domain.setCurrentIndex(
                max(0, self.profile_domain.findData(domain))
            )
            self._refresh_profile_names()
            self.profile_name.setCurrentText(profile_name)
            self._refresh_actions_table()
        self._refresh_override_boxes()

        summary = (
            report.summary()
            if report is not None
            else (
                "Migration logique de collection : aucun snapshot OBS global "
                "n'a été fusionné dans un profil unique."
            )
        )
        if layout_report is not None:
            summary += "\n\nLayouts\n" + layout_report.summary()
        if asc_report is not None:
            summary += (
                "\n\nAdvanced Scene Switcher\n"
                + asc_report.summary()
            )
        if bool(options.get("wire_hdr_profiles", False)):
            if hdr_profiles_changed:
                summary += (
                    "\n\nHDR Windows\nCaptureProfile(s) câblé(s) : "
                    + ", ".join(hdr_profiles_changed)
                )
            else:
                summary += (
                    "\n\nHDR Windows\nLes CaptureProfiles HDR/SDR "
                    "étaient déjà correctement câblés."
                )
        if bool(options.get("enable_converted_rules", False)):
            summary += (
                "\n\nRègles ASC\nLes nouvelles règles converties "
                "ont été activées explicitement."
            )
        if bool(options.get("neutralize_test_layouts", False)):
            summary += (
                "\n\nLayouts de test neutralisés : "
                + (
                    ", ".join(test_layouts_neutralized)
                    if test_layouts_neutralized
                    else "aucun"
                )
            )
        elif not asc_path:
            summary += (
                "\n\nAdvanced Scene Switcher\n"
                "Aucun JSON ASC détecté pour cette collection."
            )
        summary += (
            "\n\nLa configuration est modifiée uniquement en mémoire. "
            "Vérifiez-la puis utilisez « Enregistrer et appliquer »."
        )
        QMessageBox.information(
            self,
            "Import collection OBS terminé",
            summary,
        )

        if asc_report is not None and asc_report.rejected_raw:
            self._offer_save_collection_import_report(
                snapshot=snapshot,
                domain=domain,
                profile_name=profile_name,
                asc_path=asc_path,
                collection_report=report,
                layout_report=layout_report,
                asc_report=asc_report,
            )

    def _offer_save_collection_import_report(
        self,
        *,
        snapshot,
        domain: str,
        profile_name: str,
        asc_path: str,
        collection_report,
        layout_report,
        asc_report,
    ) -> None:
        answer = QMessageBox.question(
            self,
            "Macros ASC non converties",
            (
                f"{len(asc_report.rejected_raw)} macro(s) Advanced Scene "
                "Switcher n'ont pas été converties.\n\n"
                "Leur JSON brut et la raison du refus sont conservés dans "
                "le rapport. Voulez-vous enregistrer ce rapport maintenant ?\n\n"
                "Attention : le JSON brut d'une macro peut contenir des "
                "chemins, URL, tokens ou autres paramètres sensibles."
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer != QMessageBox.Yes:
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Enregistrer le rapport d'import",
            "stream-state-router-import-report.json",
            "JSON (*.json)",
        )
        if not path:
            return
        payload = {
            "collection": snapshot.collection,
            "target": {
                "domain": domain,
                "profile": profile_name,
            },
            "advanced_scene_switcher_source": asc_path,
            "collection_import": (
                {
                    "added_actions": collection_report.added_actions,
                    "replaced_actions": collection_report.replaced_actions,
                    "skipped": list(collection_report.skipped),
                }
                if collection_report is not None
                else None
            ),
            "layout_import": (
                {
                    "added": layout_report.added,
                    "refreshed": layout_report.refreshed,
                    "skipped": list(layout_report.skipped),
                }
                if layout_report is not None
                else None
            ),
            "advanced_scene_switcher": asc_report.as_mapping(),
        }
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    payload,
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Rapport d'import",
                str(exc),
            )
            return
        self._log(f"Rapport d'import enregistré : {path}")

    def _add_action(self) -> None:
        current = self._current_profile()
        if not current:
            return
        dlg = ActionDialog(self)
        if dlg.exec() == QDialog.Accepted:
            current[2].setdefault("actions", []).append(dlg.result_action())
            self._mark_dirty()
            self._refresh_actions_table()

    def _edit_action(self, *_args) -> None:
        current = self._current_profile()
        idx = self._selected_action_index()
        if not current or idx is None:
            return
        actions = current[2].setdefault("actions", [])
        dlg = ActionDialog(self, actions[idx])
        if dlg.exec() == QDialog.Accepted:
            actions[idx] = dlg.result_action()
            self._mark_dirty()
            self._refresh_actions_table()

    def _duplicate_action(self) -> None:
        current = self._current_profile()
        idx = self._selected_action_index()
        if not current or idx is None:
            return
        actions = current[2].setdefault("actions", [])
        actions.insert(idx + 1, copy.deepcopy(actions[idx]))
        self._mark_dirty()
        self._refresh_actions_table()

    def _toggle_action(self) -> None:
        current = self._current_profile()
        idx = self._selected_action_index()
        if not current or idx is None:
            return
        action = current[2].setdefault("actions", [])[idx]
        action["enabled"] = not bool(action.get("enabled", True))
        self._mark_dirty()
        self._refresh_actions_table()

    def _delete_action(self) -> None:
        current = self._current_profile()
        idx = self._selected_action_index()
        if not current or idx is None:
            return
        current[2].setdefault("actions", []).pop(idx)
        self._mark_dirty()
        self._refresh_actions_table()

    def _move_action(self, delta: int) -> None:
        current = self._current_profile()
        idx = self._selected_action_index()
        if not current or idx is None:
            return
        actions = current[2].setdefault("actions", [])
        new_idx = idx + delta
        if not 0 <= new_idx < len(actions):
            return
        actions[idx], actions[new_idx] = actions[new_idx], actions[idx]
        self._mark_dirty()
        self._refresh_actions_table()
        self.actions_table.selectRow(new_idx)

    def _test_profile(self) -> None:
        current = self._current_profile()
        if not current:
            return
        self._collect_settings()
        try:
            client = OBSClientManager(build_obs_config(self.config))
            dispatcher = OBSDispatcher(
                client,
                build_profiles(self.config),
                build_layout_profiles(self.config),
            )
            result = dispatcher.execute_profile(current[0], current[1])
        except Exception as exc:
            QMessageBox.critical(self, "Test du profil", str(exc))
            return
        QMessageBox.information(
            self,
            "Test du profil",
            f"{result.executed} action(s) exécutée(s), {result.skipped} ignorée(s).",
        )

    def _state_profile_choices(self) -> dict[str, list[str]]:
        profiles = self.config.get("profiles", {})
        choices = {
            domain: sorted((profiles.get(domain) or {}).keys(), key=str.casefold)
            for domain in PROFILE_DOMAINS
        }
        choices["layout"] = sorted(
            self.config.get("layout_profiles", {}).keys(), key=str.casefold
        )
        return choices

    def _refresh_override_boxes(self) -> None:
        choices = self._state_profile_choices()
        for domain, box in self.override_boxes.items():
            current = box.currentText()
            box.clear()
            box.addItems(choices.get(domain, []))
            idx = box.findText(current)
            if idx >= 0:
                box.setCurrentIndex(idx)

    # ---------- layouts / module catalog ----------
    def _layout_profiles(self) -> dict:
        return self.config.setdefault("layout_profiles", {})

    def _refresh_layout_profile_names(self) -> None:
        current = self.layout_profile_name.currentText() if hasattr(self, "layout_profile_name") else ""
        if not hasattr(self, "layout_profile_name"):
            return
        self.layout_profile_name.blockSignals(True)
        self.layout_profile_name.clear()
        self.layout_profile_name.addItems(sorted(self._layout_profiles(), key=str.casefold))
        if current:
            idx = self.layout_profile_name.findText(current)
            if idx >= 0:
                self.layout_profile_name.setCurrentIndex(idx)
        self.layout_profile_name.blockSignals(False)
        self._refresh_layout_profile_view()

    def _current_layout_profile(self) -> tuple[str, dict] | None:
        if not hasattr(self, "layout_profile_name"):
            return None
        name = self.layout_profile_name.currentText().strip()
        profile = self._layout_profiles().get(name)
        return (name, profile) if name and isinstance(profile, dict) else None

    def _refresh_layout_profile_view(self, *_args) -> None:
        if not hasattr(self, "layout_modules_table"):
            return
        current = self._current_layout_profile()
        self._refresh_layout_options(current)
        modules = current[1].get("modules", {}) if current else {}
        if not isinstance(modules, dict):
            modules = {}
        self.layout_modules_table.setRowCount(len(modules))
        for row, (module_name, module) in enumerate(sorted(modules.items(), key=lambda item: item[0].casefold())):
            geometry = module.get("geometry", {}) if isinstance(module, dict) else {}
            elements = module.get("elements", []) if isinstance(module, dict) else []
            included = sum(
                1
                for element in elements
                if isinstance(element, dict) and bool(element.get("included", True))
            )
            values = [
                module_name,
                f"{included}/{len(elements)}",
                f"{float(geometry.get('x', 0.0)):.1f}",
                f"{float(geometry.get('y', 0.0)):.1f}",
                f"{float(geometry.get('width', 0.0)):.1f}",
                f"{float(geometry.get('height', 0.0)):.1f}",
                "Oui" if bool(module.get("visible", True)) else "Non",
                str(module.get("anchor") or "top_left"),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.layout_modules_table.setItem(row, col, item)

    def _refresh_layout_options(self, current=None) -> None:
        if not hasattr(self, "layout_parent"):
            return
        current = current or self._current_layout_profile()
        for widget in (self.layout_parent, self.layout_coordinate_mode, self.layout_transition, self.layout_transition_ms):
            widget.blockSignals(True)
        self.layout_parent.clear()
        self.layout_parent.addItem("— Aucune —", "")
        if current:
            name, profile = current
            for candidate in sorted(self._layout_profiles(), key=str.casefold):
                if candidate != name:
                    self.layout_parent.addItem(candidate, candidate)
            idx = self.layout_parent.findData(str(profile.get("extends") or ""))
            self.layout_parent.setCurrentIndex(max(0, idx))
            mode = str(profile.get("coordinate_mode") or "normalized")
            idx = self.layout_coordinate_mode.findData(mode)
            self.layout_coordinate_mode.setCurrentIndex(max(0, idx))
            transition = profile.get("transition") if isinstance(profile.get("transition"), dict) else {}
            idx = self.layout_transition.findData(str(transition.get("mode") or "instant"))
            self.layout_transition.setCurrentIndex(max(0, idx))
            self.layout_transition_ms.setValue(int(transition.get("duration_ms", 0)))
        for widget in (self.layout_parent, self.layout_coordinate_mode, self.layout_transition, self.layout_transition_ms):
            widget.blockSignals(False)

    def _layout_option_changed(self, *_args) -> None:
        current = self._current_layout_profile()
        if not current or not hasattr(self, "layout_parent"):
            return
        profile = current[1]
        profile["extends"] = str(self.layout_parent.currentData() or "")
        profile["coordinate_mode"] = str(self.layout_coordinate_mode.currentData() or "normalized")
        transition = profile.setdefault("transition", {})
        transition["mode"] = str(self.layout_transition.currentData() or "instant")
        transition["duration_ms"] = self.layout_transition_ms.value()
        transition.setdefault("steps", 8)
        self._mark_dirty()

    def _new_layout_profile(self) -> None:
        name, ok = QInputDialog.getText(self, "Nouveau LayoutProfile", "Nom du layout")
        name = name.strip()
        if not ok or not name:
            return
        profiles = self._layout_profiles()
        if name in profiles:
            QMessageBox.warning(self, "LayoutProfile", "Ce layout existe déjà.")
            return
        scene = self.layout_scene.currentText().strip() if hasattr(self, "layout_scene") else ""
        profiles[name] = {
            "scene": scene,
            "modules": {},
            "extends": "",
            "coordinate_mode": "normalized",
            "conditions": {},
            "transition": {"mode": "instant", "duration_ms": 0, "steps": 8},
        }
        self._mark_dirty()
        self._refresh_layout_profile_names()
        self.layout_profile_name.setCurrentText(name)
        self._refresh_override_boxes()

    def _duplicate_layout_profile(self) -> None:
        current = self._current_layout_profile()
        if not current:
            return
        old_name, profile = current
        name, ok = QInputDialog.getText(
            self, "Dupliquer le LayoutProfile", "Nouveau nom", text=f"{old_name} (copie)"
        )
        name = name.strip()
        if not ok or not name:
            return
        profiles = self._layout_profiles()
        if name in profiles:
            QMessageBox.warning(self, "LayoutProfile", "Ce layout existe déjà.")
            return
        profiles[name] = copy.deepcopy(profile)
        self._mark_dirty()
        self._refresh_layout_profile_names()
        self.layout_profile_name.setCurrentText(name)
        self._refresh_override_boxes()

    def _layout_profile_references(self, name: str) -> list[str]:
        refs: list[str] = []
        fallback = self.config.get("router", {}).get("fallback_state", {})
        if isinstance(fallback, dict) and fallback.get("LayoutProfile") == name:
            refs.append("fallback")
        for rule in self.config.get("rules", []):
            if rule.get("behavior", "match") != "match":
                continue
            state = rule.get("state") if isinstance(rule.get("state"), dict) else {}
            if state.get("LayoutProfile") == name:
                refs.append(str(rule.get("name") or "règle sans nom"))
        for child_name, child in self._layout_profiles().items():
            if child_name != name and isinstance(child, dict) and str(child.get("extends") or "") == name:
                refs.append(f"layout {child_name} (héritage)")
        return refs

    def _replace_layout_profile_references(self, old: str, new: str) -> None:
        fallback = self.config.get("router", {}).get("fallback_state", {})
        if isinstance(fallback, dict) and fallback.get("LayoutProfile") == old:
            fallback["LayoutProfile"] = new
        for rule in self.config.get("rules", []):
            state = rule.get("state") if isinstance(rule.get("state"), dict) else {}
            if state.get("LayoutProfile") == old:
                state["LayoutProfile"] = new
        for child in self._layout_profiles().values():
            if isinstance(child, dict) and str(child.get("extends") or "") == old:
                child["extends"] = new

    def _rename_layout_profile(self) -> None:
        current = self._current_layout_profile()
        if not current:
            return
        old_name, profile = current
        name, ok = QInputDialog.getText(
            self, "Renommer le LayoutProfile", "Nouveau nom", text=old_name
        )
        name = name.strip()
        if not ok or not name or name == old_name:
            return
        profiles = self._layout_profiles()
        if name in profiles:
            QMessageBox.warning(self, "LayoutProfile", "Ce layout existe déjà.")
            return
        del profiles[old_name]
        profiles[name] = profile
        self._replace_layout_profile_references(old_name, name)
        self._mark_dirty()
        self._refresh_layout_profile_names()
        self.layout_profile_name.setCurrentText(name)
        self._refresh_rules_table()
        self._refresh_override_boxes()

    def _delete_layout_profile(self) -> None:
        current = self._current_layout_profile()
        if not current:
            return
        name, _profile = current
        refs = self._layout_profile_references(name)
        if refs:
            QMessageBox.warning(
                self,
                "LayoutProfile utilisé",
                "Ce layout est encore référencé par : " + ", ".join(refs) + ".",
            )
            return
        if QMessageBox.question(self, "Supprimer", f"Supprimer le LayoutProfile « {name} » ?") != QMessageBox.Yes:
            return
        del self._layout_profiles()[name]
        self._mark_dirty()
        self._refresh_layout_profile_names()
        self._refresh_override_boxes()

    def _sync_obs_modules(self) -> None:
        self._collect_settings()
        cfg = build_obs_config(self.config)
        if not cfg.enabled:
            QMessageBox.information(
                self,
                "Layouts OBS",
                "Activez « Piloter OBS » dans Paramètres, puis enregistrez/appliquez la configuration.",
            )
            return
        try:
            manager = self._dispatcher.layout_manager if self._dispatcher is not None else OBSLayoutManager(OBSClientManager(cfg))
            scenes, current = manager.list_scenes()
        except Exception as exc:
            QMessageBox.critical(self, "Layouts OBS", str(exc))
            return
        self._layout_sync_manager = manager
        previous = self.layout_scene.currentText().strip()
        self.layout_scene.blockSignals(True)
        self.layout_scene.clear()
        self.layout_scene.addItems(scenes)
        preferred = current or previous
        idx = self.layout_scene.findText(preferred)
        if idx >= 0:
            self.layout_scene.setCurrentIndex(idx)
        self.layout_scene.blockSignals(False)
        self._layout_scene_changed()
        self._known_catalog_sources = {
            element.source for values in self._obs_module_catalog.values() for element in values
        }

    def _layout_scene_changed(self, *_args) -> None:
        manager = self._layout_sync_manager
        scene = self.layout_scene.currentText().strip() if hasattr(self, "layout_scene") else ""
        if manager is None or not scene:
            return
        try:
            self._obs_module_catalog = manager.discover_scene(scene)
        except Exception as exc:
            QMessageBox.critical(self, "Catalogue OBS", str(exc))
            return
        self._populate_module_tree()

    def _populate_module_tree(self) -> None:
        self._catalog_tree_guard = True
        try:
            self.module_tree.clear()
            by_type: dict[str, list[tuple[str, object]]] = {}
            for module_key, elements in self._obs_module_catalog.items():
                if not elements:
                    continue
                module_type = str(elements[0].module or "Autre")
                by_type.setdefault(module_type, []).append((module_key, elements[0]))

            for module_type, modules in sorted(by_type.items(), key=lambda item: item[0].casefold()):
                parent = QTreeWidgetItem([f"[{module_type}]", ""])
                parent.setFlags(
                    parent.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate
                )
                parent.setCheckState(0, Qt.Checked)
                self.module_tree.addTopLevelItem(parent)
                for module_key, element in sorted(
                    modules, key=lambda item: (str(item[1].element).casefold(), item[0].casefold())
                ):
                    label = str(element.element)
                    if " @ " in module_key:
                        label = f"{label} @ {element.container}"
                    child = QTreeWidgetItem([label, element.source])
                    child.setData(0, Qt.UserRole, element.source)
                    child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                    child.setCheckState(0, Qt.Checked)
                    parent.addChild(child)
                parent.setExpanded(True)
        finally:
            self._catalog_tree_guard = False

    def _catalog_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._catalog_tree_guard or column != 0 or item.parent() is not None:
            return
        self._catalog_tree_guard = True
        try:
            state = item.checkState(0)
            if state == Qt.PartiallyChecked:
                return
            for index in range(item.childCount()):
                item.child(index).setCheckState(0, state)
        finally:
            self._catalog_tree_guard = False

    def _selected_catalog_sources(self) -> set[str]:
        selected: set[str] = set()
        for top_index in range(self.module_tree.topLevelItemCount()):
            parent = self.module_tree.topLevelItem(top_index)
            for child_index in range(parent.childCount()):
                child = parent.child(child_index)
                if child.checkState(0) == Qt.Checked:
                    source = str(child.data(0, Qt.UserRole) or "")
                    if source:
                        selected.add(source)
        return selected

    def _capture_layout_profile(self) -> None:
        if self._layout_sync_manager is None:
            self._sync_obs_modules()
            if self._layout_sync_manager is None:
                return
        current = self._current_layout_profile()
        if not current:
            self._new_layout_profile()
            current = self._current_layout_profile()
            if not current:
                return
        name, old_profile = current
        scene = self.layout_scene.currentText().strip()
        selected = self._selected_catalog_sources()
        if not scene or not selected:
            QMessageBox.warning(
                self, "Capturer le layout", "Sélectionnez une scène et au moins un élément OBS."
            )
            return
        try:
            capture = self._layout_sync_manager.capture_profile_result(
                scene,
                selected_sources=selected,
                extends=str(old_profile.get("extends") or ""),
                transition=(
                    old_profile.get("transition")
                    if isinstance(old_profile.get("transition"), dict)
                    else None
                ),
            )
            raw_captured = capture.profile
            if capture.captured_modules <= 0:
                QMessageBox.warning(
                    self,
                    "Capturer le layout",
                    "Aucune source correspondant à « [Type de module] Nom du module » "
                    "n'a été trouvée parmi la sélection.",
                )
                return
            if not capture.complete:
                QMessageBox.warning(
                    self,
                    "Capture OBS incomplète",
                    "Le profil existant n'a pas été remplacé. Certains sous-arbres OBS "
                    "n'ont pas pu être lus :\n\n- " + "\n- ".join(capture.warnings[:20]),
                )
                return

            candidate = copy.deepcopy(raw_captured)
            candidate["coordinate_mode"] = str(old_profile.get("coordinate_mode") or "normalized")
            candidate["conditions"] = copy.deepcopy(old_profile.get("conditions") or {})
            parent_name = str(candidate.get("extends") or "")
            if parent_name and parent_name in self._layout_profiles():
                parent = resolve_layout_profile(parent_name, self._layout_profiles())
                candidate = compact_layout_overrides(candidate, parent)

            candidate_profiles = copy.deepcopy(self._layout_profiles())
            candidate_profiles[name] = candidate
            resolved = resolve_layout_profile(name, candidate_profiles)
            issues = self._layout_sync_manager.validate_profile(resolved)
            errors = [issue for issue in issues if issue.level == "error"]
            if errors:
                QMessageBox.warning(
                    self,
                    "Capture OBS refusée",
                    "Le profil existant n'a pas été remplacé :\n\n"
                    + "\n".join(f"• {issue.message}" for issue in errors[:20]),
                )
                return
        except Exception as exc:
            QMessageBox.critical(self, "Capturer le layout", str(exc))
            return

        push_layout_history(self.config, name, old_profile)
        self._layout_profiles()[name] = candidate
        self._mark_dirty()
        self._refresh_layout_profile_view()
        self._refresh_override_boxes()
        self.statusBar().showMessage(f"Layout « {name} » capturé depuis OBS", 4000)
        warnings = [issue for issue in issues if issue.level != "error"]
        if warnings:
            QMessageBox.information(
                self,
                "Layout capturé",
                "Capture valide avec informations :\n\n"
                + "\n".join(f"• {issue.message}" for issue in warnings[:20]),
            )

    def _selected_layout_module_name(self) -> str | None:
        rows = self.layout_modules_table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.layout_modules_table.item(rows[0].row(), 0)
        return item.text() if item else None

    @staticmethod
    def _activation_candidates_for_module(
        profile: dict,
        module_name: str,
        module: dict,
    ) -> list[dict]:
        module_source = str(module.get("source_name") or module_name).strip()
        candidates: list[dict] = []
        seen: set[tuple[str, str, str]] = set()

        def add_candidate(raw) -> None:
            if not isinstance(raw, dict):
                return
            path = (
                [str(item) for item in raw.get("path", [])]
                if isinstance(raw.get("path"), list)
                else []
            )
            container = str(raw.get("container") or "").strip()
            source = str(raw.get("source") or "").strip()
            container_kind = str(raw.get("container_kind") or "scene").strip() or "scene"
            if not container or not source or source == module_source:
                return
            direct_child = (
                container == module_source
                or bool(path and path[-1] == module_source)
            )
            if not direct_child:
                return
            key = (container, container_kind, source)
            if key in seen:
                return
            seen.add(key)
            candidates.append(
                {
                    "container": container,
                    "container_kind": container_kind,
                    "path": path,
                    "source": source,
                    "enabled": True,
                    "weight": 1.0,
                }
            )

        # Module-named descendants live in profile.modules, while ordinary
        # implementation children live in support_items. Both are needed to
        # present the actual direct children of a module scene/group.
        modules = profile.get("modules") if isinstance(profile, dict) else None
        if isinstance(modules, dict):
            for child_name, child_module in modules.items():
                if child_name == module_name or not isinstance(child_module, dict):
                    continue
                elements = child_module.get("elements")
                if not isinstance(elements, list):
                    continue
                for element in elements:
                    add_candidate(element)

        support_items = profile.get("support_items") if isinstance(profile, dict) else None
        if isinstance(support_items, list):
            for raw in support_items:
                add_candidate(raw)

        return candidates

    def _release_runtime_visibility_ownership(self, container: str, source: str) -> int:
        configured = build_activation_policies(self.config)
        identity = TriggerTargetIdentity(container, source)
        for policy_name, policy in configured.items():
            if any(target.identity.container == identity.container and target.identity.source == identity.source for target in policy.targets):
                raise RuntimeError(
                    f"Cette source appartient encore à la politique d'activation {policy_name}. "
                    "Retirez-la d'abord de la politique puis enregistrez."
                )
        changed = release_runtime_visibility_ownership(
            self.config,
            container=container,
            source=source,
        )
        if changed:
            self._mark_dirty()
            self._refresh_layout_profile_view()
        return changed

    def _activation_status(self, policy_name: str) -> dict:
        if self._service is None:
            return {"available": False, "phase": "idle"}
        return self._service.activation_status(policy_name)

    def _activation_command(
        self,
        action: str,
        policy_name: str,
        target=None,
        options=None,
    ) -> str:
        service = self._service
        if service is None:
            raise RuntimeError("Runtime non disponible")
        options = options or {}
        identity = None
        legacy_source = None
        if isinstance(target, dict):
            identity = TriggerTargetIdentity.from_mapping(target)
        elif target:
            legacy_source = str(target)

        if action == "test_roll":
            return service.activation_test_roll(policy_name)
        if action == "trigger":
            return service.activation_trigger_now(
                policy_name,
                target_identity=identity,
                target_source=legacy_source,
            )
        if action == "stop":
            return service.activation_stop(policy_name)
        if action == "reset_cooldown":
            return service.activation_reset_cooldown(policy_name)
        if action == "reset_all":
            return service.activation_reset_all()
        if action == "simulate":
            return service.activation_simulate(
                policy_name,
                trials=int(options.get("trials", 1000)),
                seed=int(options.get("seed", 12345)),
            )
        raise ValueError(f"Commande de déclenchement inconnue : {action}")

    def _edit_layout_module(self, *_args) -> None:
        current = self._current_layout_profile()
        module_name = self._selected_layout_module_name()
        if not current or not module_name:
            return
        modules = current[1].get("modules", {})
        module = modules.get(module_name) if isinstance(modules, dict) else None
        if not isinstance(module, dict):
            return
        policy_name = str(module.get("source_name") or module_name).strip()
        activation_policies = self.config.setdefault("activation_policies", {})
        if not isinstance(activation_policies, dict):
            activation_policies = {}
            self.config["activation_policies"] = activation_policies
        activation_policy = activation_policies.get(policy_name)
        candidates = self._activation_candidates_for_module(current[1], module_name, module)
        dlg = ModuleLayoutDialog(
            self,
            module_name,
            module,
            activation_policy=activation_policy if isinstance(activation_policy, dict) else None,
            activation_candidates=candidates,
            activation_status_provider=self._activation_status,
            activation_command=self._activation_command,
            visibility_release_command=self._release_runtime_visibility_ownership,
            activation_result_signal=self.bridge.activation_result,
        )
        if dlg.exec() == QDialog.Accepted:
            updated = dlg.result_module()
            updated_policy = dlg.result_activation_policy()
            if updated_policy is None:
                activation_policies.pop(policy_name, None)
            else:
                activation_policies[policy_name] = updated_policy
            canvas = current[1].get("canvas") if isinstance(current[1].get("canvas"), dict) else {}
            width = float(canvas.get("width", 0) or 0)
            height = float(canvas.get("height", 0) or 0)
            geometry = updated.get("geometry") if isinstance(updated.get("geometry"), dict) else {}
            coordinate_space = str(updated.get("coordinate_space") or "").strip()
            if not coordinate_space:
                scene = str(current[1].get("scene") or "").strip()
                container = str(updated.get("container") or scene).strip()
                coordinate_space = "root_canvas" if not scene or container == scene else "container_local"
                updated["coordinate_space"] = coordinate_space
            if width > 0 and height > 0 and coordinate_space == "root_canvas":
                updated["normalized_geometry"] = {
                    "x": float(geometry.get("x", 0.0)) / width,
                    "y": float(geometry.get("y", 0.0)) / height,
                    "width": float(geometry.get("width", 1.0)) / width,
                    "height": float(geometry.get("height", 1.0)) / height,
                }
                ax, ay = anchor_factors(str(updated.get("anchor") or "top_left"))
                updated["anchor_offsets"] = {
                    "x": float(geometry.get("x", 0.0)) + float(geometry.get("width", 1.0)) * ax - width * ax,
                    "y": float(geometry.get("y", 0.0)) + float(geometry.get("height", 1.0)) * ay - height * ay,
                }
            elif coordinate_space != "root_canvas":
                updated.pop("normalized_geometry", None)
                updated.pop("anchor_offsets", None)
            modules[module_name] = updated
            self._mark_dirty()
            self._refresh_layout_profile_view()

    def _apply_layout_profile(self) -> None:
        current = self._current_layout_profile()
        if not current or self._service is None:
            return
        self._collect_settings()
        try:
            request_id = self._service.request_layout("apply", current[0])
            self._log(f"Application layout {current[0]} mise en file ({request_id[:8]}).")
            self.statusBar().showMessage(f"Application du layout {current[0]}…", 3000)
        except Exception as exc:
            QMessageBox.critical(self, "Appliquer le layout", str(exc))

    def _layout_manager_for_tools(self) -> OBSLayoutManager:
        self._collect_settings()
        cfg = build_obs_config(self.config)
        if not cfg.enabled:
            raise RuntimeError("Activez le pilotage OBS avant d'utiliser cet outil.")
        if self._dispatcher is not None:
            return self._dispatcher.layout_manager
        # Tools must never silently use a manager kept from an older runtime.
        self._layout_sync_manager = None
        return OBSLayoutManager(OBSClientManager(cfg))

    def _preview_layout_profile(self) -> None:
        current = self._current_layout_profile()
        if not current or self._service is None:
            return
        try:
            request_id = self._service.request_layout("preview", current[0])
            self._log(f"Aperçu layout {current[0]} mis en file ({request_id[:8]}).")
        except Exception as exc:
            QMessageBox.critical(self, "Aperçu layout", str(exc))

    def _cancel_layout_preview(self) -> None:
        if self._service is None:
            return
        try:
            request_id = self._service.request_layout("cancel-preview")
            self._log(f"Annulation aperçu mise en file ({request_id[:8]}).")
        except Exception as exc:
            QMessageBox.critical(self, "Aperçu layout", str(exc))

    def _undo_layout_obs(self) -> None:
        if self._service is None:
            return
        try:
            request_id = self._service.request_layout("undo")
            self._log(f"Undo OBS mis en file ({request_id[:8]}).")
        except Exception as exc:
            QMessageBox.critical(self, "Undo OBS", str(exc))

    def _diff_layout_with_obs(self) -> None:
        current = self._current_layout_profile()
        if not current:
            return
        try:
            manager = self._layout_manager_for_tools()
            resolved = resolve_layout_profile(current[0], self._layout_profiles())
            diffs = manager.diff_profile(resolved)
        except Exception as exc:
            QMessageBox.critical(self, "Comparaison OBS", str(exc))
            return
        if not diffs:
            QMessageBox.information(self, "Comparaison OBS", "Aucune différence détectée.")
            return
        text = "\n".join(f"• {item.module} / {item.source}: {', '.join(item.changes)}" for item in diffs[:80])
        QMessageBox.information(self, "Comparaison OBS", text)

    def _diff_two_layouts(self) -> None:
        current = self._current_layout_profile()
        if not current:
            return
        names = [name for name in sorted(self._layout_profiles(), key=str.casefold) if name != current[0]]
        if not names:
            return
        other, ok = QInputDialog.getItem(self, "Comparer deux layouts", "Comparer avec", names, 0, False)
        if not ok or not other:
            return
        try:
            left = resolve_layout_profile(current[0], self._layout_profiles())
            right = resolve_layout_profile(other, self._layout_profiles())
            differences = diff_layout_profiles(left, right)
        except Exception as exc:
            QMessageBox.critical(self, "Comparer layouts", str(exc))
            return
        QMessageBox.information(
            self,
            "Comparer layouts",
            "Aucune différence." if not differences else "\n".join(f"• {item}" for item in differences[:100]),
        )

    def _validate_layout_profile(self) -> None:
        current = self._current_layout_profile()
        if not current:
            return
        try:
            manager = self._layout_manager_for_tools()
            resolved = resolve_layout_profile(current[0], self._layout_profiles())
            issues = manager.validate_profile(resolved)
        except Exception as exc:
            QMessageBox.critical(self, "Validation layout", str(exc))
            return
        if not issues:
            QMessageBox.information(self, "Validation layout", "Layout valide : aucun problème détecté.")
            return
        text = "\n".join(
            f"[{issue.level.upper()}] {issue.module + ' / ' if issue.module else ''}{issue.source + ': ' if issue.source else ''}{issue.message}"
            for issue in issues[:100]
        )
        QMessageBox.information(self, "Validation layout", text)

    def _restore_layout_revision(self) -> None:
        current = self._current_layout_profile()
        if not current:
            return
        restored = pop_layout_history(self.config, current[0])
        if restored is None:
            QMessageBox.information(self, "Historique", "Aucune version précédente enregistrée.")
            return
        push_layout_history(self.config, current[0], current[1])
        self._layout_profiles()[current[0]] = restored
        self._mark_dirty()
        self._refresh_layout_profile_view()
        self._log(f"Version précédente du layout {current[0]} restaurée.")

    def _configure_module_scan_timer(self) -> None:
        if not hasattr(self, "_module_scan_timer"):
            return
        ui = self.config.get("ui", {})
        if bool(ui.get("auto_detect_modules", True)):
            self._module_scan_timer.start(max(2, int(ui.get("module_scan_seconds", 5))) * 1000)
        else:
            self._module_scan_timer.stop()

    def _auto_scan_modules(self) -> None:
        if self._layout_sync_manager is None or not hasattr(self, "layout_scene"):
            return
        scene = self.layout_scene.currentText().strip()
        if not scene:
            return
        try:
            catalog = self._layout_sync_manager.discover_scene(scene)
        except Exception:
            return
        sources = {element.source for values in catalog.values() for element in values}
        new_sources = sources - self._known_catalog_sources
        if new_sources and self._known_catalog_sources:
            self._log("Nouveaux éléments OBS détectés : " + ", ".join(sorted(new_sources)))
            if self.tray.isVisible():
                self.tray.showMessage(
                    "Stream State Router",
                    f"{len(new_sources)} nouvel(aux) élément(s) de module détecté(s) dans {scene}.",
                    QSystemTrayIcon.MessageIcon.Information,
                    2500,
                )
        self._known_catalog_sources = sources
        if new_sources:
            self._obs_module_catalog = catalog
            self._populate_module_tree()

    def _start_api(self) -> None:
        raw = self.config.get("api", {})
        cfg = APIConfig(
            enabled=bool(raw.get("enabled", True)),
            host="127.0.0.1",
            port=int(raw.get("port", 8765)),
            token=str(raw.get("token") or ""),
        )
        self._api = LocalControlAPI(
            cfg,
            status=self._api_status,
            action=self._api_action,
            request_status=self._api_request_status,
        )
        try:
            self._api.start()
            if cfg.enabled:
                self._log(f"API locale active sur 127.0.0.1:{cfg.port}.")
        except Exception as exc:
            self._log(f"API locale indisponible : {exc}")

    def _restart_api(self) -> None:
        if self._api:
            self._api.stop()
        self._start_api()

    def _api_status(self) -> dict:
        service = self._service
        state = service.engine.current_state if service else None
        app = service.last_app if service else None
        return {
            "paused": bool(service.paused) if service else False,
            "foreground": app.exe_name if app else "",
            "rule": service.engine.current_rule if service else "",
            "state": state.as_variables() if state else {},
            "obs_connected": bool(self._client.connected) if self._client else False,
            "control_variables": (
                service.control_variables() if service else {}
            ),
            "config_revision": {
                "saved": self._saved_revision,
                "applied": self._applied_revision,
            },
            "routing": service.routing_status() if service else {},
            "obs_catalog": (
                service.obs_catalog_status()
                if service
                else {"available": False}
            ),
        }

    def _api_request_status(self, request_id: str) -> dict | None:
        if self._service is None:
            return None
        return self._service.command_status(request_id)

    def _api_action(self, action: str, payload: dict) -> dict:
        if self._service is None or self._dispatcher is None:
            raise RuntimeError("Runtime non disponible")
        if action == "explain":
            return {"explanation": self._service.explain_decision()}
        if action == "pause":
            self._service.pause(bool(payload.get("paused", True)))
            return {"paused": self._service.paused}
        if action == "auto":
            self._service.pause(False)
            self._service.clear_manual_override()
            return {"paused": False}
        if action == "catalog.sync":
            request_id = self._service.request_catalog_sync()
            return {"request_id": request_id, "status": "accepted"}
        if action == "catalog.snapshot":
            return {"catalog": self._service.obs_catalog_snapshot()}
        if action == "planner.current":
            request_id = self._service.request_declarative_plan(
                refresh_catalog=bool(payload.get("refresh_catalog", True))
            )
            return {"request_id": request_id, "status": "accepted"}
        if action == "planner.prepare_current":
            request_id = self._service.request_prepare_declarative_execution()
            return {"request_id": request_id, "status": "accepted"}
        if action == "planner.execute":
            plan_id = str(payload.get("plan_id") or "").strip()
            if not plan_id:
                raise ValueError("plan_id requis")
            request_id = self._service.request_execute_declarative_plan(plan_id)
            return {"request_id": request_id, "status": "accepted"}
        if action == "control.set":
            name = str(payload.get("name") or "").strip()
            if not name:
                raise ValueError("name requis")
            if "value" not in payload:
                raise ValueError("value requis")
            request_id = self._service.request_control_variable(
                name,
                payload.get("value"),
            )
            return {"request_id": request_id, "status": "accepted"}
        if action == "reapply":
            request_id = self._service.request_force_reapply()
            return {"request_id": request_id, "status": "accepted"}
        if action == "override":
            state = StreamState.from_mapping(payload.get("state") if isinstance(payload.get("state"), dict) else {})
            duration = float(payload.get("duration_seconds", 0) or 0)
            self._service.set_manual_override(state, duration_seconds=duration or None)
            return {"state": state.as_variables()}
        if action == "layout.apply":
            name = str(payload.get("name") or "").strip()
            if not name:
                raise ValueError("name requis")
            request_id = self._service.request_layout("apply", name)
            return {"request_id": request_id, "status": "accepted"}
        if action == "layout.preview":
            name = str(payload.get("name") or "").strip()
            if not name:
                raise ValueError("name requis")
            request_id = self._service.request_layout("preview", name)
            return {"request_id": request_id, "status": "accepted"}
        if action == "layout.cancel-preview":
            request_id = self._service.request_layout("cancel-preview")
            return {"request_id": request_id, "status": "accepted"}
        if action == "layout.undo":
            request_id = self._service.request_layout("undo")
            return {"request_id": request_id, "status": "accepted"}
        raise ValueError(f"Action inconnue : {action}")

    def _edit_layout_in_obs(self) -> None:
        current = self._current_layout_profile()
        if not current:
            return
        try:
            if self._service is None:
                raise RuntimeError("Runtime non disponible")
            request_id = self._service.request_layout("apply", current[0])
            self._log(
                f"Mode édition OBS — application de {current[0]} mise en file "
                f"({request_id[:8]}). Ajustez dans OBS après confirmation runtime puis capturez."
            )
            self.statusBar().showMessage(
                "Mode édition : application OBS en cours…",
                5000,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Mode édition OBS", str(exc))

    # ---------- settings / import / export ----------
    def _test_obs(self) -> None:
        self._collect_settings()
        cfg = build_obs_config(self.config)
        if not cfg.enabled:
            QMessageBox.information(self, "OBS", "Activez « Piloter OBS » pour tester la connexion.")
            return

        # Reuse the live client when the applied configuration already matches.
        # Otherwise keep the test isolated so unsaved settings never mutate the
        # running router until « Enregistrer et appliquer » is pressed.
        if self._client is not None and self._client.config == cfg:
            manager = self._client
            live_test = True
        else:
            manager = OBSClientManager(cfg)
            live_test = False
        ok, message = manager.probe()
        if live_test:
            self._update_obs_status()
        elif ok:
            message += "\n\nTest réussi. Cliquez sur « Enregistrer et appliquer » pour utiliser ces paramètres dans SSR."
        (QMessageBox.information if ok else QMessageBox.critical)(self, "OBS", message)

    def _export_config(self) -> None:
        self._collect_settings()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Exporter la configuration",
            "stream-state-router-config.json",
            "JSON (*.json)",
        )
        if not path:
            return
        include_secrets = QMessageBox.question(
            self,
            "Secrets de l'export",
            "Inclure le mot de passe OBS et le jeton API dans cet export ?\n\n"
            "Choisissez Non pour un fichier partageable.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) == QMessageBox.Yes
        try:
            export_config(self.config, path, include_secrets=include_secrets)
            QMessageBox.information(
                self,
                "Export",
                "Configuration exportée avec secrets."
                if include_secrets
                else "Configuration partageable exportée sans mot de passe OBS ni jeton API.",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Export", str(exc))

    def _import_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Importer une configuration", "", "JSON (*.json)")
        if not path:
            return
        try:
            incoming = import_config(path)
        except Exception as exc:
            QMessageBox.critical(self, "Import", str(exc))
            return
        if QMessageBox.question(
            self,
            "Importer",
            "Remplacer la configuration courante par le fichier sélectionné ?",
        ) != QMessageBox.Yes:
            return
        self.config = copy.deepcopy(incoming)
        self._load_config_into_ui()
        self._mark_dirty()

    def _restore_config_backup(self) -> None:
        try:
            found = latest_valid_backup()
        except Exception as exc:
            QMessageBox.critical(self, "Sauvegarde", str(exc))
            return
        if found is None:
            QMessageBox.information(self, "Sauvegarde", "Aucune sauvegarde valide n'a été trouvée.")
            return
        incoming, path = found
        if QMessageBox.question(
            self,
            "Restaurer une sauvegarde",
            f"Charger « {path.name} » comme brouillon ?\n\n"
            "La configuration active ne changera qu'après « Enregistrer et appliquer ».",
        ) != QMessageBox.Yes:
            return
        self.config = copy.deepcopy(incoming)
        self._load_config_into_ui()
        self._mark_dirty()
        self._log(f"Sauvegarde valide chargée en brouillon : {path}")

    def _refresh_config_revision_status(
        self,
        *,
        draft_dirty: bool | None = None,
    ) -> None:
        if draft_dirty is not None:
            self._draft_dirty = bool(draft_dirty)
        saved = self._saved_revision or "—"
        applied = self._applied_revision or "—"

        if self._expert_mode:
            if self._draft_dirty:
                text = (
                    f"Brouillon modifié · enregistré {saved} · "
                    f"appliqué {applied}"
                )
            elif saved != applied:
                text = f"Enregistré {saved} · runtime encore sur {applied}"
            else:
                text = f"Enregistré / appliqué {applied}"
        else:
            if self._draft_dirty:
                text = "Modifications non enregistrées"
            elif saved != applied:
                text = "Configuration enregistrée · application en attente"
            else:
                text = "Configuration à jour"
        self.unsaved.setText(text)
        self.unsaved.setToolTip(
            f"Révision enregistrée : {saved}\nRévision runtime : {applied}"
        )

    def _mark_dirty(self, *_args) -> None:
        self._refresh_config_revision_status(draft_dirty=True)

    def _log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    # ---------- tray / close ----------
    def _tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._restore_from_tray()

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _stop_runtime_for_exit(self):
        if self._service is None:
            return None
        result = self._service.stop()
        marker = self._runtime_marker
        if marker is not None:
            marker.finish(
                clean_shutdown=bool(result),
                cleanup_complete=bool(result.cleanup_complete),
                pending_cleanup=result.pending_cleanup,
            )
        if not result.cleanup_complete:
            self._log(
                f"Arrêt avec {len(result.pending_cleanup)} obligation(s) de nettoyage OBS conservée(s)."
            )
        return result

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_window_geometry()
        if not self._quitting and self.close_to_tray.isChecked() and self.tray.isVisible():
            event.ignore()
            self.hide()
            self.tray.showMessage(
                "Stream State Router",
                "L'application continue de fonctionner en arrière-plan.",
                QSystemTrayIcon.MessageIcon.Information,
                2500,
            )
            return
        if self._api:
            self._api.stop()
        self._stop_runtime_for_exit()
        event.accept()
        QApplication.instance().quit()

    def _quit_app(self) -> None:
        self._save_window_geometry()
        self._quitting = True
        if self._api:
            self._api.stop()
        self._stop_runtime_for_exit()
        self.tray.hide()
        QApplication.instance().quit()
