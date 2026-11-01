"""Histórico dos backups e situação geral exibida na tela Início.

Fica em um arquivo próprio porque backups também rodam em outro processo (--backup):
cada backup acrescenta uma linha JSON, e a interface lê o arquivo para mostrar o resultado.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

from .log_config import error_log
from .paths import CONTENT_DIR, atomic_write_text

HISTORY_FILE = CONTENT_DIR / "history.jsonl"
MAX_ENTRIES = 200
STALE_AFTER = timedelta(days=7)  # sem backup bem-sucedido há mais tempo que isso: alerta

LEVEL_OK = "ok"
LEVEL_INFO = "info"
LEVEL_RUNNING = "running"
LEVEL_WARNING = "warning"
LEVEL_ERROR = "error"


@dataclass
class DatabaseEntry:
    engine: str
    database: str
    success: bool
    message: str
    size: Optional[int] = None


@dataclass
class HistoryEntry:
    started: datetime
    finished: datetime
    origin: str
    success: bool
    summary: str
    databases: List[DatabaseEntry] = field(default_factory=list)


@dataclass
class Health:
    level: str
    title: str
    detail: str


# ---------------------------------------------------------------------------
# Leitura e gravação
# ---------------------------------------------------------------------------
def _file_size(path: Optional[str]) -> Optional[int]:
    try:
        return os.path.getsize(path) if path else None
    except OSError:  # ex.: pasta do SQL Server em outra máquina
        return None


def record(report) -> None:
    """Grava o resultado de um backup (BackupReport). Uma falha aqui só gera log."""
    started = report.started
    entry = {
        "started": started.isoformat(timespec="seconds"),
        "finished": (report.finished or started).isoformat(timespec="seconds"),
        "origin": report.origin,
        "success": report.success,
        "summary": report.summary(),
        "databases": [
            {"engine": r.engine, "database": r.database, "success": r.success,
             "message": r.message, "size": _file_size(r.path)}
            for r in report.results
        ],
    }
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_FILE, "a", encoding="utf-8") as file:
            file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _trim()
    except OSError as exc:
        error_log.error(f"Não foi possível gravar o histórico de backups: {exc}")


def _trim() -> None:
    lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    if len(lines) > MAX_ENTRIES * 3 // 2:  # sobra de folga para não regravar a cada backup
        atomic_write_text(HISTORY_FILE, "\n".join(lines[-MAX_ENTRIES:]) + "\n")


def _parse(line: str) -> HistoryEntry:
    data = json.loads(line)
    return HistoryEntry(
        started=datetime.fromisoformat(data["started"]),
        finished=datetime.fromisoformat(data["finished"]),
        origin=data["origin"],
        success=data["success"],
        summary=data["summary"],
        databases=[DatabaseEntry(**item) for item in data.get("databases", [])],
    )


def load() -> List[HistoryEntry]:
    """Histórico do mais recente para o mais antigo; linhas corrompidas são ignoradas."""
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        error_log.error(f"Não foi possível ler o histórico de backups: {exc}")
        return []
    entries = []
    for line in reversed(lines[-MAX_ENTRIES:]):
        try:
            entries.append(_parse(line))
        except (ValueError, KeyError, TypeError):
            continue
    return entries


# ---------------------------------------------------------------------------
# Apresentação
# ---------------------------------------------------------------------------
def _count(value: int, singular: str, plural: str) -> str:
    return f"{value} {singular if value == 1 else plural}"


def _span(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return _count(minutes, "minuto", "minutos")
    if minutes < 24 * 60:
        return _count(minutes // 60, "hora", "horas")
    return _count(minutes // (24 * 60), "dia", "dias")


def format_relative(moment: datetime, now: Optional[datetime] = None) -> str:
    """Ex.: 'há 2 horas', 'em 3 dias', 'agora há pouco'."""
    delta = moment - (now or datetime.now())
    if abs(delta) < timedelta(minutes=1):
        return "agora há pouco"
    return f"em {_span(delta)}" if delta > timedelta(0) else f"há {_span(-delta)}"


def format_duration(entry: HistoryEntry) -> str:
    seconds = max(0, int((entry.finished - entry.started).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}min {seconds % 60:02d}s"


def format_size(size: Optional[int]) -> str:
    if size is None:
        return ""
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.2f} GB"
    return f"{size / 1024 ** 2:.2f} MB"


def evaluate(entries: List[HistoryEntry], has_schedule: bool, has_engine: bool, running: bool,
             now: Optional[datetime] = None) -> Health:
    """Responde à pergunta principal da tela Início: os backups estão em dia?"""
    now = now or datetime.now()
    latest = entries[0] if entries else None
    last_success = next((entry for entry in entries if entry.success), None)

    if running:
        return Health(LEVEL_RUNNING, "Backup em andamento", "O resultado aparece aqui ao terminar.")
    if not has_engine:
        return Health(LEVEL_WARNING, "Nenhum banco selecionado",
                      "Escolha MySQL ou SQL Server na aba Agendamento.")
    if latest is not None and not latest.success:
        return Health(LEVEL_ERROR, "O último backup falhou", latest.summary)
    if last_success is None:
        if has_schedule:
            return Health(LEVEL_INFO, "Aguardando o primeiro backup", "Ele será feito no próximo horário agendado.")
        return Health(LEVEL_WARNING, "Nenhum backup agendado", "Crie uma regra na aba Agendamento.")
    if now - last_success.finished > STALE_AFTER:
        return Health(LEVEL_WARNING, f"Sem backup há {_span(now - last_success.finished)}",
                      "Confira a agenda e se o computador ficou ligado nos horários previstos.")
    if not has_schedule:
        return Health(LEVEL_WARNING, "Nenhum backup agendado",
                      "O último funcionou, mas os próximos não vão acontecer sozinhos.")
    return Health(LEVEL_OK, "Backups em dia", last_success.summary)
