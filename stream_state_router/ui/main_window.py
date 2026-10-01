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
    QDockWidget,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
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
from ..media import MediaArtworkStore, MediaCommandStore, MediaRuntime, MediaStateStore
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
    build_media_provider,
    build_media_runtime_config,
    build_presentation_profiles,
    build_ruleset,
    build_widget_runtime_config,
    export_config,
    import_config,
    list_valid_backups,
    save_config,
    validate_config,
    push_layout_history,
    pop_layout_history,
    release_runtime_visibility_ownership,
)
from ..services.config_insights import (
    apply_reference_repairs,
    build_config_change_review,
    build_effective_dependency_tree,
    build_effective_provenance,
    detach_profile_inheritance,
    live_output_active,
    profile_content_entries,
    profile_lineage,
    profile_usages,
    scan_obs_reference_repairs,
    simulate_rule_scenario,
)
from ..presentation import PresentationStateStore
from ..services.control_variables import ControlVariableStore
from ..services.runtime import RoutingService, RuntimeEvent
from ..services.api import APIConfig, LocalControlAPI
from ..services.startup import is_startup_enabled, set_startup_enabled
from ..services.system_check import run_system_check
from ..widgets import (
    WidgetRuntime,
    import_html_module,
    list_widget_packages,
)
from .dialogs import (
    ActionDialog,
    CollectionImportDialog,
    CurrentStateCaptureDialog,
    CollectionLogicImportDialog,
    ModuleLayoutDialog,
    RuleDialog,
)
from .ergonomics import (
    build_attention_items,
    build_contextual_action,
    build_decision_trail,
    build_draft_banner,
    build_rule_health,
    build_status_strip,
    humanize_rule,
)
from .setup_guide import SetupGuideDialog, WidgetObsInstallDialog
from .presentation_editor import PresentationEditor
from .presentation import (
    UserActivityEntry,
    build_automation_rows,
    build_dashboard_snapshot,
    build_diagnostic_report,
    build_manual_override_presentation,
    build_obs_drift_presentation,
    build_simulation_report,
    render_capability_report_text,
    user_activity_from_runtime_event,
)


