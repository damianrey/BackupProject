"""Leitura e gravação das configurações.

* config/settings.ini       -> agenda (regras), retenção, bancos selecionados e caminhos
* config/dbcredentials.ini  -> credenciais dos bancos (cifradas com Fernet)
"""

import configparser
import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

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

DAY_SHORT = {
    "Segunda": "Seg", "Terca": "Ter", "Quarta": "Qua", "Quinta": "Qui",
    "Sexta": "Sex", "Sabado": "Sáb", "Domingo": "Dom",
}

DEFAULT_TIME = "00:00"
DEFAULT_MYSQL_PORT = 3306
DEFAULT_RETENTION_DAYS = 7     # equivale ao antigo rodízio semanal
MIN_INTERVAL_MINUTES = 30      # evita acumular backups que seriam ignorados

MODE_TIMES = "times"           # lista de horários fixos
MODE_INTERVAL = "interval"     # a cada N minutos dentro de uma janela


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
def _minutes(time_text: str) -> int:
    hours, minutes = time_text.split(":")
    return int(hours) * 60 + int(minutes)


def _hhmm(total_minutes: int) -> str:
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def format_interval(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest} min"
    return f"{hours}h{rest:02d}" if rest else f"{hours}h"


def describe_days(days: Iterable[str]) -> str:
    """'Seg–Sex', 'Sáb, Dom', 'Todos os dias'..."""
    indexes = sorted(DAYS.index(d) for d in set(days))
    if len(indexes) == len(DAYS):
        return "Todos os dias"
    if len(indexes) >= 3 and indexes[-1] - indexes[0] == len(indexes) - 1:
        return f"{DAY_SHORT[DAYS[indexes[0]]]}–{DAY_SHORT[DAYS[indexes[-1]]]}"
    return ", ".join(DAY_SHORT[DAYS[i]] for i in indexes)


@dataclass
class ScheduleRule:
    """Uma regra da agenda: em quais dias e em quais horários fazer backup."""

    days: List[str] = field(default_factory=list)
    mode: str = MODE_TIMES
    times: List[str] = field(default_factory=list)  # modo "times"
    every_minutes: int = 60                         # modo "interval"
    start: str = "08:00"
    end: str = "18:00"
    enabled: bool = True

    def times_of_day(self) -> List[str]:
        """Horários ('HH:MM', ordenados e sem repetição) em que a regra dispara."""
        if self.mode == MODE_INTERVAL:
            step = max(self.every_minutes, MIN_INTERVAL_MINUTES)
            return [_hhmm(m) for m in range(_minutes(self.start), _minutes(self.end) + 1, step)]
        return sorted(set(self.times))

    def slots(self) -> Set[Tuple[str, str]]:
        """Pares (dia, horário) desta regra; vazio se estiver desativada."""
        if not self.enabled:
            return set()
        return {(day, time) for day in self.days for time in self.times_of_day()}

    def validate(self) -> Optional[str]:
        """Mensagem de erro, ou None se a regra for válida."""
        if not self.days:
            return "Selecione pelo menos um dia."
        if self.mode == MODE_INTERVAL:
            if self.every_minutes < MIN_INTERVAL_MINUTES:
                return f"O intervalo mínimo é de {MIN_INTERVAL_MINUTES} minutos."
            if _minutes(self.end) < _minutes(self.start):
                return "O horário final deve ser igual ou posterior ao inicial."
        elif not self.times:
            return "Adicione pelo menos um horário."
        return None

    def describe(self) -> str:
        if self.mode == MODE_INTERVAL:
            when = f"a cada {format_interval(self.every_minutes)}, das {self.start} às {self.end}"
        else:
            when = "às " + ", ".join(self.times_of_day())
        return f"{describe_days(self.days)}: {when}"


def all_slots(rules: Iterable[ScheduleRule]) -> List[Tuple[str, str]]:
    """União dos pares (dia, horário) de todas as regras, em ordem cronológica."""
    slots: Set[Tuple[str, str]] = set()
    for rule in rules:
        slots |= rule.slots()
    return sorted(slots, key=lambda slot: (DAYS.index(slot[0]), slot[1]))


def upcoming_runs(rules: Iterable[ScheduleRule], count: int = 5,
                  now: Optional[datetime] = None) -> List[datetime]:
    """Próximas `count` execuções previstas pelas regras."""
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    slots = all_slots(rules)
    runs: List[datetime] = []
    for offset in range(8):  # uma semana à frente cobre qualquer agenda semanal
        date = now + timedelta(days=offset)
        for slot_day, time in slots:
            if slot_day != day_of(date):
                continue
            hours, minutes = map(int, time.split(":"))
            moment = date.replace(hour=hours, minute=minutes)
            if moment > now:
                runs.append(moment)
                if len(runs) == count:
                    return runs
    return runs


