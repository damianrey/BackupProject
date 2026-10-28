"""Execução dos backups.

Este módulo não usa Qt nem widgets: lê apenas os arquivos de configuração e a
chave. Por isso pode rodar com segurança em uma thread de segundo plano ou
direto pela linha de comando (BackupProject.py --backup), por exemplo a
partir do Agendador de Tarefas do Windows.
"""

import glob
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import List, Optional

from .locks import InterProcessLock
from .log_config import error_log, exception_log, main_log
from .paths import BACKUP_LOCK_FILE
from .security import SecretBox, load_key
from .settings import (
    AppSettings, MySQLCredentials, SQLServerCredentials, day_of, load_credentials, load_settings,
)

MYSQL_FOLDER = "mysql"
SQL_FOLDER = "sql"

# Bancos internos do MySQL que não devem ser copiados
MYSQL_EXCLUDED_DATABASES = {"information_schema", "performance_schema", "mysql", "sys", "test"}

# Caminho usado na versão original; continua tendo prioridade na detecção automática
LEGACY_MYSQLDUMP = r"C:\Program Files (x86)\MySQL\MySQL Server 5.1\bin\mysqldump.exe"

CONNECT_TIMEOUT = 10                 # segundos
MYSQLDUMP_TIMEOUT = 4 * 60 * 60      # um dump travado não pode bloquear os próximos backups

# Evita que uma janela de console pisque a cada mysqldump em um app sem console
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class BackupAlreadyRunning(RuntimeError):
    pass


@dataclass
class BackupResult:
    engine: str
    database: str
    success: bool
    message: str
    path: Optional[str] = None


@dataclass
class BackupReport:
    origin: str = ""
    started: datetime = field(default_factory=datetime.now)
    finished: Optional[datetime] = None
    results: List[BackupResult] = field(default_factory=list)
    error: Optional[str] = None  # erro geral (antes de chegar aos bancos)

    @property
    def success(self) -> bool:
        return self.error is None and bool(self.results) and all(r.success for r in self.results)

    @property
    def failures(self) -> List[BackupResult]:
        return [r for r in self.results if not r.success]

    def summary(self) -> str:
        if self.error:
            return self.error
        if not self.results:
            return "Nenhum banco de dados foi copiado."
        ok = len(self.results) - len(self.failures)
        text = f"{ok} de {len(self.results)} backup(s) concluído(s)."
        if self.failures:
            text += " Falhas: " + ", ".join(f"{r.engine}/{r.database}" for r in self.failures)
        return text


# ---------------------------------------------------------------------------
# MySQL
# ---------------------------------------------------------------------------
def connect_mysql(creds: MySQLCredentials):
    """Conecta ao MySQL. Usada tanto no teste de conexão quanto no backup.

    Tenta primeiro o PyMySQL e, se falhar, o mysql-connector-python (útil em
    servidores antigos). Assim teste e backup se comportam da mesma forma.
    """
    errors = []
    try:
        import pymysql
        return pymysql.connect(
            host=creds.host, user=creds.user, password=creds.password,
            port=int(creds.port), connect_timeout=CONNECT_TIMEOUT,
        )
    except ImportError:
        errors.append("PyMySQL não instalado")
    except Exception as exc:
        errors.append(f"PyMySQL: {exc}")

    try:
        import mysql.connector
        return mysql.connector.connect(
            host=creds.host, user=creds.user, password=creds.password,
            port=int(creds.port), connection_timeout=CONNECT_TIMEOUT,
        )
    except ImportError:
        errors.append("mysql-connector-python não instalado")
    except Exception as exc:
        errors.append(f"mysql-connector: {exc}")

    raise ConnectionError(" | ".join(errors))


def list_mysql_databases(creds: MySQLCredentials) -> List[str]:
    connection = connect_mysql(creds)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute("SHOW DATABASES")
            return [row[0] for row in cursor.fetchall()]
        finally:
            cursor.close()
    finally:
        connection.close()


def find_mysqldump(configured: str = "") -> Optional[str]:
    """Localiza o mysqldump: caminho configurado > caminho legado > PATH > instalações padrão."""
    if configured:
        return configured if Path(configured).is_file() else None
    if Path(LEGACY_MYSQLDUMP).is_file():
        return LEGACY_MYSQLDUMP
    found = shutil.which("mysqldump")
    if found:
        return found
    candidates = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env)
        if base:
            candidates += glob.glob(os.path.join(base, "MySQL", "MySQL Server *", "bin", "mysqldump.exe"))
    return sorted(candidates, reverse=True)[0] if candidates else None


