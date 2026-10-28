"""Inicialização automática com o Windows (HKCU\\...\\Run, não exige administrador)."""

import sys
from pathlib import Path

from .paths import ENTRY_SCRIPT

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "BackupProject"
STARTUP_ARG = "--startup"


def startup_command() -> str:
    """Comando registrado. Usa pythonw.exe para não abrir janela de console."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {STARTUP_ARG}'
    interpreter = Path(sys.executable)
    pythonw = interpreter.with_name("pythonw.exe")
    if pythonw.is_file():
        interpreter = pythonw
    return f'"{interpreter}" "{ENTRY_SCRIPT}" {STARTUP_ARG}'


def is_supported() -> bool:
    return sys.platform == "win32"


def _registered_value():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            return winreg.QueryValueEx(key, VALUE_NAME)[0]
    except FileNotFoundError:
        return None


def is_enabled() -> bool:
    return is_supported() and _registered_value() is not None


def is_outdated() -> bool:
    """True se estiver habilitado, mas apontando para outro caminho/interpretador."""
    value = _registered_value() if is_supported() else None
    return value is not None and value != startup_command()


def set_enabled(enabled: bool) -> None:
    if not is_supported():
        raise OSError("Inicialização automática disponível apenas no Windows.")
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
