"""Execução dos backups.

Este módulo não usa Qt nem widgets: lê apenas os arquivos de configuração e a
chave. Por isso pode rodar com segurança em uma thread de segundo plano ou
direto pela linha de comando (BackupProject.py --backup), por exemplo a
partir do Agendador de Tarefas do Windows.

Compatibilidade de versões:
* MySQL: 5.1 (majoritário) até 5.5, 5.6, 5.7 e 8.x
* SQL Server: 2008 / 2008 R2, 2012, 2014, 2016, 2017 e 2019
"""

import glob
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import List, Optional, Tuple

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

# Caminhos comuns do MySQL 5.1 (32 e 64 bits) têm prioridade por ser a versão majoritária
LEGACY_MYSQLDUMP_PATHS = (
    r"C:\Program Files (x86)\MySQL\MySQL Server 5.1\bin\mysqldump.exe",
    r"C:\Program Files\MySQL\MySQL Server 5.1\bin\mysqldump.exe",
)

CONNECT_TIMEOUT = 10                 # segundos para abrir conexão
MYSQLDUMP_TIMEOUT = 4 * 60 * 60      # 4h: um dump travado não pode bloquear os próximos backups

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
# MySQL (5.1 até 8.x)
# ---------------------------------------------------------------------------
def connect_mysql(creds: MySQLCredentials):
    """Conecta ao MySQL (5.1 a 8.x). Usada tanto no teste quanto no backup.

    Importante para o MySQL 5.1:
    * O MySQL 5.1 NÃO possui o charset 'utf8mb4' (introduzido no 5.5.3).
      Drivers modernos (PyMySQL >= 1.0 e mysql-connector >= 8.0) enviam
      'utf8mb4' por padrão, causando erro 1115 ("Unknown character set: 'utf8mb4'").
      Por isso usamos 'utf8' (aceito tanto no 5.1 quanto no 5.5/5.7/8.x).
    * No mysql-connector-python usamos use_pure=True para manter compatibilidade
      com o protocolo/handshake de servidores 5.1.
    """
    errors = []

    try:
        import pymysql
        for charset in ("utf8", None):
            try:
                kwargs = dict(
                    host=creds.host,
                    user=creds.user,
                    password=creds.password,
                    port=int(creds.port),
                    connect_timeout=CONNECT_TIMEOUT,
                )
                if charset:
                    kwargs["charset"] = charset
                return pymysql.connect(**kwargs)
            except Exception as exc:
                errors.append(f"PyMySQL({charset or 'default'}): {exc}")
                # Se for erro de usuário/senha ou conexão recusada, não precisa repetir com outro charset
                if "1115" not in str(exc) and "character set" not in str(exc).lower():
                    break
    except ImportError:
        errors.append("PyMySQL não instalado")

    try:
        import mysql.connector
        for extra in ({"charset": "utf8", "use_pure": True}, {"charset": "utf8"}):
            try:
                return mysql.connector.connect(
                    host=creds.host,
                    user=creds.user,
                    password=creds.password,
                    port=int(creds.port),
                    connection_timeout=CONNECT_TIMEOUT,
                    **extra,
                )
            except Exception as exc:
                errors.append(f"mysql-connector: {exc}")
                if "1115" not in str(exc) and "character set" not in str(exc).lower():
                    break
    except ImportError:
        errors.append("mysql-connector-python não instalado")

    raise ConnectionError(" | ".join(errors))


def get_mysql_databases_and_version(creds: MySQLCredentials) -> Tuple[List[str], str]:
    """Retorna (lista_de_bancos, versao_do_servidor)."""
    connection = connect_mysql(creds)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT VERSION()")
            version_row = cursor.fetchone()
            server_version = str(version_row[0]) if version_row else ""

            cursor.execute("SHOW DATABASES")
            databases = [row[0] for row in cursor.fetchall()]
            return databases, server_version
        finally:
            cursor.close()
    finally:
        connection.close()


def list_mysql_databases(creds: MySQLCredentials) -> List[str]:
    databases, _version = get_mysql_databases_and_version(creds)
    return databases


def find_mysqldump(configured: str = "") -> Optional[str]:
    """Localiza o mysqldump: caminho configurado > MySQL 5.1 > PATH > demais versões instaladas."""
    if configured:
        return configured if Path(configured).is_file() else None
    for legacy in LEGACY_MYSQLDUMP_PATHS:
        if Path(legacy).is_file():
            return legacy
    found = shutil.which("mysqldump")
    if found:
        return found
    candidates = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        base = os.environ.get(env)
        if base:
            candidates += glob.glob(os.path.join(base, "MySQL", "MySQL Server *", "bin", "mysqldump.exe"))
            candidates += glob.glob(os.path.join(base, "MariaDB*", "bin", "mysqldump.exe"))
    return candidates[0] if candidates else None