def _dump_mysql(mysqldump: str, creds: MySQLCredentials, database: str, destination: Path) -> BackupResult:
    partial = destination.with_name(destination.name + ".part")
    command = [
        mysqldump,
        f"--host={creds.host}",
        f"--port={creds.port}",
        f"--user={creds.user}",
        f"--result-file={partial}",
        database,
    ]
    # A senha vai por variável de ambiente: não aparece na lista de processos
    # e não há interpretação de caracteres especiais pelo shell.
    env = os.environ.copy()
    env.pop("MYSQL_PWD", None)
    if creds.password:
        env["MYSQL_PWD"] = creds.password

    try:
        process = subprocess.run(
            command, env=env, capture_output=True, text=True, errors="replace",
            timeout=MYSQLDUMP_TIMEOUT, creationflags=_NO_WINDOW,
        )
        stderr = (process.stderr or "").strip()
        if process.returncode != 0:
            raise RuntimeError(stderr or f"mysqldump retornou o código {process.returncode}")
        if not partial.is_file() or partial.stat().st_size == 0:
            raise RuntimeError("o arquivo gerado está vazio")
        # Só substitui o backup anterior quando o novo foi gerado com sucesso
        os.replace(partial, destination)
        if stderr:
            main_log.info(f"MySQL: avisos do mysqldump para '{database}': {stderr}")
        return BackupResult("MySQL", database, True,
                            f"MySQL: backup de {database} realizado em '{destination}'!", str(destination))
    except subprocess.TimeoutExpired:
        message = f"Falha no backup da base de dados '{database}' do MySQL: tempo limite excedido."
    except Exception as exc:
        message = f"Falha no backup da base de dados '{database}' do MySQL. Erro: {exc}"
    try:
        partial.unlink()
    except OSError:
        pass
    return BackupResult("MySQL", database, False, message)


def backup_mysql(settings: AppSettings, creds: MySQLCredentials, day: str) -> List[BackupResult]:
    if not creds.configured:
        return [BackupResult("MySQL", "-", False, "MySQL: credenciais não configuradas.")]

    mysqldump = find_mysqldump(settings.mysqldump_path.strip())
    if not mysqldump:
        return [BackupResult("MySQL", "-", False,
                             "MySQL: mysqldump.exe não encontrado. Informe o caminho na aba Conexões.")]

    try:
        databases = list_mysql_databases(creds)
        main_log.info("Conexão com MySQL bem sucedida!")
    except Exception as exc:
        return [BackupResult("MySQL", "-", False, f"Erro ao conectar ao MySQL: {exc}")]

    folder = settings.backup_root / MYSQL_FOLDER
    folder.mkdir(parents=True, exist_ok=True)

    results = []
    for database in databases:
        if database.lower() in MYSQL_EXCLUDED_DATABASES:
            continue
        destination = folder / f"{day}_{database}.sql"
        results.append(_dump_mysql(mysqldump, creds, database, destination))
    if not results:
        results.append(BackupResult("MySQL", "-", False, "MySQL: nenhum banco de dados de usuário encontrado."))
    return results


# ---------------------------------------------------------------------------
# SQL Server (via pyodbc, sem depender do sqlcmd)
# ---------------------------------------------------------------------------
_PREFERRED_ODBC_DRIVERS = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13.1 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "ODBC Driver 11 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server Native Client 10.0",
    "SQL Server",
)


def pick_odbc_driver() -> str:
    import pyodbc
    installed = set(pyodbc.drivers())
    for driver in _PREFERRED_ODBC_DRIVERS:
        if driver in installed:
            return driver
    raise RuntimeError("Nenhum driver ODBC do SQL Server está instalado.")


def _odbc_value(value: str) -> str:
    """Escapa um valor da connection string ODBC."""
    if any(ch in value for ch in ";{}=") or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