DOMAIN_LABELS = {
    "game": "Game",
    "overlay": "OverlayProfile",
    "capture": "CaptureProfile",
    "audio": "AudioProfile",
    "layout": "LayoutProfile",
    "presentation": "PresentationProfile",
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
        self._favorite_targets: list[tuple[str, str, str]] = []
        raw_favorites = self._window_settings.value(
            "main_window/favorites",
            "[]",
        )
        try:
            decoded_favorites = json.loads(str(raw_favorites or "[]"))
            if isinstance(decoded_favorites, list):
                for raw in decoded_favorites:
                    if (
                        isinstance(raw, list)
                        and len(raw) == 3
                        and all(isinstance(item, str) for item in raw)
                    ):
                        self._favorite_targets.append(
                            (raw[0], raw[1], raw[2])
                        )
        except (TypeError, ValueError):
            self._favorite_targets = []
        self._edit_mode = False
        self._edit_mode_owned_pause = False
        self.config = copy.deepcopy(config)
        self._last_saved_config = copy.deepcopy(config)
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
        self._presentation_state_store = PresentationStateStore()
        self._media_state_store = MediaStateStore("vlc")
        self._media_artwork_store = MediaArtworkStore()
        self._media_command_store = MediaCommandStore()
        self._media_runtime: MediaRuntime | None = None
        self._widget_runtime: WidgetRuntime | None = None
        self._obs_module_catalog: dict[str, list] = {}
        self._layout_sync_manager: OBSLayoutManager | None = None
        self._catalog_tree_guard = False
        self._quitting = False
        self._api: LocalControlAPI | None = None
        self._known_catalog_sources: set[str] = set()
        self._obs_connected_controls: list[tuple[object, bool]] = []
        self._preview_active = False
        self._routing_incomplete = False
        self._ignored_drift_signature = ""
        self._runtime_restart_in_progress = False
        self._last_runtime_restart_previous_stopped = False
        self._last_runtime_restart_diagnostic = ""
        self._pending_collection_imports: dict[str, dict[str, object]] = {}
        self._pending_obs_controls: dict[str, tuple[object, str]] = {}
        self._manual_undo: dict[str, object] | None = None
        self._manual_undo_controls: list[object] = []
        self._pending_layout_manual_apply: dict[str, str] = {}
        self._manual_undo_request_id = ""
        self._user_activity_history: list[
            tuple[str, UserActivityEntry, int, float]
        ] = []
        self._last_system_check_report = None
        self._current_foreground_app: ForegroundApp | None = None
        self._recent_inspector_targets: list[
            tuple[str, str, str]
        ] = []
        self._inspector_context: tuple[str, str, str] | None = None
        self._inspector_payload: Mapping[str, object] | None = None

        self.bridge = RuntimeBridge()
        self.bridge.foreground.connect(self._on_foreground)
        self.bridge.state_change.connect(self._on_state_change)
        self.bridge.dispatch.connect(self._on_dispatch)
        self.bridge.runtime_event.connect(self._on_runtime_event)

        self._build_ui()
        self._apply_ui_mode()
        self._build_menu()
        self._apply_edit_mode_surfaces()
        self._build_tray()
        self._load_config_into_ui()
        self._wire_dirty_signals()
        self._dashboard_refresh_timer = QTimer(self)
        self._dashboard_refresh_timer.setSingleShot(True)
        self._dashboard_refresh_timer.setInterval(75)
        self._dashboard_refresh_timer.timeout.connect(
            self._refresh_dashboard_summary
        )
        self._start_runtime()
        self._start_widget_runtime()
        self._start_media_runtime()
        self._start_api()
        self._media_status_timer = QTimer(self)
        self._media_status_timer.setInterval(1000)
        self._media_status_timer.timeout.connect(
            self._update_media_runtime_status
        )
        self._media_status_timer.start()
        self._update_media_runtime_status()
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
        layout.setSpacing(10)
        self.setCentralWidget(root)

        top = QHBoxLayout()
        title = QLabel("Stream State Router")
        title.setObjectName("Title")
        top.addWidget(title)
        top.addStretch(1)

        def status_box(
            caption: str,
            value: QLabel,
            action: QPushButton | None = None,
        ) -> QFrame:
            frame = QFrame()
            frame.setObjectName("StatusBox")
            box = QVBoxLayout(frame)
            box.setContentsMargins(10, 6, 10, 6)
            box.setSpacing(2)
            header = QLabel(caption)
            header.setObjectName("Muted")
            box.addWidget(header)
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            row.addWidget(value)
            if action is not None:
                action.setObjectName("Compact")
                row.addWidget(action)
            box.addLayout(row)
            return frame

        self.interface_status = QLabel("—")
        self.interface_status.setObjectName("Good")
        self.mode_button = QPushButton()
        self.mode_button.clicked.connect(self._toggle_ui_mode)
        top.addWidget(
            status_box("Interface", self.interface_status, self.mode_button)
        )

        self.config_status = QLabel("—")
        self.config_status.setObjectName("Good")
        self.edit_mode_button = QPushButton()
        self.edit_mode_button.clicked.connect(self._toggle_edit_mode)
        top.addWidget(
            status_box(
                "Configuration",
                self.config_status,
                self.edit_mode_button,
            )
        )

        self.routing_status_label = QLabel("—")
        self.routing_status_label.setObjectName("Good")
        self.pause_button = QPushButton()
        self.pause_button.clicked.connect(self._toggle_pause)
        top.addWidget(
            status_box(
                "Routage",
                self.routing_status_label,
                self.pause_button,
            )
        )

        self.obs_status = QLabel("—")
        self.obs_status.setObjectName("Muted")
        top.addWidget(status_box("OBS", self.obs_status))
        layout.addLayout(top)

        self.edit_banner = QFrame()
        self.edit_banner.setObjectName("EditBanner")
        edit_row = QHBoxLayout(self.edit_banner)
        edit_row.setContentsMargins(12, 8, 12, 8)
        self.edit_banner_label = QLabel(
            "MODE ÉDITION — routage automatique gelé — "
            "les changements restent dans le brouillon."
        )
        self.edit_banner_label.setObjectName("Warn")
        self.edit_banner_label.setWordWrap(True)
        edit_row.addWidget(self.edit_banner_label, 1)
        edit_exit = QPushButton("Quitter l’édition")
        edit_exit.setObjectName("Compact")
        edit_exit.clicked.connect(self._toggle_edit_mode)
        edit_row.addWidget(edit_exit)
        self.edit_banner.setVisible(False)
        layout.addWidget(self.edit_banner)

        self.draft_banner = QFrame()
        self.draft_banner.setObjectName("DraftBanner")
        draft_row = QHBoxLayout(self.draft_banner)
        draft_row.setContentsMargins(12, 8, 12, 8)
        draft_text = QVBoxLayout()
        draft_text.setSpacing(1)
        self.unsaved = QLabel("")
        self.unsaved.setObjectName("Warn")
        self.draft_detail = QLabel("")
        self.draft_detail.setObjectName("Muted")
        self.draft_detail.setWordWrap(True)
        draft_text.addWidget(self.unsaved)
        draft_text.addWidget(self.draft_detail)
        draft_row.addLayout(draft_text, 1)
        self.review_draft_button = QPushButton("Revoir…")
        self.review_draft_button.clicked.connect(
            self._show_draft_change_review
        )
        draft_row.addWidget(self.review_draft_button)
        self.discard_draft_button = QPushButton("Abandonner")
        self.discard_draft_button.clicked.connect(self._discard_draft)
        draft_row.addWidget(self.discard_draft_button)
        self.save_button = QPushButton("Enregistrer et appliquer")
        self.save_button.setObjectName("Primary")
        self.save_button.clicked.connect(self.save_and_apply)
        draft_row.addWidget(self.save_button)
        self.draft_banner.setVisible(False)
        layout.addWidget(self.draft_banner)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.dashboard_tab_index = self.tabs.addTab(
            self._scrollable_tab(self._build_dashboard()), "Accueil"
        )
        self.automations_tab_index = self.tabs.addTab(
            self._scrollable_tab(self._build_automations_tab()),
            "Automatisations",
        )
        self.configure_tab_index = self.tabs.addTab(
            self._scrollable_tab(self._build_configure_tab()),
            "Configurer",
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
        self.presentation_editor = PresentationEditor(
            self.config,
            self,
        )
        self.presentation_editor.changed.connect(self._mark_dirty)
        self.presentation_tab_index = self.tabs.addTab(
            self._scrollable_tab(self.presentation_editor),
            "Présentation",
        )
        self.diagnostics_tab_index = self.tabs.addTab(
            self._scrollable_tab(self._build_diagnostics_tab()),
            "Diagnostics",
        )
        self.settings_tab_index = self.tabs.addTab(
            self._scrollable_tab(self._build_settings_tab()), "Paramètres"
        )
        self.logs_tab_index = self.tabs.addTab(
            self._scrollable_tab(self._build_logs_tab()), "Journal"
        )
        self._build_inspector_dock()

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

    def _set_status_label(
        self,
        label: QLabel,
        text: str,
        style: str,
    ) -> None:
        label.setText(str(text))
        label.setObjectName(str(style or "Muted"))
        label.style().unpolish(label)
        label.style().polish(label)

    def _refresh_status_strip(self) -> None:
        if not hasattr(self, "interface_status"):
            return
        service = self._service
        client = self._client
        obs_raw = self.config.get("obs")
        obs_enabled = bool(
            client.config.enabled
            if client is not None
            else (
                obs_raw.get("enabled", False)
                if isinstance(obs_raw, Mapping)
                else False
            )
        )
        view = build_status_strip(
            expert_mode=self._expert_mode,
            edit_mode=self._edit_mode,
            paused=bool(service.paused) if service is not None else False,
            obs_enabled=obs_enabled,
            obs_connected=bool(client and client.connected),
            routing_incomplete=self._routing_incomplete,
            runtime_available=service is not None,
        )
        self._set_status_label(
            self.interface_status,
            view.interface_text,
            "Good" if self._expert_mode else "Muted",
        )
        self.mode_button.setText(view.interface_action)
        self.mode_button.setToolTip(
            "Afficher tous les écrans techniques."
            if not self._expert_mode
            else "Revenir aux parcours essentiels."
        )
        self._set_status_label(
            self.config_status,
            view.config_text,
            view.config_style,
        )
        self.edit_mode_button.setText(view.config_action)
        self.edit_mode_button.setToolTip(
            "Le mode édition gèle le routage automatique et autorise "
            "les modifications du brouillon."
        )
        self._set_status_label(
            self.routing_status_label,
            view.routing_text,
            view.routing_style,
        )
        self.pause_button.setText(view.routing_action)
        self.pause_button.setEnabled(
            service is not None and not self._edit_mode
        )
        self._set_status_label(
            self.obs_status,
            view.obs_text,
            view.obs_style,
        )
        if hasattr(self, "edit_banner"):
            self.edit_banner.setVisible(self._edit_mode)
        if hasattr(self, "configure_requirements"):
            if not obs_enabled:
                self.configure_requirements.setText(
                    "Activez « Piloter OBS » dans Paramètres avant de capturer."
                )
                self.configure_requirements.setObjectName("Warn")
            elif not bool(client and client.connected):
                self.configure_requirements.setText(
                    "OBS doit être connecté. Testez la connexion dans Paramètres."
                )
                self.configure_requirements.setObjectName("Warn")
            elif self._edit_mode:
                self.configure_requirements.setText(
                    "Mode édition actif : la capture modifiera uniquement le brouillon."
                )
                self.configure_requirements.setObjectName("Warn")
            else:
                self.configure_requirements.setText(
                    "Prêt. L’assistant activera automatiquement le Mode édition avant de modifier le brouillon."
                )
                self.configure_requirements.setObjectName("Good")
            self.configure_requirements.style().unpolish(
                self.configure_requirements
            )
            self.configure_requirements.style().polish(
                self.configure_requirements
            )
        if hasattr(self, "layout_obs_hint"):
            if not obs_enabled:
                hint = "Pilotage OBS désactivé : activez-le dans Paramètres."
                style = "Warn"
            elif not bool(client and client.connected):
                hint = "OBS déconnecté : synchronisation et application indisponibles."
                style = "Warn"
            else:
                hint = (
                    "Synchroniser lit uniquement OBS. Capturer écrit dans le "
                    "brouillon. Appliquer modifie OBS immédiatement."
                )
                style = "Good"
            self.layout_obs_hint.setText(hint)
            self.layout_obs_hint.setObjectName(style)
            self.layout_obs_hint.style().unpolish(self.layout_obs_hint)
            self.layout_obs_hint.style().polish(self.layout_obs_hint)

    def _apply_ui_mode(self) -> None:
        if not hasattr(self, "tabs"):
            return
        expert_only = (
            self.rules_tab_index,
            self.profiles_tab_index,
            self.layouts_tab_index,
            self.presentation_tab_index,
            self.diagnostics_tab_index,
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
        self.tabs.setTabVisible(self.configure_tab_index, True)
        self.tabs.setTabVisible(self.settings_tab_index, True)

        for widget_name in (
            "router_settings_card",
            "api_settings_card",
        ):
            widget = getattr(self, widget_name, None)
            if widget is not None:
                widget.setVisible(self._expert_mode)

        if (
            not self._expert_mode
            and self.tabs.currentIndex() in expert_only
        ):
            self.tabs.setCurrentIndex(self.dashboard_tab_index)
        if hasattr(self, "inspector_dock") and not self._expert_mode:
            self.inspector_dock.hide()
        self._refresh_status_strip()
        if hasattr(self, "unsaved"):
            self._refresh_config_revision_status()

    def _apply_edit_mode_surfaces(self) -> None:
        enabled = bool(self._edit_mode)
        for index in (
            getattr(self, "rules_tab_index", -1),
            getattr(self, "profiles_tab_index", -1),
            getattr(self, "layouts_tab_index", -1),
            getattr(self, "presentation_tab_index", -1),
            getattr(self, "settings_tab_index", -1),
        ):
            if index < 0 or not hasattr(self, "tabs"):
                continue
            scroll = self.tabs.widget(index)
            page = (
                scroll.widget()
                if scroll is not None
                and callable(getattr(scroll, "widget", None))
                else scroll
            )
            if page is not None:
                page.setEnabled(enabled)
            self.tabs.setTabToolTip(
                index,
                ""
                if enabled
                else (
                    "Lecture seule hors Mode édition. "
                    "Activez Mode édition pour modifier ce contenu."
                ),
            )

        for name in (
            "_import_config_action",
            "_restore_backup_action",
        ):
            action = getattr(self, name, None)
            if action is not None:
                action.setEnabled(enabled)

        self._refresh_obs_connected_controls()

    def _obs_connection_available(self) -> bool:
        client = self._client
        return bool(
            client is not None
            and client.config.enabled
            and client.connected
        )

    def _apply_obs_connected_control_state(
        self,
        control,
        *,
        requires_edit_mode: bool = False,
    ) -> None:
        connected = self._obs_connection_available()
        edit_allowed = not requires_edit_mode or bool(self._edit_mode)
        control.setEnabled(connected and edit_allowed)

        reasons: list[str] = []
        if not connected:
            reasons.append("Connexion OBS requise.")
        if requires_edit_mode and not self._edit_mode:
            reasons.append(
                "Activez Mode édition pour modifier le brouillon."
            )
        control.setToolTip(" ".join(reasons))

    def _register_obs_connected_control(
        self,
        control,
        *,
        requires_edit_mode: bool = False,
    ) -> None:
        self._obs_connected_controls.append(
            (control, bool(requires_edit_mode))
        )
        self._apply_obs_connected_control_state(
            control,
            requires_edit_mode=requires_edit_mode,
        )

    def _refresh_obs_connected_controls(self) -> None:
        for control, requires_edit_mode in tuple(
            self._obs_connected_controls
        ):
            self._apply_obs_connected_control_state(
                control,
                requires_edit_mode=requires_edit_mode,
            )
        self._refresh_manual_undo_controls()

    def _register_manual_undo_control(self, control) -> None:
        self._manual_undo_controls.append(control)
        self._refresh_manual_undo_controls()

    def _refresh_manual_undo_controls(self) -> None:
        checkpoint = self._manual_undo
        for control in tuple(self._manual_undo_controls):
            if checkpoint is None:
                control.setEnabled(False)
                control.setToolTip(
                    "Aucune opération manuelle réversible disponible."
                )
                continue

            kind = str(checkpoint.get("kind") or "")
            label = str(checkpoint.get("label") or "dernière opération")
            if kind == "config":
                enabled = bool(self._edit_mode)
                tooltip = (
                    f"Annuler : {label}"
                    if enabled
                    else (
                        "Activez Mode édition pour restaurer le brouillon "
                        f"précédent ({label})."
                    )
                )
            elif kind == "layout_obs":
                enabled = self._obs_connection_available()
                tooltip = (
                    f"Annuler : {label}"
                    if enabled
                    else (
                        "Connexion OBS requise pour restaurer le layout "
                        f"précédent ({label})."
                    )
                )
            else:
                enabled = False
                tooltip = "Cette opération ne possède pas de rollback sûr."

            control.setEnabled(enabled)
            control.setToolTip(tooltip)

        if hasattr(self, "history_undo_status"):
            if checkpoint is None:
                self.history_undo_status.setText(
                    "Dernière opération réversible : aucune"
                )
                self.history_undo_status.setObjectName("Muted")
            else:
                label = str(
                    checkpoint.get("label") or "dernière opération"
                )
                self.history_undo_status.setText(
                    f"Dernière opération réversible : {label}"
                )
                self.history_undo_status.setObjectName("Good")
            self.history_undo_status.style().unpolish(
                self.history_undo_status
            )
            self.history_undo_status.style().polish(
                self.history_undo_status
            )

    def _set_config_undo_checkpoint(
        self,
        label: str,
        previous_config: Mapping[str, object],
    ) -> None:
        previous = copy.deepcopy(dict(previous_config))
        if config_revision(previous) == config_revision(self.config):
            return
        self._manual_undo = {
            "kind": "config",
            "label": str(label),
            "previous_config": previous,
            "after_revision": config_revision(self.config),
        }
        self._refresh_manual_undo_controls()

    def _set_layout_undo_checkpoint(self, label: str) -> None:
        self._manual_undo = {
            "kind": "layout_obs",
            "label": str(label),
        }
        self._refresh_manual_undo_controls()

    def _clear_manual_undo_checkpoint(self) -> None:
        self._manual_undo = None
        self._manual_undo_request_id = ""
        self._refresh_manual_undo_controls()

    def _invalidate_layout_undo_checkpoint(self) -> None:
        checkpoint = self._manual_undo
        if (
            isinstance(checkpoint, Mapping)
            and str(checkpoint.get("kind") or "") == "layout_obs"
        ):
            self._clear_manual_undo_checkpoint()

    def _undo_last_manual_operation(self) -> None:
        checkpoint = self._manual_undo
        if checkpoint is None:
            QMessageBox.information(
                self,
                "Annuler la dernière opération",
                "Aucune opération manuelle réversible n’est disponible.",
            )
            return

        kind = str(checkpoint.get("kind") or "")
        label = str(checkpoint.get("label") or "dernière opération")

        if kind == "config":
            if not self._require_edit_mode("Annuler la dernière opération"):
                return
            previous = checkpoint.get("previous_config")
            if not isinstance(previous, Mapping):
                self._clear_manual_undo_checkpoint()
                return

            self._collect_settings()
            expected_revision = str(
                checkpoint.get("after_revision") or ""
            )
            current_revision = config_revision(self.config)
            if (
                expected_revision
                and current_revision != expected_revision
                and QMessageBox.question(
                    self,
                    "Annuler la dernière opération",
                    (
                        "Le brouillon a été modifié depuis cette opération.\n\n"
                        "L’annuler restaurera son snapshot précédent et "
                        "supprimera les modifications effectuées ensuite. "
                        "Continuer ?"
                    ),
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                != QMessageBox.Yes
            ):
                return

            self.config = copy.deepcopy(dict(previous))
            self._load_config_into_ui()
            dirty = config_revision(self.config) != self._saved_revision
            self._refresh_config_revision_status(draft_dirty=dirty)
            self._refresh_dashboard_summary()
            self._record_user_activity(
                UserActivityEntry(
                    "Good",
                    "Dernière opération annulée",
                    label,
                )
            )
            self.statusBar().showMessage(
                f"Rollback du brouillon effectué : {label}",
                6000,
            )
            self._clear_manual_undo_checkpoint()
            return

        if kind == "layout_obs":
            if self._service is None or not self._obs_connection_available():
                QMessageBox.warning(
                    self,
                    "Annuler la dernière opération",
                    "OBS doit être connecté pour restaurer le layout précédent.",
                )
                return
            if not self._safe_live_confirm(
                "Restaurer le layout OBS précédent"
            ):
                return
            try:
                request_id = self._service.request_layout("undo")
                self._manual_undo_request_id = request_id
                self._track_obs_request(
                    request_id,
                    busy_text="Restauration…",
                )
                self.statusBar().showMessage(
                    "Restauration du layout OBS précédent…",
                    4000,
                )
            except Exception as exc:
                QMessageBox.critical(
                    self,
                    "Annuler la dernière opération",
                    str(exc),
                )
            return

        QMessageBox.information(
            self,
            "Annuler la dernière opération",
            "Cette opération ne possède pas de rollback sûr.",
        )

    def _track_obs_request(
        self,
        request_id: str,
        *,
        busy_text: str,
        control=None,
    ) -> None:
        request_id = str(request_id or "").strip()
        if not request_id:
            return
        if control is None:
            control = self.sender()
        if control is None or not callable(
            getattr(control, "setEnabled", None)
        ):
            return

        text_getter = getattr(control, "text", None)
        original = (
            str(text_getter())
            if callable(text_getter)
            else ""
        )
        self._pending_obs_controls[request_id] = (control, original)
        try:
            control.setEnabled(False)
            setter = getattr(control, "setText", None)
            if original and callable(setter):
                setter(str(busy_text or "En cours…"))
        except RuntimeError:
            self._pending_obs_controls.pop(request_id, None)

    def _finish_obs_request_feedback(
        self,
        request_id: str,
        *,
        success: bool,
    ) -> None:
        pending = self._pending_obs_controls.pop(
            str(request_id or ""),
            None,
        )
        if pending is None:
            return
        control, original = pending
        setter = getattr(control, "setText", None)
        try:
            if original and callable(setter):
                setter(
                    ("✓ " if success else "✕ ")
                    + original
                )
            control.setEnabled(False)
        except RuntimeError:
            return

        def restore() -> None:
            try:
                if original and callable(setter):
                    setter(original)
                control.setEnabled(True)
                self._refresh_obs_connected_controls()
            except RuntimeError:
                pass

        QTimer.singleShot(1200, restore)

    def _require_edit_mode(self, operation: str) -> bool:
        if self._edit_mode:
            return True
        QMessageBox.information(
            self,
            "Mode édition requis",
            (
                f"{operation}\n\n"
                "Activez « Mode édition » en haut de la fenêtre. "
                "SSR gèlera alors le routage automatique pendant que vous "
                "modifiez le brouillon."
            ),
        )
        return False

    @staticmethod
    def _collection_import_requires_edit_mode(mode: str) -> bool:
        # The guided collection analysis is read-only. Every other current or
        # future completion mode is treated conservatively as draft-mutating.
        return str(mode or "").strip().casefold() != "guided_analysis"

    def _schedule_dashboard_refresh(self) -> None:
        timer = getattr(self, "_dashboard_refresh_timer", None)
        if timer is None:
            self._refresh_dashboard_summary()
            return
        timer.start()

    def _refresh_dashboard_summary(self) -> None:
        if not hasattr(self, "dashboard_health"):
            return
        service = self._service
        client = self._client
        self._refresh_status_strip()
        if service is None:
            self.dashboard_health.setText("Runtime indisponible")
            self.dashboard_health.setObjectName("Bad")
            self.dashboard_decision.setText("Décision : —")
            self.dashboard_reason.setText(
                "Pourquoi : le moteur de routage n’est pas actif."
            )
            self.dashboard_override_row.setVisible(False)
            self.dashboard_diff.clear()
            if hasattr(self, "drift_card"):
                self.drift_card.setVisible(False)
            if hasattr(self, "diagnostics_status"):
                self.diagnostics_status.setText(
                    "Runtime indisponible"
                )
                self.diagnostics_detail.setText(
                    "Le moteur de routage n’est pas actif."
                )
            return

        try:
            explanation = service.explain_decision()
            routing_status = service.routing_status()
        except Exception as exc:
            self.dashboard_health.setText(
                "Diagnostic indisponible"
            )
            self.dashboard_health.setObjectName("Warn")
            self.dashboard_decision.setText("Décision : —")
            self.dashboard_reason.setText(f"Pourquoi : {exc}")
            self.dashboard_override_row.setVisible(False)
            self.dashboard_diff.clear()
            if hasattr(self, "drift_card"):
                self.drift_card.setVisible(False)
            return

        snapshot = build_dashboard_snapshot(
            explanation,
            routing_status,
            obs_enabled=bool(client and client.config.enabled),
            obs_connected=bool(client and client.connected),
        )
        self._set_status_label(
            self.dashboard_health,
            snapshot.health_text,
            snapshot.health_style,
        )
        self.dashboard_decision.setText(
            f"Décision : {snapshot.decision}"
        )
        self.dashboard_reason.setText(
            f"Pourquoi : {snapshot.reason}"
        )
        self._refresh_decision_trail(explanation)

        override_view = build_manual_override_presentation(
            service.manual_override_status()
        )
        self.dashboard_override_row.setVisible(
            override_view.active
        )
        if override_view.active:
            self._set_status_label(
                self.dashboard_override_label,
                (
                    f"{override_view.title} · "
                    f"{override_view.detail}"
                ),
                override_view.style,
            )

        self.dashboard_diff.clear()
        for difference in snapshot.differences:
            item = QTreeWidgetItem(
                [
                    difference.label,
                    difference.desired,
                    difference.applied,
                    difference.status_label,
                ]
            )
            if difference.message:
                item.setToolTip(3, difference.message)
            self.dashboard_diff.addTopLevelItem(item)

        drift_status = service.drift_status()
        drift_view = build_obs_drift_presentation(
            drift_status,
            ignored_signature=self._ignored_drift_signature,
        )
        self.drift_card.setVisible(drift_view.visible)
        if drift_view.visible:
            self.drift_title.setText(drift_view.title)
            self.drift_detail.setText(
                drift_view.detail
                or (
                    "OBS ne correspond plus à une partie de la "
                    "cible gérée par SSR."
                )
            )

        self._refresh_dashboard_context_action(
            snapshot,
            override_active=override_view.active,
            drift_detected=drift_view.visible,
            drift_count=(
                int(drift_status.get("count", 0) or 0)
                if isinstance(drift_status, Mapping)
                else 0
            ),
        )
        if hasattr(self, "diagnostics_status"):
            self._set_status_label(
                self.diagnostics_status,
                snapshot.health_text,
                snapshot.health_style,
            )
            self.diagnostics_detail.setText(
                f"{snapshot.decision} · {snapshot.reason}"
            )

    def _clear_layout(self, layout: QHBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _open_rule_by_name(self, name: str) -> None:
        self._ensure_expert_mode()
        self.tabs.setCurrentIndex(self.rules_tab_index)
        for row, rule in enumerate(self.config.get("rules", [])):
            if (
                isinstance(rule, Mapping)
                and str(rule.get("name") or "") == str(name)
            ):
                self.rules_table.selectRow(row)
                item = self.rules_table.item(row, 1)
                if item is not None:
                    self.rules_table.scrollToItem(item)
                return

    def _open_profile_target(
        self,
        domain: str,
        name: str,
    ) -> None:
        self._ensure_expert_mode()
        if domain == "layout":
            self.tabs.setCurrentIndex(self.layouts_tab_index)
            self.layout_profile_name.setCurrentText(str(name))
            self._refresh_layout_profile_view()
            return
        self.tabs.setCurrentIndex(self.profiles_tab_index)
        index = self.profile_domain.findData(str(domain))
        if index >= 0:
            self.profile_domain.setCurrentIndex(index)
        self._refresh_profile_names()
        self.profile_name.setCurrentText(str(name))

    def _refresh_decision_trail(
        self,
        explanation: Mapping[str, object],
    ) -> None:
        layout = getattr(self, "dashboard_trail_layout", None)
        if layout is None:
            return
        self._clear_layout(layout)
        trail = build_decision_trail(explanation)
        if not trail:
            empty = QLabel("Aucune décision active")
            empty.setObjectName("Muted")
            layout.addWidget(empty)
            layout.addStretch(1)
            return
        for index, step in enumerate(trail):
            if index:
                arrow = QLabel("→")
                arrow.setObjectName("Muted")
                layout.addWidget(arrow)
            button = QPushButton(step.label)
            button.setObjectName("Compact")
            if step.kind == "rule":
                button.clicked.connect(
                    lambda _checked=False, name=step.target:
                    self._open_rule_by_name(name)
                )
            elif step.kind == "profile":
                button.clicked.connect(
                    lambda _checked=False,
                    domain=step.domain,
                    name=step.target:
                    self._open_profile_target(domain, name)
                )
            else:
                button.setEnabled(False)
            layout.addWidget(button)
        layout.addStretch(1)

    def _execute_dashboard_action(self) -> None:
        key = str(
            getattr(self, "_dashboard_context_action_key", "")
            or ""
        )
        if key == "obs_settings":
            self.tabs.setCurrentIndex(self.settings_tab_index)
        elif key == "obs_test":
            self.tabs.setCurrentIndex(self.settings_tab_index)
            self._test_obs()
        elif key == "review_draft":
            self._show_draft_change_review()
        elif key == "apply_config":
            self.save_and_apply()
        elif key == "resume":
            if self._service is not None and self._service.paused:
                self._toggle_pause()
        elif key == "clear_override":
            self._clear_override()
        elif key == "reapply":
            self._force_reapply()
        elif key == "diagnose":
            self._show_guided_diagnostic()

    def _refresh_dashboard_context_action(
        self,
        snapshot,
        *,
        override_active: bool,
        drift_detected: bool,
        drift_count: int = 0,
    ) -> None:
        client = self._client
        service = self._service
        view = build_contextual_action(
            obs_enabled=bool(client and client.config.enabled),
            obs_connected=bool(client and client.connected),
            paused=bool(service and service.paused),
            draft_dirty=self._draft_dirty,
            revision_mismatch=(
                bool(self._saved_revision)
                and bool(self._applied_revision)
                and self._saved_revision != self._applied_revision
            ),
            override_active=override_active,
            drift_detected=drift_detected,
            difference_statuses=tuple(
                difference.status
                for difference in snapshot.differences
            ),
            difference_count=(
                max(0, int(drift_count or 0))
                if drift_detected
                else sum(
                    1
                    for difference in snapshot.differences
                    if difference.status_label
                    not in {"Conforme", "Non géré"}
                )
            ),
        )
        self._dashboard_context_action_key = view.key
        self.dashboard_action_title.setText(view.title)
        self.dashboard_action_detail.setText(view.detail)
        self.dashboard_action_button.setVisible(view.actionable)
        self.dashboard_action_button.setText(view.button_text)
        object_name = {
            "Good": "ActionCardGood",
            "Warn": "ActionCardWarn",
            "Bad": "ActionCardBad",
        }.get(view.style, "ActionCardGood")
        self.dashboard_action_card.setObjectName(object_name)
        self.dashboard_action_card.style().unpolish(
            self.dashboard_action_card
        )
        self.dashboard_action_card.style().polish(
            self.dashboard_action_card
        )

    def _ignore_current_obs_drift(self) -> None:
        service = self._service
        if service is None:
            return
        status = service.drift_status()
        signature = str(status.get("signature") or "")
        if not signature:
            return
        self._ignored_drift_signature = signature
        self._record_user_activity(
            UserActivityEntry(
                "Muted",
                "Écart OBS ignoré",
                "Masqué jusqu’au prochain changement détecté.",
            )
        )
        self._schedule_dashboard_refresh()

    def _show_capability_report(self) -> None:
        service = self._service
        client = self._client
        self._collect_settings()

        system_report = run_system_check(self.config)
        self._last_system_check_report = system_report
        self._populate_attention_center(system_report)
        report = system_report.capabilities

        dialog = QDialog(self)
        dialog.setWindowTitle("Santé et capacités SSR")
        dialog.resize(1080, 650)
        root = QVBoxLayout(dialog)

        title = QLabel(report.summary)
        title.setStyleSheet("font-size: 15pt; font-weight: 700;")
        root.addWidget(title)

        if system_report.config_errors:
            validation = QLabel(
                "Configuration invalide :\n• "
                + "\n• ".join(system_report.config_errors)
            )
            validation.setWordWrap(True)
            validation.setObjectName("Bad")
            root.addWidget(validation)

        capabilities = QTreeWidget()
        capabilities.setColumnCount(4)
        capabilities.setHeaderLabels(
            ["Capacité", "État", "Détail", "Action recommandée"]
        )
        capabilities.setRootIsDecorated(False)
        capabilities.setAlternatingRowColors(True)
        for item in report.items:
            row = QTreeWidgetItem(
                [
                    item.label,
                    item.status_label,
                    item.detail,
                    item.action,
                ]
            )
            row.setData(0, Qt.UserRole, item.key)
            row.setToolTip(
                0,
                "Double-cliquez pour ouvrir l’écran le plus pertinent.",
            )
            capabilities.addTopLevelItem(row)
        capabilities.header().setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        capabilities.header().setSectionResizeMode(
            1,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        capabilities.header().setSectionResizeMode(
            2,
            QHeaderView.ResizeMode.Stretch,
        )
        capabilities.header().setSectionResizeMode(
            3,
            QHeaderView.ResizeMode.Stretch,
        )
        root.addWidget(capabilities, 1)

        if report.findings:
            findings_title = QLabel("Santé de la configuration")
            findings_title.setObjectName("Section")
            root.addWidget(findings_title)
            findings = QTreeWidget()
            findings.setColumnCount(4)
            findings.setHeaderLabels(
                ["Niveau", "Diagnostic", "Détail", "Action"]
            )
            findings.setRootIsDecorated(False)
            findings.setAlternatingRowColors(True)
            findings.setMaximumHeight(220)
            severity_label = {
                "error": "Erreur",
                "warning": "À vérifier",
                "info": "Information",
            }
            for finding in report.findings:
                findings.addTopLevelItem(
                    QTreeWidgetItem(
                        [
                            severity_label.get(
                                finding.severity,
                                finding.severity,
                            ),
                            finding.title,
                            finding.detail,
                            finding.action,
                        ]
                    )
                )
            findings.header().setSectionResizeMode(
                0,
                QHeaderView.ResizeMode.ResizeToContents,
            )
            findings.header().setSectionResizeMode(
                1,
                QHeaderView.ResizeMode.ResizeToContents,
            )
            findings.header().setSectionResizeMode(
                2,
                QHeaderView.ResizeMode.Stretch,
            )
            findings.header().setSectionResizeMode(
                3,
                QHeaderView.ResizeMode.Stretch,
            )
            root.addWidget(findings)

        def navigate_capability(*_args) -> None:
            selected = capabilities.currentItem()
            if selected is None:
                return
            key = str(selected.data(0, Qt.UserRole) or "")
            dialog.accept()
            if key in {"obs", "audio", "hdr"}:
                self.tabs.setCurrentIndex(self.settings_tab_index)
            elif key == "obs_references":
                self.tabs.setCurrentIndex(self.configure_tab_index)
            else:
                self._open_diagnostics_tab()

        capabilities.itemDoubleClicked.connect(navigate_capability)

        actions = QHBoxLayout()
        open_related = QPushButton("Ouvrir l’emplacement")
        open_related.clicked.connect(navigate_capability)
        actions.addWidget(open_related)

        if service is not None and client is not None and client.connected:
            sync_catalog = QPushButton("Synchroniser le catalogue OBS")
            def sync_and_close() -> None:
                try:
                    request_id = service.request_catalog_sync()
                except Exception as exc:
                    QMessageBox.critical(
                        dialog,
                        "Catalogue OBS",
                        str(exc),
                    )
                    return
                self.statusBar().showMessage(
                    f"Synchronisation catalogue OBS mise en file ({request_id[:8]}).",
                    8000,
                )
                dialog.accept()
            sync_catalog.clicked.connect(sync_and_close)
            actions.addWidget(sync_catalog)

        repair_refs = QPushButton("Réparer les références OBS…")
        repair_refs.clicked.connect(dialog.accept)
        repair_refs.clicked.connect(self._start_reference_repair)
        self._apply_obs_connected_control_state(
            repair_refs,
            requires_edit_mode=True,
        )
        actions.addWidget(repair_refs)

        restore = QPushButton("Historique des sauvegardes…")
        restore.clicked.connect(dialog.accept)
        restore.clicked.connect(self._restore_config_backup)
        actions.addWidget(restore)

        copy_diagnostic = QPushButton("Copier le diagnostic")
        def copy_diagnostic_text() -> None:
            QApplication.clipboard().setText(
                render_capability_report_text(
                    report,
                    version=__version__,
                )
            )
            self.statusBar().showMessage(
                "Diagnostic SSR copié dans le presse-papiers",
                4000,
            )
        copy_diagnostic.clicked.connect(copy_diagnostic_text)
        actions.addWidget(copy_diagnostic)

        open_settings = QPushButton("Ouvrir Paramètres")
        open_settings.clicked.connect(dialog.accept)
        open_settings.clicked.connect(
            lambda: self.tabs.setCurrentIndex(
                self.settings_tab_index
            )
        )
        actions.addWidget(open_settings)

        open_diagnostics = QPushButton("Ouvrir Diagnostics")
        open_diagnostics.clicked.connect(dialog.accept)
        open_diagnostics.clicked.connect(
            self._open_diagnostics_tab
        )
        actions.addWidget(open_diagnostics)

        actions.addStretch(1)
        close = QPushButton("Fermer")
        close.clicked.connect(dialog.accept)
        actions.addWidget(close)
        root.addLayout(actions)
        dialog.exec()

    def _show_dependency_tree(self) -> None:
        service = self._service
        if service is None:
            QMessageBox.warning(
                self,
                "Dépendances",
                "Le runtime SSR n’est pas disponible.",
            )
            return
        try:
            explanation = service.explain_decision()
            root_node = build_effective_dependency_tree(
                self.config,
                explanation,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Dépendances", str(exc))
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Arbre des dépendances")
        dialog.resize(980, 650)
        root = QVBoxLayout(dialog)

        intro = QLabel(
            "Cette vue suit la décision courante jusqu’aux profils, bases "
            "héritées et actions/modules qui composent l’état effectif."
        )
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        root.addWidget(intro)

        tree = QTreeWidget()
        tree.setColumnCount(3)
        tree.setHeaderLabels(["Élément", "Valeur", "Détail"])
        tree.setAlternatingRowColors(True)

        def add_node(parent, node) -> None:
            item = QTreeWidgetItem(
                [node.label, node.value, node.detail]
            )
            if parent is None:
                tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            for child in node.children:
                add_node(item, child)

        add_node(None, root_node)
        tree.expandAll()
        tree.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        tree.header().setStretchLastSection(True)
        root.addWidget(tree, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        close = QPushButton("Fermer")
        close.clicked.connect(dialog.accept)
        actions.addWidget(close)
        root.addLayout(actions)
        dialog.exec()

    def _show_effective_provenance(self) -> None:
        service = self._service
        if service is None:
            QMessageBox.warning(
                self,
                "Provenance",
                "Le runtime SSR n’est pas disponible.",
            )
            return
        try:
            explanation = service.explain_decision()
            rows = build_effective_provenance(
                self.config,
                explanation,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Provenance", str(exc))
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Qui contrôle quoi ?")
        dialog.resize(1000, 520)
        root = QVBoxLayout(dialog)

        intro = QLabel(
            "Cette vue explique d’où vient chaque profil effectif, son héritage "
            "et les autres éléments de configuration qui le référencent."
        )
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        root.addWidget(intro)

        tree = QTreeWidget()
        tree.setColumnCount(6)
        tree.setHeaderLabels(
            [
                "Domaine",
                "Profil effectif",
                "Sélectionné par",
                "Héritage",
                "Contenu effectif",
                "Utilisé par",
            ]
        )
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        for row in rows:
            lineage = (
                " ← ".join(row.lineage)
                if row.lineage
                else "—"
            )
            usage_text = " · ".join(
                usage.owner for usage in row.usages
            ) or "—"
            item = QTreeWidgetItem(
                [
                    row.label,
                    row.profile,
                    row.selected_by,
                    lineage,
                    row.content_summary,
                    usage_text,
                ]
            )
            if len(row.usages) > 1:
                item.setToolTip(
                    5,
                    (
                        "Ce profil est partagé. Une modification peut affecter "
                        "plusieurs règles, le fallback ou des profils enfants."
                    ),
                )
            tree.addTopLevelItem(item)
        tree.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        tree.header().setStretchLastSection(True)
        root.addWidget(tree, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        expert = QPushButton("Ouvrir Diagnostics")
        expert.clicked.connect(dialog.accept)
        expert.clicked.connect(self._open_diagnostics_tab)
        actions.addWidget(expert)
        close = QPushButton("Fermer")
        close.clicked.connect(dialog.accept)
        actions.addWidget(close)
        root.addLayout(actions)
        dialog.exec()

    def _start_reference_repair(self) -> None:
        if not self._require_edit_mode("Réparer les références OBS"):
            return
        service = self._service
        client = self._client
        if service is None:
            QMessageBox.warning(
                self,
                "Références OBS",
                "Le runtime SSR n’est pas disponible.",
            )
            return
        if (
            client is None
            or not client.config.enabled
            or not client.connected
        ):
            QMessageBox.warning(
                self,
                "Références OBS",
                "OBS doit être connecté pour analyser les références.",
            )
            return
        try:
            request_id = service.request_collection_import_preview(
                include_layouts=False,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Références OBS", str(exc))
            return
        self._pending_collection_imports[request_id] = {
            "mode": "reference_repair",
            "domain": "",
            "profile_name": "",
            "options": {},
        }
        self._record_user_activity(
            UserActivityEntry(
                "Muted",
                "Analyse des références OBS démarrée",
            )
        )
        self.statusBar().showMessage(
            "Analyse des références OBS en cours…",
            8000,
        )

    def _show_reference_repair_dialog(self, snapshot) -> None:
        issues = scan_obs_reference_repairs(self.config, snapshot)
        if not issues:
            QMessageBox.information(
                self,
                "Références OBS",
                "Aucune référence OBS cassée n’a été détectée.",
            )
            self._record_user_activity(
                UserActivityEntry(
                    "Good",
                    "Références OBS vérifiées",
                    "Aucune référence cassée détectée",
                )
            )
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Réparer les références OBS")
        dialog.resize(1050, 620)
        root = QVBoxLayout(dialog)

        intro = QLabel(
            "SSR compare les noms stockés dans la configuration avec la "
            "collection OBS actuelle. Les propositions floues ne sont jamais "
            "appliquées automatiquement."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        tree = QTreeWidget()
        tree.setColumnCount(6)
        tree.setHeaderLabels(
            [
                "Appliquer",
                "Type",
                "Emplacement",
                "Référence actuelle",
                "Proposition",
                "Confiance",
            ]
        )
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        for index, issue in enumerate(issues):
            confidence = (
                f"{issue.confidence * 100:.0f} %"
                if issue.candidate
                else "—"
            )
            item = QTreeWidgetItem(
                [
                    "",
                    issue.kind,
                    issue.location,
                    issue.current,
                    issue.candidate or "Aucune proposition sûre",
                    confidence,
                ]
            )
            item.setData(0, Qt.UserRole, index)
            item.setToolTip(4, issue.reason)
            if issue.repairable:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(
                    0,
                    Qt.Checked
                    if issue.confidence >= 0.98
                    else Qt.Unchecked,
                )
            else:
                item.setFlags(
                    item.flags() & ~Qt.ItemIsUserCheckable
                )
            tree.addTopLevelItem(item)
        tree.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        tree.header().setStretchLastSection(True)
        root.addWidget(tree, 1)

        unresolved = sum(1 for item in issues if not item.repairable)
        note = QLabel(
            (
                "Les corrections sélectionnées seront appliquées uniquement au "
                "brouillon SSR. OBS et le runtime ne changeront qu’après "
                "« Enregistrer et appliquer »."
                + (
                    f" · {unresolved} référence(s) restent sans proposition sûre."
                    if unresolved
                    else ""
                )
            )
        )
        note.setWordWrap(True)
        note.setObjectName("Muted")
        root.addWidget(note)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("Annuler")
        cancel.clicked.connect(dialog.reject)
        actions.addWidget(cancel)
        apply_button = QPushButton("Appliquer au brouillon")
        apply_button.setObjectName("Primary")
        actions.addWidget(apply_button)
        root.addLayout(actions)

        def apply_selected() -> None:
            selected = []
            for row in range(tree.topLevelItemCount()):
                item = tree.topLevelItem(row)
                if item.checkState(0) != Qt.Checked:
                    continue
                try:
                    index = int(item.data(0, Qt.UserRole))
                except (TypeError, ValueError):
                    continue
                if 0 <= index < len(issues):
                    selected.append(issues[index])
            if not selected:
                QMessageBox.information(
                    dialog,
                    "Références OBS",
                    "Sélectionnez au moins une proposition à appliquer.",
                )
                return
            previous_config = copy.deepcopy(self.config)
            draft, applied = apply_reference_repairs(
                self.config,
                selected,
            )
            errors = validate_config(draft)
            if errors:
                QMessageBox.critical(
                    dialog,
                    "Références OBS",
                    "Le brouillon réparé n’est pas valide :\n- "
                    + "\n- ".join(errors),
                )
                return
            if applied <= 0:
                QMessageBox.information(
                    dialog,
                    "Références OBS",
                    "Aucune référence n’a pu être modifiée.",
                )
                return

            self.config = draft
            self._load_config_into_ui()
            self._mark_dirty()
            self._set_config_undo_checkpoint(
                f"Réparation de {applied} référence(s) OBS",
                previous_config,
            )
            self._refresh_dashboard_summary()
            self._record_user_activity(
                UserActivityEntry(
                    "Good",
                    "Références OBS réparées dans le brouillon",
                    f"{applied} remplacement(s)",
                )
            )
            self.statusBar().showMessage(
                (
                    f"{applied} référence(s) réparée(s) dans le brouillon — "
                    "enregistrez et appliquez pour activer les changements."
                ),
                10000,
            )
            dialog.accept()

        apply_button.clicked.connect(apply_selected)
        dialog.exec()

    def _card(self, title_text: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("Card")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(16, 14, 16, 14)
        label = QLabel(title_text)
        label.setObjectName("Section")
        lay.addWidget(label)
        return frame, lay

    @staticmethod
    def _set_action_risk(
        button: QPushButton,
        risk: str,
        detail: str = "",
    ) -> None:
        normalized = str(risk or "").strip().casefold()
        names = {
            "read": "ReadOnlyAction",
            "draft": "DraftAction",
            "live": "LiveAction",
        }
        prefixes = {
            "read": "Lecture seule",
            "draft": "Modifie le brouillon SSR",
            "live": "Peut modifier OBS immédiatement",
        }
        if normalized in names:
            button.setObjectName(names[normalized])
            prefix = prefixes[normalized]
            button.setToolTip(
                prefix + (f" · {detail}" if detail else "")
            )


    def _build_inspector_dock(self) -> None:
        self.inspector_dock = QDockWidget("Inspecteur", self)
        self.inspector_dock.setObjectName("InspectorDock")
        self.inspector_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
        )
        body = QWidget()
        root = QVBoxLayout(body)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        self.inspector_title = QLabel("Aucun élément sélectionné")
        self.inspector_title.setStyleSheet(
            "font-size: 14pt; font-weight: 700;"
        )
        self.inspector_health = QLabel("—")
        self.inspector_health.setObjectName("Muted")
        self.inspector_summary = QLabel(
            "Sélectionnez une règle, un profil ou un layout."
        )
        self.inspector_summary.setObjectName("Muted")
        self.inspector_summary.setWordWrap(True)
        root.addWidget(self.inspector_title)
        root.addWidget(self.inspector_health)
        root.addWidget(self.inspector_summary)

        self.inspector_tree = QTreeWidget()
        self.inspector_tree.setColumnCount(2)
        self.inspector_tree.setHeaderLabels(["Information", "Valeur"])
        self.inspector_tree.setRootIsDecorated(False)
        self.inspector_tree.setAlternatingRowColors(True)
        self.inspector_tree.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.inspector_tree.header().setStretchLastSection(True)
        root.addWidget(self.inspector_tree, 1)

        actions = QHBoxLayout()
        self.inspector_open_button = QPushButton("Ouvrir")
        self.inspector_open_button.clicked.connect(
            self._open_inspector_target
        )
        actions.addWidget(self.inspector_open_button)
        self.inspector_favorite_button = QPushButton("☆ Épingler")
        self.inspector_favorite_button.clicked.connect(
            self._toggle_inspector_favorite
        )
        actions.addWidget(self.inspector_favorite_button)
        self.inspector_impact_button = QPushButton("Impact / dépendances")
        self.inspector_impact_button.clicked.connect(
            self._show_inspector_impact
        )
        actions.addWidget(self.inspector_impact_button)
        raw = QPushButton("JSON…")
        raw.clicked.connect(self._show_inspector_raw)
        actions.addWidget(raw)
        actions.addStretch(1)
        root.addLayout(actions)

        self.inspector_dock.setWidget(body)
        self.addDockWidget(
            Qt.DockWidgetArea.RightDockWidgetArea,
            self.inspector_dock,
        )
        self.inspector_dock.hide()

    def _remember_inspector_target(
        self,
        kind: str,
        domain: str,
        target: str,
    ) -> None:
        item = (
            str(kind or ""),
            str(domain or ""),
            str(target or ""),
        )
        if not item[2]:
            return
        self._recent_inspector_targets = [
            current
            for current in self._recent_inspector_targets
            if current != item
        ]
        self._recent_inspector_targets.insert(0, item)
        self._recent_inspector_targets = self._recent_inspector_targets[:8]

    def _set_inspector(
        self,
        *,
        kind: str,
        title: str,
        domain: str = "",
        target: str = "",
        summary: str = "",
        health_text: str = "—",
        health_style: str = "Muted",
        rows: list[tuple[str, str]] | None = None,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        if not hasattr(self, "inspector_dock"):
            return
        self._inspector_context = (
            str(kind or ""),
            str(domain or ""),
            str(target or title),
        )
        self._inspector_payload = (
            copy.deepcopy(dict(payload))
            if isinstance(payload, Mapping)
            else None
        )
        self._remember_inspector_target(
            kind,
            domain,
            target or title,
        )
        self.inspector_title.setText(title)
        self._set_status_label(
            self.inspector_health,
            health_text,
            health_style,
        )
        self.inspector_summary.setText(summary or "—")
        self.inspector_tree.clear()
        for label, value in rows or []:
            self.inspector_tree.addTopLevelItem(
                QTreeWidgetItem([str(label), str(value)])
            )
        self.inspector_open_button.setEnabled(bool(target))
        self.inspector_impact_button.setEnabled(
            kind in {"profile", "layout"}
        )
        favorite_key = (
            str(kind or ""),
            str(domain or ""),
            str(target or title),
        )
        self.inspector_favorite_button.setEnabled(bool(favorite_key[2]))
        self.inspector_favorite_button.setText(
            "★ Épinglé"
            if favorite_key in self._favorite_targets
            else "☆ Épingler"
        )
        if self._expert_mode:
            self.inspector_dock.show()

    def _inspect_selected_rule(self) -> None:
        if not hasattr(self, "rules_table"):
            return
        idx = self._selected_rule_index()
        if idx is None:
            return
        raw_rules = self.config.get("rules")
        if not isinstance(raw_rules, list) or not 0 <= idx < len(raw_rules):
            return
        rule = raw_rules[idx]
        if not isinstance(rule, Mapping):
            return
        name = str(rule.get("name") or f"Règle {idx + 1}")
        health = build_rule_health(rule, self.config)
        conditions = (
            rule.get("conditions")
            if isinstance(rule.get("conditions"), Mapping)
            else {}
        )
        state = (
            rule.get("state")
            if isinstance(rule.get("state"), Mapping)
            else {}
        )
        rows = [
            ("Priorité", str(rule.get("priority", 0))),
            (
                "État",
                "Active" if bool(rule.get("enabled", True)) else "Désactivée",
            ),
            ("Comportement", str(rule.get("behavior") or "match")),
            ("Processus", str(rule.get("exe") or "—")),
            ("Chemin", str(rule.get("path") or "—")),
            ("Titre", str(rule.get("title_regex") or "—")),
        ]
        for key, value in conditions.items():
            rows.append((f"Condition · {key}", str(value)))
        for key, value in state.items():
            rows.append((f"Profil · {key}", str(value)))
        self._set_inspector(
            kind="rule",
            title=name,
            target=name,
            summary=humanize_rule(rule),
            health_text=health.text,
            health_style=health.style,
            rows=rows,
            payload=rule,
        )
        if hasattr(self, "rule_human_summary"):
            self.rule_human_summary.setText(humanize_rule(rule))
            self._set_status_label(
                self.rule_health_badge,
                health.text,
                health.style,
            )
            self.rule_health_badge.setToolTip(health.detail)

    def _inspect_current_profile(self) -> None:
        current = self._current_profile()
        if current is None:
            return
        domain, name, profile = current
        lineage = profile_lineage(self.config, domain, name)
        usages = profile_usages(self.config, domain, name)
        entries = profile_content_entries(self.config, domain, name)
        rows = [
            ("Domaine", DOMAIN_LABELS.get(domain, domain)),
            ("Héritage", " ← ".join(lineage) if lineage else name),
            ("Actions locales", str(len(profile.get("actions") or []))),
            ("Contenu effectif", str(len(entries))),
            ("Utilisé par", str(len(usages))),
        ]
        for usage in usages[:8]:
            rows.append(
                (
                    f"Dépendance · {usage.kind}",
                    f"{usage.owner} · {usage.detail}",
                )
            )
        parent = str(profile.get("extends") or "").strip()
        pool = self._profiles_for_domain(domain)
        if parent and parent not in pool:
            health_text = "⚠ Base introuvable"
            health_style = "Bad"
            health_detail = f"Le profil parent « {parent} » n’existe pas."
        else:
            health_text = "✓ Configuré"
            health_style = "Good"
            health_detail = (
                "Héritage résolu."
                if parent
                else "Profil autonome."
            )
        if hasattr(self, "profile_health_badge"):
            self._set_status_label(
                self.profile_health_badge,
                health_text,
                health_style,
            )
            self.profile_health_badge.setToolTip(health_detail)
        self._set_inspector(
            kind="profile",
            domain=domain,
            title=f"{DOMAIN_LABELS.get(domain, domain)} · {name}",
            target=name,
            summary=(
                f"{len(entries)} élément(s) effectif(s) · "
                f"{len(usages)} dépendance(s)"
            ),
            health_text=health_text,
            health_style=health_style,
            rows=rows,
            payload=profile,
        )

    def _inspect_current_layout(self) -> None:
        current = self._current_layout_profile()
        if current is None:
            return
        name, profile = current
        lineage = profile_lineage(self.config, "layout", name)
        usages = profile_usages(self.config, "layout", name)
        modules = (
            profile.get("modules")
            if isinstance(profile.get("modules"), Mapping)
            else {}
        )
        rows = [
            ("Héritage", " ← ".join(lineage) if lineage else name),
            ("Modules locaux", str(len(modules))),
            ("Utilisé par", str(len(usages))),
            (
                "Transition",
                str(
                    (
                        profile.get("transition")
                        if isinstance(profile.get("transition"), Mapping)
                        else {}
                    ).get("mode")
                    or "instant"
                ),
            ),
        ]
        for usage in usages[:8]:
            rows.append(
                (
                    f"Dépendance · {usage.kind}",
                    f"{usage.owner} · {usage.detail}",
                )
            )
        parent = str(profile.get("extends") or "").strip()
        pool = self._layout_profiles()
        if parent and parent not in pool:
            health_text = "⚠ Base introuvable"
            health_style = "Bad"
            health_detail = f"Le layout parent « {parent} » n’existe pas."
        else:
            health_text = "✓ Configuré"
            health_style = "Good"
            health_detail = (
                "Héritage résolu."
                if parent
                else "Layout autonome."
            )
        if hasattr(self, "layout_health_badge"):
            self._set_status_label(
                self.layout_health_badge,
                health_text,
                health_style,
            )
            self.layout_health_badge.setToolTip(health_detail)
        self._set_inspector(
            kind="layout",
            domain="layout",
            title=f"Layout · {name}",
            target=name,
            summary=(
                f"{len(modules)} module(s) local(aux) · "
                f"{len(usages)} dépendance(s)"
            ),
            health_text=health_text,
            health_style=health_style,
            rows=rows,
            payload=profile,
        )

    def _save_favorite_targets(self) -> None:
        self._window_settings.setValue(
            "main_window/favorites",
            json.dumps(
                [list(item) for item in self._favorite_targets],
                ensure_ascii=False,
            ),
        )
        self._window_settings.sync()

    def _toggle_inspector_favorite(self) -> None:
        context = self._inspector_context
        if context is None:
            return
        if context in self._favorite_targets:
            self._favorite_targets = [
                item
                for item in self._favorite_targets
                if item != context
            ]
        else:
            self._favorite_targets.insert(0, context)
            self._favorite_targets = self._favorite_targets[:20]
        self._save_favorite_targets()
        self.inspector_favorite_button.setText(
            "★ Épinglé"
            if context in self._favorite_targets
            else "☆ Épingler"
        )

    def _open_saved_target(
        self,
        kind: str,
        domain: str,
        target: str,
    ) -> None:
        if kind == "rule":
            self._open_rule_by_name(target)
        elif kind in {"profile", "layout"}:
            self._open_profile_target(domain, target)

    def _open_inspector_target(self) -> None:
        context = self._inspector_context
        if context is None:
            return
        kind, domain, target = context
        if kind == "rule":
            self._open_rule_by_name(target)
        elif kind in {"profile", "layout"}:
            self._open_profile_target(domain, target)

    def _show_inspector_impact(self) -> None:
        context = self._inspector_context
        if context is None:
            return
        kind, domain, target = context
        if kind == "profile":
            self._show_profile_impact(domain, target)
        elif kind == "layout":
            self._show_profile_impact("layout", target)

    def _show_inspector_raw(self) -> None:
        payload = self._inspector_payload
        if not isinstance(payload, Mapping):
            QMessageBox.information(
                self,
                "Inspecteur",
                "Aucune donnée brute disponible pour cette sélection.",
            )
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Inspecteur brut")
        dialog.resize(760, 620)
        root = QVBoxLayout(dialog)
        note = QLabel(
            "Vue technique en lecture seule de l’objet sélectionné."
        )
        note.setObjectName("Muted")
        root.addWidget(note)
        raw = QPlainTextEdit()
        raw.setReadOnly(True)
        raw.setPlainText(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        root.addWidget(raw, 1)
        close = QPushButton("Fermer")
        close.clicked.connect(dialog.accept)
        root.addWidget(close, alignment=Qt.AlignRight)
        dialog.exec()

    def _build_dashboard(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setSpacing(12)

        summary_card, summary_lay = self._card("État actuel")
        self.dashboard_health = QLabel("Initialisation…")
        self.dashboard_health.setStyleSheet(
            "font-size: 15pt; font-weight: 700;"
        )
        self.dashboard_decision = QLabel("Décision : —")
        self.dashboard_decision.setStyleSheet("font-weight: 700;")
        self.dashboard_reason = QLabel("Pourquoi : —")
        self.dashboard_reason.setWordWrap(True)
        self.dashboard_reason.setObjectName("Muted")
        summary_lay.addWidget(self.dashboard_health)
        summary_lay.addWidget(self.dashboard_decision)
        summary_lay.addWidget(self.dashboard_reason)

        trail_title = QLabel("Chemin de décision")
        trail_title.setObjectName("Muted")
        summary_lay.addWidget(trail_title)
        self.dashboard_trail_widget = QWidget()
        self.dashboard_trail_layout = QHBoxLayout(
            self.dashboard_trail_widget
        )
        self.dashboard_trail_layout.setContentsMargins(0, 0, 0, 0)
        self.dashboard_trail_layout.setSpacing(5)
        summary_lay.addWidget(self.dashboard_trail_widget)

        self.dashboard_override_row = QWidget()
        override_row_lay = QHBoxLayout(self.dashboard_override_row)
        override_row_lay.setContentsMargins(0, 4, 0, 4)
        self.dashboard_override_label = QLabel("")
        self.dashboard_override_label.setWordWrap(True)
        self.dashboard_override_label.setObjectName("Warn")
        override_row_lay.addWidget(self.dashboard_override_label, 1)
        dashboard_auto = QPushButton(
            "Revenir au routage automatique"
        )
        dashboard_auto.clicked.connect(self._clear_override)
        override_row_lay.addWidget(dashboard_auto)
        self.dashboard_override_row.setVisible(False)
        summary_lay.addWidget(self.dashboard_override_row)

        self.dashboard_diff = QTreeWidget()
        self.dashboard_diff.setColumnCount(4)
        self.dashboard_diff.setHeaderLabels(
            ["Élément", "Cible SSR", "Appliqué", "État"]
        )
        self.dashboard_diff.setRootIsDecorated(False)
        self.dashboard_diff.setAlternatingRowColors(True)
        self.dashboard_diff.setMaximumHeight(190)
        self.dashboard_diff.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.dashboard_diff.header().setStretchLastSection(True)
        summary_lay.addWidget(self.dashboard_diff)

        summary_actions = QHBoxLayout()
        refresh_summary = QPushButton("Actualiser")
        refresh_summary.clicked.connect(
            self._refresh_dashboard_summary
        )
        summary_actions.addWidget(refresh_summary)
        explain_summary = QPushButton("Pourquoi ?")
        explain_summary.clicked.connect(
            self._explain_current_decision
        )
        summary_actions.addWidget(explain_summary)
        details = QPushButton("Détails et outils…")
        details_menu = QMenu(details)
        preview = details_menu.addAction(
            "Prévisualiser sans appliquer"
        )
        preview.triggered.connect(self._show_routing_preview)
        diagnose = details_menu.addAction(
            "Pourquoi ça ne marche pas ?"
        )
        diagnose.triggered.connect(self._show_guided_diagnostic)
        details_menu.addSeparator()
        provenance = details_menu.addAction("Qui contrôle quoi ?")
        provenance.triggered.connect(
            self._show_effective_provenance
        )
        dependencies = details_menu.addAction(
            "Arbre des dépendances"
        )
        dependencies.triggered.connect(self._show_dependency_tree)
        details.setMenu(details_menu)
        summary_actions.addWidget(details)
        summary_actions.addStretch(1)
        summary_lay.addLayout(summary_actions)
        root.addWidget(summary_card)

        self.dashboard_action_card = QFrame()
        self.dashboard_action_card.setObjectName("ActionCardGood")
        action_lay = QHBoxLayout(self.dashboard_action_card)
        action_lay.setContentsMargins(14, 10, 14, 10)
        action_text = QVBoxLayout()
        action_text.setSpacing(2)
        self.dashboard_action_title = QLabel(
            "Aucune action requise"
        )
        self.dashboard_action_title.setStyleSheet(
            "font-weight: 700;"
        )
        self.dashboard_action_detail = QLabel("")
        self.dashboard_action_detail.setObjectName("Muted")
        self.dashboard_action_detail.setWordWrap(True)
        action_text.addWidget(self.dashboard_action_title)
        action_text.addWidget(self.dashboard_action_detail)
        action_lay.addLayout(action_text, 1)
        self.dashboard_action_button = QPushButton("")
        self.dashboard_action_button.setObjectName("Primary")
        self.dashboard_action_button.clicked.connect(
            self._execute_dashboard_action
        )
        self.dashboard_action_button.setVisible(False)
        action_lay.addWidget(self.dashboard_action_button)
        root.addWidget(self.dashboard_action_card)

        self.drift_card, drift_lay = self._card(
            "Écart OBS détecté"
        )
        self.drift_title = QLabel("")
        self.drift_title.setStyleSheet("font-weight: 700;")
        self.drift_detail = QLabel("")
        self.drift_detail.setWordWrap(True)
        self.drift_detail.setObjectName("Muted")
        drift_lay.addWidget(self.drift_title)
        drift_lay.addWidget(self.drift_detail)

        drift_actions = QHBoxLayout()
        drift_reapply = QPushButton("Réappliquer SSR")
        drift_reapply.setObjectName("LiveAction")
        drift_reapply.clicked.connect(self._force_reapply)
        self._register_obs_connected_control(drift_reapply)
        drift_actions.addWidget(drift_reapply)

        drift_adopt = QPushButton("Adopter l’état OBS…")
        drift_adopt.setObjectName("DraftAction")
        drift_adopt.clicked.connect(
            self._configure_current_application
        )
        self._register_obs_connected_control(drift_adopt)
        drift_actions.addWidget(drift_adopt)

        drift_ignore = QPushButton("Ignorer cet écart")
        drift_ignore.clicked.connect(
            self._ignore_current_obs_drift
        )
        drift_actions.addWidget(drift_ignore)
        drift_actions.addStretch(1)
        drift_lay.addLayout(drift_actions)
        self.drift_card.setVisible(False)
        root.addWidget(self.drift_card)

        activity_card, activity_lay = self._card(
            "Activité récente"
        )
        activity_hint = QLabel(
            "Les événements importants sont traduits en langage "
            "utilisateur. Le journal technique reste en mode Expert."
        )
        activity_hint.setWordWrap(True)
        activity_hint.setObjectName("Muted")
        activity_lay.addWidget(activity_hint)
        self.user_activity_tree = QTreeWidget()
        self.user_activity_tree.setColumnCount(3)
        self.user_activity_tree.setHeaderLabels(
            ["Heure", "Événement", "Détail"]
        )
        self.user_activity_tree.setRootIsDecorated(False)
        self.user_activity_tree.setAlternatingRowColors(True)
        self.user_activity_tree.setMaximumHeight(180)
        self.user_activity_tree.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.user_activity_tree.header().setStretchLastSection(True)
        activity_lay.addWidget(self.user_activity_tree)
        root.addWidget(activity_card)

        app_card, app_lay = self._card(
            "Application au premier plan"
        )
        self.app_card = app_card
        self.fg_exe = QLabel("—")
        self.fg_exe.setStyleSheet(
            "font-size: 15pt; font-weight: 700;"
        )
        self.fg_title = QLabel("—")
        self.fg_title.setObjectName("Muted")
        self.fg_path = QLabel("—")
        self.fg_path.setObjectName("Muted")
        self.fg_path.setTextInteractionFlags(
            Qt.TextSelectableByMouse
        )
        app_lay.addWidget(self.fg_exe)
        app_lay.addWidget(self.fg_title)
        app_lay.addWidget(self.fg_path)
        root.addWidget(app_card)

        state_card, state_lay = self._card(
            "État logique courant"
        )
        self.state_card = state_card
        self.state_labels: dict[str, QLabel] = {}
        state_form = QFormLayout()
        for key in (
            "Game",
            "OverlayProfile",
            "CaptureProfile",
            "AudioProfile",
            "LayoutProfile",
            "PresentationProfile",
        ):
            value = QLabel("—")
            value.setStyleSheet("font-weight: 700;")
            self.state_labels[key] = value
            state_form.addRow(key, value)
        self.rule_label = QLabel("Règle : —")
        self.rule_label.setObjectName("Muted")
        state_lay.addLayout(state_form)
        state_lay.addWidget(self.rule_label)
        root.addWidget(state_card)

        override_card, override_lay = self._card(
            "Override manuel"
        )
        self.override_card = override_card
        form = QFormLayout()
        self.override_boxes: dict[str, QComboBox] = {}
        for domain in STATE_DOMAINS:
            box = QComboBox()
            self.override_boxes[domain] = box
            form.addRow(DOMAIN_LABELS[domain], box)
        self.override_release_mode = QComboBox()
        self.override_release_mode.addItem(
            "Jusqu’à désactivation manuelle",
            "manual",
        )
        self.override_release_mode.addItem(
            "Après une durée",
            "duration",
        )
        self.override_release_mode.addItem(
            "Au prochain changement d’application",
            "foreground_change",
        )
        self.override_release_mode.addItem(
            "À la fin du stream",
            "stream_end",
        )
        self.override_duration = QSpinBox()
        self.override_duration.setRange(1, 1440)
        self.override_duration.setValue(30)
        self.override_duration.setSuffix(" min")
        self.override_duration.setEnabled(False)
        self.override_release_mode.currentIndexChanged.connect(
            lambda *_args: self.override_duration.setEnabled(
                self.override_release_mode.currentData()
                == "duration"
            )
        )
        form.addRow("Fin de l’override", self.override_release_mode)
        form.addRow("Durée", self.override_duration)
        override_lay.addLayout(form)
        actions = QHBoxLayout()
        apply_button = QPushButton("Appliquer l’override")
        apply_button.setObjectName("LiveAction")
        apply_button.clicked.connect(self._apply_override)
        clear_button = QPushButton(
            "Revenir au routage automatique"
        )
        clear_button.clicked.connect(self._clear_override)
        reapply_button = QPushButton("Réappliquer à OBS")
        reapply_button.setObjectName("LiveAction")
        reapply_button.clicked.connect(self._force_reapply)
        self._register_obs_connected_control(reapply_button)
        actions.addWidget(apply_button)
        actions.addWidget(clear_button)
        actions.addWidget(reapply_button)
        actions.addStretch(1)
        override_lay.addLayout(actions)
        root.addWidget(override_card)
        root.addStretch(1)
        return page

    def _build_automations_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setSpacing(12)

        intro = QLabel(
            "Cette vue décrit ce que SSR fera en langage utilisateur. "
            "L’ordre suit la priorité des règles. L’édition détaillée reste "
            "disponible en mode Expert."
        )
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        root.addWidget(intro)

        self.automations_tree = QTreeWidget()
        self.automations_tree.setColumnCount(4)
        self.automations_tree.setHeaderLabels(
            ["Automatisation", "Quand", "Alors", "État"]
        )
        self.automations_tree.setRootIsDecorated(False)
        self.automations_tree.setAlternatingRowColors(True)
        self.automations_tree.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.automations_tree.header().setStretchLastSection(True)
        root.addWidget(self.automations_tree, 1)

        actions = QHBoxLayout()
        refresh = QPushButton("Actualiser")
        refresh.clicked.connect(self._refresh_automations_view)
        actions.addWidget(refresh)
        edit = QPushButton("Modifier les règles")
        edit.clicked.connect(self._open_rules_editor)
        actions.addWidget(edit)
        simulate = QPushButton("Tester un scénario…")
        simulate.clicked.connect(self._show_scenario_simulator)
        actions.addWidget(simulate)
        actions.addStretch(1)
        root.addLayout(actions)
        return page

    def _refresh_automations_view(self) -> None:
        tree = getattr(self, "automations_tree", None)
        if tree is None:
            return
        tree.clear()
        for row in build_automation_rows(self.config):
            tree.addTopLevelItem(
                QTreeWidgetItem(
                    [row.name, row.trigger, row.result, row.status]
                )
            )

    def _open_rules_editor(self) -> None:
        self._ensure_expert_mode()
        self.tabs.setCurrentIndex(self.rules_tab_index)

    def _show_scenario_simulator(self) -> None:
        service = self._service
        app = (
            service.last_meaningful_app
            if service is not None
            else None
        )
        context: Mapping[str, object] = {}
        if service is not None:
            try:
                explanation = service.explain_decision(app)
                raw_plan = (
                    explanation.get("obs_plan")
                    if isinstance(explanation, Mapping)
                    else None
                )
                raw_context = (
                    raw_plan.get("context")
                    if isinstance(raw_plan, Mapping)
                    else None
                )
                if isinstance(raw_context, Mapping):
                    context = raw_context
            except Exception:
                context = {}

        dialog = QDialog(self)
        dialog.setWindowTitle("Simuler un scénario de routage")
        dialog.resize(1050, 780)
        root = QVBoxLayout(dialog)

        intro = QLabel(
            "Le simulateur utilise exactement les règles et priorités du "
            "moteur SSR, mais n’applique aucune commande à OBS."
        )
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        root.addWidget(intro)

        form = QFormLayout()
        exe = QLineEdit(str(app.exe_name if app else ""))
        path = QLineEdit(str(app.process_path if app else ""))
        title = QLineEdit(str(app.window_title if app else ""))
        scene = QLineEdit(str(context.get("program_scene") or ""))
        running = QLineEdit(
            ", ".join(
                str(item)
                for item in (
                    context.get("running_processes")
                    if isinstance(
                        context.get("running_processes"),
                        (list, tuple),
                    )
                    else ()
                )
            )
        )

        def bool_box(current) -> QComboBox:
            box = QComboBox()
            box.addItem("Inconnu / non spécifié", None)
            box.addItem("Non", False)
            box.addItem("Oui", True)
            index = box.findData(current)
            box.setCurrentIndex(max(0, index))
            return box

        streaming = bool_box(
            context.get("streaming")
            if isinstance(context.get("streaming"), bool)
            else None
        )
        recording = bool_box(
            context.get("recording")
            if isinstance(context.get("recording"), bool)
            else None
        )
        obs_enabled = bool_box(
            bool(self._client and self._client.config.enabled)
        )

        form.addRow("Processus foreground", exe)
        form.addRow("Chemin", path)
        form.addRow("Titre de fenêtre", title)
        form.addRow("Streaming", streaming)
        form.addRow("Enregistrement", recording)
        form.addRow("Scène programme", scene)
        form.addRow("Intégration OBS activée", obs_enabled)
        form.addRow(
            "Processus actifs (séparés par des virgules)",
            running,
        )
        root.addLayout(form)

        result_label = QLabel("Résultat : —")
        result_label.setStyleSheet("font-weight: 700;")
        root.addWidget(result_label)

        profile_title = QLabel("État logique et contenu effectif")
        profile_title.setObjectName("Section")
        root.addWidget(profile_title)
        profiles_tree = QTreeWidget()
        profiles_tree.setColumnCount(5)
        profiles_tree.setHeaderLabels(
            [
                "Domaine",
                "Profil / élément",
                "Origine / héritage",
                "Contenu / cible",
                "État",
            ]
        )
        profiles_tree.setAlternatingRowColors(True)
        profiles_tree.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        profiles_tree.header().setStretchLastSection(True)
        profiles_tree.setMaximumHeight(280)
        root.addWidget(profiles_tree)

        checks_title = QLabel("Évaluation des règles")
        checks_title.setObjectName("Section")
        root.addWidget(checks_title)
        checks = QTreeWidget()
        checks.setColumnCount(5)
        checks.setHeaderLabels(
            [
                "Règle",
                "Priorité",
                "Comportement",
                "Correspond",
                "Détail",
            ]
        )
        checks.setRootIsDecorated(False)
        checks.setAlternatingRowColors(True)
        checks.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        checks.header().setStretchLastSection(True)
        root.addWidget(checks, 1)

        actions = QHBoxLayout()
        simulate = QPushButton("Simuler")
        simulate.setObjectName("Primary")
        actions.addWidget(simulate)
        actions.addStretch(1)
        close = QPushButton("Fermer")
        close.clicked.connect(dialog.accept)
        actions.addWidget(close)
        root.addLayout(actions)

        def run_simulation() -> None:
            processes = tuple(
                item.strip()
                for item in running.text().split(",")
                if item.strip()
            )
            try:
                report = simulate_rule_scenario(
                    self.config,
                    exe=exe.text().strip(),
                    path=path.text().strip(),
                    title=title.text(),
                    streaming=streaming.currentData(),
                    recording=recording.currentData(),
                    program_scene=scene.text().strip(),
                    obs_enabled=obs_enabled.currentData(),
                    running_processes=processes,
                )
            except Exception as exc:
                QMessageBox.critical(
                    dialog,
                    "Simulation",
                    str(exc),
                )
                return

            if report.kind == "match":
                game = (
                    str(report.state.get("Game") or "")
                    if report.state is not None
                    else ""
                )
                result_label.setText(
                    f"Résultat : {report.rule_name} → {game or 'état MATCH'}"
                )
            elif report.kind == "fallback":
                result_label.setText(
                    "Résultat : configuration de secours"
                )
            elif report.kind == "ignore":
                result_label.setText(
                    f"Résultat : IGNORE ({report.rule_name})"
                )
            else:
                result_label.setText(
                    f"Résultat : {report.kind or '—'}"
                )

            profiles_tree.clear()
            for domain in report.domains:
                lineage = (
                    " ← ".join(domain.lineage)
                    if domain.lineage
                    else "—"
                )
                domain_item = QTreeWidgetItem(
                    [
                        domain.label,
                        domain.profile,
                        lineage,
                        domain.content_summary,
                        "Disponible" if domain.exists else "Introuvable",
                    ]
                )
                profiles_tree.addTopLevelItem(domain_item)
                for entry in domain.entries:
                    domain_item.addChild(
                        QTreeWidgetItem(
                            [
                                "",
                                entry.name,
                                entry.source_profile,
                                (
                                    f"{entry.kind}"
                                    + (
                                        f" · {entry.target}"
                                        if entry.target
                                        else ""
                                    )
                                ),
                                "Actif" if entry.enabled else "Désactivé",
                            ]
                        )
                    )
            profiles_tree.collapseAll()

            checks.clear()
            for check in report.checks:
                checks.addTopLevelItem(
                    QTreeWidgetItem(
                        [
                            check.name,
                            str(check.priority),
                            check.behavior,
                            "Oui" if check.matched else "Non",
                            check.reason,
                        ]
                    )
                )

        simulate.clicked.connect(run_simulation)
        run_simulation()
        dialog.exec()

    def _show_profile_impact(
        self,
        domain: str,
        profile_name: str,
    ) -> None:
        usages = profile_usages(
            self.config,
            domain,
            profile_name,
        )
        dialog = QDialog(self)
        dialog.setWindowTitle(
            f"Impact du profil {profile_name}"
        )
        dialog.resize(760, 440)
        root = QVBoxLayout(dialog)

        label = DOMAIN_LABELS.get(domain, domain)
        title = QLabel(f"{label} · {profile_name}")
        title.setStyleSheet("font-size: 15pt; font-weight: 700;")
        root.addWidget(title)

        if usages:
            intro = QLabel(
                (
                    f"{len(usages)} référence(s) utilisent ce profil. "
                    "Une modification peut donc affecter les éléments "
                    "ci-dessous."
                )
            )
        else:
            intro = QLabel(
                "Ce profil n’est actuellement référencé ni par une règle, "
                "ni par le fallback, ni par un profil enfant."
            )
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        root.addWidget(intro)

        tree = QTreeWidget()
        tree.setColumnCount(3)
        tree.setHeaderLabels(
            ["Type de dépendance", "Utilisateur", "Détail"]
        )
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        kind_labels = {
            "rule": "Règle",
            "fallback": "Fallback",
            "inheritance": "Héritage",
        }
        for usage in usages:
            tree.addTopLevelItem(
                QTreeWidgetItem(
                    [
                        kind_labels.get(usage.kind, usage.kind),
                        usage.owner,
                        usage.detail,
                    ]
                )
            )
        tree.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        tree.header().setStretchLastSection(True)
        root.addWidget(tree, 1)

        content_title = QLabel("Contenu effectif et origine")
        content_title.setObjectName("Section")
        root.addWidget(content_title)
        content = QTreeWidget()
        content.setColumnCount(5)
        content.setHeaderLabels(
            ["Origine", "Type", "Nom", "Cible", "Actif"]
        )
        content.setRootIsDecorated(False)
        content.setAlternatingRowColors(True)
        for entry in profile_content_entries(
            self.config,
            domain,
            profile_name,
        ):
            content.addTopLevelItem(
                QTreeWidgetItem(
                    [
                        entry.source_profile,
                        entry.kind,
                        entry.name,
                        entry.target or "—",
                        "Oui" if entry.enabled else "Non",
                    ]
                )
            )
        content.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        content.header().setStretchLastSection(True)
        content.setMaximumHeight(220)
        root.addWidget(content)

        actions = QHBoxLayout()
        actions.addStretch(1)
        close = QPushButton("Fermer")
        close.clicked.connect(dialog.accept)
        actions.addWidget(close)
        root.addLayout(actions)
        dialog.exec()

    def _show_current_profile_impact(self) -> None:
        current = self._current_profile()
        if current is None:
            return
        domain, name, _profile = current
        self._show_profile_impact(domain, name)

    def _show_current_layout_impact(self) -> None:
        current = self._current_layout_profile()
        if current is None:
            return
        name, _profile = current
        self._show_profile_impact("layout", name)

    def _build_configure_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setSpacing(12)

        intro = QLabel(
            "Configurez SSR par intention : choisissez ce que vous voulez "
            "obtenir, puis laissez l’assistant produire le brouillon. "
            "Les écrans Règles/Profils/Layouts restent disponibles en "
            "mode Expert pour les cas avancés."
        )
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        root.addWidget(intro)

        guide_card, guide_lay = self._card(
            "Guide pas à pas"
        )
        guide_hint = QLabel(
            "Choisissez simplement votre objectif. SSR vérifie les prérequis, "
            "montre un aperçu et vous conduit vers le bon workflow sans vous "
            "obliger à connaître Règles, Profils ou Layouts."
        )
        guide_hint.setWordWrap(True)
        guide_hint.setObjectName("Muted")
        guide_lay.addWidget(guide_hint)
        guide = QPushButton("Lancer le guide pas à pas…")
        guide.setObjectName("Primary")
        guide.clicked.connect(self._open_setup_guide)
        guide_lay.addWidget(guide, alignment=Qt.AlignLeft)
        root.addWidget(guide_card)

        app_card, app_lay = self._card(
            "Configurer l’application courante"
        )
        self.configure_app_label = QLabel(
            "Aucune application détectée"
        )
        self.configure_app_label.setStyleSheet(
            "font-size: 14pt; font-weight: 700;"
        )
        self.configure_app_detail = QLabel(
            "Placez l’application à configurer au premier plan."
        )
        self.configure_app_detail.setWordWrap(True)
        self.configure_app_detail.setObjectName("Muted")
        self.configure_requirements = QLabel("")
        self.configure_requirements.setObjectName("Muted")
        self.configure_requirements.setWordWrap(True)
        app_lay.addWidget(self.configure_app_label)
        app_lay.addWidget(self.configure_app_detail)
        app_lay.addWidget(self.configure_requirements)
        row = QHBoxLayout()
        self.capture_current_button = QPushButton(
            "Configurer cette application…"
        )
        self.capture_current_button.setObjectName("Primary")
        self.capture_current_button.clicked.connect(
            self._configure_current_application
        )
        self._register_obs_connected_control(
            self.capture_current_button
        )
        row.addWidget(self.capture_current_button)
        simulate = QPushButton("Tester un scénario…")
        simulate.clicked.connect(self._show_scenario_simulator)
        row.addWidget(simulate)
        row.addStretch(1)
        app_lay.addLayout(row)
        root.addWidget(app_card)

        collection_card, collection_lay = self._card(
            "Comprendre ou importer la collection OBS"
        )
        collection_hint = QLabel(
            "L’analyse est strictement en lecture seule. Une migration "
            "ne crée ensuite un brouillon que pour les éléments que SSR "
            "peut représenter sans ambiguïté."
        )
        collection_hint.setWordWrap(True)
        collection_hint.setObjectName("Muted")
        collection_lay.addWidget(collection_hint)
        row = QHBoxLayout()
        analyze = QPushButton("Analyser ma collection OBS")
        self._set_action_risk(
            analyze,
            "read",
            "Aucune mutation OBS ni configuration.",
        )
        analyze.clicked.connect(self._guided_analyze_collection)
        self._register_obs_connected_control(analyze)
        row.addWidget(analyze)
        advanced = QPushButton("Outils d’import avancés…")
        advanced.clicked.connect(self._open_import_tools)
        row.addWidget(advanced)
        row.addStretch(1)
        collection_lay.addLayout(row)
        root.addWidget(collection_card)

        widgets_card, widgets_lay = self._card(
            "Modules HTML gérés par SSR"
        )
        widgets_hint = QLabel(
            "Les modules importés restent locaux. SSR conserve leur point "
            "d’entrée et leurs assets sans exécuter le HTML pendant l’import."
        )
        widgets_hint.setWordWrap(True)
        widgets_hint.setObjectName("Muted")
        widgets_lay.addWidget(widgets_hint)
        self.widget_runtime_status = QLabel(
            "Widget Runtime : initialisation…"
        )
        self.widget_runtime_status.setObjectName("Muted")
        self.widget_runtime_status.setTextInteractionFlags(
            Qt.TextSelectableByMouse
        )
        widgets_lay.addWidget(self.widget_runtime_status)
        self.widget_library = QTreeWidget()
        self.widget_library.setColumnCount(5)
        self.widget_library.setHeaderLabels(
            ["Module", "Fichiers", "Taille", "Point d’entrée", "État"]
        )
        self.widget_library.setRootIsDecorated(False)
        self.widget_library.setAlternatingRowColors(True)
        self.widget_library.setMaximumHeight(220)
        self.widget_library.header().setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        self.widget_library.header().setSectionResizeMode(
            1,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        self.widget_library.header().setSectionResizeMode(
            2,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        self.widget_library.header().setStretchLastSection(True)
        widgets_lay.addWidget(self.widget_library)
        widget_actions = QHBoxLayout()
        import_widget = QPushButton("Importer un module HTML…")
        import_widget.setObjectName("DraftAction")
        import_widget.clicked.connect(self._open_html_widget_guide)
        widget_actions.addWidget(import_widget)
        install_chat = QPushButton("Installer le chat SSR natif…")
        self._set_action_risk(
            install_chat,
            "live",
            "Crée une Browser Source locale pour le chat SSR natif.",
        )
        install_chat.clicked.connect(self._install_builtin_chat_in_obs)
        self._register_obs_connected_control(install_chat)
        widget_actions.addWidget(install_chat)
        install_events = QPushButton("Installer Events…")
        self._set_action_risk(
            install_events,
            "live",
            "Crée une Browser Source locale pour les événements SSR.",
        )
        install_events.clicked.connect(
            self._install_builtin_events_in_obs
        )
        self._register_obs_connected_control(install_events)
        widget_actions.addWidget(install_events)
        install_alerts = QPushButton("Installer Alerts…")
        self._set_action_risk(
            install_alerts,
            "live",
            "Crée une Browser Source locale pour les alertes SSR.",
        )
        install_alerts.clicked.connect(
            self._install_builtin_alerts_in_obs
        )
        self._register_obs_connected_control(install_alerts)
        widget_actions.addWidget(install_alerts)
        install_radio = QPushButton("Installer Radio…")
        self._set_action_risk(
            install_radio,
            "live",
            "Crée une Browser Source locale pour l’état média SSR.",
        )
        install_radio.clicked.connect(
            self._install_builtin_radio_in_obs
        )
        self._register_obs_connected_control(install_radio)
        widget_actions.addWidget(install_radio)
        demo = QPushButton("Événements de démo")
        self._set_action_risk(
            demo,
            "live",
            "Publie des événements de démonstration dans les widgets SSR actifs.",
        )
        demo.clicked.connect(self._publish_widget_demo_events)
        widget_actions.addWidget(demo)
        bind_profile = QPushButton("Associer à Présentation…")
        self._set_action_risk(
            bind_profile,
            "draft",
            "Associe le package au composant d’un PresentationProfile.",
        )
        bind_profile.clicked.connect(
            self._bind_selected_widget_to_presentation
        )
        widget_actions.addWidget(bind_profile)
        create_obs = QPushButton("Créer dans OBS…")
        self._set_action_risk(
            create_obs,
            "live",
            "Crée immédiatement une Browser Source OBS via le worker SSR.",
        )
        create_obs.clicked.connect(self._create_selected_widget_in_obs)
        self._register_obs_connected_control(create_obs)
        widget_actions.addWidget(create_obs)
        copy_uri = QPushButton("Copier l’URL du Widget Runtime")
        copy_uri.clicked.connect(self._copy_selected_widget_uri)
        widget_actions.addWidget(copy_uri)
        refresh_widgets = QPushButton("Actualiser")
        refresh_widgets.clicked.connect(self._refresh_widget_library)
        widget_actions.addWidget(refresh_widgets)
        widget_actions.addStretch(1)
        widgets_lay.addLayout(widget_actions)
        root.addWidget(widgets_card)

        recipes_card, recipes_lay = self._card(
            "Recettes rapides"
        )
        recipes_hint = QLabel(
            "Ces recettes produisent des objets SSR ordinaires : aucune "
            "couche simplifiée séparée n’est créée, et tout reste éditable "
            "dans les écrans Expert."
        )
        recipes_hint.setWordWrap(True)
        recipes_hint.setObjectName("Muted")
        recipes_lay.addWidget(recipes_hint)
        recipe_row = QHBoxLayout()
        duplicate_profile = QPushButton("Variante de profil…")
        self._set_action_risk(duplicate_profile, "draft")
        duplicate_profile.clicked.connect(
            self._recipe_duplicate_profile
        )
        recipe_row.addWidget(duplicate_profile)
        duplicate_rule = QPushButton("Variante de règle…")
        self._set_action_risk(duplicate_rule, "draft")
        duplicate_rule.clicked.connect(self._recipe_duplicate_rule)
        recipe_row.addWidget(duplicate_rule)
        layout_recipe = QPushButton("Nouveau layout depuis OBS…")
        self._set_action_risk(layout_recipe, "draft")
        layout_recipe.clicked.connect(self._recipe_layout_from_obs)
        self._register_obs_connected_control(layout_recipe)
        recipe_row.addWidget(layout_recipe)
        recipe_row.addStretch(1)
        recipes_lay.addLayout(recipe_row)
        root.addWidget(recipes_card)

        repair_card, repair_lay = self._card(
            "Réparer une configuration devenue obsolète"
        )
        repair_hint = QLabel(
            "Utilisez ce parcours si des scènes, sources, groupes ou "
            "filtres ont été renommés dans OBS."
        )
        repair_hint.setWordWrap(True)
        repair_hint.setObjectName("Muted")
        repair_lay.addWidget(repair_hint)
        row = QHBoxLayout()
        repair = QPushButton("Examiner les références OBS…")
        self._set_action_risk(repair, "draft")
        repair.clicked.connect(self._configure_repair_refs)
        self._register_obs_connected_control(repair)
        row.addWidget(repair)
        capabilities = QPushButton("Vérifier les capacités")
        self._set_action_risk(capabilities, "read")
        capabilities.clicked.connect(self._show_capability_report)
        row.addWidget(capabilities)
        row.addStretch(1)
        repair_lay.addLayout(row)
        root.addWidget(repair_card)

        legend_card, legend_lay = self._card(
            "Comment lire les actions"
        )
        legend = QLabel(
            "Bleu = lecture seule · Or = modifie le brouillon SSR · "
            "Rouge = peut modifier OBS immédiatement. Les actions OBS "
            "restent protégées par Safe Live lorsqu’il est activé."
        )
        legend.setWordWrap(True)
        legend.setObjectName("Muted")
        legend_lay.addWidget(legend)
        root.addWidget(legend_card)
        root.addStretch(1)
        self._refresh_widget_library()
        return page

    def _populate_attention_center(self, report) -> None:
        if not hasattr(self, "attention_tree"):
            return
        self.attention_tree.clear()
        issues = build_attention_items(report.as_mapping())
        severity_labels = {
            "error": "Erreur",
            "warning": "À vérifier",
            "info": "Information",
        }
        for issue in issues:
            item = QTreeWidgetItem(
                [
                    severity_labels.get(
                        issue.severity,
                        issue.severity,
                    ),
                    issue.title,
                    issue.detail,
                    issue.action or "—",
                ]
            )
            item.setData(0, Qt.UserRole, issue.key)
            self.attention_tree.addTopLevelItem(item)

        if issues:
            errors = sum(
                1 for issue in issues if issue.severity == "error"
            )
            warnings = sum(
                1 for issue in issues if issue.severity == "warning"
            )
            self.attention_summary.setText(
                f"{len(issues)} point(s) à examiner · "
                f"{errors} erreur(s) · {warnings} avertissement(s). "
                "Double-cliquez pour aller au bon endroit."
            )
            self.attention_summary.setObjectName(
                "Bad" if errors else "Warn"
            )
        else:
            self.attention_summary.setText(
                "✓ Rien à corriger : aucun problème actionnable détecté."
            )
            self.attention_summary.setObjectName("Good")
        self.attention_summary.style().unpolish(
            self.attention_summary
        )
        self.attention_summary.style().polish(
            self.attention_summary
        )

    def _refresh_attention_center(self) -> None:
        self._collect_settings()
        try:
            report = run_system_check(self.config)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "À corriger",
                str(exc),
            )
            return
        self._last_system_check_report = report
        self._populate_attention_center(report)
        self._record_user_activity(
            UserActivityEntry(
                "Good" if report.ok else "Warn",
                "Diagnostic système actualisé",
                report.capabilities.summary,
            )
        )

    def _navigate_attention_key(self, key: str) -> None:
        normalized = str(key or "").strip()
        if normalized == "config":
            self._show_draft_change_review()
            return
        if normalized == "obs_references":
            self.tabs.setCurrentIndex(self.configure_tab_index)
            return
        if normalized in {"obs", "audio", "hdr"}:
            self.tabs.setCurrentIndex(self.settings_tab_index)
            return
        self._ensure_expert_mode()
        self.tabs.setCurrentIndex(self.diagnostics_tab_index)

    def _open_attention_item(self, *_args) -> None:
        if not hasattr(self, "attention_tree"):
            return
        item = self.attention_tree.currentItem()
        if item is None:
            return
        self._navigate_attention_key(
            str(item.data(0, Qt.UserRole) or "")
        )

    def _ensure_recipe_edit_mode(self, operation: str) -> bool:
        if self._edit_mode:
            return True
        if self._service is None:
            QMessageBox.warning(
                self,
                operation,
                "Le runtime SSR doit être disponible pour entrer en Mode édition.",
            )
            return False
        self._toggle_edit_mode()
        return self._edit_mode

    def _recipe_duplicate_profile(self) -> None:
        if not self._ensure_recipe_edit_mode(
            "Créer une variante de profil"
        ):
            return

        domains = [
            domain
            for domain in PROFILE_DOMAINS
            if self._profiles_for_domain(domain)
        ]
        if not domains:
            QMessageBox.information(
                self,
                "Variante de profil",
                "Aucun profil existant ne peut servir de base.",
            )
            return
        labels = [
            DOMAIN_LABELS.get(domain, domain)
            for domain in domains
        ]
        label, ok = QInputDialog.getItem(
            self,
            "Variante de profil",
            "Domaine",
            labels,
            0,
            False,
        )
        if not ok:
            return
        domain = domains[labels.index(label)]
        profiles = self._profiles_for_domain(domain)
        names = sorted(profiles, key=str.casefold)
        base_name, ok = QInputDialog.getItem(
            self,
            "Variante de profil",
            "Profil de départ",
            names,
            0,
            False,
        )
        if not ok:
            return
        new_name, ok = QInputDialog.getText(
            self,
            "Variante de profil",
            "Nom de la nouvelle variante",
            text=f"{base_name} - Variante",
        )
        new_name = new_name.strip()
        if not ok or not new_name:
            return
        if new_name in profiles:
            QMessageBox.warning(
                self,
                "Variante de profil",
                "Ce profil existe déjà.",
            )
            return

        previous = copy.deepcopy(self.config)
        profiles[new_name] = copy.deepcopy(profiles[base_name])
        errors = validate_config(self.config)
        if errors:
            self.config = previous
            QMessageBox.critical(
                self,
                "Variante de profil",
                "\n".join(errors),
            )
            return
        self._mark_dirty()
        self._set_config_undo_checkpoint(
            f"Création de la variante {domain}/{new_name}",
            previous,
        )
        self._ensure_expert_mode()
        self.tabs.setCurrentIndex(self.profiles_tab_index)
        index = self.profile_domain.findData(domain)
        if index >= 0:
            self.profile_domain.setCurrentIndex(index)
        self._refresh_profile_names()
        self.profile_name.setCurrentText(new_name)
        self._inspect_current_profile()
        self._record_user_activity(
            UserActivityEntry(
                "Good",
                "Variante de profil créée",
                f"{domain}/{base_name} → {new_name}",
            )
        )

    def _recipe_duplicate_rule(self) -> None:
        if not self._ensure_recipe_edit_mode(
            "Créer une variante de règle"
        ):
            return
        rules = self.config.get("rules")
        if not isinstance(rules, list) or not rules:
            QMessageBox.information(
                self,
                "Variante de règle",
                "Aucune règle existante ne peut servir de base.",
            )
            return
        names = [
            str(rule.get("name") or f"Règle {index + 1}")
            for index, rule in enumerate(rules)
            if isinstance(rule, Mapping)
        ]
        if not names:
            return
        base_name, ok = QInputDialog.getItem(
            self,
            "Variante de règle",
            "Règle de départ",
            names,
            0,
            False,
        )
        if not ok:
            return
        source_index = next(
            (
                index
                for index, rule in enumerate(rules)
                if isinstance(rule, Mapping)
                and str(rule.get("name") or f"Règle {index + 1}")
                == base_name
            ),
            None,
        )
        if source_index is None:
            return
        new_name, ok = QInputDialog.getText(
            self,
            "Variante de règle",
            "Nom de la nouvelle règle",
            text=f"{base_name} - Variante",
        )
        new_name = new_name.strip()
        if not ok or not new_name:
            return
        if any(
            isinstance(rule, Mapping)
            and str(rule.get("name") or "") == new_name
            for rule in rules
        ):
            QMessageBox.warning(
                self,
                "Variante de règle",
                "Une règle porte déjà ce nom.",
            )
            return

        previous = copy.deepcopy(self.config)
        clone = copy.deepcopy(rules[source_index])
        clone["name"] = new_name
        clone["enabled"] = False
        rules.insert(source_index + 1, clone)
        errors = validate_config(self.config)
        if errors:
            self.config = previous
            QMessageBox.critical(
                self,
                "Variante de règle",
                "\n".join(errors),
            )
            return
        self._mark_dirty()
        self._set_config_undo_checkpoint(
            f"Création de la variante de règle {new_name}",
            previous,
        )
        self._refresh_rules_table()
        self._ensure_expert_mode()
        self.tabs.setCurrentIndex(self.rules_tab_index)
        self.rules_table.selectRow(source_index + 1)
        self._inspect_selected_rule()
        QMessageBox.information(
            self,
            "Variante de règle",
            (
                f"« {new_name} » a été créée désactivée dans le brouillon.\n\n"
                "Modifiez ses conditions/profils puis activez-la lorsque "
                "vous êtes prêt."
            ),
        )

    def _recipe_layout_from_obs(self) -> None:
        if not self._ensure_recipe_edit_mode(
            "Créer un layout depuis OBS"
        ):
            return
        self._ensure_expert_mode()
        self.tabs.setCurrentIndex(self.layouts_tab_index)
        self._sync_obs_modules()
        before = set(self._layout_profiles())
        self._new_layout_profile()
        after = set(self._layout_profiles())
        created = sorted(after - before, key=str.casefold)
        if created:
            self.layout_profile_name.setCurrentText(created[0])
            self._refresh_layout_profile_view()
            self.statusBar().showMessage(
                "Sélectionnez les éléments OBS à inclure puis utilisez "
                f"« Capturer OBS → {created[0]} ».",
                10000,
            )

    def _build_diagnostics_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setSpacing(12)

        intro = QLabel(
            "Les diagnostics regroupent les outils d’explication et de "
            "maintenance. Ils n’encombrent plus l’Accueil, mais restent "
            "accessibles ici en mode Expert."
        )
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        root.addWidget(intro)

        attention_card, attention_lay = self._card(
            "À corriger"
        )
        self.attention_summary = QLabel(
            "Cliquez sur « Actualiser » pour vérifier la configuration, "
            "les capacités et les références OBS."
        )
        self.attention_summary.setWordWrap(True)
        self.attention_summary.setObjectName("Muted")
        attention_lay.addWidget(self.attention_summary)
        self.attention_tree = QTreeWidget()
        self.attention_tree.setColumnCount(4)
        self.attention_tree.setHeaderLabels(
            ["Niveau", "Élément", "Détail", "Action recommandée"]
        )
        self.attention_tree.setRootIsDecorated(False)
        self.attention_tree.setAlternatingRowColors(True)
        self.attention_tree.setMaximumHeight(240)
        self.attention_tree.header().setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        self.attention_tree.header().setSectionResizeMode(
            1,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        self.attention_tree.header().setStretchLastSection(True)
        self.attention_tree.itemDoubleClicked.connect(
            self._open_attention_item
        )
        attention_lay.addWidget(self.attention_tree)
        attention_actions = QHBoxLayout()
        refresh_attention = QPushButton("Actualiser")
        refresh_attention.setObjectName("ReadOnlyAction")
        refresh_attention.clicked.connect(
            self._refresh_attention_center
        )
        attention_actions.addWidget(refresh_attention)
        open_attention = QPushButton("Ouvrir l’élément")
        open_attention.clicked.connect(self._open_attention_item)
        attention_actions.addWidget(open_attention)
        attention_actions.addStretch(1)
        attention_lay.addLayout(attention_actions)
        root.addWidget(attention_card)

        status_card, status_lay = self._card(
            "Diagnostic guidé"
        )
        self.diagnostics_status = QLabel("État en cours…")
        self.diagnostics_status.setStyleSheet(
            "font-size: 14pt; font-weight: 700;"
        )
        self.diagnostics_detail = QLabel(
            "SSR actualise cette synthèse depuis l’Accueil."
        )
        self.diagnostics_detail.setObjectName("Muted")
        self.diagnostics_detail.setWordWrap(True)
        status_lay.addWidget(self.diagnostics_status)
        status_lay.addWidget(self.diagnostics_detail)
        row = QHBoxLayout()
        guided = QPushButton("Diagnostiquer maintenant")
        guided.setObjectName("Primary")
        guided.clicked.connect(self._show_guided_diagnostic)
        row.addWidget(guided)
        capabilities = QPushButton("Santé et capacités…")
        capabilities.clicked.connect(self._show_capability_report)
        row.addWidget(capabilities)
        row.addStretch(1)
        status_lay.addLayout(row)
        root.addWidget(status_card)

        explain_card, explain_lay = self._card(
            "Comprendre la configuration"
        )
        row = QHBoxLayout()
        provenance = QPushButton("Qui contrôle quoi ?")
        provenance.clicked.connect(
            self._show_effective_provenance
        )
        row.addWidget(provenance)
        dependencies = QPushButton("Arbre des dépendances")
        dependencies.clicked.connect(self._show_dependency_tree)
        row.addWidget(dependencies)
        references = QPushButton("Références OBS…")
        references.clicked.connect(self._configure_repair_refs)
        self._register_obs_connected_control(references)
        row.addWidget(references)
        self.manual_undo_button = QPushButton(
            "Annuler la dernière opération"
        )
        self.manual_undo_button.clicked.connect(
            self._undo_last_manual_operation
        )
        self._register_manual_undo_control(
            self.manual_undo_button
        )
        row.addWidget(self.manual_undo_button)
        row.addStretch(1)
        explain_lay.addLayout(row)
        root.addWidget(explain_card)

        activity_card, activity_lay = self._card(
            "Historique des opérations et restauration"
        )
        self.history_undo_status = QLabel(
            "Dernière opération réversible : —"
        )
        self.history_undo_status.setObjectName("Muted")
        activity_lay.addWidget(self.history_undo_status)
        self.diagnostics_activity_tree = QTreeWidget()
        self.diagnostics_activity_tree.setColumnCount(3)
        self.diagnostics_activity_tree.setHeaderLabels(
            ["Heure", "Événement", "Détail"]
        )
        self.diagnostics_activity_tree.setRootIsDecorated(False)
        self.diagnostics_activity_tree.setAlternatingRowColors(True)
        self.diagnostics_activity_tree.setMaximumHeight(220)
        self.diagnostics_activity_tree.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.diagnostics_activity_tree.header().setStretchLastSection(
            True
        )
        activity_lay.addWidget(self.diagnostics_activity_tree)
        history_actions = QHBoxLayout()
        history_undo = QPushButton("Annuler la dernière opération")
        history_undo.clicked.connect(self._undo_last_manual_operation)
        self._register_manual_undo_control(history_undo)
        history_actions.addWidget(history_undo)
        backups = QPushButton("Sauvegardes…")
        backups.clicked.connect(self._restore_config_backup)
        history_actions.addWidget(backups)
        review = QPushButton("Revoir le brouillon")
        review.clicked.connect(self._show_draft_change_review)
        history_actions.addWidget(review)
        open_log = QPushButton("Journal technique")
        open_log.clicked.connect(self._open_logs_tab)
        history_actions.addWidget(open_log)
        history_actions.addStretch(1)
        activity_lay.addLayout(history_actions)
        root.addWidget(activity_card)
        root.addStretch(1)
        return page

    def _build_rules_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)

        intro = QLabel(
            "Les règles sont évaluées par priorité. La vue "
            "Automatisations est préférable pour comprendre le résultat ; "
            "cet écran sert à l’édition détaillée."
        )
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        root.addWidget(intro)

        self.rules_table = QTableWidget(0, 11)
        self.rules_table.setHorizontalHeaderLabels(
            [
                "Actif",
                "Nom",
                "Comportement",
                "Priorité",
                "Processus",
                "Launcher",
                "Premier plan",
                "Chemin",
                "Titre",
                "Game",
                "Profils",
            ]
        )
        self.rules_table.setAlternatingRowColors(True)
        self.rules_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.rules_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.rules_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.rules_table.horizontalHeader().setStretchLastSection(True)
        self.rules_table.doubleClicked.connect(self._edit_rule)
        self.rules_table.itemSelectionChanged.connect(
            self._inspect_selected_rule
        )
        root.addWidget(self.rules_table, 1)

        rule_summary_row = QHBoxLayout()
        self.rule_health_badge = QLabel("—")
        self.rule_health_badge.setObjectName("Muted")
        rule_summary_row.addWidget(self.rule_health_badge)
        self.rule_human_summary = QLabel(
            "Sélectionnez une règle pour afficher sa lecture humaine."
        )
        self.rule_human_summary.setWordWrap(True)
        self.rule_human_summary.setObjectName("Muted")
        rule_summary_row.addWidget(self.rule_human_summary, 1)
        root.addLayout(rule_summary_row)

        buttons = QHBoxLayout()
        add = QPushButton("Ajouter")
        add.setObjectName("DraftAction")
        add.clicked.connect(self._add_rule)
        buttons.addWidget(add)

        edit = QPushButton("Modifier")
        edit.clicked.connect(self._edit_rule)
        buttons.addWidget(edit)

        test = QPushButton("Tester sur l’app courante")
        self._set_action_risk(
            test,
            "read",
            "Évalue la règle sans appliquer un profil à OBS.",
        )
        test.clicked.connect(self._test_rule)
        buttons.addWidget(test)

        more = QPushButton("⋯")
        more.setToolTip("Actions supplémentaires sur la règle sélectionnée")
        menu = QMenu(more)
        duplicate = menu.addAction("Dupliquer")
        duplicate.triggered.connect(self._duplicate_rule)
        toggle = menu.addAction("Activer / désactiver")
        toggle.triggered.connect(self._toggle_rule)
        bulk = menu.addAction("Actions groupées…")
        bulk.triggered.connect(self._bulk_edit_rules)
        menu.addSeparator()
        delete = menu.addAction("Supprimer")
        delete.triggered.connect(self._delete_rule)
        more.setMenu(menu)
        buttons.addWidget(more)
        buttons.addStretch(1)
        root.addLayout(buttons)
        return page

    def _build_profiles_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)

        intro = QLabel(
            "Un profil décrit ce que SSR doit appliquer pour un domaine. "
            "Les actions rares sont regroupées dans ⋯ pour garder "
            "l’opération courante lisible."
        )
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        root.addWidget(intro)

        top = QHBoxLayout()
        top.addWidget(QLabel("Domaine"))
        self.profile_domain = QComboBox()
        for domain in PROFILE_DOMAINS:
            self.profile_domain.addItem(
                DOMAIN_LABELS[domain],
                domain,
            )
        self.profile_domain.currentIndexChanged.connect(
            self._refresh_profile_names
        )
        top.addWidget(self.profile_domain)
        top.addWidget(QLabel("Profil"))
        self.profile_name = QComboBox()
        self.profile_name.currentIndexChanged.connect(
            self._refresh_actions_table
        )
        top.addWidget(self.profile_name, 1)
        self.profile_health_badge = QLabel("—")
        self.profile_health_badge.setObjectName("Muted")
        top.addWidget(self.profile_health_badge)

        new_profile = QPushButton("Nouveau")
        self._set_action_risk(new_profile, "draft")
        new_profile.clicked.connect(self._new_profile)
        top.addWidget(new_profile)

        self.profile_test_button = QPushButton("Tester dans OBS")
        self._set_action_risk(
            self.profile_test_button,
            "live",
            "Exécute directement le profil sélectionné.",
        )
        self.profile_test_button.clicked.connect(self._test_profile)
        self._register_obs_connected_control(
            self.profile_test_button
        )
        top.addWidget(self.profile_test_button)

        more = QPushButton("⋯")
        more.setToolTip("Gestion du profil sélectionné")
        menu = QMenu(more)
        impact = menu.addAction("Voir l’impact…")
        impact.triggered.connect(self._show_current_profile_impact)
        duplicate = menu.addAction("Dupliquer")
        duplicate.triggered.connect(self._duplicate_profile)
        rename = menu.addAction("Renommer")
        rename.triggered.connect(self._rename_profile)
        menu.addSeparator()
        delete = menu.addAction("Supprimer")
        delete.triggered.connect(self._delete_profile)
        more.setMenu(menu)
        top.addWidget(more)
        root.addLayout(top)

        inheritance = QHBoxLayout()
        inheritance.addWidget(QLabel("Hérite de"))
        self.profile_parent = QComboBox()
        self.profile_parent.addItem("— Aucun —", "")
        self.profile_parent.currentIndexChanged.connect(
            self._profile_parent_changed
        )
        inheritance.addWidget(self.profile_parent, 1)
        detach_profile = QPushButton("Détacher de la base")
        detach_profile.clicked.connect(
            self._detach_current_profile
        )
        inheritance.addWidget(detach_profile)
        hint = QLabel(
            "Les actions du parent sont exécutées avant celles de ce profil."
        )
        hint.setObjectName("Muted")
        inheritance.addWidget(hint)
        root.addLayout(inheritance)
        self.profile_inheritance_hint = QLabel(
            "Héritage effectif : —"
        )
        self.profile_inheritance_hint.setWordWrap(True)
        self.profile_inheritance_hint.setObjectName("Muted")
        root.addWidget(self.profile_inheritance_hint)

        profile_scope = QHBoxLayout()
        local_hint = QLabel(
            "Actions locales — différences propres à ce profil"
        )
        local_hint.setObjectName("Section")
        profile_scope.addWidget(local_hint)
        effective = QPushButton("Voir le contenu effectif…")
        effective.clicked.connect(self._show_current_profile_impact)
        profile_scope.addWidget(effective)
        profile_scope.addStretch(1)
        root.addLayout(profile_scope)

        self.actions_table = QTableWidget(0, 4)
        self.actions_table.setHorizontalHeaderLabels(
            ["Actif", "Type", "Nom", "Paramètres"]
        )
        self.actions_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.actions_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.actions_table.setAlternatingRowColors(True)
        self.actions_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.actions_table.horizontalHeader().setStretchLastSection(True)
        self.actions_table.doubleClicked.connect(self._edit_action)
        root.addWidget(self.actions_table, 1)

        buttons = QHBoxLayout()
        add_action = QPushButton("Ajouter une action")
        self._set_action_risk(add_action, "draft")
        add_action.clicked.connect(self._add_action)
        buttons.addWidget(add_action)

        edit_action = QPushButton("Modifier")
        edit_action.clicked.connect(self._edit_action)
        buttons.addWidget(edit_action)

        more_actions = QPushButton("⋯")
        more_actions.setToolTip(
            "Import, duplication, activation et ordre des actions"
        )
        action_menu = QMenu(more_actions)
        import_collection = action_menu.addAction(
            "Importer collection OBS…"
        )
        import_collection.triggered.connect(
            self._import_collection_to_profile
        )
        self._register_obs_connected_control(import_collection)
        migrate = action_menu.addAction(
            "Migrer logique collection / ASC…"
        )
        migrate.triggered.connect(self._migrate_collection_logic)
        self._register_obs_connected_control(migrate)
        action_menu.addSeparator()
        duplicate_action = action_menu.addAction("Dupliquer")
        duplicate_action.triggered.connect(self._duplicate_action)
        toggle_action = action_menu.addAction("Activer / désactiver")
        toggle_action.triggered.connect(self._toggle_action)
        move_up = action_menu.addAction("Monter")
        move_up.triggered.connect(lambda: self._move_action(-1))
        move_down = action_menu.addAction("Descendre")
        move_down.triggered.connect(lambda: self._move_action(1))
        action_menu.addSeparator()
        delete_action = action_menu.addAction("Supprimer")
        delete_action.triggered.connect(self._delete_action)
        more_actions.setMenu(action_menu)
        buttons.addWidget(more_actions)
        buttons.addStretch(1)
        root.addLayout(buttons)

        risk = QLabel(
            "Le brouillon n’agit pas sur OBS tant que vous n’utilisez pas "
            "« Enregistrer et appliquer ». « Tester dans OBS » est l’exception "
            "et exécute immédiatement le profil."
        )
        risk.setWordWrap(True)
        risk.setObjectName("Muted")
        root.addWidget(risk)
        return page

    def _build_layouts_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setSpacing(10)

        intro = QLabel(
            "Un LayoutProfile mémorise position, taille et visibilité. "
            "Le flux est volontairement séparé : lire OBS, modifier le "
            "brouillon SSR, puis éventuellement appliquer à OBS."
        )
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        root.addWidget(intro)

        source_title = QLabel("1. Source OBS — lecture seule")
        source_title.setObjectName("Section")
        root.addWidget(source_title)
        obs_row = QHBoxLayout()
        obs_row.addWidget(QLabel("Scène OBS"))
        self.layout_scene = QComboBox()
        self.layout_scene.setMinimumWidth(260)
        self.layout_scene.currentIndexChanged.connect(
            self._layout_scene_changed
        )
        obs_row.addWidget(self.layout_scene, 1)
        sync = QPushButton("Lire / synchroniser OBS")
        self._set_action_risk(
            sync,
            "read",
            "Met à jour le catalogue affiché sans modifier OBS.",
        )
        sync.clicked.connect(self._sync_obs_modules)
        obs_row.addWidget(sync)
        self._register_obs_connected_control(sync)
        root.addLayout(obs_row)
        self.layout_obs_hint = QLabel(
            "Synchroniser lit uniquement la structure OBS."
        )
        self.layout_obs_hint.setObjectName("Muted")
        root.addWidget(self.layout_obs_hint)

        profile_title = QLabel("2. Layout SSR — brouillon")
        profile_title.setObjectName("Section")
        root.addWidget(profile_title)
        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("LayoutProfile"))
        self.layout_profile_name = QComboBox()
        self.layout_profile_name.currentIndexChanged.connect(
            self._refresh_layout_profile_view
        )
        profile_row.addWidget(self.layout_profile_name, 1)
        self.layout_health_badge = QLabel("—")
        self.layout_health_badge.setObjectName("Muted")
        profile_row.addWidget(self.layout_health_badge)

        new_layout = QPushButton("Nouveau")
        self._set_action_risk(new_layout, "draft")
        new_layout.clicked.connect(self._new_layout_profile)
        profile_row.addWidget(new_layout)

        self.layout_capture_button = QPushButton(
            "Capturer OBS dans le brouillon"
        )
        self._set_action_risk(
            self.layout_capture_button,
            "draft",
            "Lit OBS puis remplace le contenu du LayoutProfile dans le brouillon.",
        )
        self.layout_capture_button.clicked.connect(
            self._capture_layout_profile
        )
        self._register_obs_connected_control(
            self.layout_capture_button
        )
        profile_row.addWidget(self.layout_capture_button)

        self.layout_apply_button = QPushButton("Appliquer à OBS")
        self._set_action_risk(
            self.layout_apply_button,
            "live",
            "Modifie immédiatement la géométrie/visibilité OBS.",
        )
        self.layout_apply_button.clicked.connect(
            self._apply_layout_profile
        )
        self._register_obs_connected_control(
            self.layout_apply_button
        )
        profile_row.addWidget(self.layout_apply_button)

        more = QPushButton("⋯")
        more.setToolTip("Gestion et outils du LayoutProfile")
        menu = QMenu(more)
        impact = menu.addAction("Voir l’impact…")
        impact.triggered.connect(self._show_current_layout_impact)
        duplicate = menu.addAction("Dupliquer")
        duplicate.triggered.connect(self._duplicate_layout_profile)
        rename = menu.addAction("Renommer")
        rename.triggered.connect(self._rename_layout_profile)
        menu.addSeparator()
        edit_obs = menu.addAction("Éditer dans OBS…")
        edit_obs.triggered.connect(self._edit_layout_in_obs)
        self._register_obs_connected_control(edit_obs)
        menu.addSeparator()
        delete = menu.addAction("Supprimer")
        delete.triggered.connect(self._delete_layout_profile)
        more.setMenu(menu)
        profile_row.addWidget(more)
        root.addLayout(profile_row)

        options = QHBoxLayout()
        options.addWidget(QLabel("Base"))
        self.layout_parent = QComboBox()
        self.layout_parent.addItem("— Aucune —", "")
        self.layout_parent.currentIndexChanged.connect(
            self._layout_option_changed
        )
        options.addWidget(self.layout_parent)
        detach_layout = QPushButton("Détacher de la base")
        detach_layout.clicked.connect(
            self._detach_current_layout_profile
        )
        options.addWidget(detach_layout)
        options.addWidget(QLabel("Coordonnées"))
        self.layout_coordinate_mode = QComboBox()
        self.layout_coordinate_mode.addItem(
            "Normalisées",
            "normalized",
        )
        self.layout_coordinate_mode.addItem("Absolues", "absolute")
        self.layout_coordinate_mode.currentIndexChanged.connect(
            self._layout_option_changed
        )
        options.addWidget(self.layout_coordinate_mode)
        options.addWidget(QLabel("Transition"))
        self.layout_transition = QComboBox()
        for label, value in [
            ("Instantanée", "instant"),
            ("Déplacement", "move"),
            ("Fondu", "fade"),
            ("Déplacement + fondu", "move_fade"),
        ]:
            self.layout_transition.addItem(label, value)
        self.layout_transition.currentIndexChanged.connect(
            self._layout_option_changed
        )
        options.addWidget(self.layout_transition)
        self.layout_transition_ms = QSpinBox()
        self.layout_transition_ms.setRange(0, 10000)
        self.layout_transition_ms.setSuffix(" ms")
        self.layout_transition_ms.valueChanged.connect(
            self._layout_option_changed
        )
        options.addWidget(self.layout_transition_ms)
        options.addStretch(1)
        root.addLayout(options)
        self.layout_inheritance_hint = QLabel(
            "Héritage effectif : —"
        )
        self.layout_inheritance_hint.setWordWrap(True)
        self.layout_inheritance_hint.setObjectName("Muted")
        root.addWidget(self.layout_inheritance_hint)

        tools = QHBoxLayout()
        preview = QPushButton("Prévisualiser dans OBS")
        self._set_action_risk(preview, "live")
        preview.clicked.connect(self._preview_layout_profile)
        self._register_obs_connected_control(preview)
        tools.addWidget(preview)

        compare_obs = QPushButton("Comparer à OBS")
        self._set_action_risk(compare_obs, "read")
        compare_obs.clicked.connect(self._diff_layout_with_obs)
        self._register_obs_connected_control(compare_obs)
        tools.addWidget(compare_obs)

        more_tools = QPushButton("⋯")
        more_tools.setToolTip("Outils avancés du layout")
        tools_menu = QMenu(more_tools)
        cancel_preview = tools_menu.addAction("Annuler aperçu OBS")
        cancel_preview.triggered.connect(self._cancel_layout_preview)
        self._register_obs_connected_control(cancel_preview)
        undo_obs = tools_menu.addAction("Restaurer l’OBS précédent")
        undo_obs.triggered.connect(self._undo_layout_obs)
        self._register_obs_connected_control(undo_obs)
        tools_menu.addSeparator()
        compare_two = tools_menu.addAction("Comparer 2 layouts")
        compare_two.triggered.connect(self._diff_two_layouts)
        validate = tools_menu.addAction("Valider le layout")
        validate.triggered.connect(self._validate_layout_profile)
        self._register_obs_connected_control(validate)
        tools_menu.addSeparator()
        restore = tools_menu.addAction(
            "Restaurer la version précédente du brouillon"
        )
        restore.triggered.connect(self._restore_layout_revision)
        more_tools.setMenu(tools_menu)
        tools.addWidget(more_tools)
        tools.addStretch(1)
        root.addLayout(tools)

        content_title = QLabel(
            "3. Contenu local du layout — différences et géométrie"
        )
        content_title.setObjectName("Section")
        root.addWidget(content_title)
        content = QHBoxLayout()

        catalog_card, catalog_lay = self._card(
            "Catalogue OBS — sélection pour la prochaine capture"
        )
        self.module_tree = QTreeWidget()
        self.module_tree.setHeaderLabels(
            ["Type / module", "Source OBS"]
        )
        self.module_tree.header().setSectionResizeMode(
            0,
            QHeaderView.ResizeMode.ResizeToContents,
        )
        self.module_tree.header().setStretchLastSection(True)
        self.module_tree.itemChanged.connect(
            self._catalog_item_changed
        )
        catalog_lay.addWidget(self.module_tree)
        content.addWidget(catalog_card, 1)

        layout_card, layout_lay = self._card(
            "Modules mémorisés dans le brouillon"
        )
        self.layout_modules_table = QTableWidget(0, 8)
        self.layout_modules_table.setHorizontalHeaderLabels(
            [
                "Module OBS",
                "Éléments",
                "X",
                "Y",
                "Largeur",
                "Hauteur",
                "Visible",
                "Ancre",
            ]
        )
        self.layout_modules_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.layout_modules_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.layout_modules_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.layout_modules_table.horizontalHeader().setStretchLastSection(
            True
        )
        self.layout_modules_table.doubleClicked.connect(
            self._edit_layout_module
        )
        layout_lay.addWidget(self.layout_modules_table)
        edit = QPushButton(
            "Modifier position, taille et éléments…"
        )
        self._set_action_risk(edit, "draft")
        edit.clicked.connect(self._edit_layout_module)
        layout_lay.addWidget(edit, alignment=Qt.AlignLeft)
        content.addWidget(layout_card, 2)

        root.addLayout(content, 1)
        return page

    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)

        router_card, router_lay = self._card("Moteur de routage")
        self.router_settings_card = router_card
        form = QFormLayout()
        self.poll_ms = QSpinBox()
        self.poll_ms.setRange(20, 5000)
        self.debounce_ms = QSpinBox()
        self.debounce_ms.setRange(0, 5000)
        self.fallback_debounce_ms = QSpinBox()
        self.fallback_debounce_ms.setRange(0, 10000)
        form.addRow("Intervalle de détection (ms)", self.poll_ms)
        form.addRow("Debounce règle (ms)", self.debounce_ms)
        form.addRow("Debounce fallback (ms)", self.fallback_debounce_ms)
        router_lay.addLayout(form)
        root.addWidget(router_card)

        obs_card, obs_lay = self._card("OBS WebSocket")
        form = QFormLayout()
        self.obs_enabled = QCheckBox("Piloter OBS")
        self.obs_host = QLineEdit()
        self.obs_port = QSpinBox()
        self.obs_port.setRange(1, 65535)
        self.obs_password = QLineEdit()
        self.obs_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.safe_live = QCheckBox(
            "Safe Live : demander confirmation avant les opérations OBS "
            "manuelles pendant un stream ou un enregistrement"
        )
        form.addRow("", self.obs_enabled)
        form.addRow("Hôte", self.obs_host)
        form.addRow("Port", self.obs_port)
        form.addRow("Mot de passe", self.obs_password)
        form.addRow("", self.safe_live)
        obs_lay.addLayout(form)
        test = QPushButton("Tester la connexion OBS")
        test.clicked.connect(self._test_obs)
        obs_lay.addWidget(test, alignment=Qt.AlignLeft)
        root.addWidget(obs_card)

        widget_card, widget_lay = self._card(
            "Widget Runtime local"
        )
        widget_form = QFormLayout()
        self.widget_runtime_enabled = QCheckBox(
            "Activer les widgets SSR locaux"
        )
        self.widget_runtime_port = QSpinBox()
        self.widget_runtime_port.setRange(1024, 65535)
        widget_form.addRow("", self.widget_runtime_enabled)
        widget_form.addRow("Port localhost", self.widget_runtime_port)
        widget_lay.addLayout(widget_form)
        widget_note = QLabel(
            "Le serveur reste lié à 127.0.0.1. Il fournit les modules HTML, "
            "le chat natif et l’état PresentationProfile aux Browser Sources OBS."
        )
        widget_note.setWordWrap(True)
        widget_note.setObjectName("Muted")
        widget_lay.addWidget(widget_note)
        root.addWidget(widget_card)

        media_card, media_lay = self._card("Média / VLC")
        media_form = QFormLayout()
        self.media_enabled = QCheckBox(
            "Activer le contrôle média via VLC"
        )
        self.vlc_port = QSpinBox()
        self.vlc_port.setRange(1, 65535)
        self.vlc_password = QLineEdit()
        self.vlc_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.vlc_password.setPlaceholderText(
            "Mot de passe de l’interface Web VLC"
        )
        media_form.addRow("", self.media_enabled)
        media_form.addRow("Port VLC localhost", self.vlc_port)
        media_form.addRow("Mot de passe VLC", self.vlc_password)
        media_lay.addLayout(media_form)
        media_note = QLabel(
            "SSR utilise uniquement l’interface HTTP locale de VLC. "
            "Le widget Radio reçoit un état normalisé et ne dépend pas "
            "directement de VLC."
        )
        media_note.setWordWrap(True)
        media_note.setObjectName("Muted")
        media_lay.addWidget(media_note)
        self.media_runtime_status = QLabel(
            "Media Runtime : désactivé"
        )
        self.media_runtime_status.setObjectName("Muted")
        self.media_runtime_status.setWordWrap(True)
        media_lay.addWidget(self.media_runtime_status)
        root.addWidget(media_card)

        host_card, host_lay = self._card("Contrôle Windows")
        host_form = QFormLayout()
        self.soundvolumeview_path = QLineEdit()
        self.soundvolumeview_path.setPlaceholderText(
            r"C:\Streaming\OBS\Tools\SoundVolumeView\SoundVolumeView.exe"
        )
        host_form.addRow("SoundVolumeView.exe", self.soundvolumeview_path)
        browse_row = QHBoxLayout()
        browse = QPushButton("Parcourir…")
        browse.clicked.connect(self._browse_soundvolumeview)
        browse_row.addWidget(browse)
        browse_row.addStretch(1)
        host_lay.addLayout(host_form)
        host_lay.addLayout(browse_row)
        host_note = QLabel(
            "Audio par application : backend SoundVolumeView. "
            "HDR/SDR : contrôle natif Windows DisplayConfig."
        )
        host_note.setWordWrap(True)
        host_note.setObjectName("Muted")
        host_lay.addWidget(host_note)
        root.addWidget(host_card)

        api_card, api_lay = self._card("API locale / Stream Deck")
        self.api_settings_card = api_card
        api_form = QFormLayout()
        self.api_enabled = QCheckBox("Activer l’API locale")
        self.api_port = QSpinBox()
        self.api_port.setRange(1, 65535)
        self.api_token = QLineEdit()
        self.api_token.setEchoMode(QLineEdit.EchoMode.Password)
        api_form.addRow("", self.api_enabled)
        api_form.addRow("Port localhost", self.api_port)
        api_form.addRow("Token facultatif", self.api_token)
        api_lay.addLayout(api_form)
        root.addWidget(api_card)

        behavior_card, behavior_lay = self._card("Application")
        self.close_to_tray = QCheckBox("Fermer la fenêtre vers la zone de notification")
        self.start_with_windows = QCheckBox("Démarrer avec Windows")
        self.auto_detect_modules = QCheckBox("Détecter automatiquement les nouveaux modules OBS")
        self.module_scan_seconds = QSpinBox()
        self.module_scan_seconds.setRange(2, 120)
        self.module_scan_seconds.setSuffix(" s")
        behavior_lay.addWidget(self.close_to_tray)
        behavior_lay.addWidget(self.start_with_windows)
        behavior_lay.addWidget(self.auto_detect_modules)
        scan_row = QHBoxLayout()
        scan_row.addWidget(QLabel("Intervalle détection modules"))
        scan_row.addWidget(self.module_scan_seconds)
        scan_row.addStretch(1)
        behavior_lay.addLayout(scan_row)
        root.addWidget(behavior_card)
        root.addStretch(1)
        return page

    def _browse_soundvolumeview(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Sélectionner SoundVolumeView.exe",
            self.soundvolumeview_path.text().strip() or "",
            "Exécutables Windows (*.exe);;Tous les fichiers (*)",
        )
        if selected:
            self.soundvolumeview_path.setText(selected)

    def _build_logs_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        root.addWidget(self.log_view, 1)
        clear = QPushButton("Effacer l'affichage")
        clear.clicked.connect(self.log_view.clear)
        root.addWidget(clear, alignment=Qt.AlignLeft)
        return page

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("Fichier")
        for text, slot in [
            ("Enregistrer et appliquer", self.save_and_apply),
            ("Revoir les changements…", self._show_draft_change_review),
            ("Abandonner le brouillon…", self._discard_draft),
            ("Exporter la configuration…", self._export_config),
            ("Importer une configuration…", self._import_config),
            ("Historique des sauvegardes…", self._restore_config_backup),
            ("Quitter", self._quit_app),
        ]:
            action = QAction(text, self)
            action.triggered.connect(slot)
            file_menu.addAction(action)
            if text.startswith("Importer une configuration"):
                self._import_config_action = action
            elif text.startswith("Historique des sauvegardes"):
                self._restore_backup_action = action

        tools_menu = self.menuBar().addMenu("Outils")
        palette = QAction("Palette de commandes…", self)
        palette.setShortcut("Ctrl+K")
        palette.triggered.connect(self._show_command_palette)
        tools_menu.addAction(palette)
        tools_menu.addSeparator()
        for text, slot in [
            ("Santé et capacités…", self._show_capability_report),
            ("Qui contrôle quoi ?…", self._show_effective_provenance),
            ("Arbre des dépendances…", self._show_dependency_tree),
            (
                "Annuler la dernière opération…",
                self._undo_last_manual_operation,
            ),
            ("Réparer les références OBS…", self._start_reference_repair),
            ("Tester un scénario…", self._show_scenario_simulator),
        ]:
            action = QAction(text, self)
            action.triggered.connect(slot)
            tools_menu.addAction(action)
            if text.startswith("Annuler la dernière opération"):
                self._manual_undo_action = action
                self._register_manual_undo_control(action)
            elif text.startswith("Réparer les références OBS"):
                self._repair_refs_action = action
                self._register_obs_connected_control(
                    action,
                    requires_edit_mode=True,
                )

        view_menu = self.menuBar().addMenu("Affichage")
        inspector = QAction("Inspecteur contextuel", self)
        inspector.triggered.connect(self._show_inspector_from_menu)
        view_menu.addAction(inspector)

    def _show_inspector_from_menu(self) -> None:
        self._ensure_expert_mode()
        self.inspector_dock.show()
        self.inspector_dock.raise_()

    def _show_command_palette(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Palette de commandes")
        dialog.resize(720, 520)
        root = QVBoxLayout(dialog)

        search = QLineEdit()
        search.setPlaceholderText(
            "Rechercher une règle, un profil, un layout, un problème ou une activité…"
        )
        root.addWidget(search)

        results = QListWidget()
        root.addWidget(results, 1)

        entries: list[tuple[str, object]] = []

        def navigate_tab(index: int) -> None:
            self.tabs.setCurrentIndex(index)

        entries.extend(
            [
                (
                    "Navigation · Accueil",
                    lambda: navigate_tab(self.dashboard_tab_index),
                ),
                (
                    "Navigation · Automatisations",
                    lambda: navigate_tab(self.automations_tab_index),
                ),
                (
                    "Navigation · Configurer",
                    lambda: navigate_tab(self.configure_tab_index),
                ),
                (
                    "Navigation · Présentation",
                    lambda: (
                        self._ensure_expert_mode(),
                        navigate_tab(self.presentation_tab_index),
                    ),
                ),
                (
                    "Navigation · Diagnostics",
                    lambda: (
                        self._ensure_expert_mode(),
                        navigate_tab(self.diagnostics_tab_index),
                    ),
                ),
                (
                    "Navigation · Paramètres",
                    lambda: navigate_tab(self.settings_tab_index),
                ),
                (
                    "Outil · Santé et capacités",
                    self._show_capability_report,
                ),
                (
                    "Outil · Qui contrôle quoi ?",
                    self._show_effective_provenance,
                ),
                (
                    "Outil · Réparer les références OBS",
                    self._start_reference_repair,
                ),
                (
                    "Outil · Tester un scénario de routage",
                    self._show_scenario_simulator,
                ),
                (
                    "Outil · Annuler la dernière opération",
                    self._undo_last_manual_operation,
                ),
                (
                    "Configuration · Revoir les changements du brouillon",
                    self._show_draft_change_review,
                ),
                (
                    "Configuration · Abandonner le brouillon",
                    self._discard_draft,
                ),
                (
                    "Configuration · Enregistrer et appliquer",
                    self.save_and_apply,
                ),
                (
                    "Outil · Historique des sauvegardes",
                    self._restore_config_backup,
                ),
            ]
        )

        raw_rules = self.config.get("rules")
        if isinstance(raw_rules, list):
            for index, rule in enumerate(raw_rules):
                if not isinstance(rule, Mapping):
                    continue
                name = str(rule.get("name") or f"Règle {index + 1}")
                def open_rule(
                    rule_index=index,
                ) -> None:
                    self._ensure_expert_mode()
                    self.tabs.setCurrentIndex(self.rules_tab_index)
                    if 0 <= rule_index < self.rules_table.rowCount():
                        self.rules_table.selectRow(rule_index)
                        self.rules_table.scrollToItem(
                            self.rules_table.item(rule_index, 1)
                        )
                health = build_rule_health(rule, self.config)
                entries.append(
                    (
                        f"Règle · {name} · {health.text} · "
                        f"{humanize_rule(rule)} · {health.detail}",
                        open_rule,
                    )
                )

        profiles = self.config.get("profiles")
        if isinstance(profiles, Mapping):
            for domain, domain_profiles in profiles.items():
                if not isinstance(domain_profiles, Mapping):
                    continue
                for name in domain_profiles:
                    profile_name = str(name)
                    domain_name = str(domain)
                    def open_profile(
                        wanted_domain=domain_name,
                        wanted_name=profile_name,
                    ) -> None:
                        self._ensure_expert_mode()
                        self.tabs.setCurrentIndex(self.profiles_tab_index)
                        domain_index = self.profile_domain.findData(
                            wanted_domain
                        )
                        if domain_index >= 0:
                            self.profile_domain.setCurrentIndex(domain_index)
                        self._refresh_profile_names()
                        self.profile_name.setCurrentText(wanted_name)
                    profile = domain_profiles.get(name)
                    parent = (
                        str(profile.get("extends") or "").strip()
                        if isinstance(profile, Mapping)
                        else ""
                    )
                    actions = (
                        profile.get("actions")
                        if isinstance(profile, Mapping)
                        and isinstance(profile.get("actions"), list)
                        else []
                    )
                    entries.append(
                        (
                            f"Profil {DOMAIN_LABELS.get(domain_name, domain_name)}"
                            f" · {profile_name} · {len(actions)} action(s)"
                            + (f" · hérite de {parent}" if parent else ""),
                            open_profile,
                        )
                    )

        layouts = self.config.get("layout_profiles")
        if isinstance(layouts, Mapping):
            for name in layouts:
                layout_name = str(name)
                def open_layout(
                    wanted_name=layout_name,
                ) -> None:
                    self._ensure_expert_mode()
                    self.tabs.setCurrentIndex(self.layouts_tab_index)
                    self.layout_profile_name.setCurrentText(wanted_name)
                    self._refresh_layout_profile_view()
                layout_profile = layouts.get(name)
                modules = (
                    layout_profile.get("modules")
                    if isinstance(layout_profile, Mapping)
                    and isinstance(layout_profile.get("modules"), Mapping)
                    else {}
                )
                parent = (
                    str(layout_profile.get("extends") or "").strip()
                    if isinstance(layout_profile, Mapping)
                    else ""
                )
                entries.append(
                    (
                        f"Layout · {layout_name} · {len(modules)} module(s)"
                        + (f" · hérite de {parent}" if parent else ""),
                        open_layout,
                    )
                )

        presentation_profiles = self.config.get(
            "presentation_profiles"
        )
        if isinstance(presentation_profiles, Mapping):
            for name, raw in presentation_profiles.items():
                profile_name = str(name)
                raw = raw if isinstance(raw, Mapping) else {}
                label = (
                    f"Présentation · {profile_name}"
                    + (
                        f" · transition={raw.get('transition_profile')}"
                        if raw.get("transition_profile")
                        else ""
                    )
                    + (
                        f" · shader={raw.get('shader_set')}"
                        if raw.get("shader_set")
                        else ""
                    )
                    + (
                        f" · sons={raw.get('sound_set')}"
                        if raw.get("sound_set")
                        else ""
                    )
                )
                def open_presentation(
                    wanted_name=profile_name,
                ) -> None:
                    self._ensure_expert_mode()
                    self.tabs.setCurrentIndex(
                        self.presentation_tab_index
                    )
                    self.presentation_editor.tabs.setCurrentIndex(0)
                    self.presentation_editor.profile_name.setCurrentText(
                        wanted_name
                    )
                entries.append((label, open_presentation))

        for resource_key, label_prefix, tab_index in (
            ("cues", "Cue", 1),
            ("transition_profiles", "Transition", 2),
            ("shader_sets", "ShaderSet", 3),
            ("sound_sets", "SoundSet", 4),
        ):
            resources = self.config.get(resource_key)
            if not isinstance(resources, Mapping):
                continue
            for name in resources:
                resource_name = str(name)
                def open_resource(
                    wanted_name=resource_name,
                    wanted_key=resource_key,
                    wanted_tab=tab_index,
                ) -> None:
                    self._ensure_expert_mode()
                    self.tabs.setCurrentIndex(
                        self.presentation_tab_index
                    )
                    self.presentation_editor.tabs.setCurrentIndex(
                        wanted_tab
                    )
                    combos = {
                        "cues": self.presentation_editor.cue_name,
                        "transition_profiles": (
                            self.presentation_editor.transition_name
                        ),
                        "shader_sets": self.presentation_editor.shader_name,
                        "sound_sets": self.presentation_editor.sound_name,
                    }
                    combos[wanted_key].setCurrentText(wanted_name)
                entries.append(
                    (
                        f"{label_prefix} · {resource_name}",
                        open_resource,
                    )
                )

        for kind, domain, target in self._favorite_targets:
            label = (
                f"★ Favori · {DOMAIN_LABELS.get(domain, domain)} · {target}"
                if domain
                else f"★ Favori · {kind} · {target}"
            )
            entries.append(
                (
                    label,
                    lambda wanted_kind=kind,
                    wanted_domain=domain,
                    wanted_target=target:
                    self._open_saved_target(
                        wanted_kind,
                        wanted_domain,
                        wanted_target,
                    ),
                )
            )

        for kind, domain, target in self._recent_inspector_targets:
            label = (
                f"Récent · {DOMAIN_LABELS.get(domain, domain)} · {target}"
                if domain
                else f"Récent · {kind} · {target}"
            )
            def open_recent(
                wanted_kind=kind,
                wanted_domain=domain,
                wanted_target=target,
            ) -> None:
                if wanted_kind == "rule":
                    self._open_rule_by_name(wanted_target)
                elif wanted_kind in {"profile", "layout"}:
                    self._open_profile_target(
                        wanted_domain,
                        wanted_target,
                    )
            entries.append((label, open_recent))

        for timestamp, entry, repeat_count, _seen_at in reversed(
            self._user_activity_history
        ):
            repeat = f" ×{repeat_count}" if repeat_count > 1 else ""
            label = (
                f"Activité · {timestamp} · {entry.message}{repeat}"
                + (f" · {entry.detail}" if entry.detail else "")
            )
            entries.append(
                (
                    label,
                    lambda: (
                        self._ensure_expert_mode(),
                        navigate_tab(self.diagnostics_tab_index),
                    ),
                )
            )

        if self._last_system_check_report is not None:
            for issue in build_attention_items(
                self._last_system_check_report.as_mapping()
            ):
                label = (
                    f"À corriger · {issue.title} · {issue.detail} · "
                    f"{issue.action}"
                )
                entries.append(
                    (
                        label,
                        lambda key=issue.key:
                        self._navigate_attention_key(key),
                    )
                )

        visible_entries: list[tuple[str, object]] = []

        def refresh() -> None:
            query = search.text().strip().casefold()
            tokens = [token for token in query.split() if token]
            visible_entries.clear()
            results.clear()
            for label, callback in entries:
                searchable = label.casefold()
                if tokens and not all(
                    token in searchable for token in tokens
                ):
                    continue
                visible_entries.append((label, callback))
                results.addItem(QListWidgetItem(label))
            if results.count():
                results.setCurrentRow(0)

        def execute_current(*_args) -> None:
            row = results.currentRow()
            if not 0 <= row < len(visible_entries):
                return
            _label, callback = visible_entries[row]
            dialog.accept()
            callback()

        search.textChanged.connect(refresh)
        search.returnPressed.connect(execute_current)
        results.itemDoubleClicked.connect(execute_current)
        refresh()
        search.setFocus()
        dialog.exec()

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        menu = QMenu()
        show_action = menu.addAction("Afficher Stream State Router")
        show_action.triggered.connect(self._restore_from_tray)
        pause_action = menu.addAction("Suspendre / reprendre")
        pause_action.triggered.connect(self._toggle_pause)
        menu.addSeparator()
        quit_action = menu.addAction("Quitter")
        quit_action.triggered.connect(self._quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    # ---------- config / runtime ----------
    def _load_config_into_ui(self) -> None:
        router = self.config.get("router", {})
        obs = self.config.get("obs", {})
        ui = self.config.get("ui", {})
        api = self.config.get("api", {})
        self.poll_ms.setValue(int(router.get("poll_ms", 50)))
        self.debounce_ms.setValue(int(router.get("debounce_ms", 150)))
        self.fallback_debounce_ms.setValue(int(router.get("fallback_debounce_ms", 350)))
        self.obs_enabled.setChecked(bool(obs.get("enabled", False)))
        self.obs_host.setText(str(obs.get("host") or "127.0.0.1"))
        self.obs_port.setValue(int(obs.get("port", 4455)))
        self.obs_password.setText(str(obs.get("password") or ""))
        host = self.config.get("host_control", {})
        self.soundvolumeview_path.setText(
            str(
                host.get("soundvolumeview_path")
                if isinstance(host, dict)
                else ""
            )
            or ""
        )
        widget_runtime = self.config.get("widget_runtime", {})
        if not isinstance(widget_runtime, Mapping):
            widget_runtime = {}
        self.widget_runtime_enabled.setChecked(
            bool(widget_runtime.get("enabled", True))
        )
        self.widget_runtime_port.setValue(
            int(widget_runtime.get("port", 8766) or 8766)
        )
        media = self.config.get("media", {})
        if not isinstance(media, Mapping):
            media = {}
        vlc = media.get("vlc", {})
        if not isinstance(vlc, Mapping):
            vlc = {}
        self.media_enabled.setChecked(
            bool(media.get("enabled", False))
        )
        self.vlc_port.setValue(
            int(vlc.get("port", 8080) or 8080)
        )
        self.vlc_password.setText(
            str(vlc.get("password") or "")
        )
        self.api_enabled.setChecked(bool(api.get("enabled", True)))
        self.api_port.setValue(int(api.get("port", 8765)))
        self.api_token.setText(str(api.get("token") or ""))
        self.close_to_tray.setChecked(bool(ui.get("close_to_tray", True)))
        self.safe_live.setChecked(bool(ui.get("safe_live", True)))
        self.auto_detect_modules.setChecked(bool(ui.get("auto_detect_modules", True)))
        self.module_scan_seconds.setValue(int(ui.get("module_scan_seconds", 5)))
        try:
            actual_startup = is_startup_enabled()
        except Exception:
            actual_startup = bool(ui.get("start_with_windows", False))
        self.start_with_windows.setChecked(actual_startup)
        self._refresh_rules_table()
        self._refresh_automations_view()
        self._refresh_profile_names()
        self._refresh_layout_profile_names()
        if hasattr(self, "presentation_editor"):
            self.presentation_editor.refresh()
        self._refresh_override_boxes()
        self.unsaved.setText("")

    def _wire_dirty_signals(self) -> None:
        for widget in (
            self.poll_ms,
            self.debounce_ms,
            self.fallback_debounce_ms,
            self.obs_port,
            self.api_port,
            self.widget_runtime_port,
            self.vlc_port,
            self.module_scan_seconds,
        ):
            widget.valueChanged.connect(self._mark_dirty)
        for widget in (
            self.obs_enabled,
            self.close_to_tray,
            self.start_with_windows,
            self.api_enabled,
            self.widget_runtime_enabled,
            self.media_enabled,
            self.auto_detect_modules,
            self.safe_live,
        ):
            widget.toggled.connect(self._mark_dirty)
        for widget in (
            self.obs_host,
            self.obs_password,
            self.api_token,
            self.vlc_password,
            self.soundvolumeview_path,
        ):
            widget.textChanged.connect(self._mark_dirty)

    def _collect_settings(self) -> None:
        self.config.setdefault("router", {})["poll_ms"] = self.poll_ms.value()
        self.config["router"]["debounce_ms"] = self.debounce_ms.value()
        self.config["router"]["fallback_debounce_ms"] = self.fallback_debounce_ms.value()
        obs = self.config.setdefault("obs", {})
        obs["enabled"] = self.obs_enabled.isChecked()
        obs["host"] = self.obs_host.text().strip() or "127.0.0.1"
        obs["port"] = self.obs_port.value()
        obs["password"] = self.obs_password.text()
        host = self.config.setdefault("host_control", {})
        host["soundvolumeview_path"] = self.soundvolumeview_path.text().strip()
        host.setdefault("audio_timeout_seconds", 5.0)
        widget_runtime = self.config.setdefault(
            "widget_runtime",
            {},
        )
        widget_runtime["enabled"] = (
            self.widget_runtime_enabled.isChecked()
        )
        widget_runtime["host"] = "127.0.0.1"
        widget_runtime["port"] = self.widget_runtime_port.value()
        media = self.config.setdefault("media", {})
        media["enabled"] = self.media_enabled.isChecked()
        media["provider"] = "vlc"
        media.setdefault("poll_seconds", 0.5)
        vlc = media.setdefault("vlc", {})
        vlc["host"] = "127.0.0.1"
        vlc["port"] = self.vlc_port.value()
        vlc["password"] = self.vlc_password.text()
        vlc.setdefault("timeout_seconds", 2.0)
        api = self.config.setdefault("api", {})
        api["enabled"] = self.api_enabled.isChecked()
        api["host"] = "127.0.0.1"
        api["port"] = self.api_port.value()
        api["token"] = self.api_token.text()
        ui = self.config.setdefault("ui", {})
        ui["close_to_tray"] = self.close_to_tray.isChecked()
        ui["start_with_windows"] = self.start_with_windows.isChecked()
        ui["safe_live"] = self.safe_live.isChecked()
        ui["auto_detect_modules"] = self.auto_detect_modules.isChecked()
        ui["module_scan_seconds"] = self.module_scan_seconds.value()

    def _preflight_runtime_config(
        self,
        config_data: Mapping[str, object],
    ) -> None:
        rules, _poll_ms, debounce_ms, fallback_ms = build_ruleset(
            config_data
        )
        client = OBSClientManager(build_obs_config(config_data))
        dispatcher = OBSDispatcher(
            client,
            build_profiles(config_data),
            build_layout_profiles(config_data),
            host_controller=build_host_controller(config_data),
            presentation_registry=build_presentation_profiles(
                config_data
            ),
        )
        StateRouterEngine(
            rules,
            debounce_ms=debounce_ms,
            fallback_debounce_ms=fallback_ms,
            context_provider=dispatcher.obs_context,
        )
        build_activation_policies(config_data)
        build_media_runtime_config(config_data)
        build_media_provider(config_data)

    def _rollback_persisted_apply(
        self,
        previous_config: Mapping[str, object],
        previous_startup: bool,
    ) -> tuple[bool, str]:
        errors: list[str] = []
        config_restored = False
        try:
            save_config(previous_config)
            config_restored = True
            self._last_saved_config = copy.deepcopy(
                dict(previous_config)
            )
            self._saved_revision = config_revision(previous_config)
        except Exception as exc:
            errors.append(f"configuration : {exc}")
        try:
            set_startup_enabled(bool(previous_startup))
        except Exception as exc:
            errors.append(f"démarrage Windows : {exc}")
        return config_restored and not errors, " · ".join(errors)

    def _recover_previous_runtime(
        self,
        previous_config: Mapping[str, object],
        *,
        bootstrap_foreground: ForegroundApp | None,
        startup_layout_profile: str,
        startup_layout_routing_baseline: str,
    ) -> tuple[bool, str]:
        current = self._service
        if current is not None:
            try:
                result = current.stop()
            except Exception as exc:
                return False, f"arrêt du runtime incomplet : {exc}"
            if not result:
                return (
                    False,
                    "le runtime partiellement démarré n’a pas pu être arrêté",
                )
            if result.pending_cleanup:
                merged = list(self._pending_cleanup_transfer)
                for item in result.pending_cleanup:
                    if item not in merged:
                        merged.append(dict(item))
                self._pending_cleanup_transfer = tuple(merged)
        try:
            self._start_runtime(
                config_data=previous_config,
                bootstrap_foreground=bootstrap_foreground,
                startup_layout_profile=startup_layout_profile,
                startup_layout_routing_baseline=(
                    startup_layout_routing_baseline
                ),
            )
        except Exception as exc:
            return False, str(exc)
        return True, ""

    def _show_draft_change_review(self) -> None:
        self._collect_settings()
        report = build_config_change_review(
            self._last_saved_config,
            self.config,
        )

        dialog = QDialog(self)
        dialog.setWindowTitle("Revue du brouillon")
        dialog.resize(1050, 650)
        root = QVBoxLayout(dialog)

        title = QLabel(report.summary)
        title.setStyleSheet("font-size: 15pt; font-weight: 700;")
        root.addWidget(title)

        intro = QLabel(
            "Cette vue compare le brouillon actuellement affiché avec la "
            "dernière configuration enregistrée. Aucune commande OBS n’est "
            "envoyée pendant cette revue."
        )
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        root.addWidget(intro)

        if report.validation_errors:
            validation = QLabel(
                "Le brouillon contient des erreurs et ne doit pas être "
                "appliqué en l’état :\n• "
                + "\n• ".join(report.validation_errors)
            )
            validation.setWordWrap(True)
            validation.setObjectName("Bad")
            root.addWidget(validation)

        tree = QTreeWidget()
        tree.setColumnCount(5)
        tree.setHeaderLabels(
            ["Catégorie", "Élément", "Modification", "Détail", "Impact"]
        )
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        for change in report.changes:
            tree.addTopLevelItem(
                QTreeWidgetItem(
                    [
                        change.category,
                        change.target,
                        change.kind,
                        change.detail,
                        change.impact,
                    ]
                )
            )
        tree.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        tree.header().setStretchLastSection(True)
        root.addWidget(tree, 1)

        if not report.changes:
            empty = QLabel(
                "Le brouillon et la configuration enregistrée sont identiques."
            )
            empty.setObjectName("Good")
            root.addWidget(empty)

        actions = QHBoxLayout()
        discard = QPushButton("Abandonner le brouillon")
        discard.setEnabled(report.has_changes)
        actions.addWidget(discard)
        actions.addStretch(1)
        close = QPushButton("Fermer")
        close.clicked.connect(dialog.reject)
        actions.addWidget(close)
        apply_button = QPushButton("Enregistrer et appliquer")
        apply_button.setObjectName("Primary")
        apply_button.setEnabled(
            report.has_changes and not report.validation_errors
        )
        actions.addWidget(apply_button)
        root.addLayout(actions)

        def discard_from_review() -> None:
            dialog.reject()
            self._discard_draft()

        def apply_from_review() -> None:
            dialog.accept()
            self.save_and_apply()

        discard.clicked.connect(discard_from_review)
        apply_button.clicked.connect(apply_from_review)
        dialog.exec()

    def _discard_draft(self) -> None:
        self._collect_settings()
        report = build_config_change_review(
            self._last_saved_config,
            self.config,
        )
        if not report.has_changes:
            self._draft_dirty = False
            self._refresh_config_revision_status(draft_dirty=False)
            return

        if QMessageBox.question(
            self,
            "Abandonner le brouillon",
            (
                f"{len(report.changes)} changement(s) non enregistré(s) "
                "seront abandonnés.\n\n"
                "La configuration actuellement appliquée au runtime n’est "
                "pas modifiée. Continuer ?"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return

        self.config = copy.deepcopy(self._last_saved_config)
        self._load_config_into_ui()
        self._draft_dirty = False
        self._refresh_config_revision_status(draft_dirty=False)
        self._refresh_dashboard_summary()
        self._record_user_activity(
            UserActivityEntry(
                "Muted",
                "Brouillon abandonné",
                f"{len(report.changes)} changement(s) annulé(s)",
            )
        )
        self.statusBar().showMessage(
            "Brouillon abandonné — configuration enregistrée rechargée",
            5000,
        )

    def save_and_apply(self) -> None:
        self._collect_settings()
        draft = copy.deepcopy(self.config)
        errors = validate_config(draft)
        if errors:
            QMessageBox.critical(
                self,
                "Configuration invalide",
                "\n".join(errors),
            )
            return

        try:
            self._preflight_runtime_config(draft)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Préparation du runtime",
                (
                    "La configuration est valide mais le runtime ne peut pas "
                    f"être construit :\n\n{exc}\n\n"
                    "Aucun fichier ni runtime n’a été modifié."
                ),
            )
            return

        previous_config = copy.deepcopy(self._last_saved_config)
        try:
            previous_startup = is_startup_enabled()
        except Exception:
            previous_ui = previous_config.get("ui")
            previous_startup = bool(
                previous_ui.get("start_with_windows", False)
                if isinstance(previous_ui, Mapping)
                else False
            )

        previous_service = self._service
        bootstrap_foreground = (
            previous_service.last_meaningful_app
            if previous_service is not None
            else None
        )
        startup_layout_profile = (
            previous_service.active_layout_apply_profile
            if previous_service is not None
            else ""
        )
        startup_layout_routing_baseline = ""
        if startup_layout_profile and previous_service is not None:
            previous_state = previous_service.engine.current_state
            if previous_state is not None:
                startup_layout_routing_baseline = (
                    previous_state.profile_name("layout")
                )

        new_saved = False
        try:
            save_config(draft)
            new_saved = True
            self._saved_revision = config_revision(draft)
            set_startup_enabled(self.start_with_windows.isChecked())
        except Exception as exc:
            rollback_detail = ""
            if new_saved:
                _ok, rollback_detail = self._rollback_persisted_apply(
                    previous_config,
                    previous_startup,
                )
            self._draft_dirty = True
            self._refresh_config_revision_status(draft_dirty=True)
            detail = (
                f"\n\nRollback : {rollback_detail}"
                if rollback_detail
                else ""
            )
            QMessageBox.critical(
                self,
                "Enregistrement",
                f"{exc}{detail}",
            )
            return

        restart_error = ""
        try:
            restarted = self._restart_runtime(config_data=draft)
        except Exception as exc:
            restarted = False
            restart_error = str(exc)

        if not restarted:
            rollback_ok, rollback_detail = self._rollback_persisted_apply(
                previous_config,
                previous_startup,
            )

            recovered = False
            recovery_error = ""
            previous_was_stopped = bool(
                previous_service is not None
                and self._last_runtime_restart_previous_stopped
            )
            if restart_error and previous_was_stopped:
                recovered, recovery_error = self._recover_previous_runtime(
                    previous_config,
                    bootstrap_foreground=bootstrap_foreground,
                    startup_layout_profile=startup_layout_profile,
                    startup_layout_routing_baseline=(
                        startup_layout_routing_baseline
                    ),
                )

            self._draft_dirty = True
            self._refresh_config_revision_status(draft_dirty=True)
            self._refresh_dashboard_summary()

            parts = []
            if restart_error:
                parts.append(f"Nouvelle configuration : {restart_error}")
            else:
                diagnostic = self._last_runtime_restart_diagnostic
                parts.append(
                    "Le runtime précédent n’a pas pu être remplacé ; "
                    "il reste actif."
                    + (
                        f"\nDiagnostic : {diagnostic}"
                        if diagnostic
                        else ""
                    )
                )
            if rollback_ok:
                parts.append(
                    "La configuration persistée précédente a été restaurée."
                )
            elif rollback_detail:
                parts.append(f"Rollback persistant incomplet : {rollback_detail}")
            if restart_error and previous_was_stopped:
                parts.append(
                    "Runtime précédent restauré."
                    if recovered
                    else (
                        "Récupération du runtime précédent impossible : "
                        f"{recovery_error}"
                    )
                )
            parts.append(
                "La nouvelle configuration reste ouverte comme brouillon "
                "non enregistré."
            )

            self.statusBar().showMessage(
                "Application annulée — brouillon conservé",
                8000,
            )
            QMessageBox.critical(
                self,
                "Application annulée",
                "\n\n".join(parts),
            )
            self._record_user_activity(
                UserActivityEntry(
                    "Warn",
                    "Application annulée et rollback déclenché",
                    (
                        "runtime récupéré"
                        if recovered
                        else "runtime précédent conservé ou récupération requise"
                    ),
                )
            )
            return

        self._last_saved_config = copy.deepcopy(draft)
        self._draft_dirty = False
        self._restart_api()
        self._restart_widget_runtime()
        self._restart_media_runtime()
        self._configure_module_scan_timer()
        self._refresh_override_boxes()
        self._refresh_config_revision_status(draft_dirty=False)
        self.statusBar().showMessage(
            "Configuration enregistrée et appliquée",
            4000,
        )
        self._log("Configuration enregistrée et appliquée.")
        self._record_user_activity(
            UserActivityEntry(
                "Good",
                "Configuration enregistrée et appliquée",
            )
        )

    def _start_runtime(
        self,
        *,
        config_data: Mapping[str, object] | None = None,
        bootstrap_foreground: ForegroundApp | None = None,
        startup_layout_profile: str = "",
        startup_layout_routing_baseline: str = "",
    ) -> None:
        runtime_config = (
            config_data if config_data is not None else self.config
        )
        rules, poll_ms, debounce_ms, fallback_ms = build_ruleset(
            runtime_config
        )
        self._client = OBSClientManager(build_obs_config(runtime_config))
        self._dispatcher = OBSDispatcher(
            self._client,
            build_profiles(runtime_config),
            build_layout_profiles(runtime_config),
            host_controller=build_host_controller(runtime_config),
            presentation_registry=build_presentation_profiles(
                runtime_config
            ),
            presentation_state_store=self._presentation_state_store,
        )
        if startup_layout_profile:
            self._dispatcher.set_manual_layout_hold(startup_layout_routing_baseline)
        engine = StateRouterEngine(
            rules,
            debounce_ms=debounce_ms,
            fallback_debounce_ms=fallback_ms,
            context_provider=self._dispatcher.obs_context,
        )
        self._service = RoutingService(
            engine,
            self._dispatcher,
            poll_ms=poll_ms,
            logger=self.logger,
            activation_policies=build_activation_policies(runtime_config),
            pending_cleanup=self._pending_cleanup_transfer,
            config_revision=config_revision(runtime_config),
            bootstrap_foreground=bootstrap_foreground,
            startup_layout_profile=startup_layout_profile,
            declarative_execution_enabled=(
                str(
                    os.environ.get(
                        "SSR_ENABLE_DECLARATIVE_EXECUTION",
                        "",
                    )
                ).strip().casefold()
                in {"1", "true", "yes", "on"}
            ),
            control_variables=(
                runtime_config.get("control_variables", {})
                if isinstance(runtime_config.get("control_variables"), Mapping)
                else {}
            ),
            control_store=ControlVariableStore.persistent(
                runtime_config.get("control_variables", {})
                if isinstance(runtime_config.get("control_variables"), Mapping)
                else {}
            ),
        )
        self._pending_cleanup_transfer = ()
        self._service.on_foreground = self.bridge.foreground.emit
        self._service.on_change = self.bridge.state_change.emit
        self._service.on_dispatch = self.bridge.dispatch.emit
        self._service.on_event = self.bridge.runtime_event.emit
        self._service.start()
        if self._edit_mode:
            self._service.pause(True)
            self.pause_button.setText("Suspendu (édition)")
            self.pause_button.setEnabled(False)
        self._applied_revision = self._service.config_revision
        self._layout_sync_manager = None
        self._obs_module_catalog = {}
        self._update_obs_status()
        self._refresh_config_revision_status()

    def _restart_runtime(
        self,
        *,
        config_data: Mapping[str, object] | None = None,
    ) -> bool:
        self._last_runtime_restart_previous_stopped = False
        self._last_runtime_restart_diagnostic = ""
        if self._runtime_restart_in_progress:
            self._log("Redémarrage runtime déjà en cours : demande ignorée.")
            return False

        self._runtime_restart_in_progress = True
        timer = getattr(self, "_module_scan_timer", None)
        timer_was_active = bool(timer is not None and timer.isActive())
        if timer_was_active:
            timer.stop()
        succeeded = False
        try:
            previous = self._service
            resume_layout_profile = (
                previous.active_layout_apply_profile if previous is not None else ""
            )
            resume_layout_routing_baseline = ""
            if resume_layout_profile and previous is not None:
                previous_state = previous.engine.current_state
                if previous_state is not None:
                    resume_layout_routing_baseline = previous_state.profile_name("layout")
            bootstrap_foreground = (
                None
                if resume_layout_profile
                else (previous.last_meaningful_app if previous is not None else None)
            )
            if previous is not None:
                result = previous.stop()
                diagnostic = result.diagnostic_summary()
                self._log(f"runtime_stop: {diagnostic}")
                if not result:
                    self._last_runtime_restart_diagnostic = diagnostic
                    self._log(
                        "Runtime précédent toujours actif : redémarrage refusé pour éviter des écritures OBS concurrentes."
                    )
                    return False
                self._last_runtime_restart_previous_stopped = True
                self._pending_cleanup_transfer = result.pending_cleanup
                if not result.cleanup_complete:
                    self._log(
                        f"Transfert de {len(result.pending_cleanup)} obligation(s) de nettoyage OBS "
                        "au nouveau runtime."
                    )
            self._start_runtime(
                config_data=config_data,
                bootstrap_foreground=bootstrap_foreground,
                startup_layout_profile=resume_layout_profile,
                startup_layout_routing_baseline=resume_layout_routing_baseline,
            )
            succeeded = True
            return True
        finally:
            self._runtime_restart_in_progress = False
            if (
                not succeeded
                and timer_was_active
                and timer is not None
            ):
                # Keep the previously applied scan cadence on a failed
                # replacement. Draft UI settings become active only after a
                # successful save/apply transaction.
                timer.start()

    def _on_foreground(self, app: ForegroundApp | None) -> None:
        self._current_foreground_app = app
        if app is None:
            self.fg_exe.setText("Aucune fenêtre exploitable")
            self.fg_title.setText("—")
            self.fg_path.setText("—")
            if hasattr(self, "configure_app_label"):
                self.configure_app_label.setText(
                    "Aucune application détectée"
                )
                self.configure_app_detail.setText(
                    "Placez l’application à configurer au premier plan."
                )
            self._schedule_dashboard_refresh()
            return
        self.fg_exe.setText(app.exe_name or f"PID {app.pid}")
        self.fg_title.setText(app.window_title or "(sans titre)")
        self.fg_path.setText(
            app.process_path or "(chemin indisponible)"
        )
        if hasattr(self, "configure_app_label"):
            self.configure_app_label.setText(
                app.exe_name or f"PID {app.pid}"
            )
            details = [
                str(app.window_title or "").strip(),
                str(app.process_path or "").strip(),
            ]
            self.configure_app_detail.setText(
                " · ".join(item for item in details if item)
                or "Application prête à être configurée."
            )
        self._schedule_dashboard_refresh()

    def _on_state_change(self, change: StateChange) -> None:
        self._ignored_drift_signature = ""
        values = change.current.as_variables()
        for key, label in self.state_labels.items():
            label.setText(values.get(key, "—"))
        self.rule_label.setText(f"Règle : {change.rule_name} · raison : {change.reason}")
        self._log(
            f"État → {values['Game']} / {values['OverlayProfile']} / "
            f"{values['CaptureProfile']} / {values['AudioProfile']} / "
            f"{values['LayoutProfile']} [{change.rule_name}]"
        )
        self._record_user_activity(
            UserActivityEntry(
                "Muted",
                f"Configuration sélectionnée : {values['Game']}",
                f"Règle : {change.rule_name}",
            )
        )
        self._schedule_dashboard_refresh()

    def _on_dispatch(self, result) -> None:
        changed_domains = tuple(
            str(item)
            for item in (
                getattr(result, "changed_domains", ()) or ()
            )
        )
        if "layout" in changed_domains:
            self._invalidate_layout_undo_checkpoint()
        self._update_obs_status()
        if result.executed:
            self._log(
                f"OBS : {result.executed} action(s) exécutée(s) · "
                f"{', '.join(result.changed_domains)}"
            )
        self._schedule_dashboard_refresh()

    def _on_runtime_event(self, event: RuntimeEvent) -> None:
        self._log(f"{event.kind}: {event.message}")
        activity = user_activity_from_runtime_event(
            event.kind,
            event.message,
            event.payload if isinstance(event.payload, Mapping) else None,
        )
        if activity is not None:
            self._record_user_activity(activity)
        if event.kind in {
            "routing_rule",
            "routing_result",
            "pause",
            "obs_connected",
            "obs_disconnected",
            "obs_error",
            "obs_command_result",
            "manual_override",
            "manual_override_released",
            "obs_drift",
            "obs_drift_cleared",
        }:
            self._schedule_dashboard_refresh()
        if event.kind == "obs_drift_cleared":
            self._ignored_drift_signature = ""
        if event.kind == "routing_rule" and isinstance(event.payload, dict):
            rule_name = str(event.payload.get("rule_name") or "—")
            reason = str(event.payload.get("reason") or "état inchangé")
            self.rule_label.setText(
                f"Règle : {rule_name} · raison : {reason}"
            )
            return
        if event.kind == "activation_command_result" and event.payload is not None:
            self.bridge.activation_result.emit(event.payload)
            return
        if event.kind == "obs_command_result" and event.payload is not None:
            payload = event.payload
            action = str(getattr(payload, "action", "") or "")
            request_id = str(getattr(payload, "request_id", "") or "")
            command_success = bool(
                getattr(payload, "success", False)
            )
            self._finish_obs_request_feedback(
                request_id,
                success=command_success,
            )

            applied_layout = self._pending_layout_manual_apply.pop(
                request_id,
                "",
            )
            if applied_layout and command_success:
                self._set_layout_undo_checkpoint(
                    f"Application du layout « {applied_layout} »"
                )
            elif (
                command_success
                and action == "layout.apply"
                and not applied_layout
            ):
                self._invalidate_layout_undo_checkpoint()

            if (
                request_id
                and request_id == self._manual_undo_request_id
            ):
                if command_success:
                    checkpoint = self._manual_undo
                    label = (
                        str(checkpoint.get("label") or "")
                        if isinstance(checkpoint, Mapping)
                        else ""
                    )
                    self._record_user_activity(
                        UserActivityEntry(
                            "Good",
                            "Dernière opération annulée",
                            label or "Layout OBS restauré",
                        )
                    )
                    self._clear_manual_undo_checkpoint()
                else:
                    self._manual_undo_request_id = ""
                    self._refresh_manual_undo_controls()

            if (
                action == "collection.import.preview"
                and request_id in self._pending_collection_imports
            ):
                context = self._pending_collection_imports.pop(request_id)
                self._complete_collection_import(payload, context)
                self._update_obs_status()
                return
            if bool(getattr(payload, "success", False)):
                if action == "widget.browser_source.create":
                    result = getattr(payload, "result", None)
                    detail = ""
                    if isinstance(result, Mapping):
                        detail = (
                            f"{result.get('input_name', '')} → "
                            f"{result.get('scene', '')}"
                        ).strip(" →")
                    self._record_user_activity(
                        UserActivityEntry(
                            "Good",
                            "Module SSR créé dans OBS",
                            detail,
                        )
                    )
                    self.statusBar().showMessage(
                        "Browser Source SSR créée dans OBS.",
                        6000,
                    )
                if action == "layout.preview":
                    self._preview_active = True
                elif action in {"layout.cancel-preview", "layout.apply"}:
                    self._preview_active = False
                self.statusBar().showMessage(f"OBS : {action} terminé", 5000)
            else:
                self.statusBar().showMessage(
                    f"OBS : {action} échoué — {getattr(payload, 'error', '')}",
                    8000,
                )
            self._update_obs_status()
            return
        if event.kind == "routing_result" and isinstance(event.payload, dict):
            self._routing_incomplete = not bool(event.payload.get("success", False))
            if self._routing_incomplete:
                failed = list(event.payload.get("failed_domains") or [])
                blocked = list(event.payload.get("blocked_domains") or [])
                pending = list(event.payload.get("pending_domains") or [])
                details = failed or blocked or pending
                detail_rows = event.payload.get("domain_details") or []
                precise = ""
                if isinstance(detail_rows, list):
                    for row in detail_rows:
                        if not isinstance(row, dict):
                            continue
                        if str(row.get("status") or "") in {"failed", "missing", "partial", "blocked"}:
                            message = str(row.get("message") or "").strip()
                            if message:
                                precise = f" — {row.get('domain', '')}: {message}"
                                break
                suffix = precise or (
                    f" — {', '.join(str(item) for item in details)}" if details else ""
                )
                self.statusBar().showMessage(
                    f"OBS connecté mais application incomplète{suffix}",
                    10000,
                )
            self._update_obs_status()
            return
        if event.kind in {"obs_connected", "obs_disconnected"}:
            if event.kind == "obs_disconnected":
                self._routing_incomplete = False
            self._update_obs_status()
        elif event.kind == "obs_error":
            self._update_obs_status()

    def _update_obs_status(self) -> None:
        self._refresh_status_strip()
        self._refresh_obs_connected_controls()

    def _record_user_activity(self, entry: UserActivityEntry) -> None:
        timestamp = time.strftime("%H:%M:%S")
        now = time.monotonic()
        for index in range(len(self._user_activity_history) - 1, -1, -1):
            (
                _previous_timestamp,
                previous_entry,
                repeat_count,
                previous_seen,
            ) = self._user_activity_history[index]
            if now - previous_seen > 5.0:
                break
            if (
                previous_entry.style == entry.style
                and previous_entry.message == entry.message
                and previous_entry.detail == entry.detail
            ):
                self._user_activity_history.pop(index)
                self._user_activity_history.append(
                    (timestamp, entry, repeat_count + 1, now)
                )
                self._refresh_user_activity()
                return

        self._user_activity_history.append((timestamp, entry, 1, now))
        self._user_activity_history = self._user_activity_history[-40:]
        self._refresh_user_activity()

    def _refresh_user_activity(self) -> None:
        trees = [
            tree
            for tree in (
                getattr(self, "user_activity_tree", None),
                getattr(self, "diagnostics_activity_tree", None),
            )
            if tree is not None
        ]
        if not trees:
            return
        markers = {
            "Good": "✓",
            "Warn": "⚠",
            "Bad": "✕",
            "Muted": "•",
        }
        rows = list(reversed(self._user_activity_history))
        for tree in trees:
            tree.clear()
            for timestamp, entry, repeat_count, _seen_at in rows:
                repeat = (
                    f" ×{repeat_count}"
                    if repeat_count > 1
                    else ""
                )
                tree.addTopLevelItem(
                    QTreeWidgetItem(
                        [
                            timestamp,
                            (
                                f"{markers.get(entry.style, '•')} "
                                f"{entry.message}{repeat}"
                            ),
                            entry.detail or "—",
                        ]
                    )
                )

    def _show_routing_preview(self) -> None:
        service = self._service
        if service is None:
            QMessageBox.information(
                self,
                "Prévisualisation",
                "Le runtime SSR n’est pas disponible.",
            )
            return
        try:
            explanation = service.explain_decision()
        except Exception as exc:
            QMessageBox.critical(self, "Prévisualisation", str(exc))
            return

        report = build_simulation_report(explanation)
        dialog = QDialog(self)
        dialog.setWindowTitle("Prévisualisation du routage")
        dialog.resize(720, 430)
        root = QVBoxLayout(dialog)

        intro = QLabel(report.summary)
        intro.setWordWrap(True)
        root.addWidget(intro)

        table = QTreeWidget()
        table.setColumnCount(5)
        table.setHeaderLabels(
            ["Élément", "Attendu", "Actuel", "État", "Opérations"]
        )
        table.setRootIsDecorated(False)
        table.setAlternatingRowColors(True)
        for step in report.steps:
            table.addTopLevelItem(
                QTreeWidgetItem(
                    [
                        step.label,
                        step.desired,
                        step.applied,
                        step.status_label,
                        str(step.operation_count),
                    ]
                )
            )
        table.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        table.header().setStretchLastSection(True)
        root.addWidget(table, 1)

        note = QLabel(
            "Cette prévisualisation utilise le plan de routage courant et "
            "n’envoie aucune mutation à OBS."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        root.addWidget(note)

        actions = QHBoxLayout()
        if report.would_change:
            apply_now = QPushButton("Appliquer maintenant")
            apply_now.setObjectName("Primary")
            apply_now.clicked.connect(dialog.accept)
            apply_now.clicked.connect(self._force_reapply)
            self._apply_obs_connected_control_state(apply_now)
            actions.addWidget(apply_now)
        actions.addStretch(1)
        close = QPushButton("Fermer")
        close.clicked.connect(dialog.accept)
        actions.addWidget(close)
        root.addLayout(actions)
        dialog.exec()

    def _show_guided_diagnostic(self) -> None:
        service = self._service
        client = self._client
        if service is None:
            QMessageBox.information(
                self,
                "Diagnostic SSR",
                "Le runtime SSR n’est pas disponible.",
            )
            return
        try:
            explanation = service.explain_decision()
            routing_status = service.routing_status()
        except Exception as exc:
            QMessageBox.critical(self, "Diagnostic SSR", str(exc))
            return

        config_dirty = self._draft_dirty
        report = build_diagnostic_report(
            explanation,
            routing_status,
            obs_enabled=bool(client and client.config.enabled),
            obs_connected=bool(client and client.connected),
            obs_last_error=(
                str(client.last_error or "")
                if client is not None
                else ""
            ),
            config_dirty=config_dirty,
            runtime_revision_mismatch=(
                bool(self._saved_revision)
                and bool(self._applied_revision)
                and self._saved_revision != self._applied_revision
            ),
        )

        dialog = QDialog(self)
        dialog.setWindowTitle("Pourquoi ça ne marche pas ?")
        dialog.resize(780, 520)
        root = QVBoxLayout(dialog)

        status = QLabel(report.status_text)
        status.setObjectName(report.status_style)
        status.setStyleSheet("font-size: 15pt; font-weight: 700;")
        root.addWidget(status)

        summary = QLabel(report.summary)
        summary.setWordWrap(True)
        root.addWidget(summary)

        diagnostics = QTreeWidget()
        diagnostics.setColumnCount(3)
        diagnostics.setHeaderLabels(
            ["Diagnostic", "Détail", "Action recommandée"]
        )
        diagnostics.setRootIsDecorated(False)
        diagnostics.setAlternatingRowColors(True)
        diagnostics.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        diagnostics.header().setStretchLastSection(True)
        markers = {
            "error": "✕",
            "warning": "⚠",
            "info": "ℹ",
        }
        for item in report.items:
            diagnostics.addTopLevelItem(
                QTreeWidgetItem(
                    [
                        f"{markers.get(item.severity, '•')} {item.title}",
                        item.detail or "—",
                        item.action or "Aucune action nécessaire.",
                    ]
                )
            )
        if not report.items:
            diagnostics.addTopLevelItem(
                QTreeWidgetItem(
                    [
                        "✓ Aucun problème détecté",
                        "SSR ne signale aucune anomalie.",
                        "Aucune action nécessaire.",
                    ]
                )
            )
        root.addWidget(diagnostics, 1)

        actions = QHBoxLayout()
        repair = QPushButton("Corriger les différences")
        repair.setObjectName("Primary")
        repair.clicked.connect(dialog.accept)
        repair.clicked.connect(self._force_reapply)
        self._apply_obs_connected_control_state(repair)
        actions.addWidget(repair)
        expert = QPushButton("Ouvrir le mode Expert")
        expert.clicked.connect(dialog.accept)
        expert.clicked.connect(self._ensure_expert_mode)
        actions.addWidget(expert)
        actions.addStretch(1)
        close = QPushButton("Fermer")
        close.clicked.connect(dialog.accept)
        actions.addWidget(close)
        root.addLayout(actions)
        dialog.exec()

    def _ensure_expert_mode(self) -> None:
        if not self._expert_mode:
            self._toggle_ui_mode()

    def _explain_current_decision(self) -> None:
        service = self._service
        if service is None:
            QMessageBox.information(self, "Explication", "Runtime non disponible.")
            return
        try:
            explanation = service.explain_decision()
        except Exception as exc:
            QMessageBox.critical(self, "Explication", str(exc))
            return

        routing = explanation.get("routing", {}) if isinstance(explanation, dict) else {}
        plan = explanation.get("obs_plan", {}) if isinstance(explanation, dict) else {}
        foreground = explanation.get("foreground", {}) if isinstance(explanation, dict) else {}
        lines = [
            f"Application : {foreground.get('exe') or '—'}",
            f"Décision : {routing.get('kind') or '—'} · {routing.get('rule_name') or '—'}",
            f"Debounce : {routing.get('debounce_ms', 0)} ms · délai OBS : {routing.get('apply_delay_ms', 0)} ms",
        ]
        if explanation.get("paused"):
            lines.append("Runtime : routage suspendu")
        lines.append("")
        lines.append("Règles évaluées :")
        checks = routing.get("checks") if isinstance(routing, dict) else None
        if isinstance(checks, list) and checks:
            for check in checks:
                if not isinstance(check, dict):
                    continue
                marker = "✓" if check.get("matched") else "·"
                lines.append(
                    f"{marker} {check.get('name', '')} — {check.get('reason', '')}"
                )
        else:
            lines.append("— aucune règle évaluée —")

        lines.append("")
        lines.append("Plan OBS :")
        domains = plan.get("domains") if isinstance(plan, dict) else None
        if isinstance(domains, list) and domains:
            for domain in domains:
                if not isinstance(domain, dict):
                    continue
                lines.append(
                    f"• {domain.get('domain', '')}: {domain.get('status', '')} "
                    f"({domain.get('applied_profile') or '—'} → {domain.get('desired_profile') or '—'})"
                )
                for operation in domain.get("operations", []) or []:
                    if not isinstance(operation, dict):
                        continue
                    target = str(operation.get("target") or operation.get("scene") or "")
                    lines.append(
                        f"    {operation.get('type', '')}" + (f" → {target}" if target else "")
                    )
        else:
            lines.append(str(plan.get("reason") or "Aucune mutation OBS prévue."))

        QMessageBox.information(self, "Expliquer cette décision", "\n".join(lines))

    def _apply_override(self) -> None:
        if not self._service:
            return
        if not self._safe_live_confirm("Appliquer un override manuel"):
            return
        state = StreamState(
            game=self.override_boxes["game"].currentText(),
            overlay_profile=self.override_boxes["overlay"].currentText(),
            capture_profile=self.override_boxes["capture"].currentText(),
            audio_profile=self.override_boxes["audio"].currentText(),
            layout_profile=self.override_boxes["layout"].currentText(),
            presentation_profile=(
                self.override_boxes["presentation"].currentText()
            ),
        )
        release_mode = str(
            self.override_release_mode.currentData() or "manual"
        )
        duration = (
            self.override_duration.value() * 60
            if release_mode == "duration"
            else None
        )
        try:
            self._service.set_manual_override(
                state,
                duration_seconds=duration,
                release_mode=release_mode,
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Override manuel",
                str(exc),
            )
            return

        labels = {
            "manual": "jusqu’à désactivation manuelle",
            "duration": f"pendant {self.override_duration.value()} min",
            "foreground_change": "jusqu’au prochain changement d’application",
            "stream_end": "jusqu’à la fin du stream",
        }
        self._log(
            "Override manuel appliqué · "
            + labels.get(release_mode, release_mode)
        )

    def _clear_override(self) -> None:
        if not self._service:
            return
        if not self._safe_live_confirm(
            "Revenir au routage automatique"
        ):
            return
        self._service.clear_manual_override()
        self._log("Override manuel désactivé.")

    def _force_reapply(self) -> None:
        if not self._service:
            return
        if not self._safe_live_confirm(
            "Réappliquer la configuration courante à OBS"
        ):
            return
        try:
            request_id = self._service.request_force_reapply()
            self._track_obs_request(
                request_id,
                busy_text="Réapplication…",
            )
            self._log(f"Réapplication OBS mise en file ({request_id[:8]}).")
            self.statusBar().showMessage("Réapplication OBS en cours…", 3000)
        except Exception as exc:
            QMessageBox.critical(self, "OBS", str(exc))

    def _safe_live_confirm(self, operation: str) -> bool:
        if not hasattr(self, "safe_live") or not self.safe_live.isChecked():
            return True
        dispatcher = self._dispatcher
        context = (
            dispatcher.cached_obs_context()
            if dispatcher is not None
            else {}
        )
        if not live_output_active(context):
            return True
        streaming = bool(context.get("streaming", False))
        recording = bool(context.get("recording", False))
        active = []
        if streaming:
            active.append("stream")
        if recording:
            active.append("enregistrement")
        return QMessageBox.question(
            self,
            "Safe Live",
            (
                f"{operation}\n\n"
                f"OBS est actuellement en {' + '.join(active)}. "
                "Cette opération peut modifier visuellement ou techniquement "
                "la sortie active. Continuer quand même ?"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) == QMessageBox.Yes

    def _toggle_edit_mode(self) -> None:
        service = self._service
        if service is None:
            return

        if not self._edit_mode:
            self._edit_mode = True
            self._edit_mode_owned_pause = not service.paused
            if self._edit_mode_owned_pause:
                service.pause(True)
            self.edit_mode_button.setText("Quitter l’édition")
            self.edit_mode_button.setObjectName("Primary")
            self.pause_button.setText("Suspendu (édition)")
            self.pause_button.setEnabled(False)
            self._apply_edit_mode_surfaces()
            refresh_status = getattr(
                self,
                "_refresh_status_strip",
                None,
            )
            if callable(refresh_status):
                refresh_status()
            self.statusBar().showMessage(
                "Mode édition actif — routage automatique gelé",
                5000,
            )
            self._record_user_activity(
                UserActivityEntry(
                    "Warn",
                    "Mode édition activé",
                    "Routage automatique gelé",
                )
            )
            return

        owned_pause = self._edit_mode_owned_pause
        self._edit_mode = False
        self._edit_mode_owned_pause = False
        self.edit_mode_button.setText("Mode édition")
        self.edit_mode_button.setObjectName("")
        self.pause_button.setEnabled(True)
        self._apply_edit_mode_surfaces()
        if owned_pause and service.paused:
            service.pause(False)
        self.pause_button.setText(
            "Reprendre" if service.paused else "Suspendre"
        )
        refresh_status = getattr(
            self,
            "_refresh_status_strip",
            None,
        )
        if callable(refresh_status):
            refresh_status()
        self.statusBar().showMessage(
            "Mode édition terminé",
            3000,
        )
        self._record_user_activity(
            UserActivityEntry(
                "Good",
                "Mode édition terminé",
            )
        )

    def _toggle_pause(self) -> None:
        if not self._service or self._edit_mode:
            return
        new_value = not self._service.paused
        self._service.pause(new_value)
        self.pause_button.setText(
            "Reprendre" if new_value else "Suspendre"
        )
        refresh_status = getattr(
            self,
            "_refresh_status_strip",
            None,
        )
        if callable(refresh_status):
            refresh_status()
        schedule = getattr(
            self,
            "_schedule_dashboard_refresh",
            None,
        )
        if callable(schedule):
            schedule()

    def _setup_guide_dialog(
        self,
        *,
        force_task: str = "",
    ) -> SetupGuideDialog:
        client = self._client
        dialog = SetupGuideDialog(
            self,
            foreground=self._current_foreground_app,
            obs_enabled=bool(
                client is not None
                and getattr(client.config, "enabled", False)
            ),
            obs_connected=bool(
                client is not None and client.connected
            ),
            profile_choices=self._state_profile_choices(),
            fallback_state=(
                self.config.get("router", {}).get(
                    "fallback_state",
                    {},
                )
                if isinstance(self.config.get("router"), Mapping)
                else {}
            ),
        )
        if force_task:
            index = dialog.task.findData(force_task)
            if index >= 0:
                dialog.task.setCurrentIndex(index)
        return dialog

    def _open_setup_guide(self) -> None:
        dialog = self._setup_guide_dialog()
        if dialog.exec() != QDialog.Accepted:
            return
        self._execute_setup_guide_result(dialog.result_value())

    def _open_html_widget_guide(self) -> None:
        dialog = self._setup_guide_dialog(force_task="html")
        if dialog.exec() != QDialog.Accepted:
            return
        self._execute_setup_guide_result(dialog.result_value())

    def _execute_setup_guide_result(self, result) -> None:
        if result.task == "app":
            self._configure_current_application(
                customizations=result.app_customizations,
            )
            return
        if result.task == "collection":
            self._guided_analyze_collection()
            return
        if result.task == "repair":
            self._configure_repair_refs()
            return
        if result.task != "html":
            return

        try:
            package = import_html_module(
                result.html_entry,
                name=result.html_name,
                package_root=(
                    result.html_package_root or None
                ),
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Import module HTML",
                str(exc),
            )
            return

        self._refresh_widget_library()
        details = [
            f"Module : {package.name}",
            f"Fichiers : {package.file_count}",
            f"Point d’entrée : {package.entry}",
            "",
            (
                "Le package est prêt dans la bibliothèque SSR. "
                "Le futur Widget Runtime pourra l’exposer directement "
                "à OBS sans dépendre de Streamlabs."
            ),
        ]
        if package.warnings:
            details.extend(
                ["", "Avertissements :"]
                + [f"• {item}" for item in package.warnings]
            )
        QMessageBox.information(
            self,
            "Module HTML importé",
            "\n".join(details),
        )
        self._record_user_activity(
            UserActivityEntry(
                "Good",
                "Module HTML importé",
                (
                    f"{package.name} · {package.file_count} fichier(s) · "
                    f"{package.total_bytes / 1024:.1f} Kio"
                ),
            )
        )

    def _refresh_widget_library(self) -> None:
        if not hasattr(self, "widget_library"):
            return
        self.widget_library.clear()
        if self._widget_runtime is not None:
            self._widget_runtime.refresh_packages()
        for package in list_widget_packages():
            state = (
                f"⚠ {len(package.warnings)} avertissement(s)"
                if package.warnings
                else "✓ Prêt"
            )
            item = QTreeWidgetItem(
                [
                    package.name,
                    str(package.file_count),
                    f"{package.total_bytes / 1024:.1f} Kio",
                    package.entry.name,
                    state,
                ]
            )
            item.setData(0, Qt.UserRole, package.package_id)
            item.setData(0, Qt.UserRole + 1, package.name)
            item.setData(3, Qt.UserRole, package.entry_uri)
            item.setToolTip(3, str(package.entry))
            if package.remote_references:
                item.setToolTip(
                    4,
                    "Dépendances distantes :\n"
                    + "\n".join(package.remote_references[:20]),
                )
            self.widget_library.addTopLevelItem(item)

    def _install_builtin_widget_in_obs(
        self,
        *,
        route: str,
        module_name: str,
        input_name: str,
        component: str,
        width: int,
        height: int,
    ) -> None:
        runtime = self._widget_runtime
        if runtime is None or not runtime.running:
            QMessageBox.warning(
                self,
                module_name,
                "Le Widget Runtime doit être actif.",
            )
            return
        if self._service is None:
            QMessageBox.warning(
                self,
                module_name,
                "Le runtime SSR n’est pas disponible.",
            )
            return

        default_scene = ""
        if self._dispatcher is not None:
            default_scene = str(
                self._dispatcher.cached_obs_context().get(
                    "program_scene",
                    "",
                )
                or ""
            ).strip()
        dialog = WidgetObsInstallDialog(
            self,
            module_name=module_name,
            default_scene=default_scene,
        )
        dialog.input_name.setText(input_name)
        dialog.component.setText(component)
        dialog.width.setValue(width)
        dialog.height.setValue(height)
        if dialog.exec() != QDialog.Accepted:
            return
        selection = dialog.result_value()

        if not self._safe_live_confirm(
            f"Créer la Browser Source « {selection.input_name} »"
        ):
            return
        url = f"{runtime.base_url}{route}"
        try:
            request_id = self._service.request_widget_browser_source(
                input_name=selection.input_name,
                url=url,
                scene=selection.scene,
                width=selection.width,
                height=selection.height,
                shutdown_when_not_visible=(
                    selection.shutdown_when_not_visible
                ),
                restart_when_active=selection.restart_when_active,
            )
            self._track_obs_request(
                request_id,
                busy_text="Installation…",
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                module_name,
                str(exc),
            )

    def _install_builtin_chat_in_obs(self) -> None:
        self._install_builtin_widget_in_obs(
            route="/component/chat",
            module_name="Chat SSR natif",
            input_name="[SSR] Chat",
            component="chat",
            width=720,
            height=900,
        )

    def _install_builtin_events_in_obs(self) -> None:
        self._install_builtin_widget_in_obs(
            route="/component/events",
            module_name="Events SSR natif",
            input_name="[SSR] Events",
            component="events",
            width=700,
            height=500,
        )

    def _install_builtin_alerts_in_obs(self) -> None:
        self._install_builtin_widget_in_obs(
            route="/component/alerts",
            module_name="Alerts SSR natif",
            input_name="[SSR] Alerts",
            component="alerts",
            width=1920,
            height=1080,
        )

    def _install_builtin_radio_in_obs(self) -> None:
        self._install_builtin_widget_in_obs(
            route="/component/radio",
            module_name="Radio SSR natif",
            input_name="[SSR] Radio",
            component="radio",
            width=900,
            height=180,
        )

    def _publish_widget_demo_events(self) -> None:
        runtime = self._widget_runtime
        if runtime is None or not runtime.running:
            QMessageBox.warning(
                self,
                "Widgets SSR",
                "Le Widget Runtime doit être actif.",
            )
            return
        if not self._safe_live_confirm(
            "Envoyer des événements de démonstration aux widgets SSR actifs"
        ):
            return
        runtime.event_bus.publish(
            channel="chat",
            type="message",
            platform="demo",
            payload={
                "display_name": "Shinra Operator",
                "text": "Connexion au réseau Midgar établie.",
                "color": "#63e6ff",
            },
        )
        runtime.event_bus.publish(
            channel="events",
            type="follow",
            platform="demo",
            payload={
                "label": "Nouveau follower",
                "display_name": "Cloud_Strife",
            },
        )
        runtime.event_bus.publish(
            channel="alerts",
            type="subscription",
            platform="demo",
            payload={
                "title": "NOUVEAU SOLDAT",
                "text": "Cloud_Strife rejoint le programme Shinra",
                "duration_ms": 4500,
            },
        )
        self.statusBar().showMessage(
            "Événements de démonstration envoyés aux widgets SSR.",
            5000,
        )

    def _bind_selected_widget_to_presentation(self) -> None:
        if not hasattr(self, "widget_library"):
            return
        item = self.widget_library.currentItem()
        if item is None:
            QMessageBox.information(
                self,
                "Associer à Présentation",
                "Sélectionnez d’abord un module HTML.",
            )
            return
        if not self._edit_mode:
            self._toggle_edit_mode()
        if not self._edit_mode:
            return

        package_id = str(item.data(0, Qt.UserRole) or "").strip()
        module_name = str(
            item.data(0, Qt.UserRole + 1) or item.text(0)
        ).strip()
        profiles = self.config.setdefault(
            "presentation_profiles",
            {},
        )
        if not isinstance(profiles, dict) or not profiles:
            QMessageBox.warning(
                self,
                "Associer à Présentation",
                "Aucun PresentationProfile disponible.",
            )
            return

        names = sorted(profiles, key=str.casefold)
        profile_name, ok = QInputDialog.getItem(
            self,
            "Associer à Présentation",
            "PresentationProfile",
            names,
            0,
            False,
        )
        if not ok:
            return
        component, ok = QInputDialog.getText(
            self,
            "Associer à Présentation",
            "Composant (ex. chat, events, alerts, radio)",
            text=(
                "chat"
                if "chat" in module_name.casefold()
                else "events"
                if "event" in module_name.casefold()
                else "radio"
                if "radio" in module_name.casefold()
                else ""
            ),
        )
        component = component.strip().casefold()
        if not ok or not component:
            return

        profile = profiles.get(profile_name)
        if not isinstance(profile, dict):
            return
        previous = copy.deepcopy(self.config)
        components = profile.setdefault("components", {})
        if not isinstance(components, dict):
            components = {}
            profile["components"] = components
        existing = components.get(component)
        settings = (
            copy.deepcopy(existing.get("settings", {}))
            if isinstance(existing, Mapping)
            and isinstance(existing.get("settings"), Mapping)
            else {}
        )
        components[component] = {
            "mode": "custom",
            "resource": f"widget:{package_id}",
            "settings": settings,
        }
        errors = validate_config(self.config)
        if errors:
            self.config.clear()
            self.config.update(previous)
            QMessageBox.critical(
                self,
                "Associer à Présentation",
                "\n".join(errors),
            )
            return

        self._mark_dirty()
        self._set_config_undo_checkpoint(
            (
                f"Association {module_name} → "
                f"{profile_name}/{component}"
            ),
            previous,
        )
        self.presentation_editor.refresh()
        self._ensure_expert_mode()
        self.tabs.setCurrentIndex(self.presentation_tab_index)
        self.presentation_editor.tabs.setCurrentIndex(0)
        self.presentation_editor.profile_name.setCurrentText(
            profile_name
        )
        self._record_user_activity(
            UserActivityEntry(
                "Good",
                "Module associé à un PresentationProfile",
                f"{module_name} → {profile_name}/{component}",
            )
        )

    def _create_selected_widget_in_obs(self) -> None:
        if self._service is None:
            QMessageBox.warning(
                self,
                "Créer le module dans OBS",
                "Le runtime SSR n’est pas disponible.",
            )
            return
        runtime = self._widget_runtime
        if runtime is None or not runtime.running:
            QMessageBox.warning(
                self,
                "Créer le module dans OBS",
                "Le Widget Runtime doit être actif.",
            )
            return
        if not hasattr(self, "widget_library"):
            return
        item = self.widget_library.currentItem()
        if item is None:
            QMessageBox.information(
                self,
                "Créer le module dans OBS",
                "Sélectionnez d’abord un module HTML.",
            )
            return

        package_id = str(item.data(0, Qt.UserRole) or "").strip()
        module_name = str(
            item.data(0, Qt.UserRole + 1) or item.text(0)
        ).strip()
        if not package_id:
            QMessageBox.warning(
                self,
                "Créer le module dans OBS",
                "Identifiant du package introuvable.",
            )
            return

        default_scene = ""
        if self._dispatcher is not None:
            default_scene = str(
                self._dispatcher.cached_obs_context().get(
                    "program_scene",
                    "",
                )
                or ""
            ).strip()

        dialog = WidgetObsInstallDialog(
            self,
            module_name=module_name,
            default_scene=default_scene,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        selection = dialog.result_value()

        try:
            url = runtime.package_url(
                package_id,
                component=selection.component,
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Créer le module dans OBS",
                str(exc),
            )
            return

        if not self._safe_live_confirm(
            f"Créer la Browser Source « {selection.input_name} »"
        ):
            return

        try:
            request_id = self._service.request_widget_browser_source(
                input_name=selection.input_name,
                url=url,
                scene=selection.scene,
                width=selection.width,
                height=selection.height,
                shutdown_when_not_visible=(
                    selection.shutdown_when_not_visible
                ),
                restart_when_active=selection.restart_when_active,
            )
            self._track_obs_request(
                request_id,
                busy_text="Création…",
            )
            self.statusBar().showMessage(
                "Création de la Browser Source OBS en cours…",
                6000,
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Créer le module dans OBS",
                str(exc),
            )

    def _copy_selected_widget_uri(self) -> None:
        if not hasattr(self, "widget_library"):
            return
        item = self.widget_library.currentItem()
        if item is None:
            QMessageBox.information(
                self,
                "Module HTML",
                "Sélectionnez d’abord un module.",
            )
            return
        package_id = str(item.data(0, Qt.UserRole) or "")
        fallback_uri = str(item.data(3, Qt.UserRole) or "")
        uri = fallback_uri
        runtime = self._widget_runtime
        if (
            runtime is not None
            and runtime.running
            and package_id
        ):
            try:
                uri = runtime.package_url(package_id)
            except KeyError:
                uri = fallback_uri
        if not uri:
            return
        QApplication.clipboard().setText(uri)
        self.statusBar().showMessage(
            (
                "URL Widget Runtime copiée."
                if uri.startswith("http://")
                else "URI locale du module copiée."
            ),
            5000,
        )

    def _configure_current_application(
        self,
        *,
        customizations: tuple[
            tuple[str, str, str],
            ...,
        ] = (),
    ) -> None:
        if not self._edit_mode:
            self._toggle_edit_mode()
        if self._edit_mode:
            self._guided_capture_current_state(
                customizations=customizations,
            )

    def _configure_repair_refs(self) -> None:
        if not self._edit_mode:
            self._toggle_edit_mode()
        if self._edit_mode:
            self._start_reference_repair()

    def _open_logs_tab(self) -> None:
        self._ensure_expert_mode()
        self.tabs.setCurrentIndex(self.logs_tab_index)

    def _open_diagnostics_tab(self) -> None:
        self._ensure_expert_mode()
        self.tabs.setCurrentIndex(self.diagnostics_tab_index)

    # ---------- rules ----------
    def _refresh_rules_table(self) -> None:
        rules = self.config.setdefault("rules", [])
        self.rules_table.setRowCount(len(rules))
        for row, rule in enumerate(rules):
            state = rule.get("state") if isinstance(rule.get("state"), dict) else {}
            conditions = (
                rule.get("conditions")
                if isinstance(rule.get("conditions"), dict)
                else {}
            )
            foreground_selectors = bool(
                str(rule.get("exe") or "").strip()
                or str(rule.get("path") or "").strip()
                or str(rule.get("title_regex") or "").strip()
            )
            process_running = str(
                conditions.get("process_running") or ""
            ).strip()
            process_display = (
                str(rule.get("exe") or "").strip()
                or (
                    process_running
                    if not foreground_selectors
                    else ""
                )
            )
            foreground_display = (
                "Oui"
                if foreground_selectors
                else ("Non" if process_running else "—")
            )
            profiles = (
                f"{state.get('OverlayProfile', '')} / {state.get('CaptureProfile', '')} / "
                f"{state.get('AudioProfile', '')} / {state.get('LayoutProfile', '')}"
                if rule.get("behavior", "match") == "match"
                else "—"
            )
            health = build_rule_health(rule, self.config)
            name = str(rule.get("name") or "")
            values = [
                "✓" if rule.get("enabled", True) else "",
                f"{health.text.split()[0]} {name}".strip(),
                rule.get("behavior", "match"),
                str(rule.get("priority", 0)),
                process_display,
                rule.get("launcher", ""),
                foreground_display,
                rule.get("path", ""),
                rule.get("title_regex", ""),
                state.get("Game", "") if rule.get("behavior", "match") == "match" else "—",
                profiles,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if str(value):
                    item.setToolTip(str(value))
                if col == 1:
                    item.setToolTip(
                        humanize_rule(rule)
                        + "\n\n"
                        + health.detail
                    )
                self.rules_table.setItem(row, col, item)
        self._refresh_automations_view()

    def _selected_rule_index(self) -> int | None:
        rows = self.rules_table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _add_rule(self) -> None:
        dlg = RuleDialog(
            self,
            profile_choices=self._state_profile_choices(),
            fallback_state=(
                self.config.get("router", {}).get("fallback_state", {})
                if isinstance(self.config.get("router"), Mapping)
                else {}
            ),
        )
        if dlg.exec() == QDialog.Accepted:
            self.config.setdefault("rules", []).append(dlg.result_rule())
            self._mark_dirty()
            self._refresh_rules_table()

    def _edit_rule(self, *_args) -> None:
        idx = self._selected_rule_index()
        if idx is None:
            return
        dlg = RuleDialog(
            self,
            self.config["rules"][idx],
            profile_choices=self._state_profile_choices(),
            fallback_state=(
                self.config.get("router", {}).get("fallback_state", {})
                if isinstance(self.config.get("router"), Mapping)
                else {}
            ),
        )
        if dlg.exec() == QDialog.Accepted:
            self.config["rules"][idx] = dlg.result_rule()
            self._mark_dirty()
            self._refresh_rules_table()

    def _duplicate_rule(self) -> None:
        idx = self._selected_rule_index()
        if idx is None:
            return
        raw = copy.deepcopy(self.config["rules"][idx])
        raw["name"] = f"{raw.get('name', 'Règle')} (copie)"
        self.config["rules"].insert(idx + 1, raw)
        self._mark_dirty()
        self._refresh_rules_table()

    def _toggle_rule(self) -> None:
        idx = self._selected_rule_index()
        if idx is None:
            return
        rule = self.config["rules"][idx]
        rule["enabled"] = not bool(rule.get("enabled", True))
        self._mark_dirty()
        self._refresh_rules_table()

    def _test_rule(self) -> None:
        idx = self._selected_rule_index()
        if idx is None or not self._service:
            return
        app = self._service.last_app
        selected = self.config["rules"][idx]
        requires_foreground = any(
            str(selected.get(key) or "").strip()
            for key in ("exe", "path", "title_regex")
        )
        if app is None and requires_foreground:
            QMessageBox.information(
                self,
                "Test de règle",
                "Cette règle exige une application au premier plan.",
            )
            return
        temp = copy.deepcopy(self.config)
        temp["rules"] = [copy.deepcopy(self.config["rules"][idx])]
        try:
            rules, _poll, _debounce, _fallback = build_ruleset(temp)
            context = (
                self._service.routing_context_snapshot()
                if self._service
                else {}
            )
            resolution = rules.resolve(app, context)
        except Exception as exc:
            QMessageBox.critical(self, "Test de règle", str(exc))
            return
        if resolution.rule_name == selected.get("name"):
            subject = app.exe_name if app is not None else "les conditions actuelles"
            if resolution.kind.value == "ignore":
                message = (
                    f"La règle correspond à {subject} et conserverait "
                    "l’état courant (IGNORE)."
                )
            else:
                state = resolution.state.as_variables() if resolution.state else {}
                message = f"La règle correspond à {subject}.\n\nÉtat : {state}"
        else:
            subject = app.exe_name if app is not None else "les conditions actuelles"
            message = f"La règle ne correspond pas à {subject}."
        QMessageBox.information(self, "Test de règle", message)

    def _bulk_edit_rules(self) -> None:
        if not self._require_edit_mode("Actions groupées sur les règles"):
            return
        rules = self.config.get("rules")
        if not isinstance(rules, list) or not rules:
            QMessageBox.information(
                self,
                "Actions groupées",
                "Aucune règle disponible.",
            )
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Actions groupées sur les règles")
        dialog.resize(620, 620)
        root = QVBoxLayout(dialog)

        intro = QLabel(
            "Sélectionnez les règles à modifier. Cette opération ne touche "
            "que le brouillon SSR et peut être annulée via l’historique."
        )
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        root.addWidget(intro)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Opération"))
        mode = QComboBox()
        mode.addItem("Activer les règles sélectionnées", True)
        mode.addItem("Désactiver les règles sélectionnées", False)
        mode_row.addWidget(mode, 1)
        root.addLayout(mode_row)

        items = QListWidget()
        for index, rule in enumerate(rules):
            if not isinstance(rule, Mapping):
                continue
            name = str(rule.get("name") or f"Règle {index + 1}")
            health = build_rule_health(rule, self.config)
            item = QListWidgetItem(
                f"{health.text.split()[0]} {name}"
            )
            item.setData(Qt.UserRole, index)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            item.setToolTip(humanize_rule(rule))
            items.addItem(item)
        root.addWidget(items, 1)

        impact = QLabel("0 règle sélectionnée")
        impact.setObjectName("Muted")
        root.addWidget(impact)

        def refresh_impact(*_args) -> None:
            count = sum(
                1
                for index in range(items.count())
                if items.item(index).checkState() == Qt.Checked
            )
            impact.setText(
                f"{count} règle(s) seront "
                + (
                    "activée(s)."
                    if bool(mode.currentData())
                    else "désactivée(s)."
                )
            )
            impact.setObjectName("Warn" if count else "Muted")
            impact.style().unpolish(impact)
            impact.style().polish(impact)

        items.itemChanged.connect(refresh_impact)
        mode.currentIndexChanged.connect(refresh_impact)

        actions = QHBoxLayout()
        select_all = QPushButton("Tout sélectionner")
        def mark_all() -> None:
            for index in range(items.count()):
                items.item(index).setCheckState(Qt.Checked)
        select_all.clicked.connect(mark_all)
        actions.addWidget(select_all)
        actions.addStretch(1)
        cancel = QPushButton("Annuler")
        cancel.clicked.connect(dialog.reject)
        actions.addWidget(cancel)
        apply_button = QPushButton("Appliquer au brouillon")
        apply_button.setObjectName("DraftAction")
        apply_button.clicked.connect(dialog.accept)
        actions.addWidget(apply_button)
        root.addLayout(actions)

        if dialog.exec() != QDialog.Accepted:
            return

        selected = [
            int(items.item(index).data(Qt.UserRole))
            for index in range(items.count())
            if items.item(index).checkState() == Qt.Checked
        ]
        if not selected:
            return
        previous = copy.deepcopy(self.config)
        enabled = bool(mode.currentData())
        for index in selected:
            if 0 <= index < len(rules) and isinstance(rules[index], dict):
                rules[index]["enabled"] = enabled
        errors = validate_config(self.config)
        if errors:
            self.config = previous
            QMessageBox.critical(
                self,
                "Actions groupées",
                "\n".join(errors),
            )
            return
        self._mark_dirty()
        self._set_config_undo_checkpoint(
            (
                f"{'Activation' if enabled else 'Désactivation'} "
                f"groupée de {len(selected)} règle(s)"
            ),
            previous,
        )
        self._refresh_rules_table()
        self._record_user_activity(
            UserActivityEntry(
                "Good",
                "Actions groupées appliquées au brouillon",
                (
                    f"{len(selected)} règle(s) "
                    + ("activée(s)" if enabled else "désactivée(s)")
                ),
            )
        )

    def _delete_rule(self) -> None:
        idx = self._selected_rule_index()
        if idx is None:
            return
        name = self.config["rules"][idx].get("name", "cette règle")
        if QMessageBox.question(self, "Supprimer", f"Supprimer « {name} » ?") != QMessageBox.Yes:
            return
        self.config["rules"].pop(idx)
        self._mark_dirty()
        self._refresh_rules_table()

    # ---------- profiles/actions ----------
    def _profiles_for_domain(self, domain: str) -> dict:
        return self.config.setdefault("profiles", {}).setdefault(domain, {})

    def _refresh_profile_names(self) -> None:
        domain = str(self.profile_domain.currentData() or "game")
        profiles = self._profiles_for_domain(domain)
        current = self.profile_name.currentText()
        self.profile_name.blockSignals(True)
        self.profile_name.clear()
        self.profile_name.addItems(sorted(profiles, key=str.casefold))
        if current:
            idx = self.profile_name.findText(current)
            if idx >= 0:
                self.profile_name.setCurrentIndex(idx)
        self.profile_name.blockSignals(False)
        self._refresh_actions_table()

    def _current_profile(self) -> tuple[str, str, dict] | None:
        domain = str(self.profile_domain.currentData() or "")
        name = self.profile_name.currentText().strip()
        if not domain or not name:
            return None
        profile = self._profiles_for_domain(domain).get(name)
        return (domain, name, profile) if isinstance(profile, dict) else None

    def _refresh_actions_table(self) -> None:
        current = self._current_profile()
        if hasattr(self, "profile_parent"):
            self.profile_parent.blockSignals(True)
            self.profile_parent.clear()
            self.profile_parent.addItem("— Aucun —", "")
            if current:
                domain, name, profile = current
                for candidate in sorted(self._profiles_for_domain(domain), key=str.casefold):
                    if candidate != name:
                        self.profile_parent.addItem(candidate, candidate)
                idx = self.profile_parent.findData(str(profile.get("extends") or ""))
                self.profile_parent.setCurrentIndex(max(0, idx))
            self.profile_parent.blockSignals(False)
        if hasattr(self, "profile_inheritance_hint"):
            if current:
                domain, name, profile = current
                lineage = profile_lineage(self.config, domain, name)
                local_actions = profile.get("actions")
                local_count = (
                    len(local_actions)
                    if isinstance(local_actions, list)
                    else 0
                )
                inherited_count = 0
                profiles = self._profiles_for_domain(domain)
                for parent_name in lineage[1:]:
                    parent = profiles.get(parent_name)
                    if not isinstance(parent, Mapping):
                        continue
                    parent_actions = parent.get("actions")
                    if isinstance(parent_actions, list):
                        inherited_count += len(parent_actions)
                chain = " ← ".join(lineage) if lineage else name
                self.profile_inheritance_hint.setText(
                    f"Héritage effectif : {chain} · "
                    f"{local_count} action(s) locale(s) · "
                    f"{inherited_count} héritée(s)"
                )
            else:
                self.profile_inheritance_hint.setText(
                    "Héritage effectif : —"
                )
        if hasattr(self, "profile_test_button"):
            self.profile_test_button.setText(
                (
                    f"Tester « {current[1]} » dans OBS"
                    if current
                    else "Tester dans OBS"
                )
            )
        actions = current[2].setdefault("actions", []) if current else []
        self.actions_table.setRowCount(len(actions))
        self._inspect_current_profile()
        for row, action in enumerate(actions):
            values = [
                "✓" if action.get("enabled", True) else "",
                action.get("type", ""),
                action.get("name", ""),
                json.dumps(action.get("params", {}), ensure_ascii=False),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.actions_table.setItem(row, col, item)

    def _detach_current_profile(self) -> None:
        current = self._current_profile()
        if current is None:
            return
        domain, name, profile = current
        if not str(profile.get("extends") or "").strip():
            QMessageBox.information(
                self,
                "Héritage",
                "Ce profil n’hérite d’aucune base.",
            )
            return
        if QMessageBox.question(
            self,
            "Détacher le profil",
            (
                f"Détacher « {name} » de sa base ?\n\n"
                "Les actions et conditions héritées seront copiées dans le "
                "profil afin de conserver le comportement effectif actuel."
            ),
        ) != QMessageBox.Yes:
            return
        draft, changed = detach_profile_inheritance(
            self.config,
            domain,
            name,
        )
        if not changed:
            return
        errors = validate_config(draft)
        if errors:
            QMessageBox.critical(
                self,
                "Héritage",
                "\n".join(errors),
            )
            return
        self.config = draft
        self._load_config_into_ui()
        self.profile_domain.setCurrentIndex(
            max(0, self.profile_domain.findData(domain))
        )
        self._refresh_profile_names()
        self.profile_name.setCurrentText(name)
        self._refresh_actions_table()
        self._mark_dirty()
        self._record_user_activity(
            UserActivityEntry(
                "Muted",
                "Profil détaché de sa base",
                f"{domain}/{name}",
            )
        )

    def _profile_parent_changed(self, *_args) -> None:
        current = self._current_profile()
        if not current or not hasattr(self, "profile_parent"):
            return
        parent = str(self.profile_parent.currentData() or "")
        if parent == current[1]:
            return
        if str(current[2].get("extends") or "") != parent:
            current[2]["extends"] = parent
            self._mark_dirty()

    def _selected_action_index(self) -> int | None:
        rows = self.actions_table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _new_profile(self) -> None:
        domain = str(self.profile_domain.currentData())
        name, ok = QInputDialog.getText(self, "Nouveau profil", "Nom du profil")
        name = name.strip()
        if not ok or not name:
            return
        profiles = self._profiles_for_domain(domain)
        if name in profiles:
            QMessageBox.warning(self, "Profil", "Ce profil existe déjà.")
            return
        profiles[name] = {"actions": [], "extends": "", "conditions": {}}
        self._mark_dirty()
        self._refresh_profile_names()
        self.profile_name.setCurrentText(name)
        self._refresh_override_boxes()

    def _duplicate_profile(self) -> None:
        current = self._current_profile()
        if not current:
            return
        domain, old_name, profile = current
        name, ok = QInputDialog.getText(self, "Dupliquer le profil", "Nouveau nom", text=f"{old_name} (copie)")
        name = name.strip()
        if not ok or not name:
            return
        profiles = self._profiles_for_domain(domain)
        if name in profiles:
            QMessageBox.warning(self, "Profil", "Ce profil existe déjà.")
            return
        profiles[name] = copy.deepcopy(profile)
        self._mark_dirty()
        self._refresh_profile_names()
        self.profile_name.setCurrentText(name)
        self._refresh_override_boxes()

    def _profile_references(self, domain: str, name: str) -> list[str]:
        key = {
            "game": "Game",
            "overlay": "OverlayProfile",
            "capture": "CaptureProfile",
            "audio": "AudioProfile",
        }[domain]
        refs: list[str] = []
        fallback = self.config.get("router", {}).get("fallback_state", {})
        if isinstance(fallback, dict) and fallback.get(key) == name:
            refs.append("fallback")
        for rule in self.config.get("rules", []):
            if rule.get("behavior", "match") != "match":
                continue
            state = rule.get("state") if isinstance(rule.get("state"), dict) else {}
            if state.get(key) == name:
                refs.append(str(rule.get("name") or "règle sans nom"))
        for child_name, child in self._profiles_for_domain(domain).items():
            if child_name != name and isinstance(child, dict) and str(child.get("extends") or "") == name:
                refs.append(f"profil {child_name} (héritage)")
        return refs

    def _replace_profile_references(self, domain: str, old: str, new: str) -> None:
        key = {
            "game": "Game",
            "overlay": "OverlayProfile",
            "capture": "CaptureProfile",
            "audio": "AudioProfile",
        }[domain]
        fallback = self.config.get("router", {}).get("fallback_state", {})
        if isinstance(fallback, dict) and fallback.get(key) == old:
            fallback[key] = new
        for rule in self.config.get("rules", []):
            state = rule.get("state") if isinstance(rule.get("state"), dict) else {}
            if state.get(key) == old:
                state[key] = new
        for child in self._profiles_for_domain(domain).values():
            if isinstance(child, dict) and str(child.get("extends") or "") == old:
                child["extends"] = new

    def _rename_profile(self) -> None:
        current = self._current_profile()
        if not current:
            return
        domain, old_name, profile = current
        name, ok = QInputDialog.getText(self, "Renommer le profil", "Nouveau nom", text=old_name)
        name = name.strip()
        if not ok or not name or name == old_name:
            return
        profiles = self._profiles_for_domain(domain)
        if name in profiles:
            QMessageBox.warning(self, "Profil", "Ce profil existe déjà.")
            return
        del profiles[old_name]
        profiles[name] = profile
        self._replace_profile_references(domain, old_name, name)
        self._mark_dirty()
        self._refresh_profile_names()
        self.profile_name.setCurrentText(name)
        self._refresh_rules_table()
        self._refresh_override_boxes()

    def _delete_profile(self) -> None:
        current = self._current_profile()
        if not current:
            return
        domain, name, _ = current
        refs = self._profile_references(domain, name)
        if refs:
            QMessageBox.warning(
                self,
                "Profil utilisé",
                "Ce profil est encore référencé par : " + ", ".join(refs) + ".\n"
                "Renommez la référence ou utilisez un autre profil avant de le supprimer.",
            )
            return
        if QMessageBox.question(self, "Supprimer", f"Supprimer le profil « {name} » ?") != QMessageBox.Yes:
            return
        del self._profiles_for_domain(domain)[name]
        self._mark_dirty()
        self._refresh_profile_names()
        self._refresh_override_boxes()

    def _guided_capture_current_state(
        self,
        *,
        customizations: tuple[
            tuple[str, str, str],
            ...,
        ] = (),
    ) -> None:
        if not self._require_edit_mode("Capturer l’état actuel"):
            return
        service = self._service
        client = self._client
        if service is None:
            QMessageBox.warning(
                self,
                "Capture de l’état actuel",
                "Le runtime SSR n’est pas disponible.",
            )
            return

        if (
            self._draft_dirty
            or (
                bool(self._saved_revision)
                and bool(self._applied_revision)
                and self._saved_revision != self._applied_revision
            )
        ):
            QMessageBox.information(
                self,
                "Capture de l’état actuel",
                (
                    "La configuration ouverte n’est pas encore synchronisée "
                    "avec le runtime.\n\n"
                    "Utilisez d’abord « Enregistrer et appliquer », puis "
                    "relancez la capture afin que SSR parte d’un état cohérent."
                ),
            )
            return

        if (
            client is None
            or not client.config.enabled
            or not client.connected
        ):
            QMessageBox.warning(
                self,
                "Capture de l’état actuel",
                "OBS doit être connecté à SSR avant de capturer l’état actuel.",
            )
            return

        app = service.last_app or service.last_meaningful_app
        if app is None:
            QMessageBox.information(
                self,
                "Capture de l’état actuel",
                (
                    "Aucune application exploitable n’est détectée. "
                    "Placez l’application à configurer au premier plan au moins "
                    "une fois, puis relancez la capture."
                ),
            )
            return

        process = str(app.exe_name or "").strip()
        if not process:
            process = os.path.basename(str(app.process_path or "").strip())
        if not process:
            QMessageBox.information(
                self,
                "Capture de l’état actuel",
                "Le processus de l’application cible est inconnu.",
            )
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
                        "L’application cible est actuellement ignorée par "
                        f"la règle « {routing_rule or 'IGNORE'} ».\n\n"
                        "L’assistant Simple ne remplace jamais une règle IGNORE. "
                        "Modifiez d’abord cette règle en mode Expert."
                    ),
                )
                return

            exact_matches = find_process_rules(self.config, process)
            exact_names = {
                str(rule.get("name") or "").strip()
                for rule in exact_matches
            }
            if (
                routing_kind == "match"
                and routing_rule
                and routing_rule not in exact_names
            ):
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
                            f"« {routing_rule} », mais cette règle n’est pas une "
                            "règle simple liée exactement au processus détecté.\n\n"
                            "Pour éviter de créer ou modifier une règle concurrente, "
                            "utilisez le mode Expert pour cette configuration."
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
            self._track_obs_request(
                request_id,
                busy_text="Capture…",
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
            "app_customizations": [
                [key, mode, value]
                for key, mode, value in customizations
            ],
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
        raw_customizations = context.get("app_customizations")
        customization_plan: dict[str, tuple[str, str]] = {}
        if isinstance(raw_customizations, list):
            for raw in raw_customizations:
                if (
                    isinstance(raw, (list, tuple))
                    and len(raw) == 3
                ):
                    key = str(raw[0] or "").strip()
                    mode = str(raw[1] or "").strip().casefold()
                    value = str(raw[2] or "").strip()
                    if key and mode in {
                        "inherit",
                        "capture",
                        "profile",
                    }:
                        customization_plan[key] = (mode, value)

        dialog = CurrentStateCaptureDialog(
            self,
            process=process,
            process_path=process_path,
            window_title=window_title,
            current_scene=str(snapshot.current_program_scene or ""),
            suggested_name=suggested_name,
            matching_rules=matches,
            customization_plan=customization_plan,
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
            input_settings_domain=str(
                raw_options.get("input_settings_domain") or "game"
            ),
            audio_state_domain=str(
                raw_options.get("audio_state_domain") or "game"
            ),
            filters_domain=str(
                raw_options.get("filters_domain") or "game"
            ),
            visibility_domain=str(
                raw_options.get("visibility_domain") or "game"
            ),
            state_overrides={
                key: value
                for key, (mode, value)
                in customization_plan.items()
                if mode == "profile" and value
            },
            inherit_state_keys=tuple(
                key
                for key, (mode, _value)
                in customization_plan.items()
                if mode == "inherit"
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

        previous_config = copy.deepcopy(self.config)
        self.config = copy.deepcopy(draft.config)
        self._load_config_into_ui()
        self._mark_dirty()
        self._set_config_undo_checkpoint(
            f"Capture de l’état OBS pour « {draft.report.rule_name} »",
            previous_config,
        )
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
            self._track_obs_request(
                request_id,
                busy_text="Analyse…",
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
        self._apply_obs_connected_control_state(migrate)
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
        feedback_control = self.sender()
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
            self._track_obs_request(
                request_id,
                busy_text="Analyse…",
                control=feedback_control,
            )
            self._track_obs_request(
                request_id,
                busy_text="Lecture OBS…",
                control=feedback_control,
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
        feedback_control = self.sender()
        if not self._require_edit_mode("Migrer la collection OBS"):
            return
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
            self._track_obs_request(
                request_id,
                busy_text="Analyse…",
                control=feedback_control,
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
        if (
            self._collection_import_requires_edit_mode(mode)
            and not self._edit_mode
        ):
            self._log(
                "Résultat d’import OBS ignoré : Mode édition terminé."
            )
            self.statusBar().showMessage(
                "Import terminé mais non appliqué : Mode édition inactif",
                8000,
            )
            QMessageBox.information(
                self,
                "Mode édition terminé",
                (
                    "La lecture OBS s’est terminée après la sortie du "
                    "Mode édition. Aucun changement n’a été appliqué au "
                    "brouillon. Réactivez Mode édition puis relancez "
                    "l’opération."
                ),
            )
            return
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

        if mode == "reference_repair":
            self._record_user_activity(
                UserActivityEntry(
                    "Muted",
                    "Analyse des références OBS terminée",
                    (
                        f"{len(snapshot.scenes)} scène(s), "
                        f"{len(snapshot.inputs)} input(s), "
                        f"{len(snapshot.filters)} filtre(s)"
                    ),
                )
            )
            self._show_reference_repair_dialog(snapshot)
            return

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
        self._set_config_undo_checkpoint(
            (
                f"Import collection OBS vers {domain}/{profile_name}"
                if mode == "snapshot_profile"
                else "Migration logique collection OBS / ASC"
            ),
            previous,
        )
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
        if not self._safe_live_confirm("Tester ce profil directement sur OBS"):
            return
        self._collect_settings()
        errors = validate_config(self.config)
        if errors:
            QMessageBox.critical(
                self,
                "Test du profil",
                "Le brouillon doit être valide avant le test :\n- "
                + "\n- ".join(errors),
            )
            return
        service = self._service
        if service is None:
            QMessageBox.critical(
                self,
                "Test du profil",
                "Runtime SSR indisponible.",
            )
            return
        domain, name, _profile = current
        profiles_root = self.config.get("profiles", {})
        domain_profiles = (
            profiles_root.get(domain, {})
            if isinstance(profiles_root, Mapping)
            else {}
        )
        if not isinstance(domain_profiles, Mapping):
            QMessageBox.critical(
                self,
                "Test du profil",
                "Domaine de profils invalide dans le brouillon.",
            )
            return
        try:
            request_id = service.request_profile_test(
                domain,
                name,
                domain_profiles,
            )
            self._track_obs_request(
                request_id,
                busy_text="Test…",
            )
            self.statusBar().showMessage(
                f"Test du profil « {name} » envoyé au worker SSR.",
                5000,
            )
        except Exception as exc:
            QMessageBox.critical(self, "Test du profil", str(exc))

    def _state_profile_choices(self) -> dict[str, list[str]]:
        profiles = self.config.get("profiles", {})
        choices = {
            domain: sorted((profiles.get(domain) or {}).keys(), key=str.casefold)
            for domain in PROFILE_DOMAINS
        }
        choices["layout"] = sorted(
            self.config.get("layout_profiles", {}).keys(),
            key=str.casefold,
        )
        choices["presentation"] = sorted(
            (
                self.config.get("presentation_profiles", {})
                if isinstance(
                    self.config.get("presentation_profiles"),
                    Mapping,
                )
                else {}
            ).keys(),
            key=str.casefold,
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
        if hasattr(self, "layout_capture_button"):
            self.layout_capture_button.setText(
                (
                    f"Capturer OBS → « {current[0]} »"
                    if current
                    else "Capturer OBS dans le brouillon"
                )
            )
        if hasattr(self, "layout_apply_button"):
            self.layout_apply_button.setText(
                (
                    f"Appliquer « {current[0]} » à OBS"
                    if current
                    else "Appliquer à OBS"
                )
            )
        modules = current[1].get("modules", {}) if current else {}
        if not isinstance(modules, dict):
            modules = {}
        self.layout_modules_table.setRowCount(len(modules))
        self._inspect_current_layout()
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
        if hasattr(self, "layout_inheritance_hint"):
            if current:
                name, profile = current
                lineage = profile_lineage(
                    self.config,
                    "layout",
                    name,
                )
                local_modules = profile.get("modules")
                local_count = (
                    len(local_modules)
                    if isinstance(local_modules, Mapping)
                    else 0
                )
                inherited_count = 0
                profiles = self._layout_profiles()
                for parent_name in lineage[1:]:
                    parent = profiles.get(parent_name)
                    if not isinstance(parent, Mapping):
                        continue
                    parent_modules = parent.get("modules")
                    if isinstance(parent_modules, Mapping):
                        inherited_count += len(parent_modules)
                chain = " ← ".join(lineage) if lineage else name
                self.layout_inheritance_hint.setText(
                    f"Héritage effectif : {chain} · "
                    f"{local_count} module(s) local(aux) · "
                    f"{inherited_count} hérité(s)"
                )
            else:
                self.layout_inheritance_hint.setText(
                    "Héritage effectif : —"
                )

    def _detach_current_layout_profile(self) -> None:
        current = self._current_layout_profile()
        if current is None:
            return
        name, profile = current
        if not str(profile.get("extends") or "").strip():
            QMessageBox.information(
                self,
                "Héritage layout",
                "Ce LayoutProfile n’hérite d’aucune base.",
            )
            return
        if QMessageBox.question(
            self,
            "Détacher le LayoutProfile",
            (
                f"Détacher « {name} » de sa base ?\n\n"
                "Le layout résolu sera matérialisé dans ce profil afin de "
                "conserver exactement son état effectif."
            ),
        ) != QMessageBox.Yes:
            return
        draft, changed = detach_profile_inheritance(
            self.config,
            "layout",
            name,
        )
        if not changed:
            return
        errors = validate_config(draft)
        if errors:
            QMessageBox.critical(
                self,
                "Héritage layout",
                "\n".join(errors),
            )
            return
        self.config = draft
        self._load_config_into_ui()
        self.layout_profile_name.setCurrentText(name)
        self._refresh_layout_profile_view()
        self._mark_dirty()
        self._record_user_activity(
            UserActivityEntry(
                "Muted",
                "LayoutProfile détaché de sa base",
                name,
            )
        )

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
        if not self._safe_live_confirm("Appliquer ce layout maintenant"):
            return
        self._collect_settings()
        try:
            request_id = self._service.request_layout("apply", current[0])
            self._pending_layout_manual_apply[request_id] = current[0]
            self._track_obs_request(
                request_id,
                busy_text="Application…",
            )
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
        if not self._safe_live_confirm("Prévisualiser ce layout dans OBS"):
            return
        try:
            request_id = self._service.request_layout("preview", current[0])
            self._track_obs_request(
                request_id,
                busy_text="Prévisualisation…",
            )
            self._log(f"Aperçu layout {current[0]} mis en file ({request_id[:8]}).")
        except Exception as exc:
            QMessageBox.critical(self, "Aperçu layout", str(exc))

    def _cancel_layout_preview(self) -> None:
        if self._service is None:
            return
        try:
            request_id = self._service.request_layout("cancel-preview")
            self._track_obs_request(
                request_id,
                busy_text="Annulation…",
            )
            self._log(f"Annulation aperçu mise en file ({request_id[:8]}).")
        except Exception as exc:
            QMessageBox.critical(self, "Aperçu layout", str(exc))

    def _undo_layout_obs(self) -> None:
        if self._service is None:
            return
        if not self._safe_live_confirm("Restaurer le layout précédent dans OBS"):
            return
        try:
            request_id = self._service.request_layout("undo")
            if (
                isinstance(self._manual_undo, Mapping)
                and str(self._manual_undo.get("kind") or "") == "layout_obs"
            ):
                self._manual_undo_request_id = request_id
            self._track_obs_request(
                request_id,
                busy_text="Restauration…",
            )
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

    def _update_widget_runtime_status(self) -> None:
        label = getattr(self, "widget_runtime_status", None)
        if label is None:
            return
        runtime = self._widget_runtime
        if runtime is not None and runtime.running:
            label.setText(
                "Widget Runtime : "
                f"{runtime.base_url} · Browser Sources SSR actives"
            )
            label.setObjectName("Good")
        elif runtime is not None and not runtime.config.enabled:
            label.setText(
                "Widget Runtime : désactivé · repli file:// disponible"
            )
            label.setObjectName("Muted")
        else:
            label.setText(
                "Widget Runtime : indisponible · repli file:// disponible"
            )
            label.setObjectName("Warn")
        label.style().unpolish(label)
        label.style().polish(label)

    def _start_widget_runtime(self, *, event_bus=None) -> None:
        cfg = build_widget_runtime_config(self.config)
        runtime = WidgetRuntime(
            cfg,
            self._presentation_state_store,
            event_bus=event_bus,
            media_state_store=self._media_state_store,
            media_artwork_store=self._media_artwork_store,
        )
        self._widget_runtime = runtime
        try:
            runtime.start()
            if cfg.enabled:
                self._log(
                    f"Widget Runtime actif sur {runtime.base_url}."
                )
        except Exception as exc:
            self._log(f"Widget Runtime indisponible : {exc}")
        self._update_widget_runtime_status()

    def _restart_widget_runtime(self) -> None:
        runtime = self._widget_runtime
        event_bus = runtime.event_bus if runtime is not None else None
        if runtime is not None:
            runtime.stop()
        # Restarting the HTTP server for a saved configuration must not create
        # a new event stream. Existing Browser Sources retain their cursor and
        # continue on the same bus/session identity.
        self._start_widget_runtime(event_bus=event_bus)
        self._refresh_widget_library()

    def _stop_widget_runtime(self) -> None:
        runtime = self._widget_runtime
        self._widget_runtime = None
        if runtime is not None:
            runtime.stop()
        self._update_widget_runtime_status()

    def _update_media_runtime_status(self) -> None:
        label = getattr(self, "media_runtime_status", None)
        if label is None:
            return
        runtime = self._media_runtime
        state = self._media_state_store.snapshot()
        if runtime is None or not runtime.config.enabled:
            label.setText("Media Runtime : désactivé")
            label.setObjectName("Muted")
        elif not runtime.running:
            label.setText("Media Runtime : indisponible")
            label.setObjectName("Warn")
        elif bool(state.get("connected", False)):
            playback = str(
                state.get("playback_state") or "unknown"
            )
            labels = {
                "playing": "lecture",
                "paused": "pause",
                "stopped": "arrêt",
                "unknown": "état inconnu",
            }
            title = str(state.get("title") or "").strip()
            suffix = (
                f" · {title}"
                if title
                else ""
            )
            label.setText(
                "Media Runtime : VLC connecté · "
                + labels.get(playback, playback)
                + suffix
            )
            label.setObjectName("Good")
        else:
            error = str(state.get("error") or "").strip()
            label.setText(
                "Media Runtime : VLC non joignable"
                + (f" · {error}" if error else "")
            )
            label.setObjectName("Warn")
        label.style().unpolish(label)
        label.style().polish(label)

    def _start_media_runtime(self) -> None:
        cfg = build_media_runtime_config(self.config)
        provider = build_media_provider(self.config)
        event_bus = (
            self._widget_runtime.event_bus
            if self._widget_runtime is not None
            else None
        )
        runtime = MediaRuntime(
            cfg,
            provider,
            state_store=self._media_state_store,
            artwork_store=self._media_artwork_store,
            command_store=self._media_command_store,
            event_bus=event_bus,
        )
        self._media_runtime = runtime
        runtime.start()
        self._update_media_runtime_status()
        if cfg.enabled:
            self._log(
                "Media Runtime actif · provider VLC local."
            )

    def _restart_media_runtime(self) -> None:
        runtime = self._media_runtime
        if runtime is not None and not runtime.stop():
            self._log(
                "Media Runtime : redémarrage refusé, "
                "ancien worker encore actif."
            )
            return
        self._media_runtime = None
        self._start_media_runtime()

    def _stop_media_runtime(self) -> None:
        runtime = self._media_runtime
        self._media_runtime = None
        if runtime is not None:
            runtime.stop()
        self._update_media_runtime_status()

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
            "widget_runtime": {
                "running": bool(
                    self._widget_runtime
                    and self._widget_runtime.running
                ),
                "base_url": (
                    self._widget_runtime.base_url
                    if self._widget_runtime
                    and self._widget_runtime.running
                    else ""
                ),
            },
            "media": {
                "running": bool(
                    self._media_runtime
                    and self._media_runtime.running
                ),
                "state": self._media_state_store.public_snapshot(),
            },
            "control_variables": (
                service.control_variables() if service else {}
            ),
            "config_revision": {
                "saved": self._saved_revision,
                "applied": self._applied_revision,
            },
            "routing": service.routing_status() if service else {},
            "manual_override": (
                service.manual_override_status()
                if service
                else {"active": False}
            ),
            "obs_catalog": (
                service.obs_catalog_status()
                if service
                else {"available": False}
            ),
        }

    def _api_request_status(self, request_id: str) -> dict | None:
        if self._service is not None:
            result = self._service.command_status(request_id)
            if result is not None:
                return result
        media_result = self._media_command_store.get(request_id)
        if media_result is not None:
            return media_result
        return None

    def _api_action(self, action: str, payload: dict) -> dict:
        if str(action or "").startswith("media."):
            runtime = self._media_runtime
            if runtime is None or not runtime.running:
                raise RuntimeError("Media Runtime non disponible")
            media_action = str(action).split(".", 1)[1].strip().casefold()
            options: dict[str, object] = {}
            if media_action == "seek":
                options["seconds"] = payload.get("seconds", 0)
            elif media_action == "set_volume":
                options["percent"] = payload.get("percent", 0)
            elif media_action in {"play_uri", "enqueue_uri"}:
                options["uri"] = str(payload.get("uri") or "")
            request_id = runtime.request(media_action, **options)
            return {
                "request_id": request_id,
                "status": "accepted",
            }

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
            return {
                "paused": False,
                "manual_override": self._service.manual_override_status(),
            }
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
            state = StreamState.from_mapping(
                payload.get("state")
                if isinstance(payload.get("state"), dict)
                else {}
            )
            duration = float(
                payload.get("duration_seconds", 0) or 0
            )
            release_mode = str(
                payload.get("release_mode") or "manual"
            ).strip().casefold()
            self._service.set_manual_override(
                state,
                duration_seconds=duration or None,
                release_mode=release_mode,
            )
            return {
                "state": state.as_variables(),
                "manual_override": self._service.manual_override_status(),
            }
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
        if not self._safe_live_confirm("Passer ce layout en mode édition OBS"):
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
        if not self._require_edit_mode("Importer une configuration"):
            return
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

    def _load_backup_as_draft(
        self,
        incoming: Mapping[str, object],
        *,
        source_name: str,
    ) -> bool:
        current_review = build_config_change_review(
            self._last_saved_config,
            self.config,
        )
        if current_review.has_changes:
            if QMessageBox.question(
                self,
                "Remplacer le brouillon actuel",
                (
                    f"Le brouillon actuel contient "
                    f"{len(current_review.changes)} changement(s) non "
                    "enregistré(s).\n\n"
                    "Le chargement de la sauvegarde les remplacera. Continuer ?"
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            ) != QMessageBox.Yes:
                return False

        self.config = copy.deepcopy(dict(incoming))
        self._load_config_into_ui()
        incoming_review = build_config_change_review(
            self._last_saved_config,
            self.config,
        )
        self._draft_dirty = incoming_review.has_changes
        self._refresh_config_revision_status(
            draft_dirty=incoming_review.has_changes
        )
        self._refresh_dashboard_summary()
        self._record_user_activity(
            UserActivityEntry(
                "Muted",
                "Sauvegarde chargée comme brouillon",
                source_name,
            )
        )
        self.statusBar().showMessage(
            (
                "Sauvegarde chargée comme brouillon"
                if incoming_review.has_changes
                else "Sauvegarde identique à la configuration enregistrée"
            ),
            5000,
        )
        return True

    def _restore_config_backup(self) -> None:
        if not self._require_edit_mode("Restaurer une sauvegarde"):
            return
        try:
            backups = list_valid_backups(limit=20)
        except Exception as exc:
            QMessageBox.critical(self, "Sauvegarde", str(exc))
            return
        if not backups:
            QMessageBox.information(
                self,
                "Sauvegarde",
                "Aucune sauvegarde valide n’a été trouvée.",
            )
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Historique des sauvegardes")
        dialog.resize(1050, 620)
        root = QVBoxLayout(dialog)

        intro = QLabel(
            "SSR conserve automatiquement les configurations précédentes. "
            "Sélectionnez une sauvegarde pour voir précisément les différences "
            "avec la configuration enregistrée actuelle. La restauration charge "
            "uniquement un brouillon : le runtime ne change qu’après "
            "« Enregistrer et appliquer »."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        selector = QComboBox()
        for index, (_payload, path) in enumerate(backups):
            try:
                stamp = time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(path.stat().st_mtime),
                )
            except OSError:
                stamp = path.name
            selector.addItem(
                f"{stamp} · {path.name}",
                index,
            )
        root.addWidget(selector)

        details = QLabel()
        details.setWordWrap(True)
        details.setObjectName("Muted")
        root.addWidget(details)

        change_summary = QLabel()
        change_summary.setStyleSheet("font-weight: 700;")
        root.addWidget(change_summary)

        change_tree = QTreeWidget()
        change_tree.setColumnCount(5)
        change_tree.setHeaderLabels(
            ["Catégorie", "Élément", "Modification", "Détail", "Impact"]
        )
        change_tree.setRootIsDecorated(False)
        change_tree.setAlternatingRowColors(True)
        change_tree.header().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        change_tree.header().setStretchLastSection(True)
        root.addWidget(change_tree, 1)

        def refresh_details() -> None:
            try:
                index = int(selector.currentData())
            except (TypeError, ValueError):
                index = 0
            payload, path = backups[max(0, min(index, len(backups) - 1))]
            rules = payload.get("rules")
            rule_count = len(rules) if isinstance(rules, list) else 0
            profiles = payload.get("profiles")
            profile_count = 0
            if isinstance(profiles, Mapping):
                profile_count = sum(
                    len(values)
                    for values in profiles.values()
                    if isinstance(values, Mapping)
                )
            layouts = payload.get("layout_profiles")
            layout_count = (
                len(layouts)
                if isinstance(layouts, Mapping)
                else 0
            )
            details.setText(
                f"Révision {config_revision(payload)} · "
                f"{rule_count} règle(s) · "
                f"{profile_count} profil(s) · "
                f"{layout_count} layout(s)\n{path}"
            )

            review = build_config_change_review(
                self._last_saved_config,
                payload,
            )
            change_summary.setText(
                "Effet du chargement : " + review.summary
            )
            change_tree.clear()
            for change in review.changes:
                change_tree.addTopLevelItem(
                    QTreeWidgetItem(
                        [
                            change.category,
                            change.target,
                            change.kind,
                            change.detail,
                            change.impact,
                        ]
                    )
                )

        selector.currentIndexChanged.connect(refresh_details)
        refresh_details()

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("Annuler")
        cancel.clicked.connect(dialog.reject)
        actions.addWidget(cancel)
        restore = QPushButton("Charger comme brouillon")
        restore.setObjectName("Primary")
        restore.clicked.connect(dialog.accept)
        actions.addWidget(restore)
        root.addLayout(actions)

        if dialog.exec() != QDialog.Accepted:
            return
        try:
            index = int(selector.currentData())
        except (TypeError, ValueError):
            index = 0
        incoming, path = backups[max(0, min(index, len(backups) - 1))]
        if not self._load_backup_as_draft(
            incoming,
            source_name=path.name,
        ):
            return
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

        change_categories: list[str] = []
        if self._draft_dirty:
            try:
                report = build_config_change_review(
                    self._last_saved_config,
                    self.config,
                )
                change_categories = [
                    change.category for change in report.changes
                ]
            except Exception:
                change_categories = []

        banner = build_draft_banner(
            draft_dirty=self._draft_dirty,
            saved_revision=self._saved_revision,
            applied_revision=self._applied_revision,
            change_categories=change_categories,
        )

        if self._expert_mode:
            if self._draft_dirty:
                text = (
                    f"{banner.title} · enregistré {saved} · "
                    f"appliqué {applied}"
                )
            elif saved != applied:
                text = (
                    f"Enregistré {saved} · runtime encore sur {applied}"
                )
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
            f"Révision enregistrée : {saved}\n"
            f"Révision runtime : {applied}"
        )
        if hasattr(self, "draft_detail"):
            self.draft_detail.setText(
                (
                    f"{banner.title} · {banner.detail}"
                    if self._draft_dirty
                    else banner.detail
                )
            )
        if hasattr(self, "draft_banner"):
            self.draft_banner.setVisible(banner.visible)
        if hasattr(self, "review_draft_button"):
            self.review_draft_button.setEnabled(self._draft_dirty)
        if hasattr(self, "discard_draft_button"):
            self.discard_draft_button.setEnabled(self._draft_dirty)
        if hasattr(self, "save_button"):
            self.save_button.setEnabled(
                self._draft_dirty
                or (
                    bool(self._saved_revision)
                    and bool(self._applied_revision)
                    and self._saved_revision != self._applied_revision
                )
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
        self._stop_media_runtime()
        self._stop_widget_runtime()
        self._stop_runtime_for_exit()
        event.accept()
        QApplication.instance().quit()

    def _quit_app(self) -> None:
        self._save_window_geometry()
        self._quitting = True
        if self._api:
            self._api.stop()
        self._stop_media_runtime()
        self._stop_widget_runtime()
        self._stop_runtime_for_exit()
        self.tray.hide()
        QApplication.instance().quit()
