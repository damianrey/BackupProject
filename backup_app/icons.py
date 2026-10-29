"""Ícones e tema visual da aplicação.

Os ícones vêm da família Phosphor (traço fino, via QtAwesome) e usam só duas
cores: um tom neutro para o estado normal e a cor de destaque para o estado
ativo/selecionado. Se o QtAwesome não estiver instalado, devolve um ícone vazio
para que a interface continue funcionando.
"""

import tempfile
from pathlib import Path

from PyQt5.QtGui import QColor, QIcon, QPalette
from PyQt5.QtWidgets import QApplication, QStyleFactory

try:
    import qtawesome as qta
except ImportError:  # pragma: no cover - dependência opcional em tempo de execução
    qta = None

# Paleta única: todas as telas e ícones derivam destas cores
ACCENT = "#4c9aff"
ICON = "#c3c9d2"
ICON_DISABLED = "#5b616b"
WINDOW = "#25272b"
SURFACE = "#1d1f22"
BORDER = "#3a3d43"
TEXT = "#e3e6ea"
MUTED = "#8b929c"
ON_ACCENT = "#0e1a2b"  # texto/ícone sobre fundo na cor de destaque

# Nome semântico -> ícone Phosphor. Use sempre um nome daqui, nunca o ícone direto,
# para manter a combinação consistente entre as telas.
_ICONS = {
    # Abas
    "connections": "ph.database",
    "schedule": "ph.calendar-check",
    "system": "ph.gear-six",
    # Campos
    "server": "ph.hard-drives",
    "user": "ph.user",
    "password": "ph.lock-simple",
    "port": "ph.hash",
    "database": "ph.database",
    "executable": "ph.terminal-window",
    "folder": "ph.folder-simple",
    "browse": "ph.folder-open",
    "show": "ph.eye",
    "hide": "ph.eye-slash",
    # Ações
    "login": "ph.sign-in",
    "register": "ph.user-plus",
    "test": "ph.lightning",
    "save": "ph.floppy-disk",
    "run": "ph.play",
    "add": "ph.plus",
    "edit": "ph.pencil-simple",
    "remove": "ph.trash",
    "logs": "ph.file-text",
    "open": "ph.app-window",
    "quit": "ph.sign-out",
    "clock": "ph.clock",
}


def icon(name: str, color: str = ICON, active: str = ACCENT) -> QIcon:
    """Ícone na cor neutra que passa para a cor de destaque quando ativo (hover)."""
    if qta is None:
        return QIcon()
    return qta.icon(_ICONS[name], color=color, color_active=active, color_selected=active,
                    color_disabled=ICON_DISABLED)


def tab_icon(name: str) -> QIcon:
    """Ícone da barra lateral: neutro quando inativo, cor de destaque quando selecionado."""
    if qta is None:
        return QIcon()
    return qta.icon(_ICONS[name], color_off=MUTED, color_on=ACCENT, color_off_active=ICON,
                    color_on_active=ACCENT, color_disabled=ICON_DISABLED)


def _indicator_assets() -> dict:
    """Grava as setas/marcas usadas pela folha de estilo (QSS só aceita arquivos de imagem)."""
    if qta is None:
        return {}
    folder = Path(tempfile.gettempdir()) / "DBKeeper-theme"
    folder.mkdir(exist_ok=True)
    assets = {}
    for key, glyph, color in (("up", "ph.caret-up-bold", ICON), ("down", "ph.caret-down-bold", ICON),
                              ("check", "ph.check-bold", ON_ACCENT)):
        path = folder / f"{key}.png"
        qta.icon(glyph, color=color).pixmap(32, 32).save(str(path))
        assets[key] = path.as_posix()
    return assets


