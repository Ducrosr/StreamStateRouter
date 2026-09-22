from __future__ import annotations

import json
from copy import deepcopy
from typing import Mapping, Sequence

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..obs.models import OBSAction
from ..router.models import StreamState


class RuleDialog(QDialog):
    def __init__(
        self,
        parent=None,
        rule: dict | None = None,
        *,
        profile_choices: Mapping[str, Sequence[str]] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Règle d'application")
        self.resize(580, 680)
        self._source = deepcopy(rule or {})
        self._choices = {key: list(values) for key, values in (profile_choices or {}).items()}

        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)
        root.addLayout(form)

        self.name = QLineEdit(str(self._source.get("name") or ""))
        self.behavior = QComboBox()
        self.behavior.addItem("Appliquer un état", "match")
        self.behavior.addItem("Ignorer / conserver l'état courant", "ignore")
        idx = self.behavior.findData(str(self._source.get("behavior", "match")))
        self.behavior.setCurrentIndex(max(0, idx))
        self.priority = QSpinBox()
        self.priority.setRange(-100000, 100000)
        self.priority.setValue(int(self._source.get("priority", 0)))
        self.enabled = QCheckBox("Règle active")
        self.enabled.setChecked(bool(self._source.get("enabled", True)))
        self.exe = QLineEdit(str(self._source.get("exe") or ""))
        self.exe.setPlaceholderText("ex. Overwatch.exe ou *.exe")
        self.path = QLineEdit(str(self._source.get("path") or ""))
        self.path.setPlaceholderText(r"ex. C:\Games\*\game.exe")
        self.title_regex = QLineEdit(str(self._source.get("title_regex") or ""))
        self.title_regex.setPlaceholderText("Expression régulière facultative")
        self.apply_delay = QSpinBox()
        self.apply_delay.setRange(0, 10000)
        self.apply_delay.setSuffix(" ms")
        self.apply_delay.setValue(int(self._source.get("apply_delay_ms", 0)))

        conditions = self._source.get("conditions") if isinstance(self._source.get("conditions"), dict) else {}
        self.cond_streaming = self._condition_combo(conditions.get("streaming"))
        self.cond_recording = self._condition_combo(conditions.get("recording"))
        self.cond_program_scene = QLineEdit(str(conditions.get("program_scene") or ""))
        self.cond_program_scene.setPlaceholderText("facultatif, ex. In Game")

        form.addRow("Nom", self.name)
        form.addRow("Comportement", self.behavior)
        form.addRow("Priorité", self.priority)
        form.addRow("", self.enabled)
        form.addRow("Exécutable", self.exe)
        form.addRow("Chemin", self.path)
        form.addRow("Titre fenêtre", self.title_regex)
        form.addRow("Délai actions OBS", self.apply_delay)
        form.addRow("Condition : stream actif", self.cond_streaming)
        form.addRow("Condition : enregistrement actif", self.cond_recording)
        form.addRow("Condition : scène programme", self.cond_program_scene)

        title = QLabel("État logique")
        title.setObjectName("Section")
        root.addWidget(title)
        state_form = QFormLayout()
        root.addLayout(state_form)
        raw_state = self._source.get("state") if isinstance(self._source.get("state"), dict) else {}
        state = StreamState.from_mapping(raw_state)

        self.game = self._profile_combo("game", state.game)
        self.overlay = self._profile_combo("overlay", state.overlay_profile)
        self.capture = self._profile_combo("capture", state.capture_profile)
        self.audio = self._profile_combo("audio", state.audio_profile)
        self.layout = self._profile_combo("layout", state.layout_profile)
        state_form.addRow("Game", self.game)
        state_form.addRow("OverlayProfile", self.overlay)
        state_form.addRow("CaptureProfile", self.capture)
        state_form.addRow("AudioProfile", self.audio)
        state_form.addRow("LayoutProfile", self.layout)

        hint = QLabel(
            "LayoutProfile choisit la disposition des modules OBS nommés [Type de module] Nom du module "
            "pour cette application."
        )
        hint.setWordWrap(True)
        hint.setObjectName("Muted")
        root.addWidget(hint)

        self.behavior.currentIndexChanged.connect(self._sync_behavior)
        self._sync_behavior()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save
        )
        buttons.accepted.connect(self._accept_checked)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _condition_combo(current) -> QComboBox:
        box = QComboBox()
        box.addItem("Peu importe", None)
        box.addItem("Oui", True)
        box.addItem("Non", False)
        index = box.findData(current)
        box.setCurrentIndex(max(0, index))
        return box

    def _profile_combo(self, domain: str, current: str) -> QComboBox:
        box = QComboBox()
        box.setEditable(True)
        box.addItems(sorted({str(item) for item in self._choices.get(domain, [])}, key=str.casefold))
        if box.findText(current) < 0 and current:
            box.addItem(current)
        box.setCurrentText(current)
        return box

    def _sync_behavior(self) -> None:
        enabled = self.behavior.currentData() == "match"
        for widget in (self.game, self.overlay, self.capture, self.audio, self.layout):
            widget.setEnabled(enabled)

    def _accept_checked(self) -> None:
        if not self.name.text().strip():
            QMessageBox.warning(self, "Règle", "Le nom de la règle est requis.")
            return
        if not any(w.text().strip() for w in (self.exe, self.path, self.title_regex)):
            QMessageBox.warning(
                self,
                "Règle",
                "Indiquez au moins un sélecteur : exécutable, chemin ou titre.",
            )
            return
        self.accept()

    def result_rule(self) -> dict:
        raw = {
            "name": self.name.text().strip(),
            "behavior": str(self.behavior.currentData()),
            "priority": self.priority.value(),
            "enabled": self.enabled.isChecked(),
            "exe": self.exe.text().strip(),
            "path": self.path.text().strip(),
            "title_regex": self.title_regex.text().strip(),
            "apply_delay_ms": self.apply_delay.value(),
            "conditions": {},
        }
        if self.cond_streaming.currentData() is not None:
            raw["conditions"]["streaming"] = bool(self.cond_streaming.currentData())
        if self.cond_recording.currentData() is not None:
            raw["conditions"]["recording"] = bool(self.cond_recording.currentData())
        if self.cond_program_scene.text().strip():
            raw["conditions"]["program_scene"] = self.cond_program_scene.text().strip()
        if raw["behavior"] == "match":
            raw["state"] = {
                "Game": self.game.currentText().strip() or "Vanilla",
                "OverlayProfile": self.overlay.currentText().strip() or "Vanilla",
                "CaptureProfile": self.capture.currentText().strip() or "Default",
                "AudioProfile": self.audio.currentText().strip() or "Default",
                "LayoutProfile": self.layout.currentText().strip() or "Vanilla",
            }
        return raw


