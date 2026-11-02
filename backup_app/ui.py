"""Telas da aplicação: login, registro de usuário e janela principal."""

import os
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTime, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QPainter
from PyQt5.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QRadioButton, QSizePolicy, QSpinBox, QStackedWidget,
    QTimeEdit, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

try:
    from PyQt5.QtWinExtras import QWinTaskbarButton
except ImportError:  # pragma: no cover - módulo exclusivo do Windows
    QWinTaskbarButton = None

from . import __version__, history, startup, users
from .backup import connect_mysql, connect_sqlserver, find_mysqldump, retention_preview
from .icons import ACCENT, DANGER, MUTED, ON_ACCENT, SUCCESS, WARNING, icon, tab_icon
from .log_config import error_log, main_log
from .paths import BASE_DIR, LOGS_DIR, image
from .restore import (
    ENGINE_MYSQL, ENGINE_SQLSERVER, BackupFile, RestoreRequest, available_backups, original_database,
    restore_backup, suggested_target, target_problem,
)
from .scheduler import BackupScheduler
from .security import CryptoKeyError, SecretBox, load_key
from .settings import (
    DAY_LABELS, DAY_SHORT, DAYS, DEFAULT_MYSQL_PORT, MIN_INTERVAL_MINUTES, MODE_INTERVAL, MODE_TIMES,
    DbCredentials, MySQLCredentials, ScheduleRule, SQLServerCredentials, day_of, format_interval,
    load_credentials, load_settings, needs_setup, parse_port, save_credentials, save_settings, upcoming_runs,
)


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------
HEALTH_STYLES = {
    history.LEVEL_OK: ("ok", SUCCESS),
    history.LEVEL_INFO: ("info", ACCENT),
    history.LEVEL_RUNNING: ("running", ACCENT),
    history.LEVEL_WARNING: ("warning", WARNING),
    history.LEVEL_ERROR: ("error", DANGER),
}


def muted_label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setProperty("muted", True)
    label.setWordWrap(True)
    return label


def mark_invalid(edit: QLineEdit, invalid: bool = True) -> None:
    """Destaca o campo com erro; o destaque sai quando o texto muda."""
    edit.setProperty("invalid", invalid)
    edit.style().unpolish(edit)
    edit.style().polish(edit)


def make_field(icon_name: str, placeholder: str, password: bool = False) -> QLineEdit:
    """Campo de texto com o ícone dentro, à esquerda (e botão de mostrar senha)."""
    edit = QLineEdit()
    edit.setPlaceholderText(placeholder)
    edit.setToolTip(placeholder)
    edit.textChanged.connect(lambda _text: edit.property("invalid") and mark_invalid(edit, False))
    edit.addAction(icon(icon_name), QLineEdit.LeadingPosition)
    if password:
        edit.setEchoMode(QLineEdit.Password)
        reveal = edit.addAction(icon("show"), QLineEdit.TrailingPosition)
        reveal.setToolTip("Mostrar senha")

        def toggle() -> None:
            hidden = edit.echoMode() == QLineEdit.Password
            edit.setEchoMode(QLineEdit.Normal if hidden else QLineEdit.Password)
            reveal.setIcon(icon("hide" if hidden else "show"))
            reveal.setToolTip("Ocultar senha" if hidden else "Mostrar senha")

        reveal.triggered.connect(toggle)
    return edit


def make_button(text: str, icon_name: str, slot=None, primary: bool = False) -> QPushButton:
    button = QPushButton(text)
    button.setIcon(icon(icon_name, ON_ACCENT, ON_ACCENT) if primary else icon(icon_name))
    button.setIconSize(QSize(16, 16))
    if primary:
        button.setProperty("primary", True)
    if slot is not None:
        button.clicked.connect(slot)
    return button


def make_icon_button(icon_name: str, tooltip: str, slot) -> QPushButton:
    button = QPushButton()
    button.setIcon(icon(icon_name))
    button.setIconSize(QSize(16, 16))
    button.setToolTip(tooltip)
    button.setFixedWidth(36)
    button.clicked.connect(slot)
    return button


class StatusLine(QWidget):
    """Mensagem na própria tela, com ícone, para o que não precisa de uma janela para fechar."""
    HIDE_AFTER_MS = 5000

    def __init__(self, auto_hide: bool = False):
        super().__init__()
        self._auto_hide = auto_hide
        self._icon = QLabel()
        self._text = muted_label()
        policy = self._text.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Ignored)  # uma mensagem longa quebra linha, não alarga a tela
        self._text.setSizePolicy(policy)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self._icon, 0, Qt.AlignTop)
        layout.addWidget(self._text, 1)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.hide()

    def show_message(self, level: str, text: str, details: str = "") -> None:
        icon_name, color = HEALTH_STYLES[level]
        self._icon.setPixmap(icon(icon_name, color, color).pixmap(16, 16))
        self._text.setText(text)
        self._text.setToolTip(details or text)
        self.show()
        self._timer.stop()
        if self._auto_hide and level == history.LEVEL_OK:  # avisos ficam até serem lidos
            self._timer.start(self.HIDE_AFTER_MS)

    def text(self) -> str:
        return self._text.text()

    def details(self) -> str:
        return self._text.toolTip()


class PlaceholderList(QListWidget):
    """Lista que orienta o que fazer enquanto está vazia."""

    def __init__(self, placeholder: str):
        super().__init__()
        self._placeholder = placeholder

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.count() == 0:
            painter = QPainter(self.viewport())
            painter.setPen(QColor(MUTED))
            painter.drawText(self.viewport().rect().adjusted(12, 0, -12, 0),
                             Qt.AlignCenter | Qt.TextWordWrap, self._placeholder)


# Trecho do erro do banco (em minúsculas) -> o que fazer. Códigos valem em qualquer idioma do servidor.
CONNECTION_HINTS = (
    (("1045", "18456", "28000", "access denied", "login failed"), "Confira o usuário e a senha."),
    (("1130",), "O servidor não permite este usuário a partir deste computador."),
    (("1049", "4060"), "Confira o nome do banco de dados."),
    (("2003", "10061", "can't connect"), "Confira o host, a porta e se o serviço do banco está em execução."),
    (("08001", "hyt00", "timed out", "timeout"),
     "Confira o nome do servidor e da instância, se o serviço está em execução e o firewall."),
)


def connection_error_text(engine: str, error: Exception) -> str:
    """Mensagem do teste de conexão: o que fazer, quando o erro é conhecido."""
    raw = str(error)
    lowered = raw.lower()
    for fragments, hint in CONNECTION_HINTS:
        if any(fragment in lowered for fragment in fragments):
            return f"Não foi possível conectar ao {engine}. {hint}"
    return f"Erro ao conectar ao {engine}: {raw}"


def probe_mysql(creds: MySQLCredentials) -> str:
    """Conecta ao MySQL e devolve a versão do servidor."""
    conn = connect_mysql(creds)
    try:
        cur = conn.cursor()
        cur.execute("SELECT VERSION()")
        row = cur.fetchone()
        cur.close()
        return str(row[0]) if row and row[0] else ""
    finally:
        conn.close()


def probe_sqlserver(creds: SQLServerCredentials) -> str:
    """Conecta ao SQL Server e devolve a versão e a edição do servidor."""
    conn = connect_sqlserver(creds)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT CAST(SERVERPROPERTY('ProductVersion') AS VARCHAR(50)), "
            "CAST(SERVERPROPERTY('Edition') AS VARCHAR(100))"
        )
        row = cur.fetchone()
        cur.close()
        if row and row[0]:
            return f"{row[0]} ({row[1]})" if row[1] else str(row[0])
        return ""
    finally:
        conn.close()


class _BackgroundCallSignals(QObject):
    finished = pyqtSignal(str, object)  # resultado, exceção (ou None)


class BackgroundCall(QRunnable):
    """Executa uma chamada demorada fora da thread da interface, que congelaria até o fim."""

    def __init__(self, function, argument):
        super().__init__()
        self.function = function
        self.argument = argument
        self.signals = _BackgroundCallSignals()

    def run(self) -> None:  # executa na thread do pool
        try:
            result, error = self.function(self.argument), None
        except Exception as exc:
            result, error = "", exc
        self.signals.finished.emit(result, error)


def start_connection_test(tests: list, button: QPushButton, status: StatusLine, engine: str, probe, creds) -> None:
    label = button.text()
    button.setEnabled(False)
    button.setText("Testando...")
    status.show_message(history.LEVEL_RUNNING, f"Conectando ao {engine}...")
    task = BackgroundCall(probe, creds)

    def finished(version: str, error) -> None:
        tests.remove(task)  # a lista mantém a tarefa viva até o fim
        button.setText(label)
        button.setEnabled(True)
        if error is not None:
            status.show_message(history.LEVEL_ERROR, connection_error_text(engine, error), str(error))
        else:
            details = f" Versão do servidor: {version}" if version else ""
            status.show_message(history.LEVEL_OK, f"Conexão bem-sucedida.{details}")

    task.signals.finished.connect(finished)
    tests.append(task)
    QThreadPool.globalInstance().start(task)


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.startfile(str(path))  # type: ignore[attr-defined]  # apenas Windows


