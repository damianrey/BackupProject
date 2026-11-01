"""Telas da aplicação: login, registro de usuário e janela principal."""

import os
from pathlib import Path

from PyQt5.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, QTime, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QPainter
from PyQt5.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QRadioButton, QSizePolicy, QSpinBox, QStackedWidget, QTimeEdit,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import __version__, history, startup, users
from .backup import connect_mysql, connect_sqlserver, find_mysqldump
from .icons import ACCENT, DANGER, MUTED, ON_ACCENT, SUCCESS, WARNING, icon, tab_icon
from .log_config import error_log, main_log
from .paths import BASE_DIR, LOGS_DIR, image
from .scheduler import BackupScheduler
from .security import CryptoKeyError, SecretBox, load_key
from .settings import (
    DAY_LABELS, DAY_SHORT, DAYS, DEFAULT_MYSQL_PORT, MIN_INTERVAL_MINUTES, MODE_INTERVAL, MODE_TIMES,
    DbCredentials, MySQLCredentials, ScheduleRule, SQLServerCredentials, day_of, format_interval,
    load_credentials, load_settings, parse_port, save_credentials, save_settings, upcoming_runs,
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


def make_field(icon_name: str, placeholder: str, password: bool = False) -> QLineEdit:
    """Campo de texto com o ícone dentro, à esquerda (e botão de mostrar senha)."""
    edit = QLineEdit()
    edit.setPlaceholderText(placeholder)
    edit.setToolTip(placeholder)
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

    def show_message(self, level: str, text: str) -> None:
        icon_name, color = HEALTH_STYLES[level]
        self._icon.setPixmap(icon(icon_name, color, color).pixmap(16, 16))
        self._text.setText(text)
        self._text.setToolTip(text)
        self.show()
        self._timer.stop()
        if self._auto_hide and level == history.LEVEL_OK:  # avisos ficam até serem lidos
            self._timer.start(self.HIDE_AFTER_MS)

    def text(self) -> str:
        return self._text.text()


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


class _ConnectionTestSignals(QObject):
    finished = pyqtSignal(str, object)  # versão do servidor, exceção (ou None)


class ConnectionTest(QRunnable):
    """Testa uma conexão fora da thread da interface, que congelaria até o tempo limite."""

    def __init__(self, probe, creds):
        super().__init__()
        self.probe = probe
        self.creds = creds
        self.signals = _ConnectionTestSignals()

    def run(self) -> None:  # executa na thread do pool
        try:
            version, error = self.probe(self.creds), None
        except Exception as exc:
            version, error = "", exc
        self.signals.finished.emit(version, error)


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
        self.setFixedSize(360, 200)
        self.setWindowIcon(QIcon(image("app_icon.ico")))

        self.new_username_entry = make_field("user", "Novo usuário")
        self.new_password_entry = make_field(
            "password", f"Nova senha (mínimo {users.MIN_PASSWORD_LENGTH} caracteres)", password=True)
        self.confirm_password_entry = make_field("password", "Confirme a senha", password=True)
        self.confirm_password_entry.returnPressed.connect(self.register_user)

        self.register_button = make_button("Registrar", "register", self.register_user, primary=True)
        self.register_button.setAutoDefault(False)  # o Enter já é tratado pelo returnPressed

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)
        layout.addWidget(self.new_username_entry)
        layout.addWidget(self.new_password_entry)
        layout.addWidget(self.confirm_password_entry)
        layout.addWidget(self.register_button)

    def register_user(self) -> None:
        try:
            users.register_user(
                self.new_username_entry.text(),
                self.new_password_entry.text(),
                self.confirm_password_entry.text(),
            )
        except users.UserError as exc:
            QMessageBox.warning(self, "Registro", str(exc))
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

    def accept(self) -> None:
        problem = self.rule().validate()
        if problem:
            QMessageBox.warning(self, "Regra de agendamento", problem)
            return
        super().accept()


