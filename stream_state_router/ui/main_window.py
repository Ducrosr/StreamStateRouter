from __future__ import annotations

import copy
import json
from PySide6.QtCore import QObject, Qt, Signal, QTimer
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
)

from ..activation import TriggerTargetIdentity
from ..obs.client import OBSClientManager
from ..obs.dispatcher import PROFILE_DOMAINS, STATE_DOMAINS, OBSDispatcher
from ..obs.layouts import OBSLayoutManager, anchor_factors, compact_layout_overrides, diff_layout_profiles, resolve_layout_profile
from ..router.engine import StateChange, StateRouterEngine
from ..router.models import ForegroundApp, StreamState
from ..services.config import (
    build_activation_policies,
    build_obs_config,
    build_profiles,
    build_layout_profiles,
    build_ruleset,
    export_config,
    import_config,
    save_config,
    validate_config,
    push_layout_history,
    pop_layout_history,
)
from ..services.runtime import RoutingService, RuntimeEvent
from ..services.api import APIConfig, LocalControlAPI
from ..services.startup import is_startup_enabled, set_startup_enabled
from .dialogs import ActionDialog, ModuleLayoutDialog, RuleDialog


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
    def __init__(self, config: dict, *, logger, start_minimized: bool = False):
        super().__init__()
        self.setWindowTitle("Stream State Router 2.0.13")
        self.resize(1180, 760)
        self.config = copy.deepcopy(config)
        self.logger = logger
        self.start_minimized = start_minimized
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

        self.bridge = RuntimeBridge()
        self.bridge.foreground.connect(self._on_foreground)
        self.bridge.state_change.connect(self._on_state_change)
        self.bridge.dispatch.connect(self._on_dispatch)
        self.bridge.runtime_event.connect(self._on_runtime_event)

        self._build_ui()
        self._build_menu()
        self._build_tray()
        self._load_config_into_ui()
        self._wire_dirty_signals()
        self._start_runtime()
        self._start_api()
        self._module_scan_timer = QTimer(self)
        self._module_scan_timer.timeout.connect(self._auto_scan_modules)
        self._configure_module_scan_timer()

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
        self.pause_button = QPushButton("Suspendre")
        self.pause_button.clicked.connect(self._toggle_pause)
        top.addWidget(self.pause_button)
        layout.addLayout(top)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.tabs.addTab(self._build_dashboard(), "Dashboard")
        self.tabs.addTab(self._build_rules_tab(), "Règles")
        self.tabs.addTab(self._build_profiles_tab(), "Profils OBS")
        self.tabs.addTab(self._build_layouts_tab(), "Layouts")
        self.tabs.addTab(self._build_settings_tab(), "Paramètres")
        self.tabs.addTab(self._build_logs_tab(), "Journal")

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

    def _card(self, title_text: str) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("Card")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(16, 14, 16, 14)
        label = QLabel(title_text)
        label.setObjectName("Section")
        lay.addWidget(label)
        return frame, lay

    def _build_dashboard(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setSpacing(12)

        app_card, app_lay = self._card("Application au premier plan")
        self.fg_exe = QLabel("—")
        self.fg_exe.setStyleSheet("font-size: 15pt; font-weight: 700;")
        self.fg_title = QLabel("—")
        self.fg_title.setObjectName("Muted")
        self.fg_path = QLabel("—")
        self.fg_path.setObjectName("Muted")
        self.fg_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        app_lay.addWidget(self.fg_exe)
        app_lay.addWidget(self.fg_title)
        app_lay.addWidget(self.fg_path)
        root.addWidget(app_card)

        state_card, state_lay = self._card("État logique courant")
        self.state_labels: dict[str, QLabel] = {}
        state_form = QFormLayout()
        for key in ("Game", "OverlayProfile", "CaptureProfile", "AudioProfile", "LayoutProfile"):
            value = QLabel("—")
            value.setStyleSheet("font-weight: 700;")
            self.state_labels[key] = value
            state_form.addRow(key, value)
        self.rule_label = QLabel("Règle : —")
        self.rule_label.setObjectName("Muted")
        state_lay.addLayout(state_form)
        state_lay.addWidget(self.rule_label)
        root.addWidget(state_card)

        override_card, override_lay = self._card("Override manuel")
        form = QFormLayout()
        self.override_boxes: dict[str, QComboBox] = {}
        for domain in STATE_DOMAINS:
            box = QComboBox()
            self.override_boxes[domain] = box
            form.addRow(DOMAIN_LABELS[domain], box)
        self.override_duration = QSpinBox()
        self.override_duration.setRange(0, 1440)
        self.override_duration.setSuffix(" min")
        self.override_duration.setSpecialValueText("Permanent")
        form.addRow("Durée override", self.override_duration)
        override_lay.addLayout(form)
        actions = QHBoxLayout()
        apply_button = QPushButton("Appliquer l'override")
        apply_button.setObjectName("Primary")
        apply_button.clicked.connect(self._apply_override)
        clear_button = QPushButton("Revenir au routage automatique")
        clear_button.clicked.connect(self._clear_override)
        reapply_button = QPushButton("Réappliquer à OBS")
        reapply_button.clicked.connect(self._force_reapply)
        actions.addWidget(apply_button)
        actions.addWidget(clear_button)
        actions.addWidget(reapply_button)
        actions.addStretch(1)
        override_lay.addLayout(actions)
        root.addWidget(override_card)
        root.addStretch(1)
        return page

    def _build_rules_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        self.rules_table = QTableWidget(0, 9)
        self.rules_table.setHorizontalHeaderLabels(
            ["Actif", "Nom", "Comportement", "Priorité", "Exe", "Chemin", "Titre", "Game", "Profils"]
        )
        self.rules_table.setAlternatingRowColors(True)
        self.rules_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.rules_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.rules_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.rules_table.horizontalHeader().setStretchLastSection(True)
        self.rules_table.doubleClicked.connect(self._edit_rule)
        root.addWidget(self.rules_table, 1)

        buttons = QHBoxLayout()
        for text, slot, primary in [
            ("Ajouter", self._add_rule, True),
            ("Modifier", self._edit_rule, False),
            ("Dupliquer", self._duplicate_rule, False),
            ("Activer/Désactiver", self._toggle_rule, False),
            ("Tester sur l’app courante", self._test_rule, False),
            ("Supprimer", self._delete_rule, False),
        ]:
            b = QPushButton(text)
            if primary:
                b.setObjectName("Primary")
            if text == "Supprimer":
                b.setObjectName("Danger")
            b.clicked.connect(slot)
            buttons.addWidget(b)
        buttons.addStretch(1)
        root.addLayout(buttons)
        return page

    def _build_profiles_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)

        top = QHBoxLayout()
        top.addWidget(QLabel("Domaine"))
        self.profile_domain = QComboBox()
        for domain in PROFILE_DOMAINS:
            self.profile_domain.addItem(DOMAIN_LABELS[domain], domain)
        self.profile_domain.currentIndexChanged.connect(self._refresh_profile_names)
        top.addWidget(self.profile_domain)
        top.addWidget(QLabel("Profil"))
        self.profile_name = QComboBox()
        self.profile_name.currentIndexChanged.connect(self._refresh_actions_table)
        top.addWidget(self.profile_name, 1)
        for text, slot in [
            ("Nouveau", self._new_profile),
            ("Dupliquer", self._duplicate_profile),
            ("Renommer", self._rename_profile),
            ("Supprimer", self._delete_profile),
            ("Tester", self._test_profile),
        ]:
            b = QPushButton(text)
            if text == "Nouveau":
                b.setObjectName("Primary")
            if text == "Supprimer":
                b.setObjectName("Danger")
            b.clicked.connect(slot)
            top.addWidget(b)
        root.addLayout(top)

        inheritance = QHBoxLayout()
        inheritance.addWidget(QLabel("Hérite de"))
        self.profile_parent = QComboBox()
        self.profile_parent.addItem("— Aucun —", "")
        self.profile_parent.currentIndexChanged.connect(self._profile_parent_changed)
        inheritance.addWidget(self.profile_parent, 1)
        hint = QLabel("Les actions du parent sont exécutées avant celles de ce profil.")
        hint.setObjectName("Muted")
        inheritance.addWidget(hint)
        root.addLayout(inheritance)

        self.actions_table = QTableWidget(0, 4)
        self.actions_table.setHorizontalHeaderLabels(["Actif", "Type", "Nom", "Paramètres"])
        self.actions_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.actions_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.actions_table.setAlternatingRowColors(True)
        self.actions_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.actions_table.horizontalHeader().setStretchLastSection(True)
        self.actions_table.doubleClicked.connect(self._edit_action)
        root.addWidget(self.actions_table, 1)

        buttons = QHBoxLayout()
        for text, slot in [
            ("Ajouter une action", self._add_action),
            ("Modifier", self._edit_action),
            ("Dupliquer", self._duplicate_action),
            ("Activer/Désactiver", self._toggle_action),
            ("Supprimer", self._delete_action),
            ("Monter", lambda: self._move_action(-1)),
            ("Descendre", lambda: self._move_action(1)),
        ]:
            b = QPushButton(text)
            if text == "Ajouter une action":
                b.setObjectName("Primary")
            if text == "Supprimer":
                b.setObjectName("Danger")
            b.clicked.connect(slot)
            buttons.addWidget(b)
        buttons.addStretch(1)
        root.addLayout(buttons)
        return page

    def _build_layouts_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setSpacing(10)

        intro = QLabel(
            "Les sources OBS nommées « [Type de module] Nom du module » sont détectées automatiquement. "
            "Le préfixe entre crochets sert uniquement de catégorie ; chaque source OBS reste un module distinct. "
            "Un LayoutProfile mémorise sa position, sa taille et sa visibilité."
        )
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        root.addWidget(intro)

        obs_row = QHBoxLayout()
        obs_row.addWidget(QLabel("Scène OBS"))
        self.layout_scene = QComboBox()
        self.layout_scene.setMinimumWidth(260)
        self.layout_scene.currentIndexChanged.connect(self._layout_scene_changed)
        obs_row.addWidget(self.layout_scene, 1)
        sync = QPushButton("Synchroniser avec OBS")
        sync.setObjectName("Primary")
        sync.clicked.connect(self._sync_obs_modules)
        obs_row.addWidget(sync)
        root.addLayout(obs_row)

        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("LayoutProfile"))
        self.layout_profile_name = QComboBox()
        self.layout_profile_name.currentIndexChanged.connect(self._refresh_layout_profile_view)
        profile_row.addWidget(self.layout_profile_name, 1)
        for text, slot in [
            ("Nouveau", self._new_layout_profile),
            ("Dupliquer", self._duplicate_layout_profile),
            ("Renommer", self._rename_layout_profile),
            ("Supprimer", self._delete_layout_profile),
            ("Capturer depuis OBS", self._capture_layout_profile),
            ("Appliquer maintenant", self._apply_layout_profile),
            ("Éditer dans OBS", self._edit_layout_in_obs),
        ]:
            button = QPushButton(text)
            if text == "Capturer depuis OBS":
                button.setObjectName("Primary")
            elif text == "Supprimer":
                button.setObjectName("Danger")
            button.clicked.connect(slot)
            profile_row.addWidget(button)
        root.addLayout(profile_row)

        options = QHBoxLayout()
        options.addWidget(QLabel("Base"))
        self.layout_parent = QComboBox()
        self.layout_parent.addItem("— Aucune —", "")
        self.layout_parent.currentIndexChanged.connect(self._layout_option_changed)
        options.addWidget(self.layout_parent)
        options.addWidget(QLabel("Coordonnées"))
        self.layout_coordinate_mode = QComboBox()
        self.layout_coordinate_mode.addItem("Normalisées", "normalized")
        self.layout_coordinate_mode.addItem("Absolues", "absolute")
        self.layout_coordinate_mode.currentIndexChanged.connect(self._layout_option_changed)
        options.addWidget(self.layout_coordinate_mode)
        options.addWidget(QLabel("Transition"))
        self.layout_transition = QComboBox()
        for label, value in [("Instantanée", "instant"), ("Déplacement", "move"), ("Fondu", "fade"), ("Déplacement + fondu", "move_fade")]:
            self.layout_transition.addItem(label, value)
        self.layout_transition.currentIndexChanged.connect(self._layout_option_changed)
        options.addWidget(self.layout_transition)
        self.layout_transition_ms = QSpinBox()
        self.layout_transition_ms.setRange(0, 3000)
        self.layout_transition_ms.setSuffix(" ms")
        self.layout_transition_ms.valueChanged.connect(self._layout_option_changed)
        options.addWidget(self.layout_transition_ms)
        options.addStretch(1)
        root.addLayout(options)

        tools = QHBoxLayout()
        for text, slot in [
            ("Prévisualiser", self._preview_layout_profile),
            ("Annuler aperçu", self._cancel_layout_preview),
            ("Undo OBS", self._undo_layout_obs),
            ("Comparer à OBS", self._diff_layout_with_obs),
            ("Comparer 2 layouts", self._diff_two_layouts),
            ("Valider", self._validate_layout_profile),
            ("Restaurer version précédente", self._restore_layout_revision),
        ]:
            button = QPushButton(text)
            button.clicked.connect(slot)
            tools.addWidget(button)
        tools.addStretch(1)
        root.addLayout(tools)

        content = QHBoxLayout()

        catalog_card, catalog_lay = self._card("Catalogue OBS — éléments à inclure lors de la capture")
        self.module_tree = QTreeWidget()
        self.module_tree.setHeaderLabels(["Type / module", "Source OBS"])
        self.module_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.module_tree.header().setStretchLastSection(True)
        self.module_tree.itemChanged.connect(self._catalog_item_changed)
        catalog_lay.addWidget(self.module_tree)
        content.addWidget(catalog_card, 1)

        layout_card, layout_lay = self._card("Modules mémorisés dans le LayoutProfile")
        self.layout_modules_table = QTableWidget(0, 8)
        self.layout_modules_table.setHorizontalHeaderLabels(
            ["Module OBS", "Éléments", "X", "Y", "Largeur", "Hauteur", "Visible", "Ancre"]
        )
        self.layout_modules_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.layout_modules_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.layout_modules_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.layout_modules_table.horizontalHeader().setStretchLastSection(True)
        self.layout_modules_table.doubleClicked.connect(self._edit_layout_module)
        layout_lay.addWidget(self.layout_modules_table)
        edit = QPushButton("Modifier position, taille et éléments…")
        edit.clicked.connect(self._edit_layout_module)
        layout_lay.addWidget(edit, alignment=Qt.AlignLeft)
        content.addWidget(layout_card, 2)

        root.addLayout(content, 1)
        return page

    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)

        router_card, router_lay = self._card("Moteur de routage")
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
        form.addRow("", self.obs_enabled)
        form.addRow("Hôte", self.obs_host)
        form.addRow("Port", self.obs_port)
        form.addRow("Mot de passe", self.obs_password)
        obs_lay.addLayout(form)
        test = QPushButton("Tester la connexion OBS")
        test.clicked.connect(self._test_obs)
        obs_lay.addWidget(test, alignment=Qt.AlignLeft)
        root.addWidget(obs_card)

        api_card, api_lay = self._card("API locale / Stream Deck")
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
            ("Exporter la configuration…", self._export_config),
            ("Importer une configuration…", self._import_config),
            ("Quitter", self._quit_app),
        ]:
            action = QAction(text, self)
            action.triggered.connect(slot)
            file_menu.addAction(action)

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
        self.api_enabled.setChecked(bool(api.get("enabled", True)))
        self.api_port.setValue(int(api.get("port", 8765)))
        self.api_token.setText(str(api.get("token") or ""))
        self.close_to_tray.setChecked(bool(ui.get("close_to_tray", True)))
        self.auto_detect_modules.setChecked(bool(ui.get("auto_detect_modules", True)))
        self.module_scan_seconds.setValue(int(ui.get("module_scan_seconds", 5)))
        try:
            actual_startup = is_startup_enabled()
        except Exception:
            actual_startup = bool(ui.get("start_with_windows", False))
        self.start_with_windows.setChecked(actual_startup)
        self._refresh_rules_table()
        self._refresh_profile_names()
        self._refresh_layout_profile_names()
        self._refresh_override_boxes()
        self.unsaved.setText("")

    def _wire_dirty_signals(self) -> None:
        for widget in (self.poll_ms, self.debounce_ms, self.fallback_debounce_ms, self.obs_port, self.api_port, self.module_scan_seconds):
            widget.valueChanged.connect(self._mark_dirty)
        for widget in (self.obs_enabled, self.close_to_tray, self.start_with_windows, self.api_enabled, self.auto_detect_modules):
            widget.toggled.connect(self._mark_dirty)
        for widget in (self.obs_host, self.obs_password, self.api_token):
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
        api = self.config.setdefault("api", {})
        api["enabled"] = self.api_enabled.isChecked()
        api["host"] = "127.0.0.1"
        api["port"] = self.api_port.value()
        api["token"] = self.api_token.text()
        ui = self.config.setdefault("ui", {})
        ui["close_to_tray"] = self.close_to_tray.isChecked()
        ui["start_with_windows"] = self.start_with_windows.isChecked()
        ui["auto_detect_modules"] = self.auto_detect_modules.isChecked()
        ui["module_scan_seconds"] = self.module_scan_seconds.value()

    def save_and_apply(self) -> None:
        self._collect_settings()
        errors = validate_config(self.config)
        if errors:
            QMessageBox.critical(self, "Configuration invalide", "\n".join(errors))
            return
        try:
            save_config(self.config)
            set_startup_enabled(self.start_with_windows.isChecked())
        except Exception as exc:
            QMessageBox.critical(self, "Enregistrement", str(exc))
            return
        if not self._restart_runtime():
            self.unsaved.setText("Configuration enregistrée, application runtime incomplète")
            self.statusBar().showMessage(
                "Configuration enregistrée — runtime précédent encore actif",
                6000,
            )
            return
        self._restart_api()
        self._configure_module_scan_timer()
        self._refresh_override_boxes()
        self.unsaved.setText("")
        self.statusBar().showMessage("Configuration enregistrée et appliquée", 4000)
        self._log("Configuration enregistrée et appliquée.")

    def _start_runtime(self) -> None:
        rules, poll_ms, debounce_ms, fallback_ms = build_ruleset(self.config)
        self._client = OBSClientManager(build_obs_config(self.config))
        self._dispatcher = OBSDispatcher(
            self._client,
            build_profiles(self.config),
            build_layout_profiles(self.config),
        )
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
            activation_policies=build_activation_policies(self.config),
        )
        self._service.on_foreground = self.bridge.foreground.emit
        self._service.on_change = self.bridge.state_change.emit
        self._service.on_dispatch = self.bridge.dispatch.emit
        self._service.on_event = self.bridge.runtime_event.emit
        self._service.start()
        self._update_obs_status()

    def _restart_runtime(self) -> bool:
        previous = self._service
        if previous is not None and not previous.stop():
            self._log(
                "Runtime précédent toujours actif : redémarrage refusé pour éviter des écritures OBS concurrentes."
            )
            QMessageBox.critical(
                self,
                "Runtime",
                "Le runtime précédent n'a pas terminé son nettoyage. "
                "Le nouveau runtime n'a pas été démarré.",
            )
            return False
        self._start_runtime()
        return True

    def _on_foreground(self, app: ForegroundApp | None) -> None:
        if app is None:
            self.fg_exe.setText("Aucune fenêtre")
            self.fg_title.setText("—")
            self.fg_path.setText("—")
            return
        self.fg_exe.setText(app.exe_name or f"PID {app.pid}")
        self.fg_title.setText(app.window_title or "(sans titre)")
        self.fg_path.setText(app.process_path or "(chemin indisponible)")

    def _on_state_change(self, change: StateChange) -> None:
        values = change.current.as_variables()
        for key, label in self.state_labels.items():
            label.setText(values.get(key, "—"))
        self.rule_label.setText(f"Règle : {change.rule_name} · raison : {change.reason}")
        self._log(
            f"État → {values['Game']} / {values['OverlayProfile']} / "
            f"{values['CaptureProfile']} / {values['AudioProfile']} / "
            f"{values['LayoutProfile']} [{change.rule_name}]"
        )

    def _on_dispatch(self, result) -> None:
        self._update_obs_status()
        if result.executed:
            self._log(
                f"OBS : {result.executed} action(s) exécutée(s) · "
                f"{', '.join(result.changed_domains)}"
            )

    def _on_runtime_event(self, event: RuntimeEvent) -> None:
        self._log(f"{event.kind}: {event.message}")
        if event.kind == "activation_command_result" and event.payload is not None:
            self.bridge.activation_result.emit(event.payload)
            return
        if event.kind in {"obs_connected", "obs_disconnected"}:
            self._update_obs_status()
        elif event.kind == "obs_error":
            self.obs_status.setText("OBS : erreur")
            self.obs_status.setObjectName("Bad")
            self.obs_status.style().unpolish(self.obs_status)
            self.obs_status.style().polish(self.obs_status)

    def _update_obs_status(self) -> None:
        if not self._client:
            return
        if not self._client.config.enabled:
            text, style = "OBS : désactivé", "Muted"
        elif self._client.connected:
            text, style = "OBS : connecté", "Good"
        elif self._client.last_error:
            text, style = "OBS : déconnecté", "Bad"
        else:
            text, style = "OBS : connexion…", "Warn"
        self.obs_status.setText(text)
        self.obs_status.setObjectName(style)
        self.obs_status.style().unpolish(self.obs_status)
        self.obs_status.style().polish(self.obs_status)

    def _apply_override(self) -> None:
        if not self._service:
            return
        state = StreamState(
            game=self.override_boxes["game"].currentText(),
            overlay_profile=self.override_boxes["overlay"].currentText(),
            capture_profile=self.override_boxes["capture"].currentText(),
            audio_profile=self.override_boxes["audio"].currentText(),
            layout_profile=self.override_boxes["layout"].currentText(),
        )
        duration = self.override_duration.value() * 60
        self._service.set_manual_override(
            state,
            duration_seconds=(duration if duration > 0 else None),
        )
        self._log(
            "Override manuel appliqué"
            + (f" pour {self.override_duration.value()} min." if duration else " sans expiration.")
        )

    def _clear_override(self) -> None:
        if self._service:
            self._service.clear_manual_override()
            self._log("Override manuel désactivé.")

    def _force_reapply(self) -> None:
        if not self._service:
            return
        try:
            result = self._service.force_reapply()
            if result is None:
                QMessageBox.information(self, "OBS", "Aucun état courant à réappliquer.")
            else:
                QMessageBox.information(
                    self,
                    "OBS",
                    f"{result.executed} action(s) réappliquée(s).",
                )
        except Exception as exc:
            QMessageBox.critical(self, "OBS", str(exc))

    def _toggle_pause(self) -> None:
        if not self._service:
            return
        new_value = not self._service.paused
        self._service.pause(new_value)
        self.pause_button.setText("Reprendre" if new_value else "Suspendre")

    # ---------- rules ----------
    def _refresh_rules_table(self) -> None:
        rules = self.config.setdefault("rules", [])
        self.rules_table.setRowCount(len(rules))
        for row, rule in enumerate(rules):
            state = rule.get("state") if isinstance(rule.get("state"), dict) else {}
            profiles = (
                f"{state.get('OverlayProfile', '')} / {state.get('CaptureProfile', '')} / "
                f"{state.get('AudioProfile', '')} / {state.get('LayoutProfile', '')}"
                if rule.get("behavior", "match") == "match"
                else "—"
            )
            values = [
                "✓" if rule.get("enabled", True) else "",
                rule.get("name", ""),
                rule.get("behavior", "match"),
                str(rule.get("priority", 0)),
                rule.get("exe", ""),
                rule.get("path", ""),
                rule.get("title_regex", ""),
                state.get("Game", "") if rule.get("behavior", "match") == "match" else "—",
                profiles,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.rules_table.setItem(row, col, item)

    def _selected_rule_index(self) -> int | None:
        rows = self.rules_table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _add_rule(self) -> None:
        dlg = RuleDialog(self, profile_choices=self._state_profile_choices())
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
        if app is None:
            QMessageBox.information(self, "Test de règle", "Aucune application au premier plan détectée.")
            return
        temp = copy.deepcopy(self.config)
        temp["rules"] = [copy.deepcopy(self.config["rules"][idx])]
        try:
            rules, _poll, _debounce, _fallback = build_ruleset(temp)
            context = self._dispatcher.obs_context() if self._dispatcher else {}
            resolution = rules.resolve(app, context)
        except Exception as exc:
            QMessageBox.critical(self, "Test de règle", str(exc))
            return
        selected = self.config["rules"][idx]
        if resolution.rule_name == selected.get("name"):
            if resolution.kind.value == "ignore":
                message = f"La règle correspond à {app.exe_name} et conserverait l’état courant (IGNORE)."
            else:
                state = resolution.state.as_variables() if resolution.state else {}
                message = f"La règle correspond à {app.exe_name}.\n\nÉtat : {state}"
        else:
            message = f"La règle ne correspond pas à l’application courante : {app.exe_name}."
        QMessageBox.information(self, "Test de règle", message)

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
        actions = current[2].setdefault("actions", []) if current else []
        self.actions_table.setRowCount(len(actions))
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
        self._refresh_actions_table(); self.actions_table.selectRow(new_idx)

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
            push_layout_history(self.config, name, old_profile)
            captured = self._layout_sync_manager.capture_profile(
                scene,
                selected_sources=selected,
                extends=str(old_profile.get("extends") or ""),
                transition=old_profile.get("transition") if isinstance(old_profile.get("transition"), dict) else None,
            )
            captured["coordinate_mode"] = str(old_profile.get("coordinate_mode") or "normalized")
            captured["conditions"] = copy.deepcopy(old_profile.get("conditions") or {})
            parent_name = str(captured.get("extends") or "")
            if parent_name and parent_name in self._layout_profiles():
                parent = resolve_layout_profile(parent_name, self._layout_profiles())
                captured = compact_layout_overrides(captured, parent)
        except Exception as exc:
            QMessageBox.critical(self, "Capturer le layout", str(exc))
            return
        if not captured.get("modules"):
            QMessageBox.warning(
                self,
                "Capturer le layout",
                "Aucune source correspondant à « [Type de module] Nom du module » n'a été trouvée parmi la sélection.",
            )
            return
        self._layout_profiles()[name] = captured
        self._mark_dirty()
        self._refresh_layout_profile_view()
        self._refresh_override_boxes()
        self.statusBar().showMessage(f"Layout « {name} » capturé depuis OBS", 4000)
        try:
            resolved = resolve_layout_profile(name, self._layout_profiles())
            issues = self._layout_sync_manager.validate_profile(resolved)
        except Exception:
            issues = []
        errors = [issue for issue in issues if issue.level == "error"]
        if errors:
            QMessageBox.warning(
                self,
                "Layout capturé avec avertissements",
                "Le layout a été enregistré, mais certains éléments devront être vérifiés :\n\n"
                + "\n".join(f"• {issue.message}" for issue in errors[:20]),
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
        if not current:
            return
        self._collect_settings()
        try:
            manager = self._layout_manager_for_tools()
            resolved = resolve_layout_profile(current[0], self._layout_profiles())
            result = manager.apply_profile(resolved)
        except Exception as exc:
            QMessageBox.critical(self, "Appliquer le layout", str(exc))
            return
        details = f"{result.elements_applied} élément(s) appliqué(s), {result.elements_skipped} ignoré(s)."
        if result.missing_sources:
            details += "\n\nSources introuvables :\n- " + "\n- ".join(result.missing_sources)
        if result.warnings:
            details += "\n\nAvertissements :\n- " + "\n- ".join(result.warnings)
        QMessageBox.information(self, "Layout appliqué", details)

    def _layout_manager_for_tools(self) -> OBSLayoutManager:
        self._collect_settings()
        cfg = build_obs_config(self.config)
        if not cfg.enabled:
            raise RuntimeError("Activez le pilotage OBS avant d'utiliser cet outil.")
        if self._dispatcher is not None:
            return self._dispatcher.layout_manager
        if self._layout_sync_manager is not None:
            return self._layout_sync_manager
        return OBSLayoutManager(OBSClientManager(cfg))

    def _preview_layout_profile(self) -> None:
        current = self._current_layout_profile()
        if not current:
            return
        try:
            manager = self._layout_manager_for_tools()
            resolved = resolve_layout_profile(current[0], self._layout_profiles())
            result = manager.preview_profile(resolved)
            self._preview_active = True
            self._log(f"Aperçu layout {current[0]} : {result.elements_applied} élément(s).")
        except Exception as exc:
            QMessageBox.critical(self, "Aperçu layout", str(exc))

    def _cancel_layout_preview(self) -> None:
        try:
            manager = self._layout_manager_for_tools()
            result = manager.cancel_preview()
            self._preview_active = False
            self._log(f"Aperçu annulé : {result.elements_applied} élément(s) restauré(s).")
        except Exception as exc:
            QMessageBox.critical(self, "Aperçu layout", str(exc))

    def _undo_layout_obs(self) -> None:
        try:
            manager = self._layout_manager_for_tools()
            result = manager.undo_last()
            self._log(f"Undo OBS : {result.elements_applied} élément(s) restauré(s).")
            if result.warnings:
                QMessageBox.information(self, "Undo OBS", "\n".join(result.warnings))
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
        self._api = LocalControlAPI(cfg, status=self._api_status, action=self._api_action)
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
        }

    def _api_action(self, action: str, payload: dict) -> dict:
        if self._service is None or self._dispatcher is None:
            raise RuntimeError("Runtime non disponible")
        if action == "pause":
            self._service.pause(bool(payload.get("paused", True)))
            return {"paused": self._service.paused}
        if action == "auto":
            self._service.pause(False)
            self._service.clear_manual_override()
            return {"paused": False}
        if action == "reapply":
            result = self._service.force_reapply()
            return {"executed": result.executed if result else 0}
        if action == "override":
            state = StreamState.from_mapping(payload.get("state") if isinstance(payload.get("state"), dict) else {})
            duration = float(payload.get("duration_seconds", 0) or 0)
            self._service.set_manual_override(state, duration_seconds=duration or None)
            return {"state": state.as_variables()}
        if action == "layout.apply":
            name = str(payload.get("name") or "").strip()
            if not name:
                raise ValueError("name requis")
            result = self._dispatcher.execute_layout_profile(name)
            return {"executed": result.executed}
        if action == "layout.preview":
            name = str(payload.get("name") or "").strip()
            if not name:
                raise ValueError("name requis")
            result = self._dispatcher.execute_layout_profile(name, preview=True)
            return {"executed": result.executed}
        if action == "layout.cancel-preview":
            result = self._dispatcher.layout_manager.cancel_preview()
            return {"executed": result.elements_applied}
        if action == "layout.undo":
            result = self._dispatcher.layout_manager.undo_last()
            return {"executed": result.elements_applied}
        raise ValueError(f"Action inconnue : {action}")

    def _edit_layout_in_obs(self) -> None:
        current = self._current_layout_profile()
        if not current:
            return
        try:
            manager = self._layout_manager_for_tools()
            resolved = resolve_layout_profile(current[0], self._layout_profiles())
            result = manager.apply_profile(resolved)
            self._log(
                f"Mode édition OBS — layout {current[0]} appliqué ({result.elements_applied} élément(s)). "
                "Ajustez dans OBS puis utilisez Capturer depuis OBS."
            )
            self.statusBar().showMessage(
                "Mode édition : ajustez le layout dans OBS puis cliquez Capturer depuis OBS",
                8000,
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
        path, _ = QFileDialog.getSaveFileName(self, "Exporter la configuration", "stream-state-router-config.json", "JSON (*.json)")
        if not path:
            return
        try:
            export_config(self.config, path)
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

    def _mark_dirty(self, *_args) -> None:
        self.unsaved.setText("Modifications non enregistrées")

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

    def closeEvent(self, event: QCloseEvent) -> None:
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
        if self._service:
            self._service.stop()
        event.accept()
        QApplication.instance().quit()

    def _quit_app(self) -> None:
        self._quitting = True
        if self._api:
            self._api.stop()
        if self._service:
            self._service.stop()
        self.tray.hide()
        QApplication.instance().quit()