def get_secret_box(parent: QWidget) -> "SecretBox | None":
    """Carrega a chave (criando-a na primeira execução) ou mostra o erro."""
    try:
        return SecretBox(load_key(create=True))
    except CryptoKeyError as exc:
        error_log.error(str(exc))
        QMessageBox.critical(
            parent, "Erro de criptografia",
            f"{exc}\n\nSe a chave foi perdida, apague 'content\\fernet.key' e "
            "informe novamente as credenciais dos bancos.",
        )
        return None


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
class LoginScreen(QMainWindow):
    authenticated = pyqtSignal(str)

    def __init__(self):
        super().__init__()

        # Define o titulo da tela e o tamanho fixo
        self.setWindowTitle("DBKeeper - Login")
        self.setFixedSize(340, 330)
        self.setWindowIcon(QIcon(image("app_icon.ico")))

        # Cabeçalho: ícone do programa + nome
        logo = QLabel()
        logo.setPixmap(QIcon(image("app_icon.ico")).pixmap(48, 48))
        title = QLabel("DBKeeper")
        title_font = QFont(title.font())
        title_font.setPointSize(14)
        title_font.setBold(True)
        title.setFont(title_font)

        self.username_entry = make_field("user", "Usuário")
        self.username_entry.returnPressed.connect(self.password_entry_focus)

        self.password_entry = make_field("password", "Senha", password=True)
        self.password_entry.returnPressed.connect(self.login)

        self.login_button = make_button("Entrar", "login", self.login, primary=True)

        # Botão de primeiro cadastro: só aparece enquanto não existir nenhum usuário.
        # (O antigo atalho Ctrl+R permitia que qualquer pessoa criasse um usuário.)
        self.first_user_button = make_button("Criar primeiro usuário", "register", self.open_first_registration)
        self.first_user_button.setFlat(True)
        self.first_user_button.setIcon(icon("register", ACCENT))

        self.status = StatusLine()

        layout = QVBoxLayout()
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(10)
        layout.addWidget(logo, alignment=Qt.AlignHCenter)
        layout.addWidget(title, alignment=Qt.AlignHCenter)
        layout.addSpacing(8)
        layout.addWidget(self.username_entry)
        layout.addWidget(self.password_entry)
        layout.addWidget(self.status)
        layout.addWidget(self.login_button)
        layout.addWidget(self.first_user_button, alignment=Qt.AlignHCenter)
        layout.addStretch(1)

        central_widget = QWidget()
        central_widget.setLayout(layout)
        self.setCentralWidget(central_widget)

    def password_entry_focus(self) -> None:
        self.password_entry.setFocus()

    def showEvent(self, event) -> None:
        self.first_user_button.setVisible(not users.has_users())
        self.username_entry.setFocus()
        super().showEvent(event)

    def _reset_fields(self) -> None:
        self.username_entry.clear()
        self.password_entry.clear()
        self.status.hide()
        self.username_entry.setFocus()

    # Função de login da tela inicial
    def login(self) -> None:
        username = self.username_entry.text().strip()
        password = self.password_entry.text()
        try:
            ok = users.check_login(username, password)
        except Exception as exc:
            error_log.error(f"Erro ao verificar login: {exc}")
            QMessageBox.critical(self, "Erro", f"Não foi possível ler o arquivo de usuários:\n{exc}")
            return

        if ok:
            main_log.info(f"Login bem sucedido do usuario {username}!")
            self._reset_fields()
            self.hide()
            self.authenticated.emit(username)
        else:
            main_log.info(f"Tentativa de login sem sucesso para o usuario '{username}'.")
            self.status.show_message(history.LEVEL_ERROR, "Usuário ou senha incorretos.")
            self.password_entry.clear()
            self.password_entry.setFocus()

    def open_first_registration(self) -> None:
        if users.has_users():  # proteção extra: só o primeiro cadastro é livre
            self.first_user_button.hide()
            return
        if RegistrationScreen(self).exec_() == QDialog.Accepted:
            self.status.show_message(history.LEVEL_OK, "Usuário criado. Faça o login.")
        self.first_user_button.setVisible(not users.has_users())

    # Esconde a tela de login em vez de fechar
    def closeEvent(self, event) -> None:
        event.ignore()
        self._reset_fields()
        self.hide()


# ---------------------------------------------------------------------------
# Registro de usuário
# ---------------------------------------------------------------------------
class RegistrationScreen(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Novo usuário")
        self.setFixedSize(360, 230)
        self.setWindowIcon(QIcon(image("app_icon.ico")))

        self.new_username_entry = make_field("user", "Novo usuário")
        self.new_password_entry = make_field(
            "password", f"Nova senha (mínimo {users.MIN_PASSWORD_LENGTH} caracteres)", password=True)
        self.confirm_password_entry = make_field("password", "Confirme a senha", password=True)
        self.confirm_password_entry.returnPressed.connect(self.register_user)

        self.register_button = make_button("Registrar", "register", self.register_user, primary=True)
        self.register_button.setAutoDefault(False)  # o Enter já é tratado pelo returnPressed
        self.status = StatusLine()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)
        layout.addWidget(self.new_username_entry)
        layout.addWidget(self.new_password_entry)
        layout.addWidget(self.confirm_password_entry)
        layout.addWidget(self.status)
        layout.addWidget(self.register_button)
        layout.addStretch(1)

    def register_user(self) -> None:
        try:
            users.register_user(
                self.new_username_entry.text(),
                self.new_password_entry.text(),
                self.confirm_password_entry.text(),
            )
        except users.UserError as exc:
            self.status.show_message(history.LEVEL_ERROR, str(exc))
            return
        except Exception as exc:
            error_log.error(f"Erro ao registrar usuário: {exc}")
            QMessageBox.critical(self, "Erro", f"Não foi possível salvar o usuário:\n{exc}")
            return
        self.accept()


# ---------------------------------------------------------------------------
# Edição de uma regra de agendamento
# ---------------------------------------------------------------------------
INTERVAL_CHOICES = (30, 60, 90, 120, 180, 240, 360, 480, 720)  # minutos


def format_runs(runs) -> str:
    return ", ".join(f"{DAY_SHORT[day_of(run)]} {run.strftime('%H:%M')}" for run in runs)


