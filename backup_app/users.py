"""Cadastro e autenticação dos usuários da interface (content/credentials.json)."""

import hmac
import json
from typing import Dict, List

from .log_config import error_log, main_log
from .paths import USERS_FILE, atomic_write_text
from .security import (
    SecretBox, hash_password, is_password_hash, load_key, password_needs_rehash, verify_password,
)

MIN_PASSWORD_LENGTH = 4


class UserError(ValueError):
    """Dados de cadastro inválidos."""


def _load() -> Dict:
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return {"users": []}
    except (OSError, json.JSONDecodeError) as exc:
        error_log.error(f"Arquivo de usuários '{USERS_FILE}' ilegível: {exc}")
        raise
    if not isinstance(data.get("users"), list):
        data["users"] = []
    return data


def _save(data: Dict) -> None:
    atomic_write_text(USERS_FILE, json.dumps(data, indent=4, ensure_ascii=False))


def list_usernames() -> List[str]:
    return [user.get("username", "") for user in _load()["users"]]


def has_users() -> bool:
    try:
        return bool(_load()["users"])
    except (OSError, json.JSONDecodeError):
        # Na dúvida, não libera o "primeiro cadastro" sem login
        return True


def check_login(username: str, password: str) -> bool:
    """Verifica usuário e senha. Senhas no formato antigo (Fernet) são migradas
    automaticamente para hash no primeiro login bem-sucedido."""
    if not username or not password:
        return False
    data = _load()
    for user in data["users"]:
        if user.get("username") != username:
            continue
        stored = user.get("password", "")

        if is_password_hash(stored):
            if not verify_password(password, stored):
                return False
            if password_needs_rehash(stored):
                user["password"] = hash_password(password)
                _save(data)
            return True

        # Formato legado: senha cifrada com Fernet
        try:
            decrypted = SecretBox(load_key(create=False)).try_decrypt(stored)
        except Exception as exc:  # chave ausente/ilegível
            error_log.error(f"Não foi possível verificar a senha legada de '{username}': {exc}")
            return False
        if decrypted is None or not hmac.compare_digest(decrypted.encode(), password.encode()):
            return False
        user["password"] = hash_password(password)
        _save(data)
        main_log.info(f"Senha do usuário '{username}' migrada para hash.")
        return True
    return False


def register_user(username: str, password: str, confirmation: str) -> None:
    username = username.strip()
    if not username:
        raise UserError("Informe o nome de usuário.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserError(f"A senha deve ter pelo menos {MIN_PASSWORD_LENGTH} caracteres.")
    if password != confirmation:
        raise UserError("As senhas não conferem.")

    data = _load()
    if any(user.get("username", "").lower() == username.lower() for user in data["users"]):
        raise UserError(f"O usuário '{username}' já existe.")

    data["users"].append({"username": username, "password": hash_password(password)})
    _save(data)
    main_log.info(f"Usuário '{username}' registrado.")
