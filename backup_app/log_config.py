"""Configuração dos loggers.

Mantém os três arquivos originais (main.log, error.log, exception.log), agora
com caminho absoluto e rotação de tamanho.
"""

import logging
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler

from .paths import LOGS_DIR

main_log = logging.getLogger("main_log")            # eventos normais (logins, backups concluídos)
error_log = logging.getLogger("error_log")          # erros tratados (falhas de backup, conexão...)
exception_log = logging.getLogger("exception_log")  # exceções não tratadas

_MAX_BYTES = 2 * 1024 * 1024  # 2 MB por arquivo
_BACKUP_COUNT = 5

_configured = False


def _add_handler(logger: logging.Logger, filename: str, level: int, fmt: str) -> None:
    handler = RotatingFileHandler(
        LOGS_DIR / filename, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(fmt))
    logger.setLevel(level)
    logger.addHandler(handler)
    logger.propagate = False


def setup_logging(console: bool = False) -> None:
    """Configura os loggers e os hooks de exceção. Pode ser chamada mais de uma vez."""
    global _configured
    if _configured:
        return
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    _add_handler(main_log, "main.log", logging.INFO, "%(asctime)s - %(message)s")
    _add_handler(error_log, "error.log", logging.ERROR, "%(asctime)s - %(levelname)s - %(message)s")
    _add_handler(exception_log, "exception.log", logging.ERROR, "%(asctime)s - %(levelname)s - %(message)s")

    # Em modo linha de comando (--backup) também mostra as mensagens no console
    if console and sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
        for logger in (main_log, error_log, exception_log):
            logger.addHandler(stream)

    sys.excepthook = handle_exception
    threading.excepthook = _handle_thread_exception
    _configured = True


# Função para tratar os erros internos do python
def handle_exception(exc_type, exc_value, exc_traceback) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    formatted = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    exception_log.error(formatted)


def _handle_thread_exception(args) -> None:
    if args.exc_type is SystemExit:
        return
    handle_exception(args.exc_type, args.exc_value, args.exc_traceback)