class RuleDialog(QDialog):
    """Cria ou edita uma regra: dias + horários fixos ou intervalo."""

    def __init__(self, rule: "ScheduleRule | None" = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Regra de agendamento")
        self.setWindowIcon(QIcon(image("app_icon.ico")))
        self.setMinimumWidth(460)
        rule = rule or ScheduleRule(days=list(DAYS[:5]), times=["12:00"])
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Dias
        days_box = QGroupBox("Dias")
        days_layout = QVBoxLayout(days_box)
        row = QHBoxLayout()
        self.day_checkboxes = {}
        for day in DAYS:
            checkbox = QCheckBox(DAY_SHORT[day])
            checkbox.setChecked(day in rule.days)
            checkbox.toggled.connect(self._update_preview)
            self.day_checkboxes[day] = checkbox
            row.addWidget(checkbox)
        days_layout.addLayout(row)
        presets = QHBoxLayout()
        for label, days in (("Seg–Sex", DAYS[:5]), ("Fim de semana", DAYS[5:]), ("Todos", DAYS)):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked, d=days: self._select_days(d))
            presets.addWidget(button)
        presets.addStretch()
        days_layout.addLayout(presets)
        layout.addWidget(days_box)

        # Modo
        self.times_radio = QRadioButton("Horários fixos")
        self.interval_radio = QRadioButton("Intervalo")
        mode_group = QButtonGroup(self)
        mode_group.addButton(self.times_radio)
        mode_group.addButton(self.interval_radio)
        modes = QHBoxLayout()
        modes.addWidget(self.times_radio)
        modes.addWidget(self.interval_radio)
        modes.addStretch()
        layout.addLayout(modes)

        # Horários fixos
        self.times_box = QGroupBox("Horários")
        times_layout = QHBoxLayout(self.times_box)
        self.times_list = QListWidget()
        self.times_list.setMaximumHeight(110)
        for time in rule.times_of_day() if rule.mode == MODE_TIMES else []:
            self.times_list.addItem(time)
        times_side = QVBoxLayout()
        self.new_time = QTimeEdit(QTime(12, 0))
        self.new_time.setDisplayFormat("HH:mm")
        add_time = make_button("Adicionar", "add", self._add_time)
        remove_time = make_button("Remover", "remove", self._remove_time)
        for widget in (self.new_time, add_time, remove_time):
            times_side.addWidget(widget)
        times_side.addStretch()
        times_layout.addWidget(self.times_list)
        times_layout.addLayout(times_side)
        layout.addWidget(self.times_box)

        # Intervalo
        self.interval_box = QGroupBox("Intervalo")
        interval_layout = QGridLayout(self.interval_box)
        self.interval_combo = QComboBox()
        for minutes in INTERVAL_CHOICES:
            self.interval_combo.addItem(f"A cada {format_interval(minutes)}", minutes)
        if rule.every_minutes not in INTERVAL_CHOICES:
            self.interval_combo.addItem(f"A cada {format_interval(rule.every_minutes)}", rule.every_minutes)
        self.interval_combo.setCurrentIndex(self.interval_combo.findData(rule.every_minutes))
        self.start_time = QTimeEdit(QTime.fromString(rule.start, "HH:mm"))
        self.end_time = QTimeEdit(QTime.fromString(rule.end, "HH:mm"))
        for edit in (self.start_time, self.end_time):
            edit.setDisplayFormat("HH:mm")
            edit.timeChanged.connect(self._update_preview)
        self.interval_combo.currentIndexChanged.connect(self._update_preview)
        interval_layout.addWidget(self.interval_combo, 0, 0, 1, 4)
        interval_layout.addWidget(QLabel("Das"), 1, 0)
        interval_layout.addWidget(self.start_time, 1, 1)
        interval_layout.addWidget(QLabel("às"), 1, 2)
        interval_layout.addWidget(self.end_time, 1, 3)
        layout.addWidget(self.interval_box)

        self.preview = muted_label()
        layout.addWidget(self.preview)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)

        self._enabled = rule.enabled
        self.times_radio.toggled.connect(self._on_mode_changed)
        (self.interval_radio if rule.mode == MODE_INTERVAL else self.times_radio).setChecked(True)
        self._on_mode_changed()

    def _select_days(self, days) -> None:
        for day, checkbox in self.day_checkboxes.items():
            checkbox.setChecked(day in days)

    def _add_time(self) -> None:
        time = self.new_time.time().toString("HH:mm")
        existing = [self.times_list.item(i).text() for i in range(self.times_list.count())]
        if time not in existing:
            self.times_list.clear()
            self.times_list.addItems(sorted(existing + [time]))
        self._update_preview()

    def _remove_time(self) -> None:
        for item in self.times_list.selectedItems():
            self.times_list.takeItem(self.times_list.row(item))
        self._update_preview()

    def _on_mode_changed(self) -> None:
        self.times_box.setVisible(self.times_radio.isChecked())
        self.interval_box.setVisible(self.interval_radio.isChecked())
        self._update_preview()

    def rule(self) -> ScheduleRule:
        return ScheduleRule(
            days=[day for day in DAYS if self.day_checkboxes[day].isChecked()],
            mode=MODE_INTERVAL if self.interval_radio.isChecked() else MODE_TIMES,
            times=[self.times_list.item(i).text() for i in range(self.times_list.count())],
            every_minutes=self.interval_combo.currentData() or MIN_INTERVAL_MINUTES,
            start=self.start_time.time().toString("HH:mm"),
            end=self.end_time.time().toString("HH:mm"),
            enabled=self._enabled,
        )

    def _update_preview(self) -> None:
        rule = self.rule()
        problem = rule.validate()
        self.ok_button.setEnabled(problem is None)  # o motivo aparece logo acima do botão
        self.preview.setStyleSheet(f"color: {WARNING};" if problem else "")
        if problem:
            self._set_preview(problem)
            return
        per_day = len(rule.times_of_day())
        text = f"{per_day} backup(s) por dia: {', '.join(rule.times_of_day())}"
        rule.enabled = True
        runs = upcoming_runs([rule], 3)
        if runs:
            text += f"\nPróximos: {format_runs(runs)}"
        self._set_preview(text)

    def _set_preview(self, text: str) -> None:
        self.preview.setText(text)
        QTimer.singleShot(0, self._fit_height)  # espera o layout aplicar a mudança

    def _fit_height(self) -> None:
        """Ajusta a altura ao conteúdo visível, senão a sobra vira espaço entre os blocos."""
        layout = self.layout()
        layout.activate()
        height = (layout.totalHeightForWidth(self.width()) if layout.hasHeightForWidth()
                  else layout.totalSizeHint().height())
        self.resize(self.width(), height)


# ---------------------------------------------------------------------------
# Janela principal
# ---------------------------------------------------------------------------
def result_icon(success: bool) -> QIcon:
    return icon("ok", SUCCESS, SUCCESS) if success else icon("error", DANGER, DANGER)


def database_detail(database: history.DatabaseEntry) -> str:
    if not database.success:
        return database.message
    return " • ".join(filter(None, (history.format_size(database.size), "verificado" if database.verified else "")))


