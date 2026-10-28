"""Telas da aplicação: login, registro de usuário e janela principal."""

import os
from contextlib import contextmanager
from pathlib import Path

from PyQt5.QtCore import QSize, Qt, QTime, QTimer, pyqtSignal
from PyQt5.QtGui import QIcon, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QGraphicsOpacityEffect, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QTabWidget, QTimeEdit,
    QVBoxLayout, QWidget,
)

from . import __version__, startup, users
from .backup import connect_mysql, connect_sqlserver, find_mysqldump
from .log_config import error_log, main_log
from .paths import BASE_DIR, LOGS_DIR, image
from .scheduler import BackupScheduler
from .security import CryptoKeyError, SecretBox, load_key
from .settings import (
    DAY_LABELS, DAYS, DEFAULT_MYSQL_PORT, DbCredentials, MySQLCredentials, SQLServerCredentials,
    day_of, load_credentials, load_settings, parse_port, save_credentials, save_settings,
)

ROUNDED_EDIT_STYLE = (
    "QLineEdit { border: 2px solid gray; border-radius: 10px; padding: 0 8px; "
    "width: 200px; height: 30px; }"
)


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------
def icon_label(name: str, size: int = 40, tooltip: str = "") -> QLabel:
    label = QLabel()
    label.setPixmap(QIcon(image(name)).pixmap(size, size))
    label.setToolTip(tooltip)
    return label


