from __future__ import annotations

import copy
import json
from typing import Mapping

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QPlainTextEdit,
)

from .dialogs import ActionDialog


def _json_object(text: str) -> dict:
    value = json.loads(str(text or "").strip() or "{}")
    if not isinstance(value, dict):
        raise ValueError("Un objet JSON est requis.")
    return value


def _coerce_token(value: str):
    text = str(value).strip()
    lowered = text.casefold()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text


class _FilterDialog(QDialog):
    def __init__(self, parent=None, value: Mapping[str, object] | None = None):
        super().__init__(parent)
        raw = dict(value or {})
        self.setWindowTitle("Filtre de ShaderSet")
        root = QVBoxLayout(self)
        form = QFormLayout()
        root.addLayout(form)
        self.source = QLineEdit(str(raw.get("source") or ""))
        self.filter = QLineEdit(
            str(raw.get("filter") or raw.get("filter_name") or "")
        )
        self.enabled = QComboBox()
        self.enabled.addItem("Ne pas modifier", None)
        self.enabled.addItem("Activer", True)
        self.enabled.addItem("Désactiver", False)
        enabled = raw.get("enabled")
        index = self.enabled.findData(enabled if isinstance(enabled, bool) else None)
        self.enabled.setCurrentIndex(max(0, index))
        self.settings = QPlainTextEdit()
        self.settings.setPlainText(
            json.dumps(raw.get("settings", {}), ensure_ascii=False, indent=2)
        )
        self.overlay = QCheckBox("Fusionner avec les réglages existants")
        self.overlay.setChecked(bool(raw.get("overlay", True)))
        form.addRow("Source OBS", self.source)
        form.addRow("Filtre", self.filter)
        form.addRow("État", self.enabled)
        form.addRow("Settings", self.settings)
        form.addRow("", self.overlay)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Annuler")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        save = QPushButton("Enregistrer")
        save.clicked.connect(self._accept)
        row.addWidget(save)
        root.addLayout(row)

    def _accept(self) -> None:
        try:
            self.value()
        except Exception as exc:
            QMessageBox.warning(self, "ShaderSet", str(exc))
            return
        self.accept()

    def value(self) -> dict:
        source = self.source.text().strip()
        filter_name = self.filter.text().strip()
        if not source or not filter_name:
            raise ValueError("Source et filtre sont requis.")
        result = {
            "source": source,
            "filter": filter_name,
            "settings": _json_object(self.settings.toPlainText()),
            "overlay": self.overlay.isChecked(),
        }
        enabled = self.enabled.currentData()
        if isinstance(enabled, bool):
            result["enabled"] = enabled
        return result


class _SoundTriggerDialog(QDialog):
    def __init__(self, parent=None, value: Mapping[str, object] | None = None):
        super().__init__(parent)
        raw = dict(value or {})
        self.setWindowTitle("Son de présentation")
        root = QVBoxLayout(self)
        form = QFormLayout()
        root.addLayout(form)
        self.input = QLineEdit(
            str(raw.get("input") or raw.get("input_name") or "")
        )
        self.action = QComboBox()
        for label, value in (
            ("Redémarrer", "restart"),
            ("Lire", "play"),
            ("Pause", "pause"),
            ("Arrêter", "stop"),
            ("Suivant", "next"),
            ("Précédent", "previous"),
        ):
            self.action.addItem(label, value)
        index = self.action.findData(str(raw.get("action") or "restart"))
        self.action.setCurrentIndex(max(0, index))
        form.addRow("Source média OBS", self.input)
        form.addRow("Action", self.action)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Annuler")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        save = QPushButton("Enregistrer")
        save.clicked.connect(self._accept)
        row.addWidget(save)
        root.addLayout(row)

    def _accept(self) -> None:
        if not self.input.text().strip():
            QMessageBox.warning(
                self,
                "SoundSet",
                "La source média OBS est requise.",
            )
            return
        self.accept()

    def value(self) -> dict:
        return {
            "input": self.input.text().strip(),
            "action": str(self.action.currentData() or "restart"),
        }