class MainWindow(QMainWindow):
    def __init__(self, scheduler: BackupScheduler):
        super().__init__()
        self.scheduler = scheduler

        self.setWindowTitle("DBKeeper")
        self.setWindowIcon(QIcon(image("app_icon.ico")))
        self.setMinimumSize(780, 560)
        self._connection_tests = []  # mantém as tarefas vivas até o fim
        self._taskbar = None
        self._saved_states = {}  # como cada aba está gravada em disco

        self.home_tab = self._build_home_tab()
        self.settings_tab = self._build_connections_tab()
        self.schedule_tab = self._build_schedule_tab()
        self.system_tab = self._build_system_tab()

        # Barra lateral (botões exclusivos) + páginas empilhadas
        self.pages = QStackedWidget()
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setAttribute(Qt.WA_StyledBackground, True)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(0, 8, 0, 8)
        nav.setSpacing(4)
        self.nav_buttons = QButtonGroup(self)
        self.nav_buttons.idClicked.connect(self.pages.setCurrentIndex)
        for index, (widget, icon_name, name) in enumerate((
            (self.home_tab, "home", "Início"),
            (self.settings_tab, "connections", "Conexões"),
            (self.schedule_tab, "schedule", "Agendamento"),
            (self.system_tab, "system", "Sistema"),
        )):
            self.pages.addWidget(widget)
            button = QToolButton()
            button.setIcon(tab_icon(icon_name))
            button.setIconSize(QSize(24, 24))
            button.setText(name)
            button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setCheckable(True)
            button.setChecked(index == 0)
            self.nav_buttons.addButton(button, index)
            nav.addWidget(button)
        nav.addStretch(1)

        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(sidebar)
        root.addWidget(self.pages, 1)
        self.setCentralWidget(central)

        # Atualização do status de agendamento
        self.scheduler.schedule_changed.connect(self.refresh_status)
        self.scheduler.backup_started.connect(lambda _origin: self.refresh_status())
        self.scheduler.backup_progress.connect(self.refresh_status)
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(30_000)
        self._status_timer.timeout.connect(self.refresh_status)

    # ------------------------------------------------------------------ eventos
    def showEvent(self, event) -> None:
        # Sempre exibe o que está salvo em disco, exceto ao sair do minimizado:
        # nesse caso a janela não fechou e o que foi digitado continua valendo
        if not event.spontaneous():
            self._reload_saved()
        if QWinTaskbarButton is not None and self._taskbar is None:
            self._taskbar = QWinTaskbarButton(self)
            self._taskbar.setWindow(self.windowHandle())
        self.refresh_status()
        self._status_timer.start()
        super().showEvent(event)

    def closeEvent(self, event) -> None:
        # Fechar apenas esconde: o programa continua na bandeja executando os backups
        event.ignore()
        if self.confirm_unsaved():
            self._status_timer.stop()
            self.hide()

    def _reload_saved(self) -> None:
        self.load_connections()
        self.load_schedule()
        self.load_system()
        for name, _widget, state, _save in self._sections():
            self._saved_states[name] = state()

    # ------------------------------------------------------------------ alterações não salvas
    def _sections(self):
        """Abas com botão de salvar: nome, página, estado atual e como salvar."""
        return (
            ("Conexões", self.settings_tab, self._connections_state, self.save_credentials),
            ("Agendamento", self.schedule_tab, self._schedule_state, self.save_schedule),
            ("Pasta de backups", self.system_tab, self.backup_dir.text, self.save_system),
        )

    def _connections_state(self) -> tuple:
        return tuple(field.text() for field in (
            self.mysql_host, self.mysql_username, self.mysql_password, self.mysql_port, self.mysqldump_path,
            self.sql_server, self.sql_username, self.sql_password, self.sql_database, self.sql_port,
            self.sqlserver_backup_dir,
        ))

    def _schedule_state(self) -> tuple:
        self._sync_rule_states()
        return ([asdict(rule) for rule in self._rules], self.retention_spin.value(),
                self.checkbox_mysql.isChecked(), self.checkbox_sql_server.isChecked())

    def _mark_saved(self, name: str) -> None:
        for section, _widget, state, _save in self._sections():
            if section == name:
                self._saved_states[name] = state()

    def _ask_unsaved(self, names) -> str:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Alterações não salvas")
        box.setText(f"Há alterações não salvas em: {', '.join(names)}.")
        save = box.addButton("Salvar", QMessageBox.AcceptRole)
        discard = box.addButton("Descartar", QMessageBox.DestructiveRole)
        box.addButton("Continuar editando", QMessageBox.RejectRole)
        box.setDefaultButton(save)
        box.exec_()
        return {save: "save", discard: "discard"}.get(box.clickedButton(), "cancel")

    def confirm_unsaved(self) -> bool:
        """Pergunta o que fazer com as alterações pendentes. False = a janela deve continuar aberta."""
        pending = [section for section in self._sections() if self._saved_states.get(section[0]) != section[2]()]
        if not pending:
            return True
        answer = self._ask_unsaved([section[0] for section in pending])
        if answer == "cancel":
            return False
        if answer == "save":
            for _name, widget, _state, save in pending:
                if not save():  # mostra a aba para que o erro seja visto
                    self.nav_buttons.button(self.pages.indexOf(widget)).click()
                    return False
        return True

    # ------------------------------------------------------------------ helpers de layout
    @staticmethod
    def _add_field(layout: QVBoxLayout, icon_name: str, placeholder: str,
                   password: bool = False) -> QLineEdit:
        edit = make_field(icon_name, placeholder, password)
        layout.addWidget(edit)
        return edit

    @staticmethod
    def _add_path_field(layout: QVBoxLayout, icon_name: str, placeholder: str, on_browse) -> QLineEdit:
        edit = make_field(icon_name, placeholder)
        row = QHBoxLayout()
        row.addWidget(edit)
        row.addWidget(make_icon_button("browse", "Procurar...", on_browse))
        layout.addLayout(row)
        return edit

    @staticmethod
    def _field_error(status: StatusLine, field: QLineEdit, message: str) -> None:
        mark_invalid(field)
        field.setFocus()
        status.show_message(history.LEVEL_ERROR, message)

    @staticmethod
    def _ports_valid(status: StatusLine, *fields: QLineEdit) -> bool:
        for field in fields:
            try:
                parse_port(field.text())
            except ValueError:
                MainWindow._field_error(status, field, "Porta inválida: use um número de 1 a 65535 ou deixe vazio.")
                return False
        return True

    @staticmethod
    def _footer(*buttons: QPushButton, status: "StatusLine | None" = None) -> QHBoxLayout:
        """Linha de botões alinhada à direita, no rodapé da aba, com a mensagem à esquerda."""
        row = QHBoxLayout()
        if status is not None:
            row.addWidget(status, 1)
        row.addStretch()
        for button in buttons:
            row.addWidget(button)
        return row

    # ================================================================== Aba Início
    def _build_home_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        # Situação geral: responde se os backups estão em dia
        card = QFrame()
        card.setObjectName("statusCard")
        card_layout = QHBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(14)
        self.health_icon = QLabel()
        self.health_title = QLabel()
        title_font = QFont(self.health_title.font())
        title_font.setPointSize(13)
        title_font.setBold(True)
        self.health_title.setFont(title_font)
        self.health_detail = muted_label()
        texts = QVBoxLayout()
        texts.setSpacing(2)
        texts.addWidget(self.health_title)
        texts.addWidget(self.health_detail)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()
        texts.addSpacing(4)
        texts.addWidget(self.progress_bar)
        self.run_now_button = make_button("Executar agora", "run", self.run_backup_now, primary=True)
        self.run_now_button.setToolTip("Executa o backup em segundo plano com a agenda salva.")
        card_layout.addWidget(self.health_icon)
        card_layout.addLayout(texts, 1)
        card_layout.addWidget(self.run_now_button)
        layout.addWidget(card)

        # Último e próximo backup
        moments = QHBoxLayout()
        moments.setSpacing(20)
        self.last_run_label, self.last_run_detail = self._add_moment(moments, "Último backup")
        self.next_run_label, self.next_run_detail = self._add_moment(moments, "Próximo backup")
        layout.addLayout(moments)

        # Histórico
        history_box = QGroupBox("Histórico")
        history_layout = QVBoxLayout(history_box)
        self.history_tree = QTreeWidget()
        self.history_tree.setHeaderLabels(["Data", "Resultado", "Detalhes"])
        self.history_tree.setRootIsDecorated(True)
        self.history_tree.setUniformRowHeights(True)
        header = self.history_tree.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.history_empty = muted_label("Nenhum backup registrado ainda.")
        history_layout.addWidget(self.history_tree, 1)
        history_layout.addWidget(self.history_empty, 1, Qt.AlignTop)
        layout.addWidget(history_box, 1)
        self._history_key = None
        return tab

    @staticmethod
    def _add_moment(row: QHBoxLayout, title: str):
        box = QGroupBox(title)
        box_layout = QVBoxLayout(box)
        box_layout.setSpacing(2)
        value = QLabel()
        detail = muted_label()
        box_layout.addWidget(value)
        box_layout.addWidget(detail)
        row.addWidget(box)
        return value, detail

    def _render_history(self, entries) -> None:
        key = [(entry.started, entry.finished) for entry in entries]
        if key == self._history_key:  # preserva o que o usuário expandiu na atualização automática
            return
        self._history_key = key
        self.history_tree.clear()
        for entry in entries:
            item = QTreeWidgetItem([
                entry.started.strftime("%d/%m/%Y %H:%M"),
                "Concluído" if entry.success else "Com falhas",
                f"{entry.summary} ({history.format_duration(entry)}, {entry.origin})",
            ])
            item.setIcon(1, result_icon(entry.success))
            item.setToolTip(2, entry.summary)
            for database in entry.databases:
                child = QTreeWidgetItem([
                    f"{database.engine}: {database.database}",
                    "Concluído" if database.success else "Falhou",
                    database_detail(database),
                ])
                child.setIcon(1, result_icon(database.success))
                child.setToolTip(2, database.message)
                item.addChild(child)
            self.history_tree.addTopLevelItem(item)
        self.history_tree.setVisible(bool(entries))
        self.history_empty.setVisible(not entries)

    # ================================================================== Aba Conexões
    def _build_connections_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 12, 16, 16)
        layout.setSpacing(16)
        groups = QHBoxLayout()
        groups.setSpacing(20)

        # MySQL
        mysql_box = QGroupBox("MySQL")
        box = QVBoxLayout(mysql_box)
        self.mysql_host = self._add_field(box, "server", "Host (padrão: localhost)")
        self.mysql_username = self._add_field(box, "user", "Usuário")
        self.mysql_password = self._add_field(box, "password", "Senha", True)
        self.mysql_port = self._add_field(box, "port", f"Porta (padrão: {DEFAULT_MYSQL_PORT})")
        self.mysqldump_path = self._add_path_field(box, "executable", "Caminho do mysqldump.exe",
                                                   self._browse_mysqldump)
        box.addStretch(1)
        self.mysql_status = StatusLine()
        box.addWidget(self.mysql_status)
        self.test_mysql_button = make_button("Testar conexão", "test", self.test_mysql_connection)
        box.addWidget(self.test_mysql_button)
        groups.addWidget(mysql_box)

        # SQL Server
        sql_box = QGroupBox("SQL Server")
        box = QVBoxLayout(sql_box)
        self.sql_server = self._add_field(box, "server", "Servidor (ex.: localhost\\SQLEXPRESS)")
        self.sql_username = self._add_field(box, "user", "Usuário (vazio = autenticação do Windows)")
        self.sql_password = self._add_field(box, "password", "Senha", True)
        self.sql_database = self._add_field(box, "database", "Banco de dados")
        self.sql_port = self._add_field(box, "port", "Porta (opcional)")
        self.sqlserver_backup_dir = self._add_path_field(
            box, "folder", "Pasta de backup no servidor (opcional)", self._browse_sqlserver_dir)
        box.addStretch(1)
        self.sql_status = StatusLine()
        box.addWidget(self.sql_status)
        self.test_sql_server_button = make_button("Testar conexão", "test", self.test_sql_server_connection)
        box.addWidget(self.test_sql_server_button)
        groups.addWidget(sql_box)

        # O resultado de um teste deixa de valer quando os dados mudam
        for status, fields in (
            (self.mysql_status, (self.mysql_host, self.mysql_username, self.mysql_password, self.mysql_port)),
            (self.sql_status, (self.sql_server, self.sql_username, self.sql_password, self.sql_database,
                               self.sql_port)),
        ):
            for field in fields:
                field.textEdited.connect(lambda _text, line=status: line.hide())

        layout.addLayout(groups)
        layout.addWidget(muted_label(
            "A pasta de backup do SQL Server é o caminho visto pelo próprio servidor SQL; "
            "a conta do serviço precisa ter permissão de escrita nela."
        ))
        layout.addStretch(1)

        self.save_button = make_button("Salvar configurações", "save", self.save_credentials, primary=True)
        self.connections_status = StatusLine(auto_hide=True)
        layout.addLayout(self._footer(self.save_button, status=self.connections_status))
        return tab

    def _browse_mysqldump(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Selecione o mysqldump.exe", self.mysqldump_path.text(),
                                              "mysqldump (mysqldump.exe);;Executáveis (*.exe)")
        if path:
            self.mysqldump_path.setText(str(Path(path)))

    def _browse_sqlserver_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Pasta de backup do SQL Server",
                                                self.sqlserver_backup_dir.text())
        if path:
            self.sqlserver_backup_dir.setText(str(Path(path)))

    def load_connections(self) -> None:
        settings = load_settings()
        self.mysqldump_path.setText(settings.mysqldump_path)
        self.sqlserver_backup_dir.setText(settings.sqlserver_backup_dir)
        detected = find_mysqldump()
        self.mysqldump_path.setPlaceholderText(
            f"Automático: {detected}" if detected else "mysqldump.exe não encontrado - informe o caminho")

        box = get_secret_box(self)
        if box is None:
            return
        creds = load_credentials(box)
        m, s = creds.mysql, creds.sqlserver
        self.mysql_host.setText(m.host if m.host != "localhost" else "")
        self.mysql_username.setText(m.user)
        self.mysql_password.setText(m.password)
        self.mysql_port.setText(str(m.port) if m.port != DEFAULT_MYSQL_PORT else "")
        self.sql_server.setText(s.server)
        self.sql_username.setText(s.user)
        self.sql_password.setText(s.password)
        self.sql_database.setText(s.database)
        self.sql_port.setText(str(s.port) if s.port else "")

    def _mysql_from_fields(self) -> MySQLCredentials:
        return MySQLCredentials(
            host=self.mysql_host.text().strip() or "localhost",
            user=self.mysql_username.text().strip(),
            password=self.mysql_password.text(),
            port=parse_port(self.mysql_port.text(), DEFAULT_MYSQL_PORT),
        )

    def _sqlserver_from_fields(self) -> SQLServerCredentials:
        return SQLServerCredentials(
            server=self.sql_server.text().strip(),
            user=self.sql_username.text().strip(),
            password=self.sql_password.text(),
            database=self.sql_database.text().strip(),
            port=parse_port(self.sql_port.text(), None),
        )

    # Função para testar a conexão com MySQL
    def test_mysql_connection(self) -> None:
        if not self._ports_valid(self.mysql_status, self.mysql_port):
            return
        creds = self._mysql_from_fields()
        if not creds.user:
            self._field_error(self.mysql_status, self.mysql_username, "Informe o usuário do MySQL.")
            return
        start_connection_test(self._connection_tests, self.test_mysql_button, self.mysql_status, "MySQL",
                              probe_mysql, creds)

    # Função para testar a conexão com SQL Server
    def test_sql_server_connection(self) -> None:
        if not self._ports_valid(self.sql_status, self.sql_port):
            return
        creds = self._sqlserver_from_fields()
        if not creds.server:
            self._field_error(self.sql_status, self.sql_server, "Informe o servidor do SQL Server.")
            return
        start_connection_test(self._connection_tests, self.test_sql_server_button, self.sql_status, "SQL Server",
                              probe_sqlserver, creds)

    # Função para salvar as credenciais das conexões aos bancos de dados
    def save_credentials(self) -> bool:
        status = self.connections_status
        if not self._ports_valid(status, self.mysql_port, self.sql_port):
            return False
        creds = DbCredentials(self._mysql_from_fields(), self._sqlserver_from_fields())
        dump_path = self.mysqldump_path.text().strip()
        if dump_path and not Path(dump_path).is_file():
            self._field_error(status, self.mysqldump_path,
                              "mysqldump.exe não encontrado: escolha o arquivo ou deixe vazio para detectar.")
            return False

        box = get_secret_box(self)
        if box is None:
            return False
        try:
            save_credentials(creds, box)
            settings = load_settings()
            settings.mysqldump_path = dump_path
            settings.sqlserver_backup_dir = self.sqlserver_backup_dir.text().strip()
            save_settings(settings)
        except OSError as exc:
            error_log.error(f"Erro ao salvar as configurações de conexão: {exc}")
            QMessageBox.critical(self, "Erro", f"Não foi possível salvar as configurações:\n{exc}")
            return False
        main_log.info("Configurações de conexão salvas.")
        self._mark_saved("Conexões")
        status.show_message(history.LEVEL_OK, "Configurações salvas.")
        return True

    # ================================================================== Aba Agendamento
    def _build_schedule_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 12, 16, 16)
        layout.setSpacing(16)

        # Regras: cada item tem uma caixa para ativar/desativar sem apagar
        rules_box = QGroupBox("Regras de agendamento")
        rules_layout = QHBoxLayout(rules_box)
        self.rules_list = PlaceholderList("Nenhuma regra ainda. Clique em Adicionar para criar a primeira.")
        self.rules_list.itemDoubleClicked.connect(lambda _item: self._edit_rule())
        self.rules_list.itemChanged.connect(lambda _item: self._update_schedule_preview())
        self.rules_list.itemSelectionChanged.connect(self._update_rule_buttons)
        self.edit_rule_button = make_button("Editar", "edit", self._edit_rule)
        self.remove_rule_button = make_button("Remover", "remove", self._remove_rule)
        rules_buttons = QVBoxLayout()
        rules_buttons.addWidget(make_button("Adicionar", "add", self._add_rule))
        rules_buttons.addWidget(self.edit_rule_button)
        rules_buttons.addWidget(self.remove_rule_button)
        rules_buttons.addStretch()
        rules_layout.addWidget(self.rules_list)
        rules_layout.addLayout(rules_buttons)
        layout.addWidget(rules_box)
        self._rules = []

        self.schedule_preview_label = muted_label()
        layout.addWidget(self.schedule_preview_label)

        # Retenção
        retention = QHBoxLayout()
        retention.addWidget(QLabel("Manter backups por"))
        self.retention_spin = QSpinBox()
        self.retention_spin.setRange(0, 3650)
        self.retention_spin.setSuffix(" dia(s)")
        self.retention_spin.setSpecialValueText("sempre (não apagar)")
        self.retention_spin.setToolTip("Backups mais antigos são apagados após cada backup bem-sucedido.\n"
                                       "O backup mais recente de cada banco nunca é apagado.")
        retention.addWidget(self.retention_spin)
        retention.addStretch()
        layout.addLayout(retention)

        # Bancos a copiar
        self.checkbox_sql_server = QCheckBox("Backup SQL Server")
        self.checkbox_mysql = QCheckBox("Backup MySQL")
        engines = QHBoxLayout()
        engines.addWidget(self.checkbox_sql_server)
        engines.addWidget(self.checkbox_mysql)
        engines.addStretch()
        layout.addLayout(engines)

        layout.addStretch(1)

        self.save_button_schedule = make_button("Salvar configurações", "save",
                                                lambda: self.save_schedule(show_message=True), primary=True)
        self.schedule_status = StatusLine(auto_hide=True)
        layout.addLayout(self._footer(self.save_button_schedule, status=self.schedule_status))
        return tab

    def _render_rules(self, select: int = -1) -> None:
        self.rules_list.blockSignals(True)
        self.rules_list.clear()
        for rule in self._rules:
            item = QListWidgetItem(rule.describe())
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if rule.enabled else Qt.Unchecked)
            self.rules_list.addItem(item)
        self.rules_list.blockSignals(False)
        if 0 <= select < len(self._rules):
            self.rules_list.setCurrentRow(select)
        self._update_rule_buttons()
        self._update_schedule_preview()

    def _update_rule_buttons(self) -> None:
        selected = bool(self.rules_list.selectedItems())
        self.edit_rule_button.setEnabled(selected)
        self.remove_rule_button.setEnabled(selected)

    def _sync_rule_states(self) -> None:
        """Copia o estado das caixas de seleção da lista para as regras."""
        for row, rule in enumerate(self._rules):
            rule.enabled = self.rules_list.item(row).checkState() == Qt.Checked

    def _update_schedule_preview(self) -> None:
        self._sync_rule_states()
        runs = upcoming_runs(self._rules, 5)
        self.schedule_preview_label.setText(
            f"Próximos backups previstos: {format_runs(runs)}" if runs else "Nenhum backup previsto.")

    def _add_rule(self) -> None:
        dialog = RuleDialog(parent=self)
        if dialog.exec_() == QDialog.Accepted:
            self._sync_rule_states()
            self._rules.append(dialog.rule())
            self._render_rules(len(self._rules) - 1)

    def _edit_rule(self) -> None:
        row = self.rules_list.currentRow()
        if row < 0:
            return
        self._sync_rule_states()
        dialog = RuleDialog(self._rules[row], parent=self)
        if dialog.exec_() == QDialog.Accepted:
            self._rules[row] = dialog.rule()
            self._render_rules(row)

    def _remove_rule(self) -> None:
        row = self.rules_list.currentRow()
        if row < 0:
            return
        self._sync_rule_states()
        del self._rules[row]
        self._render_rules(min(row, len(self._rules) - 1))

    def load_schedule(self) -> None:
        settings = load_settings()
        self._rules = settings.rules
        self._render_rules()
        self.retention_spin.setValue(settings.retention_days)
        self.checkbox_mysql.setChecked(settings.use_mysql)
        self.checkbox_sql_server.setChecked(settings.use_sql_server)

    def _confirm_retention(self, settings, days: int) -> bool:
        """Reduzir o prazo apaga backups que já existem: avisa quantos antes de salvar."""
        if not days or (settings.retention_days and days >= settings.retention_days):
            return True
        count = len(retention_preview(settings, days))
        if not count:
            return True
        answer = QMessageBox.question(
            self, "Reduzir retenção",
            f"Com o prazo de {days} dia(s), {count} backup(s) antigo(s) serão apagados a partir do próximo "
            "backup bem-sucedido. O mais recente de cada banco é mantido.\n\nDeseja continuar?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return answer == QMessageBox.Yes

    def save_schedule(self, show_message: bool = True) -> bool:
        settings = load_settings()  # preserva as demais seções (caminhos)
        if not self._confirm_retention(settings, self.retention_spin.value()):
            return False
        self._sync_rule_states()
        settings.rules = self._rules
        settings.retention_days = self.retention_spin.value()
        settings.use_mysql = self.checkbox_mysql.isChecked()
        settings.use_sql_server = self.checkbox_sql_server.isChecked()
        try:
            save_settings(settings)
        except OSError as exc:
            error_log.error(f"Erro ao salvar settings.ini: {exc}")
            QMessageBox.critical(self, "Erro", f"Não foi possível salvar a agenda:\n{exc}")
            return False

        jobs = self.scheduler.reload(settings)
        self._mark_saved("Agendamento")
        if show_message:
            if jobs and not (settings.use_mysql or settings.use_sql_server):
                self.schedule_status.show_message(
                    history.LEVEL_WARNING, "Agenda salva, mas nenhum banco (MySQL/SQL Server) está selecionado.")
            elif jobs:
                self.schedule_status.show_message(history.LEVEL_OK, f"Agenda salva: {len(jobs)} regra(s) ativa(s).")
            else:
                self.schedule_status.show_message(history.LEVEL_WARNING, "Agenda salva, sem nenhuma regra ativa.")
        return True

    def run_backup_now(self) -> None:
        if not self.scheduler.trigger("manual"):
            QMessageBox.information(self, "Backup", "Já existe um backup em andamento.")

    def refresh_status(self) -> None:
        entries = history.load()
        running = self.scheduler.is_running
        next_run = self.scheduler.next_run()
        health = self.scheduler.health(entries)
        self._show_progress(running)

        icon_name, color = HEALTH_STYLES[health.level]
        self.health_icon.setPixmap(icon(icon_name, color, color).pixmap(32, 32))
        self.health_title.setText(health.title)
        self.health_detail.setText(health.detail)
        self.run_now_button.setEnabled(not running)

        if entries:
            latest = entries[0]
            self.last_run_label.setText(latest.finished.strftime("%d/%m/%Y %H:%M"))
            self.last_run_detail.setText(history.format_relative(latest.finished))
        else:
            self.last_run_label.setText("Nenhum")
            self.last_run_detail.setText("")
        if next_run:
            self.next_run_label.setText(f"{DAY_LABELS[day_of(next_run)]}, {next_run.strftime('%d/%m/%Y %H:%M')}")
            self.next_run_detail.setText(history.format_relative(next_run))
        else:
            self.next_run_label.setText("Agendamento pausado" if self.scheduler.paused else "Nenhum agendado")
            self.next_run_detail.setText("")
        self._render_history(entries)

    def _show_progress(self, running: bool) -> None:
        """Barra na tela e na barra de tarefas; com um banco só não há etapas a mostrar, então fica indeterminada."""
        progress = self.scheduler.progress if running else None
        maximum, value = (progress[2], progress[1]) if progress and progress[2] > 1 else (0, 0)
        self.progress_bar.setRange(0, maximum)
        self.progress_bar.setValue(value)
        self.progress_bar.setVisible(running)
        if self._taskbar is not None:
            taskbar = self._taskbar.progress()
            taskbar.setRange(0, maximum)
            taskbar.setValue(value)
            taskbar.setVisible(running)

    # ================================================================== Aba Sistema
    def _build_system_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 12, 16, 16)
        layout.setSpacing(16)

        startup_box = QGroupBox("Inicialização")
        startup_layout = QVBoxLayout(startup_box)
        self.autostart_checkbox = QCheckBox("Iniciar com o Windows (em segundo plano, na bandeja)")
        self.autostart_checkbox.setEnabled(startup.is_supported())
        self.autostart_checkbox.toggled.connect(self._on_autostart_toggled)
        startup_layout.addWidget(self.autostart_checkbox)
        layout.addWidget(startup_box)

        notify_box = QGroupBox("Notificações")
        notify_layout = QVBoxLayout(notify_box)
        self.notify_start_checkbox = QCheckBox("Avisar quando um backup começar")
        self.notify_success_checkbox = QCheckBox("Avisar quando um backup agendado terminar bem")
        for checkbox in (self.notify_start_checkbox, self.notify_success_checkbox):
            checkbox.toggled.connect(self._on_notifications_toggled)
            notify_layout.addWidget(checkbox)
        notify_layout.addWidget(muted_label("Falhas sempre são avisadas."))
        layout.addWidget(notify_box)

        folder_box = QGroupBox("Pasta de backups")
        folder = QVBoxLayout(folder_box)
        self.backup_dir = self._add_path_field(folder, "folder", f"Padrão: {BASE_DIR}", self._browse_backup_dir)
        self.save_system_button = make_button("Salvar pasta", "save", self.save_system, primary=True)
        self.system_status = StatusLine(auto_hide=True)
        folder.addLayout(self._footer(self.save_system_button, status=self.system_status))
        layout.addWidget(folder_box)

        tools_box = QGroupBox("Ferramentas")
        tools = QGridLayout(tools_box)
        for index, (label, icon_name, slot) in enumerate((
            ("Restaurar backup", "restore", lambda: RestoreDialog(self).exec_()),
            ("Configuração guiada", "wizard", self.open_setup_wizard),
            ("Novo usuário", "register", lambda: RegistrationScreen(self).exec_()),
            ("Pasta de backups", "browse", lambda: self._open(load_settings().backup_root)),
            ("Pasta de logs", "logs", lambda: self._open(LOGS_DIR)),
        )):
            tools.addWidget(make_button(label, icon_name, slot), index // 3, index % 3)
        layout.addWidget(tools_box)

        layout.addStretch(1)
        info = muted_label(f"DBKeeper {__version__}  •  {BASE_DIR}")
        layout.addWidget(info)
        return tab

    def open_setup_wizard(self) -> None:
        if not self.confirm_unsaved():
            return
        SetupWizard(self.scheduler, self).exec_()
        self._reload_saved()
        self.refresh_status()

    def offer_setup(self) -> None:
        """Chamado após o login: abre o assistente só em uma instalação nova."""
        if needs_setup(load_settings()):
            self.open_setup_wizard()

    def _browse_backup_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Pasta de backups", self.backup_dir.text() or str(BASE_DIR))
        if path:
            self.backup_dir.setText(str(Path(path)))

    def _open(self, path: Path) -> None:
        try:
            open_folder(path)
        except OSError as exc:
            QMessageBox.warning(self, "Abrir pasta", f"Não foi possível abrir '{path}':\n{exc}")

    def load_system(self) -> None:
        self.autostart_checkbox.blockSignals(True)
        self.autostart_checkbox.setChecked(startup.is_enabled())
        self.autostart_checkbox.blockSignals(False)
        settings = load_settings()
        self.backup_dir.setText(settings.backup_dir)
        for checkbox, checked in ((self.notify_start_checkbox, settings.notify_start),
                                  (self.notify_success_checkbox, settings.notify_success)):
            checkbox.blockSignals(True)
            checkbox.setChecked(checked)
            checkbox.blockSignals(False)

    def _on_autostart_toggled(self, enabled: bool) -> None:
        try:
            startup.set_enabled(enabled)
        except OSError as exc:
            error_log.error(f"Erro ao alterar a inicialização automática: {exc}")
            QMessageBox.critical(self, "Erro", f"Não foi possível alterar a inicialização automática:\n{exc}")
            self.load_system()
            return
        main_log.info("Inicialização com o Windows " + ("ativada." if enabled else "desativada."))

    def _on_notifications_toggled(self) -> None:
        settings = load_settings()
        settings.notify_start = self.notify_start_checkbox.isChecked()
        settings.notify_success = self.notify_success_checkbox.isChecked()
        try:
            save_settings(settings)
        except OSError as exc:
            error_log.error(f"Erro ao salvar settings.ini: {exc}")
            QMessageBox.critical(self, "Erro", f"Não foi possível salvar as notificações:\n{exc}")
            self.load_system()

    def save_system(self) -> bool:
        path = self.backup_dir.text().strip()
        if path:
            try:
                Path(path).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self._field_error(self.system_status, self.backup_dir,
                                  f"Não foi possível usar esta pasta ({exc.strerror}). Escolha outra.")
                return False
        settings = load_settings()
        settings.backup_dir = path
        try:
            save_settings(settings)
        except OSError as exc:
            QMessageBox.critical(self, "Erro", f"Não foi possível salvar:\n{exc}")
            return False
        main_log.info(f"Pasta de backups alterada para '{settings.backup_root}'.")
        self._mark_saved("Pasta de backups")
        self.system_status.show_message(history.LEVEL_OK, f"Os backups serão gravados em {settings.backup_root}")
        return True


# ---------------------------------------------------------------------------
# Restauração
# ---------------------------------------------------------------------------
class RestoreDialog(QDialog):
    """Restaura um backup. O padrão é um banco novo; substituir exige digitar o nome do banco."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Restaurar backup")
        self.setWindowIcon(QIcon(image("app_icon.ico")))
        self.setMinimumSize(640, 560)
        self._task = None
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        source_box = QGroupBox("Backup")
        source = QVBoxLayout(source_box)
        self.backups = QTreeWidget()
        self.backups.setHeaderLabels(["Data", "Banco", "Tipo", "Tamanho"])
        self.backups.setRootIsDecorated(False)
        self.backups.setUniformRowHeights(True)
        self.backups.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.backups.header().setStretchLastSection(False)
        self.backups.itemSelectionChanged.connect(self._suggest_target)
        self.backups_empty = muted_label("Nenhum backup encontrado na pasta de backups.")
        source.addWidget(self.backups, 1)
        source.addWidget(self.backups_empty)
        self.browse_button = make_button("Escolher outro arquivo", "browse", self._browse)
        source.addWidget(self.browse_button, alignment=Qt.AlignLeft)
        layout.addWidget(source_box, 1)

        target_box = QGroupBox("Destino")
        target = QVBoxLayout(target_box)
        self.new_radio = QRadioButton("Criar um banco novo (recomendado)")
        self.new_radio.setChecked(True)
        self.replace_radio = QRadioButton("Substituir um banco existente")
        self.replace_radio.toggled.connect(self._suggest_target)
        self.target = make_field("database", "Nome do banco de destino")
        self.target.textChanged.connect(self._update_state)
        self.replace_warning = muted_label(
            "O conteúdo atual deste banco será apagado e as conexões abertas nele serão encerradas. "
            "Antes disso, o programa grava uma cópia de segurança dele.")
        self.replace_warning.setStyleSheet(f"color: {WARNING};")
        self.confirm = make_field("edit", "Digite o nome do banco para confirmar")
        self.confirm.textChanged.connect(self._update_state)
        for widget in (self.new_radio, self.replace_radio, self.target, self.replace_warning, self.confirm):
            target.addWidget(widget)
        layout.addWidget(target_box)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.status = StatusLine()
        layout.addWidget(self.status)

        self.restore_button = make_button("Restaurar", "restore", self._start, primary=True)
        self.close_button = make_button("Fechar", "quit", self.reject)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self.close_button)
        row.addWidget(self.restore_button)
        layout.addLayout(row)

        self._load_backups()

    def _add_backup(self, backup: BackupFile, index: int) -> QTreeWidgetItem:
        item = QTreeWidgetItem([backup.moment.strftime("%d/%m/%Y %H:%M"), backup.database, backup.engine,
                                history.format_size(backup.size)])
        item.setData(0, Qt.UserRole, backup)
        item.setToolTip(1, str(backup.path))
        self.backups.insertTopLevelItem(index, item)
        return item

    def _load_backups(self) -> None:
        self.backups.clear()
        backups = available_backups(load_settings())
        for index, backup in enumerate(backups):
            self._add_backup(backup, index)
        self.backups.setVisible(bool(backups))
        self.backups_empty.setVisible(not backups)
        for column in (0, 2, 3):
            self.backups.resizeColumnToContents(column)
        self._suggest_target()

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Selecione o backup", str(load_settings().backup_root),
                                              "Backups (*.sql *.bak)")
        if not path:
            return
        chosen = Path(path)
        engine = ENGINE_SQLSERVER if chosen.suffix.lower() == ".bak" else ENGINE_MYSQL
        database = re.sub(r"_\d{4}-\d{2}-\d{2}_\d{4}$", "", chosen.stem)
        stat = chosen.stat()
        item = self._add_backup(BackupFile(engine, database, datetime.fromtimestamp(stat.st_mtime), chosen,
                                           stat.st_size), 0)
        self.backups.show()
        self.backups_empty.hide()
        self.backups.setCurrentItem(item)

    def _selected(self) -> "BackupFile | None":
        items = self.backups.selectedItems()
        return items[0].data(0, Qt.UserRole) if items else None

    def _suggest_target(self) -> None:
        backup = self._selected()
        replacing = self.replace_radio.isChecked()
        self.replace_warning.setVisible(replacing)
        self.confirm.setVisible(replacing)
        self.confirm.clear()
        if backup is not None:
            self.target.setText(original_database(backup.database) if replacing
                                else suggested_target(backup.database))
        self._update_state()

    def _update_state(self) -> None:
        target = self.target.text().strip()
        problem = target_problem(target) if target else None
        if problem:
            self.status.show_message(history.LEVEL_WARNING, problem)
        elif self._task is None:
            self.status.hide()
        confirmed = not self.replace_radio.isChecked() or self.confirm.text().strip() == target
        self.restore_button.setEnabled(
            self._task is None and self._selected() is not None and bool(target) and not problem and confirmed)

    def _set_running(self, running: bool) -> None:
        self.progress.setVisible(running)
        for widget in (self.backups, self.browse_button, self.new_radio, self.replace_radio, self.target,
                       self.confirm, self.close_button):
            widget.setEnabled(not running)

    def _start(self) -> None:
        backup = self._selected()
        request = RestoreRequest(backup.engine, str(backup.path), self.target.text().strip(),
                                 self.replace_radio.isChecked())
        self._task = BackgroundCall(restore_backup, request)
        self._task.signals.finished.connect(self._on_finished)
        self._set_running(True)
        self._update_state()
        self.status.show_message(history.LEVEL_RUNNING, f"Restaurando no banco '{request.target}'...")
        QThreadPool.globalInstance().start(self._task)

    def _on_finished(self, message: str, error) -> None:
        self._task = None
        self._set_running(False)
        self._load_backups()  # a cópia de segurança passa a aparecer na lista
        if error is not None:
            self.status.show_message(history.LEVEL_ERROR, str(error))
        else:
            self.status.show_message(history.LEVEL_OK, message)

    def reject(self) -> None:
        if self._task is None:  # uma restauração em andamento não pode perder quem mostra o resultado
            super().reject()


# ---------------------------------------------------------------------------
# Assistente de primeira configuração
# ---------------------------------------------------------------------------
class SetupWizard(QDialog):
    """Do banco de dados à agenda, em três passos, para quem está implantando o programa."""
    STEPS = ("Bancos de dados", "Agenda e pasta", "Conferir")
    DAY_PRESETS = (("Todos os dias", DAYS), ("Segunda a sexta", DAYS[:5]))
    DEFAULT_TIME = QTime(22, 0)

    def __init__(self, scheduler: BackupScheduler, parent=None):
        super().__init__(parent)
        self.scheduler = scheduler
        self.setWindowTitle("Configuração guiada")
        self.setWindowIcon(QIcon(image("app_icon.ico")))
        self.setMinimumSize(680, 500)
        self._connection_tests = []
        self._settings = load_settings()
        box = get_secret_box(self)
        creds = load_credentials(box) if box else DbCredentials()

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        self.step_label = QLabel()
        step_font = QFont(self.step_label.font())
        step_font.setPointSize(12)
        step_font.setBold(True)
        self.step_label.setFont(step_font)
        layout.addWidget(self.step_label)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_databases(creds))
        self.pages.addWidget(self._build_schedule())
        self.pages.addWidget(self._build_summary())
        layout.addWidget(self.pages, 1)

        self.status = StatusLine()
        layout.addWidget(self.status)

        self.skip_button = make_button("Configurar depois", "quit", self.skip)
        self.back_button = make_button("Voltar", "back", self._back)
        self.next_button = make_button("Avançar", "next", self._next, primary=True)
        row = QHBoxLayout()
        row.addWidget(self.skip_button)
        row.addStretch()
        row.addWidget(self.back_button)
        row.addWidget(self.next_button)
        layout.addLayout(row)
        self._show_page(0)

    def _build_databases(self, creds: DbCredentials) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(muted_label("Marque os bancos que este computador deve copiar. "
                                     "Depois de preencher, use Testar conexão para conferir os dados."))
        groups = QHBoxLayout()
        groups.setSpacing(16)

        self.mysql_check = QCheckBox("Fazer backup do MySQL")
        self.mysql_check.setChecked(self._settings.use_mysql)
        box = QVBoxLayout()
        box.addWidget(self.mysql_check)
        mysql_box = QGroupBox("MySQL")
        fields = QVBoxLayout(mysql_box)
        self.mysql_host = MainWindow._add_field(fields, "server", "Host (padrão: localhost)")
        self.mysql_user = MainWindow._add_field(fields, "user", "Usuário")
        self.mysql_password = MainWindow._add_field(fields, "password", "Senha", True)
        self.mysql_port = MainWindow._add_field(fields, "port", f"Porta (padrão: {DEFAULT_MYSQL_PORT})")
        self.mysqldump_path = MainWindow._add_path_field(fields, "executable", "Caminho do mysqldump.exe",
                                                         self._browse_mysqldump)
        detected = find_mysqldump()
        self.mysqldump_path.setPlaceholderText(
            f"Automático: {detected}" if detected else "mysqldump.exe não encontrado - informe o caminho")
        self.mysqldump_path.setText(self._settings.mysqldump_path)
        fields.addStretch(1)
        self.mysql_status = StatusLine()
        fields.addWidget(self.mysql_status)
        self.mysql_test = make_button("Testar conexão", "test", self._test_mysql)
        fields.addWidget(self.mysql_test)
        box.addWidget(mysql_box, 1)
        groups.addLayout(box, 1)
        m = creds.mysql
        self.mysql_host.setText(m.host if m.host != "localhost" else "")
        self.mysql_user.setText(m.user)
        self.mysql_password.setText(m.password)
        self.mysql_port.setText(str(m.port) if m.port != DEFAULT_MYSQL_PORT else "")

        self.sql_check = QCheckBox("Fazer backup do SQL Server")
        self.sql_check.setChecked(self._settings.use_sql_server)
        box = QVBoxLayout()
        box.addWidget(self.sql_check)
        sql_box = QGroupBox("SQL Server")
        fields = QVBoxLayout(sql_box)
        self.sql_server = MainWindow._add_field(fields, "server", "Servidor (ex.: localhost\\SQLEXPRESS)")
        self.sql_user = MainWindow._add_field(fields, "user", "Usuário (vazio = autenticação do Windows)")
        self.sql_password = MainWindow._add_field(fields, "password", "Senha", True)
        self.sql_database = MainWindow._add_field(fields, "database", "Banco de dados")
        self.sql_port = MainWindow._add_field(fields, "port", "Porta (opcional)")
        fields.addStretch(1)
        self.sql_status = StatusLine()
        fields.addWidget(self.sql_status)
        self.sql_test = make_button("Testar conexão", "test", self._test_sql)
        fields.addWidget(self.sql_test)
        box.addWidget(sql_box, 1)
        groups.addLayout(box, 1)
        s = creds.sqlserver
        self.sql_server.setText(s.server)
        self.sql_user.setText(s.user)
        self.sql_password.setText(s.password)
        self.sql_database.setText(s.database)
        self.sql_port.setText(str(s.port) if s.port else "")

        self.mysql_check.toggled.connect(mysql_box.setEnabled)
        self.sql_check.toggled.connect(sql_box.setEnabled)
        mysql_box.setEnabled(self.mysql_check.isChecked())
        sql_box.setEnabled(self.sql_check.isChecked())
        layout.addLayout(groups, 1)
        return page

    def _build_schedule(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        # Com uma agenda já criada, o assistente não mexe nela nem na retenção
        self.schedule_box = QGroupBox("Agenda")
        schedule = QGridLayout(self.schedule_box)
        self.days_combo = QComboBox()
        for label, days in self.DAY_PRESETS:
            self.days_combo.addItem(label, days)
        self.time_edit = QTimeEdit(self.DEFAULT_TIME)
        self.time_edit.setDisplayFormat("HH:mm")
        self.retention_spin = QSpinBox()
        self.retention_spin.setRange(0, 3650)
        self.retention_spin.setSuffix(" dia(s)")
        self.retention_spin.setSpecialValueText("sempre (não apagar)")
        self.retention_spin.setValue(self._settings.retention_days)
        schedule.addWidget(QLabel("Fazer backup"), 0, 0)
        schedule.addWidget(self.days_combo, 0, 1)
        schedule.addWidget(QLabel("às"), 0, 2)
        schedule.addWidget(self.time_edit, 0, 3)
        schedule.addWidget(QLabel("Manter backups por"), 1, 0)
        schedule.addWidget(self.retention_spin, 1, 1)
        schedule.setColumnStretch(1, 1)
        schedule.addWidget(muted_label("Depois, a aba Agendamento permite criar mais regras e horários."),
                           2, 0, 1, 4)
        layout.addWidget(self.schedule_box)
        self.schedule_box.setVisible(not self._settings.rules)
        if self._settings.rules:
            layout.addWidget(muted_label(f"A agenda atual, com {len(self._settings.rules)} regra(s), "
                                         "será mantida. Para mudá-la, use a aba Agendamento."))

        folder_box = QGroupBox("Pasta de backups")
        folder = QVBoxLayout(folder_box)
        self.backup_dir = MainWindow._add_path_field(folder, "folder", f"Padrão: {BASE_DIR}", self._browse_folder)
        self.backup_dir.setText(self._settings.backup_dir)
        layout.addWidget(folder_box)
        layout.addStretch(1)
        return page

    def _build_summary(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.PlainText)
        layout.addWidget(self.summary)
        layout.addSpacing(12)
        self.run_now_check = QCheckBox("Executar o primeiro backup ao concluir")
        self.run_now_check.setChecked(True)
        layout.addWidget(self.run_now_check)
        layout.addStretch(1)
        return page

    def _browse_mysqldump(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Selecione o mysqldump.exe", self.mysqldump_path.text(),
                                              "mysqldump (mysqldump.exe);;Executáveis (*.exe)")
        if path:
            self.mysqldump_path.setText(str(Path(path)))

    def _browse_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Pasta de backups", self.backup_dir.text() or str(BASE_DIR))
        if path:
            self.backup_dir.setText(str(Path(path)))

    # ------------------------------------------------------------------ conexões
    def _mysql_creds(self) -> MySQLCredentials:
        return MySQLCredentials(host=self.mysql_host.text().strip() or "localhost",
                                user=self.mysql_user.text().strip(), password=self.mysql_password.text(),
                                port=parse_port(self.mysql_port.text(), DEFAULT_MYSQL_PORT))

    def _sql_creds(self) -> SQLServerCredentials:
        return SQLServerCredentials(server=self.sql_server.text().strip(), user=self.sql_user.text().strip(),
                                    password=self.sql_password.text(), database=self.sql_database.text().strip(),
                                    port=parse_port(self.sql_port.text(), None))

    def _mysql_valid(self, status: StatusLine) -> bool:
        if not MainWindow._ports_valid(status, self.mysql_port):
            return False
        if not self.mysql_user.text().strip():
            MainWindow._field_error(status, self.mysql_user, "Informe o usuário do MySQL.")
            return False
        return True

    def _sql_valid(self, status: StatusLine) -> bool:
        if not MainWindow._ports_valid(status, self.sql_port):
            return False
        if not self.sql_server.text().strip():
            MainWindow._field_error(status, self.sql_server, "Informe o servidor do SQL Server.")
            return False
        if not self.sql_database.text().strip():
            MainWindow._field_error(status, self.sql_database, "Informe o banco de dados.")
            return False
        return True

    def _test_mysql(self) -> None:
        if self._mysql_valid(self.mysql_status):
            start_connection_test(self._connection_tests, self.mysql_test, self.mysql_status, "MySQL",
                                  probe_mysql, self._mysql_creds())

    def _test_sql(self) -> None:
        if self._sql_valid(self.sql_status):
            start_connection_test(self._connection_tests, self.sql_test, self.sql_status, "SQL Server",
                                  probe_sqlserver, self._sql_creds())

    # ------------------------------------------------------------------ navegação
    def _show_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.step_label.setText(f"Passo {index + 1} de {len(self.STEPS)}: {self.STEPS[index]}")
        self.back_button.setEnabled(index > 0)
        self.next_button.setText("Concluir" if index == len(self.STEPS) - 1 else "Avançar")
        self.status.hide()

    def _back(self) -> None:
        self._show_page(self.pages.currentIndex() - 1)

    def _page_valid(self) -> bool:
        if self.pages.currentIndex() == 0:
            if not (self.mysql_check.isChecked() or self.sql_check.isChecked()):
                self.status.show_message(history.LEVEL_ERROR, "Marque pelo menos um banco de dados.")
                return False
            dump_path = self.mysqldump_path.text().strip()
            if self.mysql_check.isChecked():
                if not self._mysql_valid(self.status):
                    return False
                if not find_mysqldump(dump_path):
                    MainWindow._field_error(self.status, self.mysqldump_path, "Informe onde está o mysqldump.exe.")
                    return False
            return not self.sql_check.isChecked() or self._sql_valid(self.status)
        if self.pages.currentIndex() == 1:
            path = self.backup_dir.text().strip()
            try:
                if path:
                    Path(path).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                MainWindow._field_error(self.status, self.backup_dir,
                                        f"Não foi possível usar esta pasta ({exc.strerror}). Escolha outra.")
                return False
        return True

    def _next(self) -> None:
        if not self._page_valid():
            return
        index = self.pages.currentIndex()
        if index == len(self.STEPS) - 1:
            self._finish()
            return
        if index == 1:
            self.summary.setText(self._summary_text())
        self._show_page(index + 1)

    def _new_rule(self) -> ScheduleRule:
        return ScheduleRule(days=list(self.days_combo.currentData()), times=[self.time_edit.time().toString("HH:mm")])

    def _summary_text(self) -> str:
        engines = []
        if self.mysql_check.isChecked():
            engines.append("MySQL: todos os bancos de usuário")
        if self.sql_check.isChecked():
            engines.append(f"SQL Server: {self.sql_database.text().strip()}")
        if self._settings.rules:
            schedule = f"{len(self._settings.rules)} regra(s) atual(is)"
        else:
            days = self.retention_spin.value()
            schedule = f"{self._new_rule().describe()}; manter backups por {days} dia(s)" if days \
                else f"{self._new_rule().describe()}; manter todos os backups"
        folder = self.backup_dir.text().strip() or str(BASE_DIR)
        return "\n\n".join(("Bancos\n" + "\n".join(engines), f"Agenda\n{schedule}", f"Pasta de backups\n{folder}"))

    def _finish(self) -> None:
        box = get_secret_box(self)
        if box is None:
            return
        creds = load_credentials(box)
        if self.mysql_check.isChecked():
            creds.mysql = self._mysql_creds()
        if self.sql_check.isChecked():
            creds.sqlserver = self._sql_creds()
        settings = load_settings()
        settings.use_mysql = self.mysql_check.isChecked()
        settings.use_sql_server = self.sql_check.isChecked()
        settings.mysqldump_path = self.mysqldump_path.text().strip()
        settings.backup_dir = self.backup_dir.text().strip()
        settings.setup_done = True
        if not settings.rules:
            settings.rules = [self._new_rule()]
            settings.retention_days = self.retention_spin.value()
        try:
            save_credentials(creds, box)
            save_settings(settings)
        except OSError as exc:
            error_log.error(f"Erro ao salvar a configuração guiada: {exc}")
            self.status.show_message(history.LEVEL_ERROR, f"Não foi possível salvar as configurações: {exc}")
            return
        main_log.info("Configuração guiada concluída.")
        self.scheduler.reload(settings)
        if self.run_now_check.isChecked():
            self.scheduler.trigger("manual")
        self.accept()

    def skip(self) -> None:
        settings = load_settings()
        settings.setup_done = True  # não insiste a cada login; a aba Sistema reabre o assistente
        try:
            save_settings(settings)
        except OSError as exc:
            error_log.error(f"Erro ao salvar settings.ini: {exc}")
        self.reject()

    def closeEvent(self, event) -> None:
        self.skip()
        super().closeEvent(event)
