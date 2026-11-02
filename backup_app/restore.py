"""Restauração de backups.

Assim como backup.py, este módulo não usa Qt. Regras de segurança:

* O padrão é restaurar em um banco novo: o banco de origem continua intacto.
* Substituir um banco existente exige um pedido explícito e só acontece depois
  de uma cópia de segurança verificada do banco atual.
* O arquivo é conferido antes de qualquer alteração no servidor.
* Um .sql que escolhe o próprio banco (USE, CREATE DATABASE) é recusado, porque
  gravaria em outro lugar que não o destino informado.
"""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import List, Optional, Tuple

from .backup import (
    BackupAlreadyRunning, MYSQL_FOLDER, SAFETY_SUFFIX, STAMP_FORMAT, _NO_WINDOW, _dump_mysql,
    _write_mysql_defaults_file, backup_and_verify, backup_file_name, backup_locations, connect_mysql,
    connect_sqlserver, dump_is_complete, execute_all, find_mysqldump, get_mysql_databases_and_version,
    list_backups, quote_identifier, quote_literal, sqlserver_backup_path,
)
from .locks import InterProcessLock
from .log_config import error_log, exception_log, main_log
from .paths import BACKUP_LOCK_FILE
from .security import SecretBox, load_key
from .settings import AppSettings, MySQLCredentials, SQLServerCredentials, load_credentials, load_settings

ENGINE_MYSQL = "MySQL"
ENGINE_SQLSERVER = "SQL Server"

MYSQL_RESTORE_TIMEOUT = 4 * 60 * 60
SYSTEM_DATABASES = {
    "information_schema", "performance_schema", "mysql", "sys", "master", "model", "msdb", "tempdb",
}
_VALID_NAME = re.compile(r"^[\w$-]{1,64}$")
_OWN_DATABASE = re.compile(rb"^\s*(USE\s|(CREATE|DROP)\s+(DATABASE|SCHEMA)\s)", re.IGNORECASE)


class RestoreError(RuntimeError):
    """Falha com uma mensagem pronta para mostrar ao usuário."""


@dataclass
class BackupFile:
    engine: str
    database: str
    moment: datetime
    path: Path
    size: Optional[int] = None


@dataclass
class RestoreRequest:
    engine: str
    path: str
    target: str
    replace: bool = False


def available_backups(settings: AppSettings) -> List[BackupFile]:
    """Backups visíveis desta máquina, do mais recente ao mais antigo."""
    found = []
    for engine, folder, extension in backup_locations(settings):
        for database, backups in list_backups(folder, extension).items():
            for moment, path in backups:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = None
                found.append(BackupFile(engine, database, moment, path, size))
    return sorted(found, key=lambda backup: backup.moment, reverse=True)


def original_database(database: str) -> str:
    return database[:-len(SAFETY_SUFFIX)] if database.endswith(SAFETY_SUFFIX) else database


def suggested_target(database: str) -> str:
    return f"{original_database(database)}_restaurado"[:64]


def target_problem(target: str) -> Optional[str]:
    """Motivo para recusar o nome do banco de destino, ou None."""
    if not _VALID_NAME.match(target):
        return "Use só letras, números, _, - e $ no nome do banco (até 64 caracteres)."
    if target.lower() in SYSTEM_DATABASES:
        return "Este é um banco do sistema e não pode ser usado como destino."
    return None


def check_sql_dump(path: Path) -> None:
    """Confere um .sql antes de tocar no servidor."""
    try:
        if not dump_is_complete(path):
            raise RestoreError("Este arquivo não é um dump completo do mysqldump.")
        with open(path, "rb") as file:
            for line in file:
                if _OWN_DATABASE.match(line):
                    raise RestoreError("Este arquivo escolhe o próprio banco (USE ou CREATE DATABASE), "
                                       "então não gravaria no destino informado.")
    except OSError as exc:
        raise RestoreError(f"Não foi possível ler o arquivo: {exc}")


# ---------------------------------------------------------------------------
# MySQL
# ---------------------------------------------------------------------------
def find_mysql_client(settings: AppSettings) -> Optional[str]:
    """O cliente mysql da mesma instalação do mysqldump, para as versões combinarem."""
    dump = find_mysqldump(settings.mysqldump_path.strip())
    if dump:
        sibling = Path(dump).with_name("mysql" + Path(dump).suffix)
        if sibling.is_file():
            return str(sibling)
    return shutil.which("mysql")