def _write_mysql_defaults_file(creds: MySQLCredentials) -> Path:
    """Cria um arquivo temporário --defaults-extra-file compatível com MySQL 5.1 a 8.x.

    Vantagens sobre passar -p na linha de comando ou depender apenas de MYSQL_PWD:
    * Funciona em todas as versões do mysqldump (5.1, 5.5, 5.6, 5.7, 8.x);
    * Não expõe a senha na lista de processos do Windows;
    * Trata corretamente caracteres especiais (#, ;, ", ', espaços) na senha.
    """
    escaped_pwd = creds.password.replace("\\", "\\\\").replace('"', '\\"')
    escaped_user = creds.user.replace("\\", "\\\\").replace('"', '\\"')
    content = (
        "[client]\n"
        f'host="{creds.host}"\n'
        f"port={int(creds.port)}\n"
        f'user="{escaped_user}"\n'
        f'password="{escaped_pwd}"\n'
    )
    fd, temp_path = tempfile.mkstemp(prefix="bp_mysql_", suffix=".cnf", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return Path(temp_path)


def _run_mysqldump_cmd(
    mysqldump: str,
    defaults_file: Path,
    creds: MySQLCredentials,
    database: str,
    partial: Path,
    extra_flags: List[str],
) -> subprocess.CompletedProcess:
    # IMPORTANTE: --defaults-extra-file DEVE ser o primeiro argumento do mysqldump
    command = [
        mysqldump,
        f"--defaults-extra-file={defaults_file}",
        f"--host={creds.host}",
        f"--port={creds.port}",
        f"--user={creds.user}",
        "--default-character-set=utf8",
        *extra_flags,
        f"--result-file={partial}",
        database,
    ]
    env = os.environ.copy()
    env.pop("MYSQL_PWD", None)
    if creds.password:
        env["MYSQL_PWD"] = creds.password
    return subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=MYSQLDUMP_TIMEOUT,
        creationflags=_NO_WINDOW,
    )


def _dump_mysql(
    mysqldump: str,
    creds: MySQLCredentials,
    database: str,
    destination: Path,
    server_version: str = "",
) -> BackupResult:
    partial = destination.with_name(destination.name + ".part")
    defaults_file: Optional[Path] = None

    try:
        defaults_file = _write_mysql_defaults_file(creds)

        # Flags compatíveis do MySQL 5.1 ao 8.x:
        # --routines: inclui procedures e functions (disponível desde o MySQL 5.1.2)
        # --triggers: ativo por padrão, mas explícito para consistência
        base_flags = ["--routines", "--triggers"]

        process = _run_mysqldump_cmd(mysqldump, defaults_file, creds, database, partial, base_flags)
        stderr = (process.stderr or "").strip()

        # Caso um mysqldump 8.0+ esteja sendo usado contra um servidor MySQL 5.1/5.5/5.7,
        # ele pode falhar reclamando de COLUMN_STATISTICS (1109). Reexecuta desativando a flag.
        if process.returncode != 0 and "column_statistics" in stderr.lower():
            process = _run_mysqldump_cmd(
                mysqldump,
                defaults_file,
                creds,
                database,
                partial,
                [*base_flags, "--column-statistics=0"],
            )
            stderr = (process.stderr or "").strip()

        # Caso o usuário não tenha permissão de leitura em mysql.proc no MySQL 5.1 para --routines,
        # faz fallback sem --routines para não perder o backup das tabelas.
        if process.returncode != 0 and ("mysql.proc" in stderr.lower() or "routines" in stderr.lower()):
            main_log.info(
                f"MySQL: repetindo backup de '{database}' sem '--routines' (permissão/compatibilidade)."
            )
            process = _run_mysqldump_cmd(mysqldump, defaults_file, creds, database, partial, [])
            stderr = (process.stderr or "").strip()

        if process.returncode != 0:
            raise RuntimeError(stderr or f"mysqldump retornou o código {process.returncode}")
        if not partial.is_file() or partial.stat().st_size == 0:
            raise RuntimeError("o arquivo gerado está vazio")

        # Só substitui o backup anterior quando o novo foi gerado com sucesso
        os.replace(partial, destination)
        if stderr:
            main_log.info(f"MySQL: avisos do mysqldump para '{database}': {stderr}")
        ver_info = f" (servidor {server_version})" if server_version else ""
        return BackupResult(
            "MySQL",
            database,
            True,
            f"MySQL{ver_info}: backup de {database} realizado em '{destination}'!",
            str(destination),
        )
    except subprocess.TimeoutExpired:
        message = f"Falha no backup da base de dados '{database}' do MySQL: tempo limite excedido."
    except Exception as exc:
        message = f"Falha no backup da base de dados '{database}' do MySQL. Erro: {exc}"
    finally:
        if defaults_file is not None:
            try:
                defaults_file.unlink()
            except OSError:
                pass

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
        return [
            BackupResult(
                "MySQL",
                "-",
                False,
                "MySQL: mysqldump.exe não encontrado. Informe o caminho na aba Conexões.",
            )
        ]

    try:
        databases, server_version = get_mysql_databases_and_version(creds)
        main_log.info(f"Conexão com MySQL bem sucedida! Versão do servidor: {server_version or 'desconhecida'}")
    except Exception as exc:
        return [BackupResult("MySQL", "-", False, f"Erro ao conectar ao MySQL: {exc}")]

    folder = settings.backup_root / MYSQL_FOLDER
    folder.mkdir(parents=True, exist_ok=True)

    results = []
    for database in databases:
        if database.lower() in MYSQL_EXCLUDED_DATABASES:
            continue
        destination = folder / f"{day}_{database}.sql"
        results.append(_dump_mysql(mysqldump, creds, database, destination, server_version))
    if not results:
        results.append(BackupResult("MySQL", "-", False, "MySQL: nenhum banco de dados de usuário encontrado."))
    return results


