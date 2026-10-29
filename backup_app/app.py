"""Controlador da aplicação: bandeja do sistema, instância única e ciclo de vida."""

import hashlib
import sys

from PyQt5.QtCore import QCoreApplication, QObject, Qt, QTimer
from PyQt5.QtGui import QIcon
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from PyQt5.QtWidgets import QAction, QApplication, QMenu, QMessageBox, QSystemTrayIcon

from . import startup
from .backup import BackupReport
from .icons import apply_theme, icon
from .locks import InterProcessLock
from .log_config import error_log, main_log
from .paths import APP_LOCK_FILE, BASE_DIR, image
from .scheduler import BackupScheduler
from .settings import DAY_LABELS, day_of, load_settings
from .ui import LoginScreen, MainWindow

APP_NAME = "DBKeeper"


def _server_name() -> str:
    # Um nome por instalação (pasta), para não conflitar com cópias em outras pastas
    digest = hashlib.sha1(str(BASE_DIR).lower().encode("utf-8")).hexdigest()[:12]
    return f"{APP_NAME}-{digest}"


def _set_windows_app_id() -> None:
    """Sem isso o Windows agrupa a janela sob o python.exe e mostra o ícone do Python na barra de tarefas."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_NAME}.App")
    except (AttributeError, OSError):
        pass


def notify_running_instance() -> bool:
    """Pede à instância que já está aberta para mostrar a tela de login."""
    socket = QLocalSocket()
    socket.connectToServer(_server_name())
    if not socket.waitForConnected(1000):
        return False
    socket.write(b"show")
    socket.flush()
    socket.waitForBytesWritten(1000)
    socket.disconnectFromServer()
    return True


class BackupApplication(QObject):
    TRAY_RETRY_MS = 2000
    TRAY_MAX_RETRIES = 30  # ~1 minuto esperando a bandeja ficar disponível no boot

    def __init__(self, app: QApplication, start_hidden: bool):
        super().__init__()
        self.app = app
        self.tray = None
        self._tray_menu = None
        self._tray_retries = 0
        self._quit_pending = False

        self.scheduler = BackupScheduler(self)
        self.main_window = MainWindow(self.scheduler)
        self.login_screen = LoginScreen()
        self.login_screen.authenticated.connect(self._on_authenticated)

        self.scheduler.backup_started.connect(self._on_backup_started)
        self.scheduler.backup_finished.connect(self._on_backup_finished)
        self.scheduler.schedule_changed.connect(self._update_tray_tooltip)

        self._server = QLocalServer(self)
        self._start_ipc_server()
        self._refresh_startup_entry()

        # Carrega a agenda e inicia o agendador (não depende de login)
        self.scheduler.reload(load_settings())
        self.scheduler.start()

        self._setup_tray()
        if not start_hidden:
            self.show_login()
        main_log.info("Programa iniciado" + (" com o Windows." if start_hidden else "."))

    # ------------------------------------------------------------------ instância única
    def _start_ipc_server(self) -> None:
        name = _server_name()
        QLocalServer.removeServer(name)  # seguro: temos o lock de instância
        if not self._server.listen(name):
            error_log.error(f"Não foi possível iniciar o servidor local: {self._server.errorString()}")
            return
        self._server.newConnection.connect(self._on_ipc_connection)

    def _on_ipc_connection(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            socket.disconnected.connect(socket.deleteLater)
        self.show_login()

    def _refresh_startup_entry(self) -> None:
        """Se o programa mudou de pasta/interpretador, atualiza o comando do registro."""
        try:
            if startup.is_outdated():
                startup.set_enabled(True)
                main_log.info("Comando de inicialização automática atualizado.")
        except OSError as exc:
            error_log.error(f"Erro ao atualizar a inicialização automática: {exc}")

    # ------------------------------------------------------------------ bandeja
    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            if self._tray_retries < self.TRAY_MAX_RETRIES:
                self._tray_retries += 1
                QTimer.singleShot(self.TRAY_RETRY_MS, self._setup_tray)
                return
            error_log.error("A bandeja do sistema não está disponível; exibindo a tela de login.")
            self.show_login()
            return

        self.tray = QSystemTrayIcon(QIcon(image("app_icon.ico")), self)

        open_action = QAction(icon("open"), "Abrir", self)
        open_action.triggered.connect(self.show_login)
        backup_action = QAction(icon("run"), "Executar backup", self)
        backup_action.triggered.connect(self.backup_now)
        close_action = QAction(icon("quit"), "Sair", self)
        close_action.triggered.connect(self.request_quit)

        self._tray_menu = QMenu()
        self._tray_menu.addAction(open_action)
        self._tray_menu.addAction(backup_action)
        self._tray_menu.addSeparator()
        self._tray_menu.addAction(close_action)
        self.tray.setContextMenu(self._tray_menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()
        self._update_tray_tooltip()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_login()

    def _notify(self, title: str, message: str, icon=QSystemTrayIcon.Information) -> None:
        if self.tray is not None and self.tray.isVisible():
            self.tray.showMessage(title, message, icon, 8000)

    def _update_tray_tooltip(self) -> None:
        if self.tray is None:
            return
        if self.scheduler.is_running:
            text = "Backup em andamento..."
        else:
            next_run = self.scheduler.next_run()
            text = (f"Próximo backup: {DAY_LABELS[day_of(next_run)]} {next_run.strftime('%H:%M')}"
                    if next_run else "Nenhum backup agendado")
        self.tray.setToolTip(f"{APP_NAME}\n{text}")

    # ------------------------------------------------------------------ ações
    def show_login(self) -> None:
        if self.main_window.isVisible():
            window = self.main_window
        else:
            window = self.login_screen
            window.show()
        window.showNormal()
        window.raise_()
        window.activateWindow()

    def _on_authenticated(self, _username: str) -> None:
        self.main_window.show()
        self.main_window.raise_()
        self.main_window.activateWindow()

    def backup_now(self) -> None:
        if not self.scheduler.trigger("manual"):
            self._notify(APP_NAME, "Já existe um backup em andamento.")

    def _on_backup_started(self, origin: str) -> None:
        self._update_tray_tooltip()
        if origin == "manual":
            self._notify(APP_NAME, "Backup iniciado em segundo plano.")

    def _on_backup_finished(self, report: BackupReport) -> None:
        self._update_tray_tooltip()
        if report.success:
            self._notify("Backup concluído", report.summary())
        else:
            self._notify("Falha no backup", report.summary() + "\nDetalhes em logs\\error.log",
                         QSystemTrayIcon.Warning)
        if self._quit_pending:
            self._quit()

    def request_quit(self) -> None:
        if self.scheduler.is_running:
            answer = QMessageBox.question(
                None, APP_NAME,
                "Um backup está em andamento.\nDeseja encerrar o programa assim que ele terminar?",
            )
            if answer == QMessageBox.Yes:
                self._quit_pending = True
                self._notify(APP_NAME, "O programa será encerrado ao final do backup.")
            return
        self._quit()

    def _quit(self) -> None:
        main_log.info("Programa encerrado.")
        self.scheduler.stop()
        self.scheduler.wait_for_done(5000)
        if self.tray is not None:
            self.tray.hide()
        self._server.close()
        QCoreApplication.quit()


def run_gui(start_hidden: bool) -> int:
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    _set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(image("app_icon.ico")))
    # As janelas apenas se escondem; o programa só termina pelo menu "Sair" da bandeja
    app.setQuitOnLastWindowClosed(False)

    instance_lock = InterProcessLock(APP_LOCK_FILE)
    if not instance_lock.acquire():
        # Já existe uma instância: em vez de criar outro agendador (backups em dobro),
        # pede para a instância existente mostrar a janela.
        if not start_hidden and not notify_running_instance():
            QMessageBox.information(None, APP_NAME, "O programa já está em execução (veja a bandeja do sistema).")
        main_log.info("Segunda instância encerrada: o programa já está em execução.")
        return 0

    try:
        apply_theme(app)
        controller = BackupApplication(app, start_hidden)  # noqa: F841 - precisa ficar vivo
        return app.exec_()
    finally:
        instance_lock.release()