class ModuleLayoutDialog(QDialog):
    ANCHORS = [
        ("Haut gauche", "top_left", 0.0, 0.0),
        ("Haut centre", "top_center", 0.5, 0.0),
        ("Haut droite", "top_right", 1.0, 0.0),
        ("Centre gauche", "center_left", 0.0, 0.5),
        ("Centre", "center", 0.5, 0.5),
        ("Centre droite", "center_right", 1.0, 0.5),
        ("Bas gauche", "bottom_left", 0.0, 1.0),
        ("Bas centre", "bottom_center", 0.5, 1.0),
        ("Bas droite", "bottom_right", 1.0, 1.0),
    ]

    def __init__(
        self,
        parent=None,
        module_name: str = "",
        module: dict | None = None,
        *,
        activation_policy: dict | None = None,
        activation_candidates: Sequence[Mapping[str, object]] | None = None,
        activation_status_provider=None,
        activation_command=None,
        visibility_release_command=None,
        activation_result_signal=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Module — {module_name}")
        self.resize(720, 860)
        self._source = deepcopy(module or {})
        self._activation_policy_name = str(
            self._source.get("source_name") or module_name
        ).strip()
        self._activation_source = deepcopy(activation_policy) if isinstance(activation_policy, dict) else None
        self._activation_status_provider = activation_status_provider
        self._activation_command = activation_command
        self._visibility_release_command = visibility_release_command
        self._activation_result_signal = activation_result_signal
        self._pending_activation_requests: dict[str, str] = {}
        if self._activation_result_signal is not None:
            self._activation_result_signal.connect(self._on_activation_result)
        geometry = self._source.get("geometry") if isinstance(self._source.get("geometry"), dict) else {}
        base = self._source.get("base_bounds") if isinstance(self._source.get("base_bounds"), dict) else geometry
        self._aspect = max(0.0001, float(base.get("width", 1.0) or 1.0)) / max(0.0001, float(base.get("height", 1.0) or 1.0))
        self._geometry_guard = False

        root = QVBoxLayout(self)
        form = QFormLayout()
        root.addLayout(form)

        self.x = self._number(float(geometry.get("x", 0.0)))
        self.y = self._number(float(geometry.get("y", 0.0)))
        self.width = self._number(float(geometry.get("width", 1.0)), minimum=1.0)
        self.height = self._number(float(geometry.get("height", 1.0)), minimum=1.0)
        self.visible = QCheckBox("Module visible")
        self.visible.setChecked(bool(self._source.get("visible", True)))
        self.managed = QCheckBox("SSR gère ce module")
        self.managed.setChecked(bool(self._source.get("managed", True)))
        self.locked = QCheckBox("Ne pas appliquer ce module dans ce LayoutProfile")
        self.locked.setChecked(bool(self._source.get("locked", False)))
        self.lock_aspect = QCheckBox("Conserver les proportions lors du redimensionnement")
        self.lock_aspect.setChecked(bool(self._source.get("lock_aspect", True)))
        self.anchor = QComboBox()
        for label, value, _ax, _ay in self.ANCHORS:
            self.anchor.addItem(label, value)
        idx = self.anchor.findData(str(self._source.get("anchor") or "top_left"))
        self.anchor.setCurrentIndex(max(0, idx))
        self.anchor_mode = QComboBox()
        self.anchor_mode.addItem("Position proportionnelle au canvas", "relative")
        self.anchor_mode.addItem("Marge fixe depuis l'ancre", "pixel_margin")
        mode_idx = self.anchor_mode.findData(str(self._source.get("anchor_mode") or "relative"))
        self.anchor_mode.setCurrentIndex(max(0, mode_idx))

        form.addRow("X (coin supérieur gauche)", self.x)
        form.addRow("Y (coin supérieur gauche)", self.y)
        form.addRow("Largeur", self.width)
        form.addRow("Hauteur", self.height)
        form.addRow("Ancre de redimensionnement", self.anchor)
        form.addRow("Comportement résolution", self.anchor_mode)
        form.addRow("", self.lock_aspect)
        form.addRow("", self.visible)
        form.addRow("", self.managed)
        form.addRow("", self.locked)

        title = QLabel("Éléments inclus dans ce module")
        title.setObjectName("Section")
        root.addWidget(title)
        self.elements = QListWidget()
        self.elements.setMaximumHeight(150)
        for element in self._source.get("elements", []):
            if not isinstance(element, dict):
                continue
            item = QListWidgetItem(str(element.get("source") or element.get("element") or ""))
            item.setData(Qt.UserRole, str(element.get("source") or ""))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if bool(element.get("included", True)) else Qt.Unchecked)
            self.elements.addItem(item)
        root.addWidget(self.elements)

        info = QLabel(
            "Les éléments décochés ne seront pas modifiés. Exceptions facultatives par nom : "
            "[Module:nomove] Élément, [Module:noresize] Élément, [Module:novis] Élément, [Module:fixed] Élément. "
            "[Module:locked] Élément est totalement exclu des LayoutProfiles, ainsi que son sous-arbre."
        )
        info.setWordWrap(True)
        info.setObjectName("Muted")
        root.addWidget(info)

        activation_title = QLabel("Déclenchement")
        activation_title.setObjectName("Section")
        root.addWidget(activation_title)
        activation_hint = QLabel(
            "Politique globale de cette source OBS : elle s'applique à tous les LayoutProfiles. "
            "La visibilité des sources participantes appartient au scheduler et n'est pas mémorisée par les layouts."
        )
        activation_hint.setWordWrap(True)
        activation_hint.setObjectName("Muted")
        root.addWidget(activation_hint)

        activation_form = QFormLayout()
        root.addLayout(activation_form)
        self.activation_type = QComboBox()
        self.activation_type.addItem("Aucun", "none")
        self.activation_type.addItem("Aléatoire", "random")
        self.activation_enabled = QCheckBox("Politique active")
        self.activation_when = QComboBox()
        self.activation_when.addItem("Module présent dans la scène programme", "module_in_program_scene")
        self.activation_when.addItem("Stream actif", "streaming")
        self.activation_when.addItem("Toujours", "always")
        self.activation_chance = QDoubleSpinBox()
        self.activation_chance.setRange(0.0, 100.0)
        self.activation_chance.setDecimals(3)
        self.activation_chance.setSuffix(" %")
        self.activation_interval = QDoubleSpinBox()
        self.activation_interval.setRange(0.1, 86400.0)
        self.activation_interval.setDecimals(1)
        self.activation_interval.setSuffix(" s")
        self.activation_duration = QDoubleSpinBox()
        self.activation_duration.setRange(0.1, 86400.0)
        self.activation_duration.setDecimals(1)
        self.activation_duration.setSuffix(" s")
        self.activation_cooldown = QDoubleSpinBox()
        self.activation_cooldown.setRange(0.0, 86400.0)
        self.activation_cooldown.setDecimals(1)
        self.activation_cooldown.setSuffix(" s")
        self.activation_exclusive = QCheckBox("Une seule source participante visible à la fois")
        self.activation_repeat = QCheckBox("Éviter la répétition immédiate")

        activation_form.addRow("Type de déclenchement", self.activation_type)
        activation_form.addRow("", self.activation_enabled)
        activation_form.addRow("Actif quand", self.activation_when)
        activation_form.addRow("Chance par tirage", self.activation_chance)
        activation_form.addRow("Intervalle", self.activation_interval)
        activation_form.addRow("Durée par défaut", self.activation_duration)
        activation_form.addRow("Cooldown global", self.activation_cooldown)
        activation_form.addRow("", self.activation_exclusive)
        activation_form.addRow("", self.activation_repeat)

        policy = self._activation_source or {}
        self.activation_type.setCurrentIndex(1 if self._activation_source else 0)
        self.activation_enabled.setChecked(bool(policy.get("enabled", True)))
        when_idx = self.activation_when.findData(str(policy.get("active_when") or "module_in_program_scene"))
        self.activation_when.setCurrentIndex(max(0, when_idx))
        self.activation_chance.setValue(float(policy.get("chance", 0.01)) * 100.0)
        self.activation_interval.setValue(float(policy.get("interval_seconds", 60.0)))
        self.activation_duration.setValue(float(policy.get("default_duration_seconds", 10.0)))
        self.activation_cooldown.setValue(float(policy.get("cooldown_seconds", 600.0)))
        self.activation_exclusive.setChecked(bool(policy.get("exclusive", True)))
        self.activation_repeat.setChecked(bool(policy.get("avoid_immediate_repeat", True)))

        targets_title = QLabel("Sources participantes")
        targets_title.setObjectName("Section")
        root.addWidget(targets_title)
        self.activation_targets = QTableWidget(0, 4)
        self.activation_targets.setHorizontalHeaderLabels(["Actif", "Source OBS", "Poids", "Durée"])
        self.activation_targets.verticalHeader().setVisible(False)
        self.activation_targets.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.activation_targets.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        root.addWidget(self.activation_targets, 1)
        self._populate_activation_targets(activation_candidates or (), policy)

        self.activation_status = QLabel("État runtime : —")
        self.activation_status.setObjectName("Muted")
        root.addWidget(self.activation_status)
        self.activation_eligibility = QLabel("Éligibilité : —")
        self.activation_eligibility.setObjectName("Muted")
        root.addWidget(self.activation_eligibility)
        self.activation_last_event = QLabel("Dernier événement : —")
        self.activation_last_event.setWordWrap(True)
        self.activation_last_event.setObjectName("Muted")
        root.addWidget(self.activation_last_event)

        action_row = QHBoxLayout()
        for label, action in (
            ("Tester le tirage", "test_roll"),
            ("Déclencher maintenant", "trigger"),
            ("Arrêter", "stop"),
            ("Réinitialiser le cooldown", "reset_cooldown"),
            ("Réinitialiser runtime", "reset_all"),
        ):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, a=action: self._run_activation_command(a))
            action_row.addWidget(button)
        release_button = QPushButton("Restituer visibilité au LayoutProfile")
        release_button.clicked.connect(self._release_selected_visibility)
        action_row.addWidget(release_button)
        action_row.addStretch(1)
        root.addLayout(action_row)

        simulation_row = QHBoxLayout()
        simulation_row.addWidget(QLabel("Simulation déterministe"))
        self.activation_sim_trials = QSpinBox()
        self.activation_sim_trials.setRange(1, 100000)
        self.activation_sim_trials.setValue(1000)
        self.activation_sim_trials.setSuffix(" tirages")
        self.activation_sim_seed = QSpinBox()
        self.activation_sim_seed.setRange(0, 2147483647)
        self.activation_sim_seed.setValue(12345)
        self.activation_sim_seed.setPrefix("seed ")
        self.activation_sim_button = QPushButton("Simuler")
        self.activation_sim_button.clicked.connect(
            lambda _checked=False: self._run_activation_command("simulate")
        )
        simulation_row.addWidget(self.activation_sim_trials)
        simulation_row.addWidget(self.activation_sim_seed)
        simulation_row.addWidget(self.activation_sim_button)
        simulation_row.addStretch(1)
        root.addLayout(simulation_row)

        diagnostics_title = QLabel("Diagnostic scheduler")
        diagnostics_title.setObjectName("Section")
        root.addWidget(diagnostics_title)
        self.activation_diagnostics = QPlainTextEdit()
        self.activation_diagnostics.setReadOnly(True)
        self.activation_diagnostics.setMaximumBlockCount(40)
        self.activation_diagnostics.setMaximumHeight(140)
        root.addWidget(self.activation_diagnostics)

        test_hint = QLabel(
            "Les commandes manuelles utilisent exactement la même machine d'état que l'automatique : "
            "un cooldown actif n'est donc pas contourné. Après une modification, utilisez "
            "« Enregistrer et appliquer » avant de tester. Double-cliquez une source participante "
            "pour la déclencher directement."
        )
        test_hint.setWordWrap(True)
        test_hint.setObjectName("Muted")
        root.addWidget(test_hint)
        self.activation_targets.doubleClicked.connect(self._trigger_selected_target)

        self._last_x = self.x.value()
        self._last_y = self.y.value()
        self._last_width = self.width.value()
        self._last_height = self.height.value()
        self.x.valueChanged.connect(self._remember_position)
        self.y.valueChanged.connect(self._remember_position)
        self.width.valueChanged.connect(lambda _v: self._resize_from("width"))
        self.height.valueChanged.connect(lambda _v: self._resize_from("height"))
        self.activation_type.currentIndexChanged.connect(self._sync_activation_mode)
        self._sync_activation_mode()

        self._activation_timer = QTimer(self)
        self._activation_timer.timeout.connect(self._refresh_activation_status)
        self._activation_timer.start(500)
        self._refresh_activation_status()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _number(value: float, *, minimum: float = -20000.0) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setDecimals(2)
        box.setRange(minimum, 50000.0)
        box.setSingleStep(1.0)
        box.setValue(value)
        return box

    def _populate_activation_targets(
        self,
        candidates: Sequence[Mapping[str, object]],
        policy: Mapping[str, object],
    ) -> None:
        existing = {}
        raw_targets = policy.get("targets") if isinstance(policy, Mapping) else None
        if isinstance(raw_targets, list):
            for raw in raw_targets:
                if not isinstance(raw, Mapping):
                    continue
                key = (str(raw.get("container") or ""), str(raw.get("source") or ""))
                existing[key] = dict(raw)
        rows: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for raw in candidates:
            key = (str(raw.get("container") or ""), str(raw.get("source") or ""))
            if not key[0] or not key[1] or key in seen:
                continue
            merged = dict(raw)
            merged.update(existing.get(key, {}))
            rows.append(merged)
            seen.add(key)
        for key, raw in existing.items():
            if key not in seen:
                rows.append(dict(raw))

        self.activation_targets.setRowCount(len(rows))
        for row, raw in enumerate(rows):
            enabled = QCheckBox()
            enabled.setChecked(bool(raw.get("enabled", True)))
            self.activation_targets.setCellWidget(row, 0, enabled)
            source_item = QTableWidgetItem(str(raw.get("source") or ""))
            source_item.setFlags(source_item.flags() & ~Qt.ItemIsEditable)
            source_item.setData(Qt.UserRole, dict(raw))
            self.activation_targets.setItem(row, 1, source_item)
            weight = QDoubleSpinBox()
            weight.setRange(0.0, 100000.0)
            weight.setDecimals(3)
            weight.setValue(float(raw.get("weight", 1.0)))
            self.activation_targets.setCellWidget(row, 2, weight)
            duration = QDoubleSpinBox()
            duration.setRange(0.0, 86400.0)
            duration.setDecimals(1)
            duration.setSpecialValueText("Défaut")
            duration.setSuffix(" s")
            duration.setValue(float(raw.get("duration_seconds", 0.0) or 0.0))
            self.activation_targets.setCellWidget(row, 3, duration)
        self.activation_targets.resizeColumnsToContents()
        if self.activation_targets.columnWidth(1) < 220:
            self.activation_targets.setColumnWidth(1, 220)

    def _sync_activation_mode(self) -> None:
        enabled = self.activation_type.currentData() == "random"
        for widget in (
            self.activation_enabled,
            self.activation_when,
            self.activation_chance,
            self.activation_interval,
            self.activation_duration,
            self.activation_cooldown,
            self.activation_exclusive,
            self.activation_repeat,
            self.activation_targets,
            self.activation_sim_trials,
            self.activation_sim_seed,
            self.activation_sim_button,
        ):
            widget.setEnabled(enabled)

    def _refresh_activation_status(self) -> None:
        if self._activation_status_provider is None or not self._activation_policy_name:
            self.activation_status.setText("État runtime : politique non appliquée")
            self.activation_eligibility.setText("Éligibilité : —")
            self.activation_last_event.setText("Dernier événement : —")
            self.activation_diagnostics.clear()
            return
        try:
            status = self._activation_status_provider(self._activation_policy_name) or {}
        except Exception as exc:
            self.activation_status.setText(f"État runtime : erreur — {exc}")
            self.activation_eligibility.setText("Éligibilité : erreur")
            return
        if not status.get("available"):
            self.activation_status.setText("État runtime : politique non appliquée")
            self.activation_eligibility.setText("Éligibilité : —")
            self.activation_last_event.setText("Dernier événement : —")
            self.activation_diagnostics.clear()
            return
        phase = str(status.get("phase") or "idle")
        phase_label = {
            "idle": "Inactif",
            "eligible": "Éligible",
            "visible": "Déclenché / visible",
            "cooldown": "Cooldown",
        }.get(phase, phase)
        details = []
        if status.get("active_source"):
            details.append(str(status.get("active_source")))
        for key, label in (
            ("next_roll_seconds", "prochain tirage"),
            ("visible_seconds", "fin"),
            ("cooldown_seconds", "cooldown"),
        ):
            value = status.get(key)
            if value is not None:
                details.append(f"{label} {float(value):.1f} s")
        suffix = " — " + " · ".join(details) if details else ""
        self.activation_status.setText(f"État runtime : {phase_label}{suffix}")
        eligible = bool(status.get("eligible"))
        reason = str(status.get("eligibility_reason") or "—")
        self.activation_eligibility.setText(
            f"Éligibilité : {'oui' if eligible else 'non'} — {reason}"
        )
        last_event = str(status.get("last_event") or "—")
        self.activation_last_event.setText(f"Dernier événement : {last_event}")
        diagnostics = status.get("diagnostics") or []
        self.activation_diagnostics.setPlainText("\n".join(str(item) for item in diagnostics))

    def _run_activation_command(self, action: str, target=None) -> None:
        if self._activation_command is None:
            QMessageBox.information(
                self,
                "Déclenchement",
                "Enregistrez et appliquez d'abord cette politique pour la tester.",
            )
            return
        options = None
        if action == "simulate":
            options = {
                "trials": self.activation_sim_trials.value(),
                "seed": self.activation_sim_seed.value(),
            }
        try:
            request_id = self._activation_command(
                action,
                self._activation_policy_name,
                target,
                options,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Déclenchement", str(exc))
            return
        if request_id:
            self._pending_activation_requests[str(request_id)] = action
            self.activation_last_event.setText(
                f"Dernier événement : commande {action} en attente…"
            )
        self._refresh_activation_status()

    def _on_activation_result(self, payload) -> None:
        request_id = str(getattr(payload, "request_id", "") or "")
        action = self._pending_activation_requests.pop(request_id, None)
        if action is None:
            return
        if not bool(getattr(payload, "success", False)):
            QMessageBox.warning(
                self,
                "Déclenchement",
                str(getattr(payload, "error", "") or "Commande échouée"),
            )
            self._refresh_activation_status()
            return

        result = getattr(payload, "result", None)
        if action == "test_roll" and result is not None:
            verdict = "succès" if bool(getattr(result, "triggered", False)) else "échec"
            source_name = str(getattr(result, "source", "") or "")
            extra = f" → {source_name}" if source_name else ""
            QMessageBox.information(
                self,
                "Test du tirage",
                f"Tirage {float(getattr(result, 'roll', 0.0)) * 100.0:.3f} % "
                f"/ seuil {float(getattr(result, 'chance', 0.0)) * 100.0:.3f} % : "
                f"{verdict}{extra}.",
            )
        elif action == "simulate" and result is not None:
            counts = getattr(result, "target_counts", ()) or ()
            distribution = "\n".join(
                f"• {name} : {count} "
                f"({count / max(1, result.trigger_count) * 100.0:.2f} %)"
                for name, count in counts
            ) or "• aucune source sélectionnée"
            fingerprint = str(getattr(result, "config_fingerprint", "") or "—")
            QMessageBox.information(
                self,
                "Simulation déterministe",
                (
                    f"Configuration : {fingerprint}\n"
                    f"{result.trials} tirages · seed {result.seed}\n"
                    f"Succès chance : {result.chance_hit_count}\n"
                    f"Déclenchements : {result.trigger_count} "
                    f"({result.trigger_rate * 100.0:.2f} %)\n"
                    f"Échecs chance : {result.miss_count}\n"
                    f"Bloqués sans cible : {result.blocked_count}\n\n"
                    f"Distribution :\n{distribution}"
                ),
            )
        self._refresh_activation_status()

    def _trigger_selected_target(self, *_args) -> None:
        row = self.activation_targets.currentRow()
        if row < 0:
            return
        item = self.activation_targets.item(row, 1)
        if item is None:
            return
        raw = item.data(Qt.UserRole)
        target = dict(raw) if isinstance(raw, Mapping) else {
            "source": item.text().strip()
        }
        target["source"] = item.text().strip()
        if target.get("source"):
            self._run_activation_command("trigger", target)

    def _release_selected_visibility(self) -> None:
        row = self.activation_targets.currentRow()
        if row < 0:
            QMessageBox.information(self, "Visibilité", "Sélectionnez d'abord une source participante.")
            return
        item = self.activation_targets.item(row, 1)
        raw = item.data(Qt.UserRole) if item is not None else None
        if not isinstance(raw, Mapping):
            return
        if self._visibility_release_command is None:
            QMessageBox.information(self, "Visibilité", "Action de restitution indisponible.")
            return
        container = str(raw.get("container") or "").strip()
        source = str(raw.get("source") or item.text() or "").strip()
        try:
            changed = int(self._visibility_release_command(container, source) or 0)
        except Exception as exc:
            QMessageBox.warning(self, "Visibilité", str(exc))
            return
        QMessageBox.information(
            self,
            "Visibilité",
            f"{changed} marqueur(s) runtime restitué(s) aux LayoutProfiles. "
            "Recapturez le layout si vous souhaitez enregistrer l'état visible actuel.",
        )

    def done(self, result: int) -> None:
        if self._activation_result_signal is not None:
            try:
                self._activation_result_signal.disconnect(self._on_activation_result)
            except (RuntimeError, TypeError):
                pass
        self._pending_activation_requests.clear()
        super().done(result)

    def _anchor_factors(self) -> tuple[float, float]:
        current = str(self.anchor.currentData() or "top_left")
        for _label, value, ax, ay in self.ANCHORS:
            if value == current:
                return ax, ay
        return 0.0, 0.0

    def _remember_position(self, _value: float) -> None:
        if self._geometry_guard:
            return
        self._last_x = self.x.value()
        self._last_y = self.y.value()

    def _resize_from(self, source: str) -> None:
        if self._geometry_guard:
            return
        self._geometry_guard = True
        try:
            old_x = self._last_x
            old_y = self._last_y
            old_w = self._last_width
            old_h = self._last_height
            new_w = self.width.value()
            new_h = self.height.value()
            if self.lock_aspect.isChecked():
                if source == "width":
                    new_h = max(1.0, new_w / self._aspect)
                    self.height.setValue(new_h)
                else:
                    new_w = max(1.0, new_h * self._aspect)
                    self.width.setValue(new_w)
            ax, ay = self._anchor_factors()
            anchor_x = old_x + old_w * ax
            anchor_y = old_y + old_h * ay
            new_x = anchor_x - new_w * ax
            new_y = anchor_y - new_h * ay
            self.x.setValue(new_x)
            self.y.setValue(new_y)
            self._last_x = new_x
            self._last_y = new_y
            self._last_width = new_w
            self._last_height = new_h
        finally:
            self._geometry_guard = False

    def result_module(self) -> dict:
        result = deepcopy(self._source)
        for key in list(result):
            if key.startswith("_dialog_"):
                result.pop(key, None)
        result["geometry"] = {
            "x": self.x.value(),
            "y": self.y.value(),
            "width": self.width.value(),
            "height": self.height.value(),
        }
        result["visible"] = self.visible.isChecked()
        result["managed"] = self.managed.isChecked()
        result["locked"] = self.locked.isChecked()
        result["lock_aspect"] = self.lock_aspect.isChecked()
        result["anchor"] = str(self.anchor.currentData() or "top_left")
        result["anchor_mode"] = str(self.anchor_mode.currentData() or "relative")
        included = {
            str(self.elements.item(index).data(Qt.UserRole) or ""): self.elements.item(index).checkState() == Qt.Checked
            for index in range(self.elements.count())
        }
        for element in result.get("elements", []):
            if isinstance(element, dict):
                source = str(element.get("source") or "")
                if source in included:
                    element["included"] = included[source]
        return result

    def result_activation_policy(self) -> dict | None:
        if self.activation_type.currentData() != "random":
            return None
        targets = []
        for row in range(self.activation_targets.rowCount()):
            item = self.activation_targets.item(row, 1)
            if item is None:
                continue
            raw = item.data(Qt.UserRole)
            raw = dict(raw) if isinstance(raw, Mapping) else {}
            raw["source"] = item.text().strip()
            enabled = self.activation_targets.cellWidget(row, 0)
            weight = self.activation_targets.cellWidget(row, 2)
            duration = self.activation_targets.cellWidget(row, 3)
            raw["enabled"] = bool(enabled.isChecked()) if isinstance(enabled, QCheckBox) else True
            raw["weight"] = float(weight.value()) if isinstance(weight, QDoubleSpinBox) else 1.0
            duration_value = float(duration.value()) if isinstance(duration, QDoubleSpinBox) else 0.0
            if duration_value > 0:
                raw["duration_seconds"] = duration_value
            else:
                raw.pop("duration_seconds", None)
            targets.append(raw)
        return {
            "enabled": self.activation_enabled.isChecked(),
            "type": "random",
            "active_when": str(self.activation_when.currentData() or "module_in_program_scene"),
            "chance": self.activation_chance.value() / 100.0,
            "interval_seconds": self.activation_interval.value(),
            "cooldown_seconds": self.activation_cooldown.value(),
            "default_duration_seconds": self.activation_duration.value(),
            "exclusive": self.activation_exclusive.isChecked(),
            "avoid_immediate_repeat": self.activation_repeat.isChecked(),
            "targets": targets,
        }


class CollectionImportDialog(QDialog):
    def __init__(
        self,
        parent=None,
        *,
        target_domain: str,
        target_profile: str,
    ):
        super().__init__(parent)
        self.setWindowTitle("Importer la collection OBS")
        self.resize(650, 420)

        root = QVBoxLayout(self)
        intro = QLabel(
            "La collection OBS courante sera lue sans mutation. "
            f"Les actions compatibles seront ajoutées au profil "
            f"{target_domain}/{target_profile}. Les doublons de cible sont remplacés."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.input_settings = QCheckBox("Importer les réglages des inputs OBS")
        self.input_settings.setChecked(True)
        self.audio_state = QCheckBox("Importer mute et volume des inputs")
        self.audio_state.setChecked(True)
        self.filters = QCheckBox(
            "Importer état et paramètres des filtres OBS"
        )
        self.filters.setChecked(True)
        self.visibility = QCheckBox(
            "Importer la visibilité des Scene Items non ambigus"
        )
        self.visibility.setChecked(False)
        root.addWidget(self.input_settings)
        root.addWidget(self.audio_state)
        root.addWidget(self.filters)
        root.addWidget(self.visibility)

        asc_group = QFormLayout()
        self.asc_path = QLineEdit()
        self.asc_path.setPlaceholderText(
            "Facultatif : export JSON Advanced Scene Switcher "
            "ou fichier de collection OBS"
        )
        browse_row = QHBoxLayout()
        browse_row.addWidget(self.asc_path, 1)
        browse = QPushButton("Parcourir…")
        browse.clicked.connect(self._browse_asc)
        browse_row.addWidget(browse)
        asc_group.addRow("Advanced Scene Switcher", browse_row)
        root.addLayout(asc_group)

        note = QLabel(
            "Les macros ASC ne sont converties que si leur sémantique est "
            "reproductible exactement par SSR. Les autres restent listées "
            "dans le rapport d'import et ne sont jamais approximées."
        )
        note.setWordWrap(True)
        note.setObjectName("Muted")
        root.addWidget(note)

        root.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Importer")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _browse_asc(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Sélectionner un export Advanced Scene Switcher",
            self.asc_path.text().strip() or "",
            "JSON (*.json);;Texte (*.txt);;Tous les fichiers (*)",
        )
        if selected:
            self.asc_path.setText(selected)

    def options(self) -> dict[str, object]:
        return {
            "include_input_settings": self.input_settings.isChecked(),
            "include_audio_state": self.audio_state.isChecked(),
            "include_filters": self.filters.isChecked(),
            "include_visibility": self.visibility.isChecked(),
            "asc_path": self.asc_path.text().strip(),
        }


class ActionDialog(QDialog):
    ACTION_TYPES = [
        ("Changer de scène programme", "set_program_scene"),
        ("Afficher/masquer une source de scène", "scene_item_enabled"),
        ("Activer/désactiver un filtre", "source_filter_enabled"),
        ("Modifier les réglages d'un filtre", "source_filter_settings"),
        ("Mute/unmute une entrée", "input_mute"),
        ("Régler le volume d'une entrée (dB)", "input_volume_db"),
        ("Réglages avancés d'une entrée", "set_input_settings"),
        ("Router l'audio Windows d'une application", "app_audio_output"),
        ("Activer/désactiver HDR Windows", "windows_hdr"),
    ]

    def __init__(self, parent=None, action: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Action profil")
        self.resize(620, 470)
        source = OBSAction.from_mapping(action or {})

        root = QVBoxLayout(self)
        form = QFormLayout()
        root.addLayout(form)
        self.name = QLineEdit(source.name)
        self.enabled = QCheckBox("Action active")
        self.enabled.setChecked(source.enabled)
        self.kind = QComboBox()
        for label, value in self.ACTION_TYPES:
            self.kind.addItem(label, value)
        idx = self.kind.findData(source.type)
        if idx >= 0:
            self.kind.setCurrentIndex(idx)
        form.addRow("Nom facultatif", self.name)
        form.addRow("Type", self.kind)
        form.addRow("", self.enabled)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.pages: dict[str, tuple[QWidget, dict[str, QWidget]]] = {}
        for _, kind in self.ACTION_TYPES:
            page, fields = self._build_page(kind, dict(source.params) if source.type == kind else {})
            self.pages[kind] = (page, fields)
            self.stack.addWidget(page)
        self.kind.currentIndexChanged.connect(self._sync_page)
        self._sync_page()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save)
        buttons.accepted.connect(self._accept_checked)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_page(self, kind: str, params: dict):
        page = QWidget()
        form = QFormLayout(page)
        fields: dict[str, QWidget] = {}

        def line(key: str, label: str, placeholder: str = ""):
            widget = QLineEdit(str(params.get(key) or ""))
            widget.setPlaceholderText(placeholder)
            fields[key] = widget
            form.addRow(label, widget)

        def check(key: str, label: str, default: bool):
            widget = QCheckBox(label)
            widget.setChecked(bool(params.get(key, default)))
            fields[key] = widget
            form.addRow("", widget)

        def combo(
            key: str,
            label: str,
            choices: Sequence[tuple[str, str]],
            default: str,
        ):
            widget = QComboBox()
            for text, value in choices:
                widget.addItem(text, value)
            current = str(params.get(key) or default)
            index = widget.findData(current)
            widget.setCurrentIndex(max(0, index))
            fields[key] = widget
            form.addRow(label, widget)

        def json_settings(label: str):
            editor = QPlainTextEdit()
            editor.setPlaceholderText('{"setting": "value"}')
            settings = params.get("settings", {})
            editor.setPlainText(json.dumps(settings, ensure_ascii=False, indent=2))
            fields["settings"] = editor
            form.addRow(label, editor)

        if kind == "set_program_scene":
            line("scene", "Scène")
        elif kind == "scene_item_enabled":
            line("scene", "Scène")
            line("source", "Source")
            check("enabled", "Afficher la source", True)
        elif kind == "source_filter_enabled":
            line("source", "Source")
            line("filter", "Filtre")
            check("enabled", "Activer le filtre", True)
        elif kind == "source_filter_settings":
            line("source", "Source")
            line("filter", "Filtre")
            json_settings("Settings du filtre (JSON)")
            check("overlay", "Fusionner avec les réglages existants", True)
        elif kind == "input_mute":
            line("input", "Entrée OBS")
            check("muted", "Couper le son", True)
        elif kind == "input_volume_db":
            line("input", "Entrée OBS")
            widget = QLineEdit(str(params.get("volume_db", "0")))
            widget.setPlaceholderText("ex. -12.0")
            fields["volume_db"] = widget
            form.addRow("Volume (dB)", widget)
        elif kind == "set_input_settings":
            line("input", "Entrée OBS")
            json_settings("Settings JSON")
            check("overlay", "Fusionner avec les réglages existants", True)
        elif kind == "app_audio_output":
            line(
                "device",
                "Périphérique audio",
                "ex. Game ou Command-Line Friendly ID SoundVolumeView",
            )
            line("process", "Processus", "ex. Overwatch.exe")
            combo(
                "roles",
                "Rôles Windows",
                [
                    ("Tous (Console + Multimedia + Communications)", "all"),
                    ("Console", "0"),
                    ("Multimedia", "1"),
                    ("Communications", "2"),
                ],
                "all",
            )
        elif kind == "windows_hdr":
            check("enabled", "Activer HDR", True)
            combo(
                "display",
                "Écran",
                [
                    ("Écran principal", "primary"),
                    ("Tous les écrans compatibles", "all"),
                ],
                "primary",
            )
        return page, fields

    def _sync_page(self) -> None:
        kind = str(self.kind.currentData())
        page, _ = self.pages[kind]
        self.stack.setCurrentWidget(page)

    def _accept_checked(self) -> None:
        try:
            self.result_action()
        except Exception as exc:
            QMessageBox.warning(self, "Action profil", str(exc))
            return
        self.accept()

    def result_action(self) -> dict:
        kind = str(self.kind.currentData())
        _, fields = self.pages[kind]
        params: dict = {}
        for key, widget in fields.items():
            if isinstance(widget, QCheckBox):
                params[key] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                params[key] = str(widget.currentData() or "")
            elif isinstance(widget, QPlainTextEdit):
                text = widget.toPlainText().strip() or "{}"
                value = json.loads(text)
                if not isinstance(value, dict):
                    raise ValueError("Les settings avancés doivent être un objet JSON.")
                params[key] = value
            else:
                text = widget.text().strip()
                if key == "volume_db":
                    params[key] = float(text or 0)
                else:
                    params[key] = text
        required = {
            "set_program_scene": ("scene",),
            "scene_item_enabled": ("scene", "source"),
            "source_filter_enabled": ("source", "filter"),
            "source_filter_settings": ("source", "filter", "settings"),
            "input_mute": ("input",),
            "input_volume_db": ("input",),
            "set_input_settings": ("input", "settings"),
            "app_audio_output": ("device", "process", "roles"),
            "windows_hdr": ("display",),
        }[kind]
        for key in required:
            if params.get(key) in (None, "", {}):
                raise ValueError(f"Le paramètre « {key} » est requis.")
        return {
            "type": kind,
            "name": self.name.text().strip(),
            "enabled": self.enabled.isChecked(),
            "params": params,
        }