# ---------------------------------------------------------------------------
# SQL Server (2008, 2008 R2, 2012, 2014, 2016, 2017 e 2019 via pyodbc)
# ---------------------------------------------------------------------------
# Ordem de preferência de drivers ODBC:
# * ODBC Driver 17 / 13 / 11 e Native Client 10.0 / 11.0 funcionam muito bem
#   tanto com SQL Server 2008/2008 R2 quanto com 2012-2019.
# * ODBC Driver 18 exige TLS 1.2 e Encrypt=yes por padrão, o que pode falhar
#   contra um SQL Server 2008 sem patch TLS 1.2 se não passarmos Encrypt=no.
# * "SQL Server" (SQLSRV32.DLL) vem nativamente em todo Windows e serve como
#   fallback universal para SQL Server 2008+.
_PREFERRED_ODBC_DRIVERS = (
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 13.1 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "ODBC Driver 11 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server Native Client 10.0",
    "SQL Server",
)


def installed_odbc_drivers() -> List[str]:
    """Lista todos os drivers ODBC de SQL Server instalados na máquina, em ordem de preferência."""
    import pyodbc

    available = list(pyodbc.drivers())
    ordered = [d for d in _PREFERRED_ODBC_DRIVERS if d in available]
    for drv in available:
        if "sql server" in drv.lower() and drv not in ordered:
            ordered.append(drv)
    if not ordered:
        raise RuntimeError(
            "Nenhum driver ODBC do SQL Server está instalado. "
            "Instale o Microsoft ODBC Driver 17 for SQL Server."
        )
    return ordered


def pick_odbc_driver() -> str:
    return installed_odbc_drivers()[0]


def _odbc_value(value: str) -> str:
    """Escapa um valor da connection string ODBC."""
    if any(ch in value for ch in ";{}=") or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


def _server_candidates(creds: SQLServerCredentials) -> List[str]:
    """Gera variações de SERVER para lidar com instâncias nomeadas (2008-2019).

    Exemplo: se o usuário informou 'SERVIDOR\\SQLEXPRESS' e também preencheu a
    porta, tentamos primeiro com a porta e, se falhar (comum em drivers legados
    com instância nomeada), tentamos apenas 'SERVIDOR\\SQLEXPRESS'.
    """
    base = creds.server.strip()
    if not creds.port:
        return [base]
    with_port = f"{base},{creds.port}"
    if "\\" in base:
        return [with_port, base]
    return [with_port]


def _build_conn_str(
    driver: str,
    server: str,
    database: str,
    creds: SQLServerCredentials,
    encrypt_mode: Optional[str] = None,
) -> str:
    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={_odbc_value(server)}",
        f"DATABASE={_odbc_value(database)}",
    ]
    if creds.user:
        parts += [f"UID={_odbc_value(creds.user)}", f"PWD={_odbc_value(creds.password)}"]
    else:
        parts.append("Trusted_Connection=yes")

    if encrypt_mode == "no":
        parts.append("Encrypt=no")
    elif encrypt_mode == "trust":
        parts.append("Encrypt=yes")
        parts.append("TrustServerCertificate=yes")
    return ";".join(parts) + ";"


