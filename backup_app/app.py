"""Controlador da aplicação: bandeja do sistema, instância única e ciclo de vida."""

import hashlib
import sys

from PyQt5.QtCore import QCoreApplication, QObject, Qt, QTimer
from PyQt5.QtGui import QCursor, QIcon
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from PyQt5.QtWidgets import QAction, QApplication, QMenu, QMessageBox, QSystemTrayIcon

from . import history, startup
from .backup import BackupReport
from .icons import apply_theme, icon, tray_icon
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
    TRAY_REFRESH_MS = 60_000  # percebe backups do --backup e a passagem do tempo

    def __init__(self, app: QApplication, start_hidden: bool):
        super().__init__()
        self.app = app
        self.tray = None
        self._tray_menu = None
        self._tray_level = None
        self._tray_retries = 0
        self._quit_pending = False

        self.scheduler = BackupScheduler(self)
        self.main_window = MainWindow(self.scheduler)
        self.login_screen = LoginScreen()
        self.login_screen.authenticated.connect(self._on_authenticated)

        self.scheduler.backup_started.connect(self._on_backup_started)
        self.scheduler.backup_finished.connect(self._on_backup_finished)
        self.scheduler.backup_progress.connect(self._refresh_tray)
        self.scheduler.schedule_changed.connect(self._refresh_tray)

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

        # Resumo no topo do menu: só informa, por isso fica desativado
        self._status_action = QAction(self)
        self._status_action.setEnabled(False)
        self._next_action = QAction(self)
        self._next_action.setEnabled(False)
        open_action = QAction(icon("open"), "Abrir", self)
        open_action.triggered.connect(self.show_login)
        backup_action = QAction(icon("run"), "Executar backup", self)
        backup_action.triggered.connect(self.backup_now)
        pause_action = QAction(icon("pause"), "Pausar agendamento", self)
        pause_action.setCheckable(True)
        pause_action.toggled.connect(self.scheduler.set_paused)
        close_action = QAction(icon("quit"), "Sair", self)
        close_action.triggered.connect(self.request_quit)

        self._tray_menu = QMenu()
        self._tray_menu.addAction(self._status_action)
        self._tray_menu.addAction(self._next_action)
        self._tray_menu.addSeparator()
        self._tray_menu.addAction(open_action)
        self._tray_menu.addAction(backup_action)
        self._tray_menu.addAction(pause_action)
        self._tray_menu.addSeparator()
        self._tray_menu.addAction(close_action)
        self.tray.setContextMenu(self._tray_menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.messageClicked.connect(self.show_login)

        # O clique simples espera para não abrir o menu quando for um duplo clique
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(QApplication.doubleClickInterval())
        self._click_timer.timeout.connect(self._show_tray_menu)
        self.tray.show()
        self._refresh_tray()

        refresh_timer = QTimer(self)
        refresh_timer.setInterval(self.TRAY_REFRESH_MS)
        refresh_timer.timeout.connect(self._refresh_tray)
        refresh_timer.start()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self._click_timer.start()
        elif reason == QSystemTrayIcon.DoubleClick:
            self._click_timer.stop()
            self.show_login()

    def _show_tray_menu(self) -> None:
        self._tray_menu.popup(QCursor.pos())
        self._tray_menu.activateWindow()  # sem isso o menu não fecha ao clicar fora

    def _notify(self, title: str, message: str, icon=QSystemTrayIcon.Information) -> None:
        if self.tray is not None and self.tray.isVisible():
            self.tray.showMessage(title, message, icon, 8000)

    def _refresh_tray(self) -> None:
        """Ícone e dica da bandeja acompanham a situação geral mostrada na tela Início."""
        if self.tray is None:
            return
        health = self.scheduler.health(history.load())
        if health.level != self._tray_level:
            self._tray_level = health.level
            self.tray.setIcon(tray_icon(image("app_icon.ico"), health.level))
        next_run = self.scheduler.next_run()
        if health.level == history.LEVEL_RUNNING:
            detail = health.detail
        elif next_run:
            detail = f"Próximo backup: {DAY_LABELS[day_of(next_run)]} {next_run.strftime('%H:%M')}"
        else:
            detail = "Nenhum backup previsto"
        self.tray.setToolTip("\n".join((APP_NAME, health.title, detail)))
        self._status_action.setText(health.title)
        self._next_action.setText(detail)

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
        self._refresh_tray()
        if load_settings().notify_start:
            self._notify(APP_NAME, "Backup iniciado em segundo plano.")

    def _on_backup_finished(self, report: BackupReport) -> None:
        self._refresh_tray()
        if not report.success:
            self._notify("Falha no backup", report.summary() + "\nClique para ver os detalhes.",
                         QSystemTrayIcon.Warning)
        elif report.origin == "manual" or load_settings().notify_success:  # quem pediu quer saber o resultado
            self._notify("Backup concluído", report.summary())
        if self._quit_pending:
            self._quit()

    def request_quit(self) -> None:
        if self.main_window.isVisible() and not self.main_window.confirm_unsaved():
            return
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