def sqlserver_connection_string(creds: SQLServerCredentials, database: Optional[str] = None) -> str:
    driver = pick_odbc_driver()
    server = creds.server + (f",{creds.port}" if creds.port else "")
    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={_odbc_value(server)}",
        f"DATABASE={_odbc_value(database or creds.database or 'master')}",
    ]
    if creds.user:
        parts += [f"UID={_odbc_value(creds.user)}", f"PWD={_odbc_value(creds.password)}"]
    else:
        parts.append("Trusted_Connection=yes")
    if driver.startswith("ODBC Driver 18"):
        parts.append("TrustServerCertificate=yes")  # o driver 18 exige criptografia por padrão
    return ";".join(parts) + ";"


def connect_sqlserver(creds: SQLServerCredentials, database: Optional[str] = None, autocommit: bool = False):
    import pyodbc
    return pyodbc.connect(sqlserver_connection_string(creds, database),
                          timeout=CONNECT_TIMEOUT, autocommit=autocommit)


def _quote_identifier(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _quote_literal(text: str) -> str:
    return "N'" + text.replace("'", "''") + "'"


def backup_sqlserver(settings: AppSettings, creds: SQLServerCredentials, day: str) -> List[BackupResult]:
    database = creds.database
    if not creds.configured:
        return [BackupResult("SQL Server", database or "-", False,
                             "SQL Server: servidor e banco de dados precisam estar configurados.")]

    file_name = f"{day}_{database}.bak"
    if settings.sqlserver_backup_dir.strip():
        # Pasta informada como o SQL Server a enxerga (pode ser em outra máquina)
        destination = str(PureWindowsPath(settings.sqlserver_backup_dir.strip()) / file_name)
    else:
        folder = settings.backup_root / SQL_FOLDER
        folder.mkdir(parents=True, exist_ok=True)
        destination = str(folder / file_name)

    # WITH INIT, FORMAT sobrescreve o arquivo em vez de acrescentar um novo backup
    # ao mesmo .bak a cada semana (o que fazia o arquivo crescer sem limite).
    statement = (f"BACKUP DATABASE {_quote_identifier(database)} "
                 f"TO DISK = {_quote_literal(destination)} WITH INIT, FORMAT")
    try:
        connection = connect_sqlserver(creds, database="master", autocommit=True)
        try:
            cursor = connection.cursor()
            cursor.execute(statement)
            # É obrigatório consumir todas as mensagens; caso contrário o
            # backup pode ser interrompido ao fechar a conexão.
            while cursor.nextset():
                pass
            cursor.close()
        finally:
            connection.close()
    except Exception as exc:
        return [BackupResult("SQL Server", database, False,
                             f"Falha no backup da base de dados {database} do SQL. Erro: {exc}")]
    return [BackupResult("SQL Server", database, True,
                         f"SQL: backup de {database} realizado em '{destination}'!", destination)]


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------
def run_backup(origin: str = "manual") -> BackupReport:
    """Executa o backup de acordo com o settings.ini salvo.

    Lança BackupAlreadyRunning se outro backup (deste ou de outro processo)
    estiver em andamento. Nunca lança exceções por falhas de banco: elas são
    registradas no relatório e nos logs.
    """
    lock = InterProcessLock(BACKUP_LOCK_FILE)
    if not lock.acquire():
        raise BackupAlreadyRunning("Já existe um backup em andamento.")

    report = BackupReport(origin=origin)
    main_log.info(f"Backup iniciado ({origin}).")
    try:
        settings = load_settings()
        if not (settings.use_mysql or settings.use_sql_server):
            report.error = "Nenhum banco selecionado para backup (aba Agendamento)."
        else:
            box = SecretBox(load_key(create=False))
            creds = load_credentials(box)
            day = day_of()
            if settings.use_mysql:
                report.results += backup_mysql(settings, creds.mysql, day)
            if settings.use_sql_server:
                report.results += backup_sqlserver(settings, creds.sqlserver, day)
    except Exception as exc:
        report.error = f"Erro inesperado no backup: {exc}"
        exception_log.exception(report.error)  # traceback completo; a mensagem vai ao error.log abaixo
    finally:
        lock.release()

    for result in report.results:
        (main_log.info if result.success else error_log.error)(result.message)
    if report.error:
        error_log.error(report.error)
    report.finished = datetime.now()
    main_log.info(f"Backup finalizado ({origin}): {report.summary()}")
    return report
