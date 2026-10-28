"""Leitura e gravação das configurações.

* config/settings.ini       -> agenda, bancos selecionados e caminhos
* config/dbcredentials.ini  -> credenciais dos bancos (cifradas com Fernet)
"""

import configparser
import io
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from .log_config import error_log
from .paths import BASE_DIR, DB_CREDENTIALS_FILE, SETTINGS_FILE, atomic_write_text
from .security import SecretBox

# A posição na tupla corresponde a datetime.weekday() (0 = segunda-feira).
# Os nomes sem acento são as chaves usadas no settings.ini e nos nomes dos arquivos.
DAYS = ("Segunda", "Terca", "Quarta", "Quinta", "Sexta", "Sabado", "Domingo")
DAY_LABELS = {
    "Segunda": "Segunda", "Terca": "Terça", "Quarta": "Quarta", "Quinta": "Quinta",
    "Sexta": "Sexta", "Sabado": "Sábado", "Domingo": "Domingo",
}

DEFAULT_TIME = "00:00"
DEFAULT_MYSQL_PORT = 3306


def day_of(moment: Optional[datetime] = None) -> str:
    """Nome do dia (independente do locale do Windows)."""
    return DAYS[(moment or datetime.now()).weekday()]


def parse_time(text: str) -> Optional[str]:
    """Normaliza um horário para 'HH:MM'. Retorna None se for inválido."""
    try:
        return datetime.strptime(text.strip(), "%H:%M").strftime("%H:%M")
    except (ValueError, AttributeError):
        return None


def parse_port(text: str, default: Optional[int] = None) -> Optional[int]:
    """Converte a porta. Vazio -> default. Lança ValueError se for inválida."""
    text = (text or "").strip()
    if not text:
        return default
    port = int(text)
    if not 0 < port < 65536:
        raise ValueError(f"Porta fora do intervalo: {port}")
    return port


def _write_config(config: configparser.ConfigParser, path: Path) -> None:
    buffer = io.StringIO()
    config.write(buffer)
    atomic_write_text(path, buffer.getvalue())


def _read_config(path: Path) -> configparser.ConfigParser:
    config = configparser.ConfigParser(interpolation=None)
    try:
        config.read(path, encoding="utf-8")
    except configparser.Error as exc:
        error_log.error(f"O arquivo de configurações '{path.name}' está incorreto: {exc}")
    return config


# ---------------------------------------------------------------------------
# settings.ini
# ---------------------------------------------------------------------------
@dataclass
class DaySchedule:
    enabled: bool = False
    time: str = DEFAULT_TIME


@dataclass
class AppSettings:
    days: Dict[str, DaySchedule] = field(default_factory=lambda: {d: DaySchedule() for d in DAYS})
    use_mysql: bool = False
    use_sql_server: bool = False
    backup_dir: str = ""            # vazio = pasta do programa
    mysqldump_path: str = ""        # vazio = detecção automática
    sqlserver_backup_dir: str = ""  # vazio = <backup_dir>\sql (pasta vista pelo SQL Server)

    @property
    def backup_root(self) -> Path:
        return Path(self.backup_dir) if self.backup_dir.strip() else BASE_DIR


def _get_bool(config, section, option, default=False) -> bool:
    try:
        return config.getboolean(section, option, fallback=default)
    except ValueError:
        error_log.error(f"Valor inválido em settings.ini [{section}] {option}; usando {default}.")
        return default


def load_settings() -> AppSettings:
    config = _read_config(SETTINGS_FILE)
    settings = AppSettings()

    for day in DAYS:
        enabled = _get_bool(config, "Days", day)
        raw_time = config.get("Time", day, fallback="")
        time = parse_time(raw_time) if raw_time else None
        if raw_time and time is None:
            error_log.error(f"Horário inválido para {day} em settings.ini: '{raw_time}'. Dia desativado.")
            enabled = False
        settings.days[day] = DaySchedule(enabled, time or DEFAULT_TIME)

    settings.use_mysql = _get_bool(config, "Backup", "use_mysql")
    settings.use_sql_server = _get_bool(config, "Backup", "use_sql_server")
    settings.backup_dir = config.get("Paths", "backup_dir", fallback="")
    settings.mysqldump_path = config.get("Paths", "mysqldump_path", fallback="")
    settings.sqlserver_backup_dir = config.get("Paths", "sqlserver_backup_dir", fallback="")
    return settings


