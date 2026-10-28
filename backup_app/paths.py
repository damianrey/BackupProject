"""Caminhos absolutos do projeto.

Quando o programa é iniciado pelo Windows (registro "Run" ou Agendador de
Tarefas) o diretório de trabalho costuma ser C:\\Windows\\System32. Por isso
NENHUM caminho do projeto pode ser relativo: tudo parte de BASE_DIR.
"""

import os
import sys
from pathlib import Path


def _base_dir() -> Path:
    # Executável gerado pelo PyInstaller: a pasta do .exe
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # Execução pelo interpretador: a pasta que contém BackupProject.py
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()

# Recursos somente-leitura (imagens) podem estar dentro do bundle do PyInstaller
_RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))

CONFIG_DIR = BASE_DIR / "config"
CONTENT_DIR = BASE_DIR / "content"
LOGS_DIR = BASE_DIR / "logs"
IMAGES_DIR = _RESOURCE_DIR / "images" if (_RESOURCE_DIR / "images").exists() else BASE_DIR / "images"

SETTINGS_FILE = CONFIG_DIR / "settings.ini"
DB_CREDENTIALS_FILE = CONFIG_DIR / "dbcredentials.ini"
USERS_FILE = CONTENT_DIR / "credentials.json"
KEY_FILE = CONTENT_DIR / "fernet.key"

APP_LOCK_FILE = CONTENT_DIR / "app.lock"        # garante uma única instância da interface
BACKUP_LOCK_FILE = CONTENT_DIR / "backup.lock"  # garante um único backup por vez (entre processos)

ENTRY_SCRIPT = BASE_DIR / "BackupProject.py"


def image(name: str) -> str:
    """Caminho absoluto (str) de um arquivo da pasta images."""
    return str(IMAGES_DIR / name)


def ensure_dirs() -> None:
    """Cria as pastas de trabalho, caso não existam."""
    for directory in (CONFIG_DIR, CONTENT_DIR, LOGS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    """Grava um arquivo de texto de forma atômica (evita arquivos corrompidos
    caso o programa seja encerrado no meio da gravação)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
