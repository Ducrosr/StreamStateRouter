from __future__ import annotations

APP_STYLE = r"""
QWidget {
    background: #0f1720;
    color: #dce7ef;
    font-family: "Segoe UI";
    font-size: 10pt;
}
QMainWindow, QDialog { background: #0f1720; }
QFrame#Card {
    background: #151f2a;
    border: 1px solid #263848;
    border-radius: 8px;
}
QLabel#Title { font-size: 19pt; font-weight: 700; color: #f3f7fa; }
QLabel#Section { font-size: 12pt; font-weight: 700; color: #6ad7e8; }
QLabel#Muted { color: #8da2b2; }
QLabel#Good { color: #70d69a; font-weight: 700; }
QLabel#Warn { color: #e7c781; font-weight: 700; }
QLabel#Bad { color: #e58b91; font-weight: 700; }
QPushButton {
    background: #1a2733;
    border: 1px solid #314656;
    border-radius: 6px;
    padding: 7px 12px;
}
QPushButton:hover { background: #223443; }
QPushButton#Primary {
    background: #c4a66a;
    border-color: #d9bd80;
    color: #11161b;
    font-weight: 700;
}
QPushButton#Primary:hover { background: #d6b978; }
QPushButton#Danger {
    color: #f3c2c6;
    border-color: #7e4149;
}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {
    background: #121c25;
    border: 1px solid #314656;
    border-radius: 5px;
    padding: 6px;
    selection-background-color: #2b7180;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextEdit:focus { border-color: #5ec8da; }
QTabWidget::pane { border: 1px solid #263848; border-radius: 6px; }
QTabBar::tab {
    background: #121c25;
    color: #9eb0bd;
    padding: 9px 14px;
    margin-right: 2px;
}
QTabBar::tab:selected { background: #1d3b49; color: #f3f7fa; }
QHeaderView::section {
    background: #192630;
    color: #a9bbc7;
    padding: 7px;
    border: none;
    border-right: 1px solid #263848;
    border-bottom: 1px solid #263848;
}
QTableWidget {
    background: #111a22;
    alternate-background-color: #141f29;
    gridline-color: #243542;
    border: 1px solid #263848;
    border-radius: 5px;
}
QTableWidget::item:selected { background: #235d6b; color: white; }
QListWidget {
    background: #111a22;
    border: 1px solid #263848;
    border-radius: 5px;
}
QListWidget::item { padding: 7px; }
QListWidget::item:selected { background: #235d6b; }
QCheckBox { spacing: 7px; }
QToolTip { background: #1b2732; color: #fff; border: 1px solid #3b5365; }
"""