@contextmanager
def busy_cursor():
    QApplication.setOverrideCursor(Qt.WaitCursor)
    try:
        yield
    finally:
        QApplication.restoreOverrideCursor()


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
        self.setWindowTitle("Tela de Login")
        self.setFixedSize(400, 240)
        self.setWindowIcon(QIcon(image("system_icon.png")))

        # ícone do usuário + campo de usuário
        self.username_label = icon_label("user_icon.png", 56)
        self.username_entry = QLineEdit()
        self.username_entry.setPlaceholderText("Usuário")
        self.username_entry.setStyleSheet(ROUNDED_EDIT_STYLE)
        self.username_entry.returnPressed.connect(self.password_entry_focus)

        # Ícone da senha + campo de senha
        self.password_label = icon_label("password_icon.png", 56)
        self.password_entry = QLineEdit()
        self.password_entry.setPlaceholderText("Senha")
        self.password_entry.setEchoMode(QLineEdit.Password)
        self.password_entry.setStyleSheet(ROUNDED_EDIT_STYLE)
        self.password_entry.returnPressed.connect(self.login)

        # Botão para login
        self.login_button = QPushButton(" ")
        self.login_button.setIcon(QIcon(image("login_icon.png")))
        self.login_button.setIconSize(QSize(32, 32))
        self.login_button.clicked.connect(self.login)

        # Botão de primeiro cadastro: só aparece enquanto não existir nenhum usuário.
        # (O antigo atalho Ctrl+R permitia que qualquer pessoa criasse um usuário.)
        self.first_user_button = QPushButton("Criar primeiro usuário")
        self.first_user_button.clicked.connect(self.open_first_registration)

        layout = QVBoxLayout()
        layout.addStretch(1)
        user_layout = QHBoxLayout()
        user_layout.addWidget(self.username_label)
        user_layout.addWidget(self.username_entry)
        layout.addLayout(user_layout)
        pass_layout = QHBoxLayout()
        pass_layout.addWidget(self.password_label)
        pass_layout.addWidget(self.password_entry)
        layout.addLayout(pass_layout)
        layout.addWidget(self.login_button, alignment=Qt.AlignHCenter)
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
            QMessageBox.critical(self, "Erro", "Usuario ou senha incorretos")
            self._reset_fields()

    def open_first_registration(self) -> None:
        if users.has_users():  # proteção extra: só o primeiro cadastro é livre
            self.first_user_button.hide()
            return
        if RegistrationScreen(self).exec_() == QDialog.Accepted:
            QMessageBox.information(self, "Registro", "Usuário criado. Faça o login.")
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
        self.setWindowTitle("Tela de Registro")
        self.setFixedSize(400, 220)
        self.setWindowIcon(QIcon(image("system_icon.png")))

        self.new_username_entry = QLineEdit()
        self.new_username_entry.setPlaceholderText("Novo usuário")

        self.new_password_entry = QLineEdit()
        self.new_password_entry.setPlaceholderText(f"Nova senha (mínimo {users.MIN_PASSWORD_LENGTH} caracteres)")
        self.new_password_entry.setEchoMode(QLineEdit.Password)

        self.confirm_password_entry = QLineEdit()
        self.confirm_password_entry.setPlaceholderText("Confirme a senha")
        self.confirm_password_entry.setEchoMode(QLineEdit.Password)
        self.confirm_password_entry.returnPressed.connect(self.register_user)

        self.register_button = QPushButton("Registrar")
        self.register_button.clicked.connect(self.register_user)

        layout = QVBoxLayout(self)
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
# Janela principal
# ---------------------------------------------------------------------------
class _BackgroundTab(QWidget):
    """Aba com uma imagem semitransparente ao fundo que acompanha o redimensionamento."""

    def __init__(self, image_name: str, size: int = 350, opacity: float = 0.7):
        super().__init__()
        self._background = QLabel(self)
        self._background.setPixmap(QPixmap(image(image_name)).scaled(size, size, Qt.KeepAspectRatio,
                                                                      Qt.SmoothTransformation))
        self._background.setAlignment(Qt.AlignRight | Qt.AlignTop)
        self._background.setAttribute(Qt.WA_TransparentForMouseEvents)
        effect = QGraphicsOpacityEffect(self._background)
        effect.setOpacity(opacity)
        self._background.setGraphicsEffect(effect)
        self._background.lower()

    def resizeEvent(self, event) -> None:
        self._background.setGeometry(0, 25, self.width(), max(0, self.height() - 50))
        super().resizeEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, scheduler: BackupScheduler):
        super().__init__()
        self.scheduler = scheduler

        self.setWindowTitle("BackupProject")
        self.setWindowIcon(QIcon(image("system_icon.png")))
        self.setMinimumSize(780, 560)

        # Abas na lateral esquerda, só com ícones
        self.tabs = QTabWidget(self)
        self.tabs.setTabPosition(QTabWidget.West)
        self.tabs.setIconSize(QSize(32, 32))

        self.settings_tab = self._build_connections_tab()
        self.schedule_tab = self._build_schedule_tab()
        self.system_tab = self._build_system_tab()

        for widget, icon, tooltip in (
            (self.settings_tab, "config.png", "Conexões"),
            (self.schedule_tab, "schedule.png", "Agendamento"),
            (self.system_tab, "monitor.png", "Sistema"),
        ):
            index = self.tabs.addTab(widget, QIcon(image(icon)), "")
            self.tabs.setTabToolTip(index, tooltip)
        self.setCentralWidget(self.tabs)

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
    def _add_field(grid: QGridLayout, row: int, icon: str, placeholder: str,
                   password: bool = False) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setToolTip(placeholder)
        if password:
            edit.setEchoMode(QLineEdit.Password)
        grid.addWidget(icon_label(icon, 40, placeholder), row, 0)
        grid.addWidget(edit, row, 1, 1, 2)
        return edit

    @staticmethod
    def _add_path_field(grid: QGridLayout, row: int, icon: str, placeholder: str, on_browse) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setToolTip(placeholder)
        browse = QPushButton("...")
        browse.setMaximumWidth(40)
        browse.clicked.connect(on_browse)
        grid.addWidget(icon_label(icon, 40, placeholder), row, 0)
        grid.addWidget(edit, row, 1)
        grid.addWidget(browse, row, 2)
        return edit

    # ================================================================== Aba Conexões
    def _build_connections_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        groups = QHBoxLayout()

        # MySQL
        mysql_box = QGroupBox("MySQL")
        grid = QGridLayout(mysql_box)
        self.mysql_host = self._add_field(grid, 0, "server_icon.png", "MySQL Host (padrão: localhost)")
        self.mysql_username = self._add_field(grid, 1, "db_user.png", "MySQL Username")
        self.mysql_password = self._add_field(grid, 2, "database_password2.png", "MySQL Password", True)
        self.mysql_port = self._add_field(grid, 3, "port_icon.png", f"MySQL Port (padrão: {DEFAULT_MYSQL_PORT})")
        self.mysqldump_path = self._add_path_field(grid, 4, "config.png", "Caminho do mysqldump.exe",
                                                   self._browse_mysqldump)
        self.test_mysql_button = QPushButton("Testar Conexão MySQL")
        self.test_mysql_button.clicked.connect(self.test_mysql_connection)
        grid.addWidget(self.test_mysql_button, 5, 0, 1, 3)
        groups.addWidget(mysql_box)

        # SQL Server
        sql_box = QGroupBox("SQL Server")
        grid = QGridLayout(sql_box)
        self.sql_server = self._add_field(grid, 0, "server_icon.png", "SQL Server (ex.: localhost\\SQLEXPRESS)")
        self.sql_username = self._add_field(grid, 1, "db_user.png", "SQL Username (vazio = autenticação do Windows)")
        self.sql_password = self._add_field(grid, 2, "database_password2.png", "SQL Password", True)
        self.sql_database = self._add_field(grid, 3, "database_icon.png", "SQL Database")
        self.sql_port = self._add_field(grid, 4, "port_icon.png", "SQL Port (opcional)")
        self.sqlserver_backup_dir = self._add_path_field(
            grid, 5, "config.png", "Pasta de backup no servidor (opcional)", self._browse_sqlserver_dir)
        self.test_sql_server_button = QPushButton("Testar Conexão SQL Server")
        self.test_sql_server_button.clicked.connect(self.test_sql_server_connection)
        grid.addWidget(self.test_sql_server_button, 6, 0, 1, 3)
        groups.addWidget(sql_box)

        layout.addLayout(groups)

        note = QLabel(
            "A pasta de backup do SQL Server é o caminho visto pelo próprio servidor SQL; "
            "a conta do serviço precisa ter permissão de escrita nela."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")
        layout.addWidget(note)
        layout.addStretch(1)

        self.save_button = QPushButton("Salvar Configurações")
        self.save_button.clicked.connect(self.save_credentials)
        layout.addWidget(self.save_button)
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
        try:
            with busy_cursor():
                connect_mysql(creds).close()
        except Exception as exc:
            QMessageBox.critical(self, "Erro", f"Erro ao conectar ao MySQL: {exc}")
            return
        QMessageBox.information(self, "Testar conexão MySQL", "Conexão com o MySQL bem-sucedida!")

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
        try:
            with busy_cursor():
                connect_sqlserver(creds).close()
        except Exception as exc:
            QMessageBox.critical(self, "Erro", f"Erro ao conectar ao SQL Server: {exc}")
            return
        QMessageBox.information(self, "Testar conexão SQL Server", "Conexão com o SQL Server bem-sucedida!")

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
        QMessageBox.information(self, "Salvar", "Configurações salvas com sucesso!")

    # ================================================================== Aba Agendamento
    def _build_schedule_tab(self) -> QWidget:
        tab = _BackgroundTab("schedule2.png")
        layout = QVBoxLayout(tab)

        self.day_checkboxes = {}
        self.time_edits = {}
        for day in DAYS:
            checkbox = QCheckBox(DAY_LABELS[day])
            time_edit = QTimeEdit()
            time_edit.setDisplayFormat("HH:mm")  # impossível digitar um horário inválido
            time_edit.setMaximumWidth(100)
            time_edit.setEnabled(False)
            checkbox.toggled.connect(time_edit.setEnabled)
            self.day_checkboxes[day] = checkbox
            self.time_edits[day] = time_edit

            row = QHBoxLayout()
            checkbox.setMinimumWidth(90)
            row.addWidget(checkbox)
            row.addWidget(time_edit)
            row.addWidget(icon_label("clock.png", 20))
            row.addStretch()
            layout.addLayout(row)

        # Bancos a copiar
        self.checkbox_sql_server = QCheckBox("Backup SQL Server")
        self.checkbox_mysql = QCheckBox("Backup MySQL")
        engines = QHBoxLayout()
        engines.addWidget(self.checkbox_sql_server)
        engines.addWidget(self.checkbox_mysql)
        engines.addStretch()
        layout.addLayout(engines)

        # Status
        self.next_run_label = QLabel()
        self.last_run_label = QLabel()
        self.last_run_label.setWordWrap(True)
        layout.addWidget(self.next_run_label)
        layout.addWidget(self.last_run_label)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        self.save_button_schedule = QPushButton("Salvar Configurações")
        self.save_button_schedule.clicked.connect(lambda: self.save_schedule(show_message=True))
        self.run_now_button = QPushButton("Executar backup agora")
        self.run_now_button.setToolTip("Salva a agenda atual e executa o backup em segundo plano.")
        self.run_now_button.clicked.connect(self.run_backup_now)
        buttons.addWidget(self.save_button_schedule)
        buttons.addWidget(self.run_now_button)
        layout.addLayout(buttons)
        return tab

    def load_schedule(self) -> None:
        settings = load_settings()
        for day in DAYS:
            day_schedule = settings.days[day]
            self.day_checkboxes[day].setChecked(day_schedule.enabled)
            self.time_edits[day].setEnabled(day_schedule.enabled)
            self.time_edits[day].setTime(QTime.fromString(day_schedule.time, "HH:mm"))
        self.checkbox_mysql.setChecked(settings.use_mysql)
        self.checkbox_sql_server.setChecked(settings.use_sql_server)

    def save_schedule(self, show_message: bool = True) -> bool:
        settings = load_settings()  # preserva as demais seções (caminhos)
        for day in DAYS:
            settings.days[day].enabled = self.day_checkboxes[day].isChecked()
            settings.days[day].time = self.time_edits[day].time().toString("HH:mm")
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
                QMessageBox.warning(self, "Agendamento",
                                    "Agenda salva, mas nenhum banco (MySQL/SQL Server) está selecionado.")
            else:
                QMessageBox.information(self, "Agendamento", "Agenda salva: " + (", ".join(jobs) or "nenhum dia ativo"))
        return True

    def run_backup_now(self) -> None:
        if not self.save_schedule(show_message=False):
            return
        if not self.scheduler.trigger("manual"):
            QMessageBox.information(self, "Backup", "Já existe um backup em andamento.")

    def refresh_status(self) -> None:
        next_run = self.scheduler.next_run()
        if next_run:
            self.next_run_label.setText(
                f"Próximo backup: {DAY_LABELS[day_of(next_run)]}, {next_run.strftime('%d/%m/%Y %H:%M')}")
        else:
            self.next_run_label.setText("Próximo backup: nenhum agendado")

        report = self.scheduler.last_report
        if self.scheduler.is_running:
            self.last_run_label.setText("Backup em andamento...")
        elif report is not None:
            when = (report.finished or report.started).strftime("%d/%m/%Y %H:%M")
            status = "OK" if report.success else "COM FALHAS"
            self.last_run_label.setText(f"Último backup ({when}) - {status}: {report.summary()}")
        else:
            self.last_run_label.setText("")
        self.run_now_button.setEnabled(not self.scheduler.is_running)

    # ================================================================== Aba Sistema
    def _build_system_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        startup_box = QGroupBox("Inicialização")
        startup_layout = QVBoxLayout(startup_box)
        self.autostart_checkbox = QCheckBox("Iniciar com o Windows (em segundo plano, na bandeja)")
        self.autostart_checkbox.setEnabled(startup.is_supported())
        self.autostart_checkbox.toggled.connect(self._on_autostart_toggled)
        startup_layout.addWidget(self.autostart_checkbox)
        layout.addWidget(startup_box)

        folder_box = QGroupBox("Pasta de backups")
        grid = QGridLayout(folder_box)
        self.backup_dir = self._add_path_field(grid, 0, "config.png", f"Padrão: {BASE_DIR}", self._browse_backup_dir)
        self.save_system_button = QPushButton("Salvar pasta")
        self.save_system_button.clicked.connect(self.save_system)
        grid.addWidget(self.save_system_button, 1, 0, 1, 3)
        layout.addWidget(folder_box)

        tools_box = QGroupBox("Ferramentas")
        tools = QHBoxLayout(tools_box)
        open_backups = QPushButton("Abrir pasta de backups")
        open_backups.clicked.connect(lambda: self._open(load_settings().backup_root))
        open_logs = QPushButton("Abrir pasta de logs")
        open_logs.clicked.connect(lambda: self._open(LOGS_DIR))
        register = QPushButton("Registrar novo usuário")
        register.clicked.connect(lambda: RegistrationScreen(self).exec_())
        for button in (open_backups, open_logs, register):
            tools.addWidget(button)
        layout.addWidget(tools_box)

        layout.addStretch(1)
        info = QLabel(f"BackupProject {__version__}  •  {BASE_DIR}")
        info.setStyleSheet("color: gray;")
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
        QMessageBox.information(self, "Pasta de backups", f"Os backups serão gravados em:\n{settings.backup_root}")