def connect_sqlserver(
    creds: SQLServerCredentials,
    database: Optional[str] = None,
    autocommit: bool = False,
):
    """Conecta ao SQL Server (2008 a 2019) testando automaticamente os drivers e
    modos de criptografia instalados caso o primeiro driver falhe por versão/TLS."""
    import pyodbc

    drivers = installed_odbc_drivers()
    servers = _server_candidates(creds)

    # Lista de bancos para tentar abrir a sessão inicial:
    # se 'database' foi pedido explicitamente (ex.: 'master'), tenta 'master' e
    # depois o banco configurado (útil quando o login não tem permissão em master).
    db_candidates: List[str] = []
    for candidate in (database, creds.database, "master"):
        if candidate and candidate not in db_candidates:
            db_candidates.append(candidate)

    errors: List[str] = []
    for driver in drivers:
        # No ODBC Driver 18, testamos primeiro Encrypt=no (compatível com SQL 2008
        # sem TLS 1.2) e depois TrustServerCertificate=yes.
        if driver.startswith("ODBC Driver 18"):
            encrypt_modes: Tuple[Optional[str], ...] = ("no", "trust")
        elif driver.startswith("ODBC Driver 17"):
            encrypt_modes = (None, "no", "trust")
        else:
            encrypt_modes = (None,)

        for server in servers:
            for db_name in db_candidates:
                for enc in encrypt_modes:
                    conn_str = _build_conn_str(driver, server, db_name, creds, enc)
                    try:
                        conn = pyodbc.connect(
                            conn_str,
                            timeout=CONNECT_TIMEOUT,
                            autocommit=autocommit,
                        )
                        # Timeout de execução de comandos = 0 (sem limite), para
                        # que backups grandes em SQL 2008-2019 não expirem em 30s.
                        conn.timeout = 0
                        return conn
                    except Exception as exc:
                        msg = str(exc)
                        errors.append(f"[{driver}] {msg}")
                        # Se a falha foi de login/senha inválida (18456), não adianta
                        # tentar outros drivers com a mesma senha incorreta.
                        if "18456" in msg:
                            raise

    raise ConnectionError(errors[0] if errors else "Não foi possível conectar ao SQL Server.")


def _quote_identifier(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _quote_literal(text: str) -> str:
    return "N'" + text.replace("'", "''") + "'"


def backup_sqlserver(settings: AppSettings, creds: SQLServerCredentials, day: str) -> List[BackupResult]:
    database = creds.database
    if not creds.configured:
        return [
            BackupResult(
                "SQL Server",
                database or "-",
                False,
                "SQL Server: servidor e banco de dados precisam estar configurados.",
            )
        ]

    file_name = f"{day}_{database}.bak"
    if settings.sqlserver_backup_dir.strip():
        # Pasta informada como o SQL Server a enxerga (pode ser em outra máquina)
        destination = str(PureWindowsPath(settings.sqlserver_backup_dir.strip()) / file_name)
    else:
        folder = settings.backup_root / SQL_FOLDER
        folder.mkdir(parents=True, exist_ok=True)
        destination = str(folder / file_name)

    # Sintaxe T-SQL compatível de SQL Server 2008 até 2019+:
    # WITH INIT, FORMAT sobrescreve o arquivo em vez de acrescentar um novo backup
    # ao mesmo .bak a cada semana (o que fazia o arquivo crescer sem limite).
    statement = (
        f"BACKUP DATABASE {_quote_identifier(database)} "
        f"TO DISK = {_quote_literal(destination)} WITH INIT, FORMAT"
    )
    try:
        connection = connect_sqlserver(creds, database="master", autocommit=True)
        try:
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT CAST(SERVERPROPERTY('ProductVersion') AS VARCHAR(50))")
                ver_row = cursor.fetchone()
                sql_version = str(ver_row[0]) if ver_row and ver_row[0] else ""
            except Exception:
                sql_version = ""

            cursor.execute(statement)
            # É obrigatório consumir todas as mensagens do progresso do BACKUP DATABASE
            # em SQL Server 2008–2019; caso contrário o backup é abortado ao fechar a conexão.
            while cursor.nextset():
                pass
            cursor.close()
        finally:
            connection.close()
    except Exception as exc:
        return [
            BackupResult(
                "SQL Server",
                database,
                False,
                f"Falha no backup da base de dados {database} do SQL. Erro: {exc}",
            )
        ]
    ver_info = f" (versão {sql_version})" if sql_version else ""
    return [
        BackupResult(
            "SQL Server",
            database,
            True,
            f"SQL{ver_info}: backup de {database} realizado em '{destination}'!",
            destination,
        )
    ]


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