_STYLESHEET = f"""
QWidget {{ font-size: 9pt; }}
QToolTip {{ color: {TEXT}; background: {SURFACE}; border: 1px solid {BORDER}; padding: 4px; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 6px; margin-top: 22px; padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {MUTED}; }}
QLineEdit, QSpinBox, QTimeEdit, QComboBox, QListWidget {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 5px; padding: 5px 6px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QTimeEdit:focus, QComboBox:focus, QListWidget:focus {{ border-color: {ACCENT}; }}
QListWidget::item {{ padding: 4px 2px; }}
QListWidget::item:selected {{ background: #2f4a6e; color: {TEXT}; }}
QPushButton {{
    background: #2e3136; border: 1px solid {BORDER}; border-radius: 5px; padding: 6px 12px;
}}
QPushButton:hover {{ background: #363a40; border-color: #4a4f57; }}
QPushButton:pressed {{ background: #2a2d31; }}
QPushButton:disabled {{ color: {ICON_DISABLED}; background: #2a2c30; }}
QPushButton[primary="true"] {{ background: {ACCENT}; border-color: {ACCENT}; color: {ON_ACCENT}; font-weight: 600; }}
QPushButton[primary="true"]:hover {{ background: #66a9ff; }}
QPushButton[primary="true"]:disabled {{ background: #2f4a6e; border-color: #2f4a6e; color: {MUTED}; }}
QPushButton[flat="true"] {{ background: transparent; border: none; color: {ACCENT}; padding: 4px; }}
QPushButton[flat="true"]:hover {{ text-decoration: underline; }}
QWidget#sidebar {{ background: {SURFACE}; border-right: 1px solid {BORDER}; }}
QWidget#sidebar QToolButton {{
    background: transparent; border: none; border-left: 2px solid transparent;
    border-right: 2px solid transparent; padding: 12px 14px;
}}
QWidget#sidebar QToolButton:hover {{ background: #2b2e33; }}
QWidget#sidebar QToolButton:checked {{ background: {WINDOW}; border-left-color: {ACCENT}; }}
QLabel[muted="true"] {{ color: {MUTED}; }}
QCheckBox::indicator, QListWidget::indicator {{
    width: 14px; height: 14px; border: 1px solid #555b64; border-radius: 3px; background: {SURFACE};
}}
QRadioButton::indicator {{
    width: 12px; height: 12px; border: 1px solid #555b64; border-radius: 7px; background: {SURFACE};
}}
QCheckBox::indicator:hover, QRadioButton::indicator:hover, QListWidget::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked, QListWidget::indicator:checked {{
    background: {ACCENT}; border-color: {ACCENT}; image: url("{{check}}");
}}
QRadioButton::indicator:checked {{ border: 4px solid {ACCENT}; width: 6px; height: 6px; background: {TEXT}; }}
QCheckBox::indicator:disabled {{ border-color: {BORDER}; background: {WINDOW}; }}
QSpinBox, QTimeEdit {{ padding-right: 20px; }}
QSpinBox::up-button, QTimeEdit::up-button, QSpinBox::down-button, QTimeEdit::down-button {{
    subcontrol-origin: border; width: 18px; border: none; background: transparent;
}}
QSpinBox::up-button, QTimeEdit::up-button {{ subcontrol-position: top right; }}
QSpinBox::down-button, QTimeEdit::down-button {{ subcontrol-position: bottom right; }}
QSpinBox::up-arrow, QTimeEdit::up-arrow {{ image: url("{{up}}"); width: 9px; height: 9px; }}
QSpinBox::down-arrow, QTimeEdit::down-arrow {{ image: url("{{down}}"); width: 9px; height: 9px; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: url("{{down}}"); width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; border: 1px solid {BORDER}; selection-background-color: #2f4a6e; }}
"""


def apply_theme(app: QApplication) -> None:
    """Tema escuro único (Fusion + paleta + folha de estilo), igual em qualquer versão do Python."""
    app.setStyle(QStyleFactory.create("Fusion"))
    palette = QPalette()
    for role, color in (
        (QPalette.Window, WINDOW), (QPalette.WindowText, TEXT), (QPalette.Base, SURFACE),
        (QPalette.AlternateBase, WINDOW), (QPalette.Text, TEXT), (QPalette.Button, "#2e3136"),
        (QPalette.ButtonText, TEXT), (QPalette.ToolTipBase, SURFACE), (QPalette.ToolTipText, TEXT),
        (QPalette.PlaceholderText, MUTED), (QPalette.Highlight, ACCENT), (QPalette.HighlightedText, ON_ACCENT),
        (QPalette.Link, ACCENT),
    ):
        palette.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor(ICON_DISABLED))
    app.setPalette(palette)
    stylesheet = _STYLESHEET
    for key, path in _indicator_assets().items():
        stylesheet = stylesheet.replace("{" + key + "}", path)
    app.setStyleSheet(stylesheet)