def save_settings(settings: AppSettings) -> None:
    config = configparser.ConfigParser(interpolation=None)
    config["Days"] = {day: str(settings.days[day].enabled) for day in DAYS}
    config["Time"] = {day: settings.days[day].time for day in DAYS}
    config["Backup"] = {
        "use_mysql": str(settings.use_mysql),
        "use_sql_server": str(settings.use_sql_server),
    }
    config["Paths"] = {
        "backup_dir": settings.backup_dir,
        "mysqldump_path": settings.mysqldump_path,
        "sqlserver_backup_dir": settings.sqlserver_backup_dir,
    }
    _write_config(config, SETTINGS_FILE)


# ---------------------------------------------------------------------------
# dbcredentials.ini
# ---------------------------------------------------------------------------
@dataclass
class MySQLCredentials:
    host: str = "localhost"
    user: str = ""
    password: str = ""
    port: int = DEFAULT_MYSQL_PORT

    @property
    def configured(self) -> bool:
        return bool(self.user)


@dataclass
class SQLServerCredentials:
    server: str = ""
    user: str = ""        # vazio = autenticação do Windows
    password: str = ""
    database: str = ""
    port: Optional[int] = None

    @property
    def configured(self) -> bool:
        return bool(self.server and self.database)


@dataclass
class DbCredentials:
    mysql: MySQLCredentials = field(default_factory=MySQLCredentials)
    sqlserver: SQLServerCredentials = field(default_factory=SQLServerCredentials)


def load_credentials(box: SecretBox) -> DbCredentials:
    """Lê as credenciais. Aceita valores antigos não cifrados (porta, banco)."""
    config = _read_config(DB_CREDENTIALS_FILE)

    def value(section: str, option: str) -> str:
        return box.decrypt_or_plain(config.get(section, option, fallback="").strip())

    def port(section: str, default: Optional[int]) -> Optional[int]:
        raw = value(section, "port")
        try:
            return parse_port(raw, default)
        except ValueError:
            error_log.error(f"Porta inválida em dbcredentials.ini [{section}]: '{raw}'.")
            return default

    creds = DbCredentials()
    creds.mysql = MySQLCredentials(
        host=value("MySQL", "host") or "localhost",
        user=value("MySQL", "user"),
        password=value("MySQL", "password"),
        port=port("MySQL", DEFAULT_MYSQL_PORT),
    )
    creds.sqlserver = SQLServerCredentials(
        server=value("SQLServer", "server"),
        user=value("SQLServer", "user"),
        password=value("SQLServer", "password"),
        database=value("SQLServer", "database"),
        port=port("SQLServer", None),
    )
    return creds


def save_credentials(creds: DbCredentials, box: SecretBox) -> None:
    """Grava todas as credenciais cifradas (inclusive campos vazios, para que
    seja possível apagar um valor)."""
    config = configparser.ConfigParser(interpolation=None)
    m, s = creds.mysql, creds.sqlserver
    config["MySQL"] = {
        "host": box.encrypt(m.host),
        "user": box.encrypt(m.user),
        "password": box.encrypt(m.password),
        "port": box.encrypt(str(m.port) if m.port else ""),
    }
    config["SQLServer"] = {
        "server": box.encrypt(s.server),
        "user": box.encrypt(s.user),
        "password": box.encrypt(s.password),
        "database": box.encrypt(s.database),
        "port": box.encrypt(str(s.port) if s.port else ""),
    }
    _write_config(config, DB_CREDENTIALS_FILE)