@dataclass
class AppSettings:
    rules: List[ScheduleRule] = field(default_factory=list)
    retention_days: int = DEFAULT_RETENTION_DAYS  # 0 = manter todos os backups
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


def _parse_days(text: str) -> List[str]:
    by_name = {d.lower(): d for d in DAYS}
    days = []
    for name in text.split(","):
        day = by_name.get(name.strip().lower())
        if day and day not in days:
            days.append(day)
    return days


def _load_rule(config, section: str) -> Optional[ScheduleRule]:
    rule = ScheduleRule(
        days=_parse_days(config.get(section, "days", fallback="")),
        mode=config.get(section, "mode", fallback=MODE_TIMES).strip().lower(),
        enabled=_get_bool(config, section, "enabled", True),
    )
    if rule.mode == MODE_INTERVAL:
        try:
            rule.every_minutes = int(config.get(section, "every_minutes", fallback="60"))
        except ValueError:
            rule.every_minutes = 0  # rejeitado pelo validate()
        rule.start = parse_time(config.get(section, "start", fallback="")) or rule.start
        rule.end = parse_time(config.get(section, "end", fallback="")) or rule.end
    else:
        rule.mode = MODE_TIMES
        raw_times = [t for t in config.get(section, "times", fallback="").split(",") if t.strip()]
        rule.times = sorted({t for t in map(parse_time, raw_times) if t})
        if len(rule.times) < len(set(raw_times)):
            error_log.error(f"Horário inválido ignorado em settings.ini [{section}].")
    problem = rule.validate()
    if problem:
        error_log.error(f"Regra de agendamento [{section}] ignorada: {problem}")
        return None
    return rule


def _load_legacy_rules(config) -> List[ScheduleRule]:
    """Converte o formato antigo ([Days] + [Time], um horário por dia) em regras,
    agrupando os dias que têm o mesmo horário."""
    days_by_time = {}
    for day in DAYS:
        if not _get_bool(config, "Days", day):
            continue
        raw_time = config.get("Time", day, fallback="")
        time = parse_time(raw_time) if raw_time else DEFAULT_TIME
        if time is None:
            error_log.error(f"Horário inválido para {day} em settings.ini: '{raw_time}'. Dia desativado.")
            continue
        days_by_time.setdefault(time, []).append(day)
    return [ScheduleRule(days=days, times=[time]) for time, days in days_by_time.items()]


def load_settings() -> AppSettings:
    config = _read_config(SETTINGS_FILE)
    settings = AppSettings()

    if config.has_section("Schedule"):
        rule_sections = sorted((s for s in config.sections() if s[:4] == "Rule" and s[4:].isdigit()),
                               key=lambda s: int(s[4:]))
        settings.rules = [rule for rule in (_load_rule(config, s) for s in rule_sections) if rule]
        try:
            retention = config.getint("Schedule", "retention_days", fallback=DEFAULT_RETENTION_DAYS)
            settings.retention_days = max(0, retention)
        except ValueError:
            error_log.error("Valor inválido em settings.ini [Schedule] retention_days; "
                            f"usando {DEFAULT_RETENTION_DAYS}.")
    else:
        settings.rules = _load_legacy_rules(config)  # migração do formato antigo

    settings.use_mysql = _get_bool(config, "Backup", "use_mysql")
    settings.use_sql_server = _get_bool(config, "Backup", "use_sql_server")
    settings.backup_dir = config.get("Paths", "backup_dir", fallback="")
    settings.mysqldump_path = config.get("Paths", "mysqldump_path", fallback="")
    settings.sqlserver_backup_dir = config.get("Paths", "sqlserver_backup_dir", fallback="")
    return settings


def save_settings(settings: AppSettings) -> None:
    config = configparser.ConfigParser(interpolation=None)
    config["Schedule"] = {"retention_days": str(settings.retention_days)}
    for number, rule in enumerate(settings.rules, start=1):
        section = {"enabled": str(rule.enabled), "days": ",".join(rule.days), "mode": rule.mode}
        if rule.mode == MODE_INTERVAL:
            section.update(every_minutes=str(rule.every_minutes), start=rule.start, end=rule.end)
        else:
            section["times"] = ",".join(rule.times_of_day())
        config[f"Rule{number}"] = section
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
