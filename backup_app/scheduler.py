"""Agendamento dos backups.

Arquitetura (substitui o antigo ScheduleThread com `while True`):

* Um QTimer na thread da interface chama `scheduler.run_pending()` a cada
  segundo. Como só a thread da interface mexe nos jobs, não há condição de
  corrida ao reagendar.
* O job apenas enfileira o trabalho em um QThreadPool com UM único worker:
  backups nunca rodam em paralelo e a interface nunca congela.
* O worker executa `backup.run_backup()`, que não acessa widgets.
* O resultado volta para a interface pelo sinal `backup_finished`.
* A agenda é uma lista de regras (horários fixos ou intervalo), expandidas em
  um job semanal por par (dia, horário).
"""

from typing import List, Optional

import schedule
from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal

from .backup import BackupAlreadyRunning, BackupReport, run_backup
from .log_config import error_log, exception_log, main_log
from .settings import DAY_LABELS, AppSettings, all_slots

_WEEKDAY_JOBS = {
    "Segunda": lambda s: s.every().monday,
    "Terca": lambda s: s.every().tuesday,
    "Quarta": lambda s: s.every().wednesday,
    "Quinta": lambda s: s.every().thursday,
    "Sexta": lambda s: s.every().friday,
    "Sabado": lambda s: s.every().saturday,
    "Domingo": lambda s: s.every().sunday,
}


class _WorkerSignals(QObject):
    finished = pyqtSignal(object)  # BackupReport


class _BackupTask(QRunnable):
    def __init__(self, origin: str):
        super().__init__()
        self.origin = origin
        self.signals = _WorkerSignals()

    def run(self) -> None:  # executa na thread do pool
        try:
            report = run_backup(self.origin)
        except BackupAlreadyRunning as exc:
            report = BackupReport(origin=self.origin, error=str(exc))
        except Exception as exc:  # nunca deixa a exceção derrubar o worker
            exception_log.exception("Erro inesperado no worker de backup")
            report = BackupReport(origin=self.origin, error=f"Erro inesperado no backup: {exc}")
        self.signals.finished.emit(report)


class BackupScheduler(QObject):
    backup_started = pyqtSignal(str)       # origem ("agendado" / "manual")
    backup_finished = pyqtSignal(object)   # BackupReport
    schedule_changed = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._scheduler = schedule.Scheduler()  # instância própria, sem estado global
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._current_task: Optional[_BackupTask] = None
        self.last_report: Optional[BackupReport] = None

    # ------------------------------------------------------------------ ciclo de vida
    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self._scheduler.clear()

    def wait_for_done(self, msecs: int = -1) -> bool:
        return self._pool.waitForDone(msecs)

    # ------------------------------------------------------------------ agenda
    def reload(self, settings: AppSettings) -> List[str]:
        """Recria os jobs a partir das regras. Retorna a descrição das regras ativas.

        Cada regra é expandida em pares (dia, horário); pares repetidos entre
        regras viram um único job, então regras sobrepostas não duplicam backups.
        """
        self._scheduler.clear()
        for day, time in all_slots(settings.rules):
            try:
                _WEEKDAY_JOBS[day](self._scheduler).at(time).do(self.trigger, "agendado")
            except schedule.ScheduleError as exc:
                error_log.error(f"Horário inválido para {DAY_LABELS[day]}: '{time}' ({exc}).")
        descriptions = [rule.describe() for rule in settings.rules if rule.enabled]
        main_log.info(f"Agenda de backups ({len(self._scheduler.jobs)} horário(s) por semana): "
                      + ("; ".join(descriptions) or "nenhuma regra ativa"))
        self.schedule_changed.emit()
        return descriptions

    def next_run(self):
        return self._scheduler.next_run if self._scheduler.jobs else None

    def _tick(self) -> None:
        try:
            self._scheduler.run_pending()
        except Exception:
            exception_log.exception("Erro ao processar a agenda de backups")

    # ------------------------------------------------------------------ execução
    @property
    def is_running(self) -> bool:
        return self._current_task is not None

    def trigger(self, origin: str = "manual") -> bool:
        """Enfileira um backup. Retorna False se já houver um em andamento."""
        if self._current_task is not None:
            main_log.info(f"Backup {origin} ignorado: já existe um backup em andamento.")
            return False
        task = _BackupTask(origin)
        task.signals.finished.connect(self._on_finished)
        self._current_task = task  # mantém a referência viva até o fim
        self.backup_started.emit(origin)
        self._pool.start(task)
        return True

    def _on_finished(self, report: BackupReport) -> None:  # executa na thread da interface
        self._current_task = None
        self.last_report = report
        self.backup_finished.emit(report)
        self.schedule_changed.emit()