def _quote_mysql(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def _mysql_statement(creds: MySQLCredentials, statement: str) -> None:
    connection = connect_mysql(creds)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(statement)
        finally:
            cursor.close()
    finally:
        connection.close()


def _import_dump(client: str, creds: MySQLCredentials, target: str, path: str) -> None:
    defaults_file = _write_mysql_defaults_file(creds)
    try:
        with open(path, "rb") as dump:
            # IMPORTANTE: --defaults-extra-file DEVE ser o primeiro argumento
            process = subprocess.run(
                [client, f"--defaults-extra-file={defaults_file}", "--default-character-set=utf8",
                 f"--database={target}"],
                stdin=dump,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=MYSQL_RESTORE_TIMEOUT,
                creationflags=_NO_WINDOW,
            )
    except subprocess.TimeoutExpired:
        raise RestoreError("tempo limite excedido")
    finally:
        try:
            os.unlink(defaults_file)
        except OSError:
            pass
    if process.returncode != 0:
        raise RestoreError((process.stderr or "").strip() or f"mysql retornou o código {process.returncode}")


def _safety_copy_mysql(settings: AppSettings, creds: MySQLCredentials, target: str, version: str) -> str:
    mysqldump = find_mysqldump(settings.mysqldump_path.strip())
    if not mysqldump:
        raise RestoreError("mysqldump.exe não encontrado: sem ele não há como copiar o banco atual.")
    folder = settings.backup_root / MYSQL_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime(STAMP_FORMAT)
    destination = folder / backup_file_name(target + SAFETY_SUFFIX, stamp, ".sql")
    result = _dump_mysql(mysqldump, creds, target, destination, version)
    if not result.success:
        raise RestoreError(f"Restauração cancelada: não foi possível copiar o banco atual. {result.message}")
    return str(destination)


def _restore_mysql(request: RestoreRequest, settings: AppSettings, creds: MySQLCredentials) -> str:
    check_sql_dump(Path(request.path))
    client = find_mysql_client(settings)
    if not client:
        raise RestoreError("mysql.exe não encontrado: ele fica na mesma pasta do mysqldump.exe.")

    databases, version = get_mysql_databases_and_version(creds)
    exists = request.target.lower() in (database.lower() for database in databases)
    if exists != request.replace:
        raise RestoreError(f"O banco '{request.target}' já existe." if exists
                           else f"O banco '{request.target}' não existe.")

    quoted = _quote_mysql(request.target)
    safety_copy = None
    if request.replace:
        safety_copy = _safety_copy_mysql(settings, creds, request.target, version)
        main_log.info(f"Cópia de segurança de '{request.target}' gravada em '{safety_copy}'.")
        _mysql_statement(creds, f"DROP DATABASE {quoted}")
    _mysql_statement(creds, f"CREATE DATABASE {quoted}")
    try:
        _import_dump(client, creds, request.target, request.path)
    except RestoreError as exc:
        if safety_copy:
            raise RestoreError(f"A restauração falhou e o banco '{request.target}' ficou incompleto. "
                               f"O conteúdo anterior está em '{safety_copy}'. Erro: {exc}")
        try:
            _mysql_statement(creds, f"DROP DATABASE {quoted}")  # só apaga o que foi criado acima
        except Exception as drop_error:
            error_log.error(f"Não foi possível apagar o banco incompleto '{request.target}': {drop_error}")
        raise RestoreError(f"A restauração falhou. Erro: {exc}")
    return _success_text(request, safety_copy)


# ---------------------------------------------------------------------------
# SQL Server
# ---------------------------------------------------------------------------
def _server_folders(cursor) -> Tuple[PureWindowsPath, PureWindowsPath]:
    """Pastas de dados e de log. As propriedades não existem no 2008: nele vale a pasta do master."""
    cursor.execute("SELECT CAST(SERVERPROPERTY('InstanceDefaultDataPath') AS NVARCHAR(400)), "
                   "CAST(SERVERPROPERTY('InstanceDefaultLogPath') AS NVARCHAR(400))")
    row = cursor.fetchone()
    if row and row[0] and row[1]:
        return PureWindowsPath(row[0]), PureWindowsPath(row[1])
    cursor.execute("SELECT physical_name FROM sys.master_files WHERE database_id = 1 ORDER BY file_id")
    files = cursor.fetchall()
    return PureWindowsPath(files[0][0]).parent, PureWindowsPath(files[1][0]).parent


def _file_moves(cursor, disk: str, target: str) -> List[str]:
    """Cláusulas MOVE: arquivos com o nome do destino, para não colidir com os do banco de origem."""
    data_folder, log_folder = _server_folders(cursor)
    cursor.execute(f"RESTORE FILELISTONLY FROM DISK = {disk}")
    rows = cursor.fetchall()
    while cursor.nextset():
        pass
    moves = []
    data_count = log_count = 0
    for row in rows:  # colunas: LogicalName, PhysicalName, Type...
        if str(row[2]).upper() == "L":
            name = f"{target}_log{f'_{log_count}' if log_count else ''}.ldf"
            path = log_folder / name
            log_count += 1
        else:
            name = f"{target}.mdf" if not data_count else f"{target}_{data_count}.ndf"
            path = data_folder / name
            data_count += 1
        moves.append(f"MOVE {quote_literal(row[0])} TO {quote_literal(str(path))}")
    return moves


def _restore_sqlserver(request: RestoreRequest, settings: AppSettings, creds: SQLServerCredentials) -> str:
    target = quote_identifier(request.target)
    disk = quote_literal(request.path)
    # A sessão não pode estar no banco que será substituído, por isso nunca abre no banco configurado
    connection = connect_sqlserver(replace(creds, database=""), database="master", autocommit=True)
    try:
        cursor = connection.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM sys.databases WHERE name = {quote_literal(request.target)}")
        exists = cursor.fetchone()[0] > 0
        if exists != request.replace:
            raise RestoreError(f"O banco '{request.target}' já existe." if exists
                               else f"O banco '{request.target}' não existe.")

        execute_all(cursor, f"RESTORE VERIFYONLY FROM DISK = {disk}")
        moves = _file_moves(cursor, disk, request.target)
        safety_copy = None
        if request.replace:
            stamp = datetime.now().strftime(STAMP_FORMAT)
            file_name = backup_file_name(request.target + SAFETY_SUFFIX, stamp, ".bak")
            safety_copy = sqlserver_backup_path(settings, file_name)
            backup_and_verify(cursor, request.target, safety_copy)
            main_log.info(f"Cópia de segurança de '{request.target}' gravada em '{safety_copy}'.")
            execute_all(cursor, f"ALTER DATABASE {target} SET SINGLE_USER WITH ROLLBACK IMMEDIATE")

        options = ", ".join(moves + (["REPLACE"] if request.replace else []))
        try:
            execute_all(cursor, f"RESTORE DATABASE {target} FROM DISK = {disk} WITH {options}")
        except Exception as exc:
            _undo_failed_restore(cursor, request, target)
            if safety_copy:
                raise RestoreError(f"A restauração falhou. Se o banco '{request.target}' ficou incompleto, "
                                   f"o conteúdo anterior está em '{safety_copy}'. Erro: {exc}")
            raise RestoreError(f"A restauração falhou. Erro: {exc}")
        cursor.close()
    finally:
        connection.close()
    return _success_text(request, safety_copy)


def _undo_failed_restore(cursor, request: RestoreRequest, target: str) -> None:
    """Devolve o acesso ao banco substituído ou remove o que sobrou de um banco novo."""
    statement = (f"ALTER DATABASE {target} SET MULTI_USER" if request.replace
                 else f"IF DB_ID({quote_literal(request.target)}) IS NOT NULL DROP DATABASE {target}")
    try:
        execute_all(cursor, statement)
    except Exception as exc:
        error_log.error(f"Não foi possível desfazer a restauração de '{request.target}': {exc}")


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------
def _success_text(request: RestoreRequest, safety_copy: Optional[str]) -> str:
    text = f"Backup restaurado no banco '{request.target}'."
    if safety_copy:
        text += f" O conteúdo anterior está em '{safety_copy}'."
    return text


def restore_backup(request: RestoreRequest) -> str:
    """Restaura o backup e devolve a mensagem de sucesso.

    Lança BackupAlreadyRunning durante um backup (a mesma trava impede que um
    backup copie um banco pela metade) e RestoreError nas demais falhas.
    """
    problem = target_problem(request.target)
    if problem:
        raise RestoreError(problem)
    lock = InterProcessLock(BACKUP_LOCK_FILE)
    if not lock.acquire():
        raise BackupAlreadyRunning("Há um backup em andamento. Tente de novo quando ele terminar.")

    action = "substituindo" if request.replace else "em banco novo"
    main_log.info(f"Restauração iniciada ({action}): '{request.path}' em '{request.target}'.")
    try:
        settings = load_settings()
        creds = load_credentials(SecretBox(load_key(create=False)))
        if request.engine == ENGINE_MYSQL:
            message = _restore_mysql(request, settings, creds.mysql)
        else:
            message = _restore_sqlserver(request, settings, creds.sqlserver)
    except RestoreError as exc:
        error_log.error(f"Restauração de '{request.path}': {exc}")
        raise
    except Exception as exc:
        exception_log.exception("Erro inesperado na restauração")
        raise RestoreError(f"Erro na restauração: {exc}")
    finally:
        lock.release()
    main_log.info(message)
    return message