# ---------------------------------------------------------------------------
# Janela principal
# ---------------------------------------------------------------------------
def result_icon(success: bool) -> QIcon:
    return icon("ok", SUCCESS, SUCCESS) if success else icon("error", DANGER, DANGER)


class MainWindow(QMainWindow):
    def __init__(self, scheduler: BackupScheduler):
        super().__init__()
        self.scheduler = scheduler

        self.setWindowTitle("DBKeeper")
        self.setWindowIcon(QIcon(image("app_icon.ico")))
        self.setMinimumSize(780, 560)
        self._connection_tests = []  # mantém as tarefas vivas até o fim

        self.home_tab = self._build_home_tab()
        self.settings_tab = self._build_connections_tab()
        self.schedule_tab = self._build_schedule_tab()
        self.system_tab = self._build_system_tab()

        # Barra lateral só com ícones (botões exclusivos) + páginas empilhadas
        self.pages = QStackedWidget()
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setAttribute(Qt.WA_StyledBackground, True)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(0, 8, 0, 8)
        nav.setSpacing(4)
        self.nav_buttons = QButtonGroup(self)
        self.nav_buttons.idClicked.connect(self.pages.setCurrentIndex)
        for index, (widget, icon_name, tooltip) in enumerate((
            (self.home_tab, "home", "Início"),
            (self.settings_tab, "connections", "Conexões"),
            (self.schedule_tab, "schedule", "Agendamento"),
            (self.system_tab, "system", "Sistema"),
        )):
            self.pages.addWidget(widget)
            button = QToolButton()
            button.setIcon(tab_icon(icon_name))
            button.setIconSize(QSize(24, 24))
            button.setToolTip(tooltip)
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
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(30_000)
        self._status_timer.timeout.connect(self.refresh_status)

    # ------------------------------------------------------------------ eventos
    def showEvent(self, event) -> None:
        # Sempre exibe o que está salvo em disco
        self.load_connections()
        self.load_schedule()
        self.load_system()
        self.refresh_status()
        self._status_timer.start()
        super().showEvent(event)

    def closeEvent(self, event) -> None:
        # Fechar apenas esconde: o programa continua na bandeja executando os backups
        event.ignore()
        self._status_timer.stop()
        self.hide()

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
                    history.format_size(database.size) if database.success else database.message,
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
        try:
            creds = self._mysql_from_fields()
        except ValueError:
            QMessageBox.warning(self, "Testar conexão MySQL", "Porta do MySQL inválida.")
            return
        if not creds.user:
            QMessageBox.information(self, "Testar conexão MySQL", "Preencha ao menos o usuário do MySQL!")
            return
        self._start_connection_test(self.test_mysql_button, self.mysql_status, "MySQL", probe_mysql, creds)

    # Função para testar a conexão com SQL Server
    def test_sql_server_connection(self) -> None:
        try:
            creds = self._sqlserver_from_fields()
        except ValueError:
            QMessageBox.warning(self, "Testar conexão SQL Server", "Porta do SQL Server inválida.")
            return
        if not creds.server:
            QMessageBox.information(self, "Testar conexão SQL Server", "Preencha ao menos o servidor!")
            return
        self._start_connection_test(self.test_sql_server_button, self.sql_status, "SQL Server",
                                    probe_sqlserver, creds)

    def _start_connection_test(self, button: QPushButton, status: StatusLine, engine: str, probe, creds) -> None:
        label = button.text()
        button.setEnabled(False)
        button.setText("Testando...")
        status.show_message(history.LEVEL_RUNNING, f"Conectando ao {engine}...")
        task = ConnectionTest(probe, creds)

        def finished(version: str, error) -> None:
            self._connection_tests.remove(task)
            button.setText(label)
            button.setEnabled(True)
            if error is not None:
                status.show_message(history.LEVEL_ERROR, f"Erro ao conectar ao {engine}: {error}")
            else:
                details = f" Versão do servidor: {version}" if version else ""
                status.show_message(history.LEVEL_OK, f"Conexão bem-sucedida.{details}")

        task.signals.finished.connect(finished)
        self._connection_tests.append(task)
        QThreadPool.globalInstance().start(task)

    # Função para salvar as credenciais das conexões aos bancos de dados
    def save_credentials(self) -> None:
        try:
            creds = DbCredentials(self._mysql_from_fields(), self._sqlserver_from_fields())
        except ValueError:
            QMessageBox.warning(self, "Salvar", "Verifique as portas informadas (1 a 65535).")
            return
        dump_path = self.mysqldump_path.text().strip()
        if dump_path and not Path(dump_path).is_file():
            QMessageBox.warning(self, "Salvar", f"O arquivo '{dump_path}' não existe.")
            return

        box = get_secret_box(self)
        if box is None:
            return
        try:
            save_credentials(creds, box)
            settings = load_settings()
            settings.mysqldump_path = dump_path
            settings.sqlserver_backup_dir = self.sqlserver_backup_dir.text().strip()
            save_settings(settings)
        except OSError as exc:
            error_log.error(f"Erro ao salvar as configurações de conexão: {exc}")
            QMessageBox.critical(self, "Erro", f"Não foi possível salvar as configurações:\n{exc}")
            return
        main_log.info("Configurações de conexão salvas.")
        self.connections_status.show_message(history.LEVEL_OK, "Configurações salvas.")

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

    def save_schedule(self, show_message: bool = True) -> bool:
        settings = load_settings()  # preserva as demais seções (caminhos)
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
            self.next_run_label.setText("Nenhum agendado")
            self.next_run_detail.setText("")
        self._render_history(entries)

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

        folder_box = QGroupBox("Pasta de backups")
        folder = QVBoxLayout(folder_box)
        self.backup_dir = self._add_path_field(folder, "folder", f"Padrão: {BASE_DIR}", self._browse_backup_dir)
        self.save_system_button = make_button("Salvar pasta", "save", self.save_system, primary=True)
        self.system_status = StatusLine(auto_hide=True)
        folder.addLayout(self._footer(self.save_system_button, status=self.system_status))
        layout.addWidget(folder_box)

        tools_box = QGroupBox("Ferramentas")
        tools = QHBoxLayout(tools_box)
        for label, icon_name, slot in (
            ("Pasta de backups", "browse", lambda: self._open(load_settings().backup_root)),
            ("Pasta de logs", "logs", lambda: self._open(LOGS_DIR)),
            ("Novo usuário", "register", lambda: RegistrationScreen(self).exec_()),
        ):
            tools.addWidget(make_button(label, icon_name, slot))
        tools.addStretch(1)
        layout.addWidget(tools_box)

        layout.addStretch(1)
        info = muted_label(f"DBKeeper {__version__}  •  {BASE_DIR}")
        layout.addWidget(info)
        return tab

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
        self.backup_dir.setText(load_settings().backup_dir)

    def _on_autostart_toggled(self, enabled: bool) -> None:
        try:
            startup.set_enabled(enabled)
        except OSError as exc:
            error_log.error(f"Erro ao alterar a inicialização automática: {exc}")
            QMessageBox.critical(self, "Erro", f"Não foi possível alterar a inicialização automática:\n{exc}")
            self.load_system()
            return
        main_log.info("Inicialização com o Windows " + ("ativada." if enabled else "desativada."))

    def save_system(self) -> None:
        path = self.backup_dir.text().strip()
        if path:
            try:
                Path(path).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                QMessageBox.warning(self, "Pasta de backups", f"Não foi possível usar '{path}':\n{exc}")
                return
        settings = load_settings()
        settings.backup_dir = path
        try:
            save_settings(settings)
        except OSError as exc:
            QMessageBox.critical(self, "Erro", f"Não foi possível salvar:\n{exc}")
            return
        main_log.info(f"Pasta de backups alterada para '{settings.backup_root}'.")
        self.system_status.show_message(history.LEVEL_OK, f"Os backups serão gravados em {settings.backup_root}")