class PresentationEditor(QWidget):
    changed = Signal()

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.config = config
        self._loading = False
        root = QVBoxLayout(self)
        intro = QLabel(
            "Les PresentationProfiles orchestrent l’identité audiovisuelle : "
            "thème de widgets, transition, shaders, sons et Cues. "
            "Toutes les modifications ci-dessous restent dans le brouillon."
        )
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        root.addWidget(intro)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._profiles_page(), "Profils")
        self.tabs.addTab(self._cues_page(), "Cues / Timeline")
        self.tabs.addTab(self._transitions_page(), "Transitions")
        self.tabs.addTab(self._shaders_page(), "Shaders")
        self.tabs.addTab(self._sounds_page(), "Sons")
        self.refresh()

    def _profiles_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(QLabel("PresentationProfile"))
        self.profile_name = QComboBox()
        self.profile_name.currentIndexChanged.connect(self._load_profile)
        row.addWidget(self.profile_name, 1)
        new = QPushButton("Nouveau")
        new.clicked.connect(self._new_profile)
        row.addWidget(new)
        duplicate = QPushButton("Dupliquer")
        duplicate.clicked.connect(self._duplicate_profile)
        row.addWidget(duplicate)
        delete = QPushButton("Supprimer")
        delete.clicked.connect(self._delete_profile)
        row.addWidget(delete)
        root.addLayout(row)

        form = QFormLayout()
        root.addLayout(form)
        self.profile_parent = QComboBox()
        self.profile_enter = QComboBox()
        self.profile_exit = QComboBox()
        self.profile_transition = QComboBox()
        self.profile_shader = QComboBox()
        self.profile_sound = QComboBox()
        self.profile_widget_theme = QLineEdit()
        self.profile_intensity = QComboBox()
        for value in ("off", "low", "normal", "high"):
            self.profile_intensity.addItem(value, value)
        form.addRow("Hérite de", self.profile_parent)
        form.addRow("Cue d’entrée", self.profile_enter)
        form.addRow("Cue de sortie", self.profile_exit)
        form.addRow("Transition", self.profile_transition)
        form.addRow("ShaderSet", self.profile_shader)
        form.addRow("SoundSet", self.profile_sound)
        form.addRow("Thème widgets", self.profile_widget_theme)
        form.addRow("Intensité animation", self.profile_intensity)

        for widget in (
            self.profile_parent,
            self.profile_enter,
            self.profile_exit,
            self.profile_transition,
            self.profile_shader,
            self.profile_sound,
            self.profile_intensity,
        ):
            widget.currentIndexChanged.connect(self._profile_fields_changed)
        self.profile_widget_theme.textEdited.connect(
            self._profile_fields_changed
        )

        root.addWidget(QLabel("Tokens de thème locaux"))
        self.theme_table = QTableWidget(0, 2)
        self.theme_table.setHorizontalHeaderLabels(["Token", "Valeur"])
        self.theme_table.horizontalHeader().setStretchLastSection(True)
        self.theme_table.itemChanged.connect(self._theme_changed)
        root.addWidget(self.theme_table)
        token_row = QHBoxLayout()
        add_token = QPushButton("Ajouter un token")
        add_token.clicked.connect(self._add_theme_token)
        token_row.addWidget(add_token)
        remove_token = QPushButton("Supprimer")
        remove_token.clicked.connect(self._remove_theme_token)
        token_row.addWidget(remove_token)
        token_row.addStretch(1)
        root.addLayout(token_row)

        root.addWidget(QLabel("Composants widgets locaux"))
        self.component_table = QTableWidget(0, 4)
        self.component_table.setHorizontalHeaderLabels(
            ["Composant", "Mode", "Ressource", "Settings JSON"]
        )
        self.component_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self.component_table.horizontalHeader().setStretchLastSection(True)
        self.component_table.itemChanged.connect(self._components_changed)
        root.addWidget(self.component_table)
        comp_row = QHBoxLayout()
        add_comp = QPushButton("Ajouter un composant")
        add_comp.clicked.connect(self._add_component)
        comp_row.addWidget(add_comp)
        remove_comp = QPushButton("Supprimer")
        remove_comp.clicked.connect(self._remove_component)
        comp_row.addWidget(remove_comp)
        comp_row.addStretch(1)
        root.addLayout(comp_row)
        return page

    def _cues_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(QLabel("Cue"))
        self.cue_name = QComboBox()
        self.cue_name.currentIndexChanged.connect(self._load_cue)
        row.addWidget(self.cue_name, 1)
        new = QPushButton("Nouveau")
        new.clicked.connect(self._new_cue)
        row.addWidget(new)
        duplicate = QPushButton("Dupliquer")
        duplicate.clicked.connect(self._duplicate_cue)
        row.addWidget(duplicate)
        delete = QPushButton("Supprimer")
        delete.clicked.connect(self._delete_cue)
        row.addWidget(delete)
        root.addLayout(row)

        hint = QLabel(
            "Une frame regroupe des actions au même instant. "
            "Les actions d’une frame restent exécutées dans l’ordre."
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.frames_table = QTableWidget(0, 3)
        self.frames_table.setHorizontalHeaderLabels(
            ["Temps (ms)", "Actions", "Résumé"]
        )
        self.frames_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.frames_table.horizontalHeader().setStretchLastSection(True)
        self.frames_table.itemSelectionChanged.connect(
            self._refresh_cue_actions
        )
        root.addWidget(self.frames_table)

        frame_row = QHBoxLayout()
        add_frame = QPushButton("Ajouter une frame")
        add_frame.clicked.connect(self._add_frame)
        frame_row.addWidget(add_frame)
        edit_time = QPushButton("Modifier le temps")
        edit_time.clicked.connect(self._edit_frame_time)
        frame_row.addWidget(edit_time)
        delete_frame = QPushButton("Supprimer la frame")
        delete_frame.clicked.connect(self._delete_frame)
        frame_row.addWidget(delete_frame)
        frame_row.addStretch(1)
        root.addLayout(frame_row)

        root.addWidget(QLabel("Actions de la frame sélectionnée"))
        self.cue_actions = QTableWidget(0, 4)
        self.cue_actions.setHorizontalHeaderLabels(
            ["Actif", "Type", "Nom", "Paramètres"]
        )
        self.cue_actions.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.cue_actions.horizontalHeader().setStretchLastSection(True)
        self.cue_actions.doubleClicked.connect(self._edit_cue_action)
        root.addWidget(self.cue_actions)

        action_row = QHBoxLayout()
        add_action = QPushButton("Ajouter une action")
        add_action.clicked.connect(self._add_cue_action)
        action_row.addWidget(add_action)
        edit_action = QPushButton("Modifier")
        edit_action.clicked.connect(self._edit_cue_action)
        action_row.addWidget(edit_action)
        delete_action = QPushButton("Supprimer")
        delete_action.clicked.connect(self._delete_cue_action)
        action_row.addWidget(delete_action)
        action_row.addStretch(1)
        root.addLayout(action_row)
        return page

    def _transitions_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(QLabel("TransitionProfile"))
        self.transition_name = QComboBox()
        self.transition_name.currentIndexChanged.connect(
            self._load_transition
        )
        row.addWidget(self.transition_name, 1)
        new = QPushButton("Nouveau")
        new.clicked.connect(self._new_transition)
        row.addWidget(new)
        delete = QPushButton("Supprimer")
        delete.clicked.connect(self._delete_transition)
        row.addWidget(delete)
        root.addLayout(row)

        form = QFormLayout()
        root.addLayout(form)
        self.transition_obs_name = QLineEdit()
        self.transition_duration = QSpinBox()
        self.transition_duration.setRange(0, 20000)
        self.transition_duration.setSpecialValueText("OBS / défaut")
        self.transition_overlay = QCheckBox(
            "Fusionner les settings de transition"
        )
        self.transition_settings = QPlainTextEdit()
        self.transition_settings.setMaximumHeight(150)
        form.addRow("Transition OBS", self.transition_obs_name)
        form.addRow("Durée", self.transition_duration)
        form.addRow("", self.transition_overlay)
        form.addRow("Settings JSON", self.transition_settings)
        save = QPushButton("Mettre à jour la transition")
        save.clicked.connect(self._save_transition)
        root.addWidget(save, alignment=Qt.AlignLeft)
        root.addStretch(1)
        return page

    def _shaders_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(QLabel("ShaderSet"))
        self.shader_name = QComboBox()
        self.shader_name.currentIndexChanged.connect(self._load_shader)
        row.addWidget(self.shader_name, 1)
        new = QPushButton("Nouveau")
        new.clicked.connect(self._new_shader)
        row.addWidget(new)
        delete = QPushButton("Supprimer")
        delete.clicked.connect(self._delete_shader)
        row.addWidget(delete)
        root.addLayout(row)

        self.shader_filters = QTableWidget(0, 4)
        self.shader_filters.setHorizontalHeaderLabels(
            ["Source", "Filtre", "État", "Settings"]
        )
        self.shader_filters.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.shader_filters.horizontalHeader().setStretchLastSection(True)
        self.shader_filters.doubleClicked.connect(self._edit_shader_filter)
        root.addWidget(self.shader_filters)
        row = QHBoxLayout()
        add = QPushButton("Ajouter un filtre")
        add.clicked.connect(self._add_shader_filter)
        row.addWidget(add)
        edit = QPushButton("Modifier")
        edit.clicked.connect(self._edit_shader_filter)
        row.addWidget(edit)
        delete_filter = QPushButton("Supprimer")
        delete_filter.clicked.connect(self._delete_shader_filter)
        row.addWidget(delete_filter)
        row.addStretch(1)
        root.addLayout(row)
        return page

    def _sounds_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(QLabel("SoundSet"))
        self.sound_name = QComboBox()
        self.sound_name.currentIndexChanged.connect(self._load_sound)
        row.addWidget(self.sound_name, 1)
        new = QPushButton("Nouveau")
        new.clicked.connect(self._new_sound)
        row.addWidget(new)
        delete = QPushButton("Supprimer")
        delete.clicked.connect(self._delete_sound)
        row.addWidget(delete)
        root.addLayout(row)

        self.sound_triggers = QTableWidget(0, 3)
        self.sound_triggers.setHorizontalHeaderLabels(
            ["Phase", "Source média OBS", "Action"]
        )
        self.sound_triggers.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.sound_triggers.horizontalHeader().setStretchLastSection(True)
        self.sound_triggers.doubleClicked.connect(self._edit_sound_trigger)
        root.addWidget(self.sound_triggers)
        row = QHBoxLayout()
        add_enter = QPushButton("Ajouter son d’entrée")
        add_enter.clicked.connect(lambda: self._add_sound_trigger("enter"))
        row.addWidget(add_enter)
        add_exit = QPushButton("Ajouter son de sortie")
        add_exit.clicked.connect(lambda: self._add_sound_trigger("exit"))
        row.addWidget(add_exit)
        edit = QPushButton("Modifier")
        edit.clicked.connect(self._edit_sound_trigger)
        row.addWidget(edit)
        delete_trigger = QPushButton("Supprimer")
        delete_trigger.clicked.connect(self._delete_sound_trigger)
        row.addWidget(delete_trigger)
        row.addStretch(1)
        root.addLayout(row)
        return page

    def refresh(self) -> None:
        self._loading = True
        try:
            current_profile = self.profile_name.currentText()
            current_cue = self.cue_name.currentText()
            current_transition = self.transition_name.currentText()
            current_shader = self.shader_name.currentText()
            current_sound = self.sound_name.currentText()
            self._fill_combo(
                self.profile_name,
                self.config.get("presentation_profiles", {}),
                current_profile,
            )
            self._fill_combo(
                self.cue_name,
                self.config.get("cues", {}),
                current_cue,
            )
            self._fill_combo(
                self.transition_name,
                self.config.get("transition_profiles", {}),
                current_transition,
            )
            self._fill_combo(
                self.shader_name,
                self.config.get("shader_sets", {}),
                current_shader,
            )
            self._fill_combo(
                self.sound_name,
                self.config.get("sound_sets", {}),
                current_sound,
            )
            self._refresh_profile_resource_combos()
        finally:
            self._loading = False
        self._load_profile()
        self._load_cue()
        self._load_transition()
        self._load_shader()
        self._load_sound()

    @staticmethod
    def _fill_combo(combo: QComboBox, mapping, current: str) -> None:
        names = sorted(
            [str(name) for name in mapping] if isinstance(mapping, Mapping) else [],
            key=str.casefold,
        )
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(names)
        if current and combo.findText(current) >= 0:
            combo.setCurrentText(current)
        combo.blockSignals(False)

    @staticmethod
    def _set_reference_combo(
        combo: QComboBox,
        names: list[str],
        current: str,
        *,
        none_label: str = "— Aucun —",
    ) -> None:
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(none_label, "")
        combo.addItems(names)
        index = combo.findData(current)
        if index < 0:
            index = combo.findText(current)
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)

    def _emit_changed(self) -> None:
        if not self._loading:
            self.changed.emit()

    def _mapping(self, key: str) -> dict:
        value = self.config.setdefault(key, {})
        if not isinstance(value, dict):
            value = {}
            self.config[key] = value
        return value

    def _unique_name(self, title: str, mapping: Mapping) -> str:
        name, ok = QInputDialog.getText(self, title, "Nom")
        name = name.strip()
        if not ok or not name:
            return ""
        if name in mapping:
            QMessageBox.warning(self, title, "Ce nom existe déjà.")
            return ""
        return name

    def _refresh_profile_resource_combos(self) -> None:
        profiles = sorted(self._mapping("presentation_profiles"), key=str.casefold)
        current = self.profile_name.currentText()
        parents = [name for name in profiles if name != current]
        self._set_reference_combo(
            self.profile_parent,
            parents,
            str(self._profile().get("extends") or "") if self._profile() else "",
        )
        resources = (
            (self.profile_enter, "cues", "enter_cue"),
            (self.profile_exit, "cues", "exit_cue"),
            (self.profile_transition, "transition_profiles", "transition_profile"),
            (self.profile_shader, "shader_sets", "shader_set"),
            (self.profile_sound, "sound_sets", "sound_set"),
        )
        profile = self._profile() or {}
        for combo, key, field in resources:
            names = sorted(self._mapping(key), key=str.casefold)
            self._set_reference_combo(
                combo,
                names,
                str(profile.get(field) or ""),
            )

    def _profile(self) -> dict | None:
        value = self._mapping("presentation_profiles").get(
            self.profile_name.currentText()
        )
        return value if isinstance(value, dict) else None

    def _load_profile(self, *_args) -> None:
        self._loading = True
        try:
            self._refresh_profile_resource_combos()
            profile = self._profile()
            if profile is None:
                self.profile_widget_theme.setText("")
                self.theme_table.setRowCount(0)
                self.component_table.setRowCount(0)
                return
            self.profile_widget_theme.setText(
                str(profile.get("widget_theme") or "")
            )
            intensity = str(
                profile.get("animation_intensity") or "normal"
            )
            index = self.profile_intensity.findData(intensity)
            self.profile_intensity.setCurrentIndex(max(0, index))
            theme = profile.get("theme", {})
            rows = list(theme.items()) if isinstance(theme, Mapping) else []
            self.theme_table.setRowCount(len(rows))
            for row, (key, value) in enumerate(rows):
                self.theme_table.setItem(row, 0, QTableWidgetItem(str(key)))
                self.theme_table.setItem(row, 1, QTableWidgetItem(str(value)))
            components = profile.get("components", {})
            items = (
                list(components.items())
                if isinstance(components, Mapping)
                else []
            )
            self.component_table.setRowCount(len(items))
            for row, (name, raw) in enumerate(items):
                raw = raw if isinstance(raw, Mapping) else {}
                values = [
                    name,
                    str(raw.get("mode") or "inherit"),
                    str(raw.get("resource") or ""),
                    json.dumps(
                        raw.get("settings", {}),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ]
                for col, value in enumerate(values):
                    self.component_table.setItem(
                        row,
                        col,
                        QTableWidgetItem(str(value)),
                    )
        finally:
            self._loading = False

    def _profile_fields_changed(self, *_args) -> None:
        if self._loading:
            return
        profile = self._profile()
        if profile is None:
            return
        profile["extends"] = str(self.profile_parent.currentData() or "")
        profile["enter_cue"] = str(self.profile_enter.currentData() or "")
        profile["exit_cue"] = str(self.profile_exit.currentData() or "")
        profile["transition_profile"] = str(
            self.profile_transition.currentData() or ""
        )
        profile["shader_set"] = str(self.profile_shader.currentData() or "")
        profile["sound_set"] = str(self.profile_sound.currentData() or "")
        profile["widget_theme"] = self.profile_widget_theme.text().strip()
        profile["animation_intensity"] = str(
            self.profile_intensity.currentData() or "normal"
        )
        self._emit_changed()

    def _theme_changed(self, *_args) -> None:
        if self._loading:
            return
        profile = self._profile()
        if profile is None:
            return
        theme = {}
        for row in range(self.theme_table.rowCount()):
            key_item = self.theme_table.item(row, 0)
            value_item = self.theme_table.item(row, 1)
            key = key_item.text().strip() if key_item else ""
            if not key:
                continue
            theme[key] = _coerce_token(
                value_item.text() if value_item else ""
            )
        profile["theme"] = theme
        self._emit_changed()

    def _components_changed(self, *_args) -> None:
        if self._loading:
            return
        profile = self._profile()
        if profile is None:
            return
        components = {}
        try:
            for row in range(self.component_table.rowCount()):
                name = (
                    self.component_table.item(row, 0).text().strip()
                    if self.component_table.item(row, 0)
                    else ""
                )
                if not name:
                    continue
                mode = (
                    self.component_table.item(row, 1).text().strip().casefold()
                    if self.component_table.item(row, 1)
                    else "inherit"
                )
                if mode not in {"inherit", "custom", "hidden"}:
                    raise ValueError(
                        f"Mode invalide pour {name}: {mode}"
                    )
                resource = (
                    self.component_table.item(row, 2).text().strip()
                    if self.component_table.item(row, 2)
                    else ""
                )
                settings = _json_object(
                    self.component_table.item(row, 3).text()
                    if self.component_table.item(row, 3)
                    else "{}"
                )
                components[name] = {
                    "mode": mode,
                    "resource": resource,
                    "settings": settings,
                }
        except Exception:
            return
        profile["components"] = components
        self._emit_changed()

    def _add_theme_token(self) -> None:
        row = self.theme_table.rowCount()
        self.theme_table.insertRow(row)
        self.theme_table.setItem(row, 0, QTableWidgetItem("token"))
        self.theme_table.setItem(row, 1, QTableWidgetItem("value"))

    def _remove_theme_token(self) -> None:
        row = self.theme_table.currentRow()
        if row >= 0:
            self.theme_table.removeRow(row)
            self._theme_changed()

    def _add_component(self) -> None:
        row = self.component_table.rowCount()
        self.component_table.insertRow(row)
        for col, value in enumerate(("component", "custom", "", "{}")):
            self.component_table.setItem(row, col, QTableWidgetItem(value))

    def _remove_component(self) -> None:
        row = self.component_table.currentRow()
        if row >= 0:
            self.component_table.removeRow(row)
            self._components_changed()

    def _new_profile(self) -> None:
        mapping = self._mapping("presentation_profiles")
        name = self._unique_name("Nouveau PresentationProfile", mapping)
        if not name:
            return
        mapping[name] = {
            "extends": "",
            "enter_cue": "",
            "exit_cue": "",
            "transition_profile": "",
            "shader_set": "",
            "sound_set": "",
            "widget_theme": "",
            "animation_intensity": "normal",
            "theme": {},
            "components": {},
        }
        self._emit_changed()
        self.refresh()
        self.profile_name.setCurrentText(name)

    def _duplicate_profile(self) -> None:
        profile = self._profile()
        if profile is None:
            return
        mapping = self._mapping("presentation_profiles")
        name = self._unique_name("Dupliquer PresentationProfile", mapping)
        if not name:
            return
        mapping[name] = copy.deepcopy(profile)
        self._emit_changed()
        self.refresh()
        self.profile_name.setCurrentText(name)

    def _delete_profile(self) -> None:
        name = self.profile_name.currentText()
        if not name:
            return
        if name == "Vanilla":
            QMessageBox.warning(
                self,
                "PresentationProfile",
                "Le profil Vanilla ne peut pas être supprimé.",
            )
            return
        if QMessageBox.question(
            self,
            "PresentationProfile",
            f"Supprimer « {name} » du brouillon ?",
        ) != QMessageBox.Yes:
            return
        self._mapping("presentation_profiles").pop(name, None)
        self._emit_changed()
        self.refresh()

    def _cue(self) -> dict | None:
        value = self._mapping("cues").get(self.cue_name.currentText())
        return value if isinstance(value, dict) else None

    def _load_cue(self, *_args) -> None:
        cue = self._cue()
        frames = cue.get("frames", []) if cue else []
        if not isinstance(frames, list):
            frames = []
        self.frames_table.setRowCount(len(frames))
        for row, frame in enumerate(frames):
            frame = frame if isinstance(frame, Mapping) else {}
            actions = frame.get("actions", [])
            actions = actions if isinstance(actions, list) else []
            summary = ", ".join(
                str(action.get("type") or "")
                for action in actions
                if isinstance(action, Mapping)
            )
            values = [
                str(frame.get("at_ms", 0)),
                str(len(actions)),
                summary,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.frames_table.setItem(row, col, item)
        if frames:
            self.frames_table.selectRow(0)
        else:
            self.cue_actions.setRowCount(0)

    def _selected_frame(self):
        cue = self._cue()
        row = self.frames_table.currentRow()
        if cue is None or row < 0:
            return None
        frames = cue.get("frames", [])
        if not isinstance(frames, list) or row >= len(frames):
            return None
        frame = frames[row]
        return frame if isinstance(frame, dict) else None

    def _refresh_cue_actions(self) -> None:
        frame = self._selected_frame()
        actions = frame.get("actions", []) if frame else []
        if not isinstance(actions, list):
            actions = []
        self.cue_actions.setRowCount(len(actions))
        for row, action in enumerate(actions):
            action = action if isinstance(action, Mapping) else {}
            values = [
                "✓" if bool(action.get("enabled", True)) else "",
                str(action.get("type") or ""),
                str(action.get("name") or ""),
                json.dumps(
                    action.get("params", {}),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.cue_actions.setItem(row, col, item)

    def _new_cue(self) -> None:
        mapping = self._mapping("cues")
        name = self._unique_name("Nouveau Cue", mapping)
        if not name:
            return
        mapping[name] = {"interrupt_policy": "replace", "frames": []}
        self._emit_changed()
        self.refresh()
        self.cue_name.setCurrentText(name)

    def _duplicate_cue(self) -> None:
        cue = self._cue()
        if cue is None:
            return
        mapping = self._mapping("cues")
        name = self._unique_name("Dupliquer Cue", mapping)
        if not name:
            return
        mapping[name] = copy.deepcopy(cue)
        self._emit_changed()
        self.refresh()
        self.cue_name.setCurrentText(name)

    def _delete_cue(self) -> None:
        name = self.cue_name.currentText()
        if not name:
            return
        self._mapping("cues").pop(name, None)
        self._emit_changed()
        self.refresh()

    def _add_frame(self) -> None:
        cue = self._cue()
        if cue is None:
            return
        value, ok = QInputDialog.getInt(
            self,
            "Ajouter une frame",
            "Temps (ms)",
            0,
            0,
            30000,
            10,
        )
        if not ok:
            return
        frames = cue.setdefault("frames", [])
        frames.append({"at_ms": value, "actions": []})
        frames.sort(key=lambda item: int(item.get("at_ms", 0)))
        self._emit_changed()
        self._load_cue()

    def _edit_frame_time(self) -> None:
        frame = self._selected_frame()
        if frame is None:
            return
        value, ok = QInputDialog.getInt(
            self,
            "Modifier la frame",
            "Temps (ms)",
            int(frame.get("at_ms", 0)),
            0,
            30000,
            10,
        )
        if not ok:
            return
        frame["at_ms"] = value
        cue = self._cue()
        cue["frames"].sort(key=lambda item: int(item.get("at_ms", 0)))
        self._emit_changed()
        self._load_cue()

    def _delete_frame(self) -> None:
        cue = self._cue()
        row = self.frames_table.currentRow()
        if cue is None or row < 0:
            return
        cue["frames"].pop(row)
        self._emit_changed()
        self._load_cue()

    def _add_cue_action(self) -> None:
        frame = self._selected_frame()
        if frame is None:
            QMessageBox.information(
                self,
                "Cue",
                "Sélectionnez ou créez d’abord une frame.",
            )
            return
        dialog = ActionDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        action = dialog.result_action()
        action.pop("preapply_on_launcher", None)
        frame.setdefault("actions", []).append(action)
        self._emit_changed()
        self._load_cue()

    def _edit_cue_action(self, *_args) -> None:
        frame = self._selected_frame()
        row = self.cue_actions.currentRow()
        if frame is None or row < 0:
            return
        actions = frame.get("actions", [])
        if not isinstance(actions, list) or row >= len(actions):
            return
        dialog = ActionDialog(self, action=actions[row])
        if dialog.exec() != QDialog.Accepted:
            return
        action = dialog.result_action()
        action.pop("preapply_on_launcher", None)
        actions[row] = action
        self._emit_changed()
        self._load_cue()

    def _delete_cue_action(self) -> None:
        frame = self._selected_frame()
        row = self.cue_actions.currentRow()
        if frame is None or row < 0:
            return
        frame["actions"].pop(row)
        self._emit_changed()
        self._load_cue()

    def _transition(self) -> dict | None:
        value = self._mapping("transition_profiles").get(
            self.transition_name.currentText()
        )
        return value if isinstance(value, dict) else None

    def _load_transition(self, *_args) -> None:
        raw = self._transition() or {}
        self.transition_obs_name.setText(
            str(raw.get("transition_name") or raw.get("transition") or "")
        )
        self.transition_duration.setValue(
            int(raw.get("duration_ms") or 0)
        )
        self.transition_overlay.setChecked(bool(raw.get("overlay", True)))
        self.transition_settings.setPlainText(
            json.dumps(raw.get("settings", {}), ensure_ascii=False, indent=2)
        )

    def _new_transition(self) -> None:
        mapping = self._mapping("transition_profiles")
        name = self._unique_name("Nouvelle transition", mapping)
        if not name:
            return
        mapping[name] = {
            "transition_name": "",
            "duration_ms": None,
            "settings": {},
            "overlay": True,
        }
        self._emit_changed()
        self.refresh()
        self.transition_name.setCurrentText(name)

    def _save_transition(self) -> None:
        raw = self._transition()
        if raw is None:
            return
        target = self.transition_obs_name.text().strip()
        if not target:
            QMessageBox.warning(
                self,
                "Transition",
                "Le nom de transition OBS est requis.",
            )
            return
        try:
            settings = _json_object(self.transition_settings.toPlainText())
        except Exception as exc:
            QMessageBox.warning(self, "Transition", str(exc))
            return
        raw["transition_name"] = target
        raw["duration_ms"] = (
            self.transition_duration.value()
            if self.transition_duration.value() > 0
            else None
        )
        raw["settings"] = settings
        raw["overlay"] = self.transition_overlay.isChecked()
        self._emit_changed()
        self._refresh_profile_resource_combos()

    def _delete_transition(self) -> None:
        name = self.transition_name.currentText()
        if not name:
            return
        self._mapping("transition_profiles").pop(name, None)
        self._emit_changed()
        self.refresh()

    def _shader(self) -> dict | None:
        value = self._mapping("shader_sets").get(
            self.shader_name.currentText()
        )
        return value if isinstance(value, dict) else None

    def _load_shader(self, *_args) -> None:
        raw = self._shader() or {}
        filters = raw.get("filters", [])
        filters = filters if isinstance(filters, list) else []
        self.shader_filters.setRowCount(len(filters))
        for row, item in enumerate(filters):
            item = item if isinstance(item, Mapping) else {}
            enabled = item.get("enabled")
            state = (
                "Actif"
                if enabled is True
                else "Inactif"
                if enabled is False
                else "Inchangé"
            )
            values = [
                str(item.get("source") or ""),
                str(item.get("filter") or item.get("filter_name") or ""),
                state,
                json.dumps(
                    item.get("settings", {}),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                self.shader_filters.setItem(row, col, cell)

    def _new_shader(self) -> None:
        mapping = self._mapping("shader_sets")
        name = self._unique_name("Nouveau ShaderSet", mapping)
        if not name:
            return
        mapping[name] = {"filters": []}
        self._emit_changed()
        self.refresh()
        self.shader_name.setCurrentText(name)

    def _delete_shader(self) -> None:
        name = self.shader_name.currentText()
        if not name:
            return
        self._mapping("shader_sets").pop(name, None)
        self._emit_changed()
        self.refresh()

    def _add_shader_filter(self) -> None:
        raw = self._shader()
        if raw is None:
            return
        dialog = _FilterDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        raw.setdefault("filters", []).append(dialog.value())
        self._emit_changed()
        self._load_shader()

    def _edit_shader_filter(self, *_args) -> None:
        raw = self._shader()
        row = self.shader_filters.currentRow()
        if raw is None or row < 0:
            return
        filters = raw.get("filters", [])
        if not isinstance(filters, list) or row >= len(filters):
            return
        dialog = _FilterDialog(self, filters[row])
        if dialog.exec() != QDialog.Accepted:
            return
        filters[row] = dialog.value()
        self._emit_changed()
        self._load_shader()

    def _delete_shader_filter(self) -> None:
        raw = self._shader()
        row = self.shader_filters.currentRow()
        if raw is None or row < 0:
            return
        raw["filters"].pop(row)
        self._emit_changed()
        self._load_shader()

    def _sound(self) -> dict | None:
        value = self._mapping("sound_sets").get(
            self.sound_name.currentText()
        )
        return value if isinstance(value, dict) else None

    def _load_sound(self, *_args) -> None:
        raw = self._sound() or {}
        rows = []
        for phase in ("enter", "exit"):
            values = raw.get(phase, [])
            if not isinstance(values, list):
                continue
            for index, trigger in enumerate(values):
                if not isinstance(trigger, Mapping):
                    continue
                rows.append((phase, index, trigger))
        self.sound_triggers.setRowCount(len(rows))
        for row, (phase, index, trigger) in enumerate(rows):
            values = [
                "Entrée" if phase == "enter" else "Sortie",
                str(trigger.get("input") or trigger.get("input_name") or ""),
                str(trigger.get("action") or "restart"),
            ]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setFlags(cell.flags() & ~Qt.ItemIsEditable)
                cell.setData(
                    Qt.UserRole,
                    (phase, index),
                )
                self.sound_triggers.setItem(row, col, cell)

    def _new_sound(self) -> None:
        mapping = self._mapping("sound_sets")
        name = self._unique_name("Nouveau SoundSet", mapping)
        if not name:
            return
        mapping[name] = {"enter": [], "exit": []}
        self._emit_changed()
        self.refresh()
        self.sound_name.setCurrentText(name)

    def _delete_sound(self) -> None:
        name = self.sound_name.currentText()
        if not name:
            return
        self._mapping("sound_sets").pop(name, None)
        self._emit_changed()
        self.refresh()

    def _add_sound_trigger(self, phase: str) -> None:
        raw = self._sound()
        if raw is None:
            return
        dialog = _SoundTriggerDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        raw.setdefault(phase, []).append(dialog.value())
        self._emit_changed()
        self._load_sound()

    def _selected_sound_trigger(self):
        raw = self._sound()
        row = self.sound_triggers.currentRow()
        if raw is None or row < 0:
            return None
        item = self.sound_triggers.item(row, 0)
        if item is None:
            return None
        data = item.data(Qt.UserRole)
        if not isinstance(data, tuple) or len(data) != 2:
            return None
        phase, index = data
        values = raw.get(phase, [])
        if not isinstance(values, list) or not 0 <= index < len(values):
            return None
        return raw, phase, index, values[index]

    def _edit_sound_trigger(self, *_args) -> None:
        selected = self._selected_sound_trigger()
        if selected is None:
            return
        raw, phase, index, trigger = selected
        dialog = _SoundTriggerDialog(self, trigger)
        if dialog.exec() != QDialog.Accepted:
            return
        raw[phase][index] = dialog.value()
        self._emit_changed()
        self._load_sound()

    def _delete_sound_trigger(self) -> None:
        selected = self._selected_sound_trigger()
        if selected is None:
            return
        raw, phase, index, _trigger = selected
        raw[phase].pop(index)
        self._emit_changed()
        self._load_sound()
