from __future__ import annotations

import json
from copy import deepcopy
from typing import Mapping, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QSpinBox,
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

    def __init__(self, parent=None, module_name: str = "", module: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Module — {module_name}")
        self.resize(560, 620)
        self._source = deepcopy(module or {})
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
        for element in self._source.get("elements", []):
            if not isinstance(element, dict):
                continue
            item = QListWidgetItem(str(element.get("source") or element.get("element") or ""))
            item.setData(Qt.UserRole, str(element.get("source") or ""))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if bool(element.get("included", True)) else Qt.Unchecked)
            self.elements.addItem(item)
        root.addWidget(self.elements, 1)

        info = QLabel(
            "Les éléments décochés ne seront pas modifiés. Exceptions facultatives par nom : "
            "[Module:nomove] Élément, [Module:noresize] Élément, [Module:novis] Élément, [Module:fixed] Élément. "
            "[Module:locked] Élément est totalement exclu des LayoutProfiles, ainsi que son sous-arbre."
        )
        info.setWordWrap(True)
        info.setObjectName("Muted")
        root.addWidget(info)

        self._last_x = self.x.value()
        self._last_y = self.y.value()
        self._last_width = self.width.value()
        self._last_height = self.height.value()
        self.x.valueChanged.connect(self._remember_position)
        self.y.valueChanged.connect(self._remember_position)
        self.width.valueChanged.connect(lambda _v: self._resize_from("width"))
        self.height.valueChanged.connect(lambda _v: self._resize_from("height"))

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


class ActionDialog(QDialog):
    ACTION_TYPES = [
        ("Changer de scène programme", "set_program_scene"),
        ("Afficher/masquer une source de scène", "scene_item_enabled"),
        ("Activer/désactiver un filtre", "source_filter_enabled"),
        ("Mute/unmute une entrée", "input_mute"),
        ("Régler le volume d'une entrée (dB)", "input_volume_db"),
        ("Réglages avancés d'une entrée", "set_input_settings"),
    ]

    def __init__(self, parent=None, action: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Action OBS")
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
            editor = QPlainTextEdit()
            editor.setPlaceholderText('{"setting": "value"}')
            settings = params.get("settings", {})
            editor.setPlainText(json.dumps(settings, ensure_ascii=False, indent=2))
            fields["settings"] = editor
            form.addRow("Settings JSON", editor)
            check("overlay", "Fusionner avec les réglages existants", True)
        return page, fields

    def _sync_page(self) -> None:
        kind = str(self.kind.currentData())
        page, _ = self.pages[kind]
        self.stack.setCurrentWidget(page)

    def _accept_checked(self) -> None:
        try:
            self.result_action()
        except Exception as exc:
            QMessageBox.warning(self, "Action OBS", str(exc))
            return
        self.accept()

    def result_action(self) -> dict:
        kind = str(self.kind.currentData())
        _, fields = self.pages[kind]
        params: dict = {}
        for key, widget in fields.items():
            if isinstance(widget, QCheckBox):
                params[key] = widget.isChecked()
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
            "input_mute": ("input",),
            "input_volume_db": ("input",),
            "set_input_settings": ("input", "settings"),
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
